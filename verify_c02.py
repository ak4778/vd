# -*- coding: utf-8 -*-
"""C02 死值校验核验补数：按 wind_alarm.py 当前规则重放指定时间窗，比对实际告警，补写漏报。

与 backfill_c02_frozen.py 的区别：那个只补「整段恒 0」的测点；本脚本覆盖全部测点——
凡窗口内存在「同一值 >=100 点且跨度 >=2700s」的序列都重放，逐 30s tick 推演
（W_FROZEN / FROZEN_MIN_SPAN / FROZEN_MIN_POINTS / FROZEN_FRESHNESS /
C02_STALE_RECOVER / ALARM_COOLDOWN 与 wind_alarm.py 一致），
再与 wind_alarm 已有 C02 事件按 (事件类型, ts±120s) 匹配：
  - 推演有、实际没有 -> 漏报，--write 时补入库
  - 实际有、推演没有 -> 多报/历史口径差异，只报告不动数据

用法：
  python verify_c02.py                                # 预览（默认 昨天15:00 ~ 现在-10分钟）
  python verify_c02.py --write                        # 补写 PG wind_alarm
  python verify_c02.py --write --csv                  # 同时追加 alarm_temgende.csv
  python verify_c02.py --start "2026-10-27 15:00:00" --end "2026-10-28 13:40:00"
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
FROZEN_FRESHNESS = 1200   # 最新点滞后 <=该值才判定（与 wind_alarm.py 一致）
C02_STALE_RECOVER = 1800  # 断采超过该值显式恢复（与 wind_alarm.py 一致）
ALARM_COOLDOWN = 600      # 同类告警冷却
POLL_INTERVAL = 30        # 主循环轮询周期
MATCH_TOLERANCE = 120     # 推演事件与实际事件匹配容差(秒)

TS_FMT = '%Y-%m-%d %H:%M:%S'
ALARM_TYPE = '02-死值校验'
RULE_CODE = 'C02_FROZEN'

TABLE_SECONDS = 'wind_seconds_di'
TABLE_ALARM = 'wind_alarm'
TABLE_DEVICE = 'wind_device'
CSV_PATH = os.path.join(HERE, 'alarm_temgende.csv')

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
    return datetime.strptime(s, TS_FMT)


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


def find_frozen_candidates(conn, start, end):
    """精确找出窗口内存在「恒值长段」的 (设备, 测点)，每个序列只返回一次。

    按 ts 排序后值不变的极大连续段（LAG 检测值变化），段内 >=100 点且跨度
    >=2700s。这是 eval_frozen 可触发的充要条件（断采新鲜度由 replay 判定），
    不会漏候选，也不会把「某值零散出现 100 次」的序列算进来。
    """
    cur = conn.cursor()
    load_from = start - timedelta(seconds=W_FROZEN)
    found = []
    for pc, (col, _name) in POINTS.items():
        cur.execute(f"""
            WITH d AS (
                SELECT device_id, ts, {col} AS v,
                       LAG({col}) OVER (PARTITION BY device_id ORDER BY ts) AS pv
                FROM {TABLE_SECONDS}
                WHERE ts >= %s AND ts < %s AND {col} IS NOT NULL
            ),
            r AS (
                SELECT device_id, ts,
                       SUM(CASE WHEN v IS DISTINCT FROM pv THEN 1 ELSE 0 END)
                           OVER (PARTITION BY device_id ORDER BY ts) AS run_id
                FROM d
            )
            SELECT DISTINCT device_id
            FROM r
            GROUP BY device_id, run_id
            HAVING COUNT(*) >= %s
               AND MAX(ts) - MIN(ts) >= INTERVAL '{FROZEN_MIN_SPAN} seconds'
        """, (load_from, end, FROZEN_MIN_POINTS))
        rows = cur.fetchall()
        for (dev,) in rows:
            found.append((dev, pc, col))
        print(f'  {pc}: {len(rows)} 个候选序列', flush=True)
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
    """窗口内实际已写入的 C02 事件 [(event, ts_dt)]。"""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT event, ts FROM {TABLE_ALARM}
        WHERE device_id = %s AND point_code = %s AND rule_code = %s
          AND ts >= %s AND ts < %s
        ORDER BY ts
    """, (wid, pc, RULE_CODE, start, end))
    out = []
    for ev, ts in cur.fetchall():
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        out.append((ev, ts.replace(microsecond=0)))
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
    """逐 30s tick 重放，返回 [(event, ts_str, stats, detail)]。

    窗口结束时仍活跃的：补一条边界 RECOVER 把状态交回实时进程，
    避免与实时进程自己产生的 ALARM 形成「双 ALARM 无 RECOVER」。
    """
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
                        events.append(('ALARM', stats['ts'], stats, 'C02核验补数'))
                elif (not is_active) and active:
                    active = False
                    events.append(('RECOVER', stats['ts'], stats, ''))
        t += POLL_INTERVAL
    if active:
        end_dt = datetime.fromtimestamp(end_epoch)
        events.append(('RECOVER', end_dt.strftime(TS_FMT), last_stats,
                       '核验窗口结束，交由实时进程接管'))
    return events


