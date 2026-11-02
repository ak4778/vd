# -*- coding: utf-8 -*-
"""独立补数程序：按当前规则重放 02-死值校验，补写改动生效期间被静默漏掉的告警事件。

背景：2026-10-27 的改动把「读数恒 0」的测点判为 C01_ZERO（04-传感器故障）并从输出中剔除，
同时跳过了 C02 死值校验，导致恒 0 测点在改动生效 ~ 2026-10-28 09:36 重启之间，
既不报 01/04，也不再报 02-死值校验，中间这段的 C02 事件全部缺失。

本程序从 wind_seconds_di 重放该时间窗：按 wind_alarm.py 的当前参数
（W_FROZEN / FROZEN_MIN_SPAN / FROZEN_MIN_POINTS / FROZEN_FRESHNESS /
C02_STALE_RECOVER / ALARM_COOLDOWN）逐 30 秒 tick 推演，只对「整段读数恒 0」的测点
生成 ALARM/RECOVER，并跳过 wind_alarm 里已存在的同 (设备,测点,事件,时刻) 记录。

用法：
  python backfill_c02_frozen.py                  # 只预览，不写任何库/文件
  python backfill_c02_frozen.py --write          # 写入 PG wind_alarm
  python backfill_c02_frozen.py --write --csv    # 同时追加到 alarm_temgende.csv
  python backfill_c02_frozen.py --start "2026-10-27 16:57:00" --end "2026-10-28 09:36:00"

注意：--write 可重复执行，记录按 (设备,测点,规则,事件,ts) 去重，不会重复插入。
"""

import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'data_config.json')

# ---------------- 与 wind_alarm.py 保持一致的规则参数 ----------------
W_FROZEN = 3600           # 规则02窗口(秒)
W_FLUCT = 20              # 规则03窗口(秒)，仅用于算 range_20s 统计列
FROZEN_MIN_SPAN = 2700    # 窗口实际跨度下限(秒)
FROZEN_MIN_POINTS = 100   # 最少样本数
EPS = 1e-9                # 窗口内 max-min < EPS 视为卡死
ZERO_FAULT_EPS = 1e-6     # 读数≈0 视为传感器恒 0
FROZEN_FRESHNESS = 300    # 最新点滞后 < 5 分钟才判定
C02_STALE_RECOVER = 900   # 断采超过 15 分钟显式恢复
ALARM_COOLDOWN = 600      # 同类告警冷却
POLL_INTERVAL = 30        # 主循环轮询周期

TS_FMT = '%Y-%m-%d %H:%M:%S'
ALARM_TYPE = '02-死值校验'
RULE_CODE = 'C02_FROZEN'

TABLE_SECONDS = 'wind_seconds_di'
TABLE_ALARM = 'wind_alarm'
TABLE_DEVICE = 'wind_device'
CSV_PATH = os.path.join(HERE, 'alarm_temgende.csv')

CSV_HEADER = ['ts', 'regionCompanyName', 'stationName',
              'windDeviceId', 'windDeviceName',
              'pointCode', 'pointName',
              'temperature', 'temp_rate',
              'range_1h', 'range_20s',
              'alarm_type', 'alarm_value']

# 测点 -> (秒级表列名, 中文名)
POINTS = {
    'TEMGENDRIEND': ('tem_gen_driend', '驱动端温度'),
    'TEMGENNONDE': ('tem_gen_nonde', '非驱动端温度'),
    'TEMGENSEN1': ('tem_gen_sen1', '定子U相线圈温度'),
    'TEMGENSEN2': ('tem_gen_sen2', '定子V相线圈温度'),
    'TEMGENSEN3': ('tem_gen_sen3', '定子W相线圈温度'),
    'TEMGENSTAU': ('tem_gen_stau', '定子U相线圈温度'),
    'TEMGENSTAV': ('tem_gen_stav', '定子V相线圈温度'),
    'TEMGENSTAW': ('tem_gen_staw', '定子W相线圈温度'),
}