def match_events(replay_events, existing):
    """推演事件 vs 实际事件：先精确匹配，再按 (event, ts±120s) 容差匹配。

    返回 (漏报[(ev, ts_str, stats, detail)], 多出[(ev, ts_dt)], 匹配数)。
    """
    pool = list(existing)
    matched = 0
    missing = []
    for ev, ts_str, stats, detail in replay_events:
        ts_dt = datetime.strptime(ts_str, TS_FMT)
        hit = None
        for k, (ev2, ts2) in enumerate(pool):
            if ev2 == ev and ts2 == ts_dt:
                hit = k
                break
        if hit is None:
            for k, (ev2, ts2) in enumerate(pool):
                if ev2 == ev and abs((ts2 - ts_dt).total_seconds()) <= MATCH_TOLERANCE:
                    hit = k
                    break
        if hit is not None:
            pool.pop(hit)
            matched += 1
        else:
            missing.append((ev, ts_str, stats, detail))
    return missing, pool, matched


SHIFT_TOLERANCE = 600  # 时序偏移判定：实际已有同类型事件在 ±600s 内视为同一事件


def classify_missing(missing, replay_events, existing, start_epoch):
    """把漏报分为「应补」与「跳过」。

    跳过规则（只报告不写库）：
      1. 边界交接 RECOVER——实时进程当前持有活跃状态，补了会形成双 RECOVER
      2. 事件时刻早于核验窗口起点——初始状态回放 artifact
      3. RECOVER 与实际某 RECOVER 偏移 <=10min——同一恢复，实时基于延迟数据判定
      4. ALARM 与实际某 ALARM 偏移 <=10min——同一报警，时序偏移
      5. ALARM 所在冻结段已被覆盖：推演后续首个 RECOVER 能匹配实际 RECOVER，
         且两者之间存在实际 ALARM——该段已有实际报警，只是起始时刻口径不同
    返回 (to_write, skipped[(ev, ts_str, reason)])。
    """
    ex_alarm = [t for e, t in existing if e == 'ALARM']
    ex_recover = [t for e, t in existing if e == 'RECOVER']
    to_write, skipped = [], []
    for item in missing:
        ev, ts_str, stats, detail = item
        ts_dt = datetime.strptime(ts_str, TS_FMT)
        reason = None
        if detail and '核验窗口结束' in detail:
            reason = '边界交接RECOVER，实时进程持有状态'
        elif ts_dt.timestamp() < start_epoch:
            reason = '事件时刻早于窗口起点'
        elif ev == 'RECOVER' and any(
                abs((t - ts_dt).total_seconds()) <= SHIFT_TOLERANCE for t in ex_recover):
            reason = '与实际RECOVER偏移<=10min（同一恢复）'
        elif ev == 'ALARM':
            if any(abs((t - ts_dt).total_seconds()) <= SHIFT_TOLERANCE for t in ex_alarm):
                reason = '与实际ALARM时序偏移<10min（同一报警）'
            else:
                idx = replay_events.index(item)
                nxt_r = next((x for x in replay_events[idx + 1:] if x[0] == 'RECOVER'), None)
                if nxt_r:
                    nxt_dt = datetime.strptime(nxt_r[1], TS_FMT)
                    r_covered = any(abs((t - nxt_dt).total_seconds()) <= MATCH_TOLERANCE
                                    for t in ex_recover)
                    a_covered = any(ts_dt < t < nxt_dt + timedelta(seconds=MATCH_TOLERANCE)
                                    for t in ex_alarm)
                    if r_covered and a_covered:
                        reason = '该冻结段已有实际ALARM覆盖'
        if reason:
            skipped.append((ev, ts_str, reason))
        else:
            to_write.append(item)
    return to_write, skipped


def main():
    ap = argparse.ArgumentParser(description='核验 C02 死值校验告警并补漏')
    ap.add_argument('--start', default='2026-10-27 15:00:00', help='核验窗口起点')
    ap.add_argument('--end', default=None, help='核验窗口终点（默认 现在-10分钟）')
    ap.add_argument('--write', action='store_true', help='补写 PG wind_alarm')
    ap.add_argument('--csv', action='store_true', help='同时追加到 alarm_temgende.csv')
    ap.add_argument('--skip-series', nargs='*', default=[],
                    help='不补写的序列，格式 设备id前缀:测点 或 设备id前缀，如 93e2e5fd:TEMGENSEN1')
    args = ap.parse_args()

    skip_series = set()
    for s in args.skip_series:
        skip_series.add(tuple(s.split(':', 1)) if ':' in s else (s, None))

    start = parse_ts(args.start)
    end = parse_ts(args.end) if args.end else (datetime.now() - timedelta(minutes=10))
    if end <= start:
        sys.exit('结束时间必须晚于开始时间')

    conn = connect()
    dev_map = load_devices(conn)
    print(f'窗口 {start.strftime(TS_FMT)} ~ {end.strftime(TS_FMT)}')
    print('扫描疑似死值序列（同一值 >=100 点且跨度 >=2700s）...')
    series = find_frozen_candidates(conn, start, end)
    print(f'候选序列共 {len(series)} 个\n')

    sum_replay = sum_actual = sum_matched = sum_missing = sum_extra = 0
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
        events = replay(points, start.timestamp(), end.timestamp(), active, last_alarm)
        existing = load_existing(conn, wid, pc, start, end)
        missing, extra, matched = match_events(events, existing)
        to_write, skipped = classify_missing(missing, events, existing,
                                             start.timestamp())
        if any(wid.startswith(p) and (c is None or c == pc) for p, c in skip_series):
            skipped.extend((ev, ts_str, '--skip-series 指定排除') for ev, ts_str, _, _ in to_write)
            to_write = []

        sum_replay += len(events)
        sum_actual += len(existing)
        sum_matched += matched
        sum_missing += len(to_write)
        sum_extra += len(extra)

        pname = POINTS[pc][1]
        tag = f'  {meta["station"]} {meta["name"]} {pname} ({wid[:8]})'
        print(f'{tag} 推演 {len(events)} / 实际 {len(existing)} / 匹配 {matched}'
              f' / 待补 {len(to_write)} / 跳过 {len(skipped)} / 多出 {len(extra)}', flush=True)
        if missing or extra:
            print(f'    推演: ' + ' | '.join(f'{e}@{t}' for e, t, _, _ in events), flush=True)
            print(f'    实际: ' + ' | '.join(f'{e}@{t.strftime(TS_FMT)}' for e, t in existing), flush=True)
        for ev, ts_str, stats, detail in to_write:
            s = stats or {}
            print(f'      补 {ev:7s} {ts_str} temp={s.get("temp")} '
                  f'r1h={s.get("range_1h")} {detail}', flush=True)
            all_rows.append((meta, pc, ev, ts_str, stats, detail))
        for ev, ts_str, reason in skipped:
            print(f'      跳 {ev:7s} {ts_str} — {reason}', flush=True)
        for ev, ts_dt in extra:
            print(f'      多 {ev:7s} {ts_dt.strftime(TS_FMT)} （实际有推演无，不动）', flush=True)

    print(f'\n=== 汇总：推演 {sum_replay} / 实际 {sum_actual} / 匹配 {sum_matched}'
          f' / 待补 {sum_missing} / 多出 {sum_extra} ===')
    if not sum_missing:
        conn.close()
        print('无漏报，无需补数')
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