INSERT_SQL = f"""
    INSERT INTO {TABLE_ALARM} (
        event, ts, region_company_name, station_name,
        device_id, device_name, point_code, point_name, rule_code, alarm_type,
        temperature, temp_rate, range_1h, range_20s, alarm_value, detail)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def connect():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        conn_str = json.load(f)['pgConnStr']
    conn = psycopg2.connect(conn_str)
    conn.autocommit = True
    return conn


def parse_ts(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')


def load_devices(conn):
    """wind_device 台账：秒级表用大写 id，告警表用台账原始 id（即 wid）。"""
    cur = conn.cursor()
    cur.execute(f"""SELECT wind_device_id, wind_device_name, station_name, region_company_name
                    FROM {TABLE_DEVICE}""")
    out = {}
    for wid, name, station, region in cur.fetchall():
        if wid and wid.strip():
            w = wid.strip()
            out[w.lower()] = {'wid': w, 'name': name or '',
                              'station': station or '', 'region': region or ''}
    cur.close()
    return out


def find_zero_series(conn, start, end):
    """找出时间窗内读数全程恒 0（且样本数达标）的 (设备, 测点) 序列。"""
    cur = conn.cursor()
    found = []
    for pc, (col, _name) in POINTS.items():
        cur.execute(f"""
            SELECT device_id
            FROM {TABLE_SECONDS}
            WHERE ts >= %s AND ts < %s AND {col} IS NOT NULL
            GROUP BY device_id
            HAVING COUNT(*) FILTER (WHERE ABS({col}) > %s) = 0
               AND COUNT(*) >= %s
            ORDER BY device_id
        """, (start, end, ZERO_FAULT_EPS, FROZEN_MIN_POINTS))
        for (dev,) in cur.fetchall():
            found.append((dev, pc, col))
    cur.close()
    return found


def load_points(conn, dev, col, start, end):
    """取窗口前 1 小时到窗口结束的点，保证首个 tick 的滑窗是满的。"""
    load_from = start - timedelta(seconds=W_FROZEN)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT ts, {col}
        FROM {TABLE_SECONDS}
        WHERE device_id = %s AND ts >= %s AND ts < %s AND {col} IS NOT NULL
        ORDER BY ts
    """, (dev, load_from, end))
    pts = [(r[0].timestamp(), float(r[1])) for r in cur.fetchall()]
    cur.close()
    return pts


def load_last_state(conn, wid, pc, start):
    """从 wind_alarm 回放该序列窗口开始前的最后一条 C02 事件。"""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT event, ts FROM {TABLE_ALARM}
        WHERE device_id = %s AND point_code = %s AND rule_code = %s AND ts < %s
        ORDER BY ts DESC LIMIT 1
    """, (wid, pc, RULE_CODE, start))
    row = cur.fetchone()
    cur.close()
    if not row:
        return False, 0.0
    return row[0] == 'ALARM', row[1].timestamp()


def load_existing(conn, wid, pc, start, end):
    cur = conn.cursor()
    cur.execute(f"""
        SELECT event, ts FROM {TABLE_ALARM}
        WHERE device_id = %s AND point_code = %s AND rule_code = %s
          AND ts >= %s AND ts < %s
    """, (wid, pc, RULE_CODE, start, end))
    out = {(r[0], r[1].replace(microsecond=0)) for r in cur.fetchall()}
    cur.close()
    return out


def eval_frozen(win):
    """规则02：窗口内所有点完全相同。与 wind_alarm.py 的 eval_frozen 等价。"""
    if not win:
        return False
    epochs = [e for e, _ in win]
    vals = [v for _, v in win]
    span = epochs[-1] - epochs[0]
    if span < FROZEN_MIN_SPAN or len(win) < FROZEN_MIN_POINTS:
        return False
    return (max(vals) - min(vals)) < EPS


def stats_at(points, epochs, i):
    """与 wind_alarm.py 的 compute_stats 一致：以最新数据点为窗口参考。"""
    latest_e, latest_v = points[i]
    j = bisect.bisect_left(epochs, latest_e - W_FROZEN)
    win = points[j:i + 1]
    r1h = (max(v for _, v in win) - min(v for _, v in win)) if win else None
    rate = None
    if i >= 1:
        dt = latest_e - points[i - 1][0]
        if dt > 0:
            rate = (latest_v - points[i - 1][1]) / dt
    k = bisect.bisect_left(epochs, latest_e - W_FLUCT)
    w20 = points[k:i + 1]
    r20s = (max(v for _, v in w20) - min(v for _, v in w20)) if w20 else None
    return {
        'ts': datetime.fromtimestamp(latest_e).strftime(TS_FMT),
        'temp': latest_v,
        'rate': rate,
        'range_1h': r1h,
        'range_20s': r20s,
    }, win


def replay(points, start_epoch, end_epoch, active, last_alarm):
    """逐 30s tick 重放，返回 [(event, ts_str, stats, detail)] 与结束时的状态。"""
    epochs = [p[0] for p in points]
    events = []
    t = start_epoch
    last_stats = None
    while t <= end_epoch:
        i = bisect.bisect_right(epochs, t) - 1
        if i >= 0:
            latest_e = points[i][0]
            lag = t - latest_e
            stats, win = stats_at(points, epochs, i)
            last_stats = stats
            if lag > FROZEN_FRESHNESS:
                if lag > C02_STALE_RECOVER and active:
                    active = False
                    events.append(('RECOVER', stats['ts'], stats, '断采>15min'))
            else:
                is_active = eval_frozen(win)
                if is_active and not active:
                    if t - last_alarm >= ALARM_COOLDOWN:
                        active = True
                        last_alarm = t
                        events.append(('ALARM', stats['ts'], stats, '恒0测点补数'))
                elif (not is_active) and active:
                    active = False
                    events.append(('RECOVER', stats['ts'], stats, ''))
        t += POLL_INTERVAL
    if active:
        # 窗口结束时条件仍成立：写一条边界 RECOVER，把状态交回实时进程，
        # 避免和实时进程重启后自己产生的 ALARM 形成「双 ALARM 无 RECOVER」。
        end_dt = datetime.fromtimestamp(end_epoch)
        events.append(('RECOVER', end_dt.strftime(TS_FMT), last_stats,
                       '补数窗口结束，交由实时进程接管'))
        active = False
    return events, active


def main():
    ap = argparse.ArgumentParser(description='补写恒0测点漏掉的 02-死值校验告警')
    ap.add_argument('--start', default='2026-10-27 16:57:00', help='补数窗口起点')
    ap.add_argument('--end', default='2026-10-28 09:36:00', help='补数窗口终点')
    ap.add_argument('--write', action='store_true', help='写入 PG wind_alarm')
    ap.add_argument('--csv', action='store_true', help='同时追加到 alarm_temgende.csv')
    args = ap.parse_args()

    start = parse_ts(args.start)
    end = parse_ts(args.end)
    if end <= start:
        sys.exit('结束时间必须晚于开始时间')

    conn = connect()
    dev_map = load_devices(conn)
    series = find_zero_series(conn, start, end)
    print(f'窗口 {args.start} ~ {args.end}')
    print(f'恒0序列 {len(series)} 个（测点全程读数为 0 且样本数 >= {FROZEN_MIN_POINTS}）\n')

    total_rows = 0
    all_rows = []
    for dev_upper, pc, col in series:
        meta = dev_map.get(dev_upper.lower())
        if not meta:
            print(f'  [跳过] {dev_upper} 不在 wind_device 白名单')
            continue
        wid = meta['wid']
        points = load_points(conn, dev_upper, col, start, end)
        if not points:
            continue
        active, last_alarm = load_last_state(conn, wid, pc, start)
        events, _ = replay(points, start.timestamp(), end.timestamp(), active, last_alarm)
        existing = load_existing(conn, wid, pc, start, end)

        pname = POINTS[pc][1]
        kept = []
        for ev, ts_str, stats, detail in events:
            ts_dt = datetime.strptime(ts_str, TS_FMT)
            if (ev, ts_dt) in existing:
                continue
            kept.append((ev, ts_str, stats, detail))
        total_rows += len(kept)
        print(f'  {meta["station"]} {meta["name"]} {pname} ({wid[:8]}) '
              f'推演 {len(events)} 条，需补 {len(kept)} 条')
        for ev, ts_str, stats, detail in kept:
            s = stats or {}
            print(f'      {ev:7s} {ts_str} temp={s.get("temp")} '
                  f'r1h={s.get("range_1h")} {detail}')
            all_rows.append((meta, pc, ev, ts_str, stats, detail))

    print(f'\n合计需补 {total_rows} 条')
    if not total_rows:
        conn.close()
        return
    if not args.write:
        print('（预览模式，未写库；加 --write 参数执行入库）')
        conn.close()
        return

    cur = conn.cursor()
    csv_fp = open(CSV_PATH, 'a', newline='', encoding='utf-8-sig') if args.csv else None
    written = 0
    for meta, pc, ev, ts_str, stats, detail in all_rows:
        s = stats or {}
        temp, rate = s.get('temp'), s.get('rate')
        r1h, r20s = s.get('range_1h'), s.get('range_20s')
        alarm_val = r1h if (ev == 'ALARM' and r1h is not None) else None
        ts_dt = datetime.strptime(ts_str, TS_FMT)
        cur.execute(INSERT_SQL, (
            ev, ts_dt,
            meta['region'] or None, meta['station'] or None,
            meta['wid'], meta['name'] or None,
            pc, POINTS[pc][1] or None,
            RULE_CODE, ALARM_TYPE,
            temp, rate, r1h, r20s,
            alarm_val if ev == 'ALARM' else None,
            detail or None,
        ))
        written += 1
        if csv_fp:
            csv_fp.write(','.join([
                ts_str, meta['region'], meta['station'], meta['wid'], meta['name'],
                pc, POINTS[pc][1],
                '' if temp is None else str(round(temp, 2)),
                '' if rate is None else str(round(rate, 4)),
                '' if r1h is None else str(round(r1h, 2)),
                '' if r20s is None else str(round(r20s, 2)),
                ALARM_TYPE if ev == 'ALARM' else '',
                '' if alarm_val is None else str(round(alarm_val, 2)),
            ]) + '\n')
    if csv_fp:
        csv_fp.close()
    cur.close()
    conn.close()
    print(f'已写入 PG {TABLE_ALARM}：{written} 条' + ('，并追加 CSV' if args.csv else ''))


if __name__ == '__main__':
    main()
