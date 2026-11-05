# -*- coding: utf-8 -*-
"""历史告警回放：分段读取秒级数据，按 30 秒合成 tick 喂给告警引擎，
补生成指定时间段漏掉的告警事件（ALARM / RECOVER）。

复用 wind_alarm.py 的 SlidingBuffer / AlarmEngine / ExceedCSV / 规则判定，
数据源 = 本地 PG wind_seconds_di 表。

与逐点回放的区别：
  1. 先回灌 WARMUP_SECONDS(1 小时) 数据做预热，保证 02-死值校验在窗口开头即可判定；
  2. 按 TICK_SECONDS(30 秒) 合成 tick 判定，与实时 POLL_INTERVAL 对齐，不是逐点；
  3. 数据按 BATCH_SECONDS(10 分钟) 分批加载，内存只保留一个批次 + 每序列 1 小时滑窗；
  4. 回放前按 ts < 开始时间恢复引擎活跃状态；回放完成后用 id 水位一次性切换
     （只删白名单设备/测点、且 id <= 回放前最大 id 的旧事件），中途失败不影响原告警；
  5. 回放结果 CSV 每次重建，避免重复运行叠加。

用法示例：
  python wind_alarm_regenerate.py "2026-11-03 17:24:00" "2026-11-04 09:40:00"
  python wind_alarm_regenerate.py "2026-11-01 13:00:00" "2026-11-02 00:00:00"

注意：
  - 03 突变的 streak_start（连续满足起点）无法从历史事件精确还原，
    窗口开始后需重新连续满足 SUSTAIN_SEC 才告警（偏保守）；
  - 事件 ts 取触发数据点时刻。若某设备在窗口内始终无新数据，会沿用旧点判定，
    其事件 ts 可能落在窗口开始之前（与实时行为一致）；这类行会在切换阶段被清理，
    不会留在表里，也不会污染下一次回放的“窗口前状态”；
  - 必须在脚本所在目录运行（connect 读取相对路径 data_config.json），
    脚本启动时会自动切到该目录。
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import psycopg2

# 复用实时监控模块（不触发它的 if __name__ == '__main__' 分支）
import wind_alarm as wa

# 导入 wind_alarm 会挂上 wind_alarm.log 的轮转句柄；回放日志不应该混进实时日志，
# 这里把该进程内的句柄摘掉（wa.log 仍会打印到控制台）。
try:
    for _h in list(wa._logger.handlers):
        wa._logger.removeHandler(_h)
        _h.close()
except Exception:
    pass


# ============================== 输出配置 ==============================
# 带 _regen 后缀，避免与实时进程正在写的 CSV 冲突；每次运行前重建，防止重复叠加
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGEN_ALARM_CSV = os.path.join(_BASE_DIR, "alarm_temgende_regen.csv")
REGEN_EXCEED_CSV = os.path.join(_BASE_DIR, "fjmjsj_temgende_exceed_regen.csv")

# 回放参数
BATCH_SECONDS = 600       # 单批数据跨度（内存上限）
TICK_SECONDS = 30         # 合成 tick 周期，与实时 POLL_INTERVAL 对齐
WARMUP_SECONDS = 3600     # 预热时长，保证规则02 的 1 小时窗口可判（需 >= FROZEN_MIN_SPAN+余量）


def _reset_file(path):
    """重建 CSV（只清空，表头由 wa.open_csv_with_header 写入）。"""
    with open(path, 'w', encoding='utf-8', newline=''):
        pass


def load_window(conn, wid_upper, upper2wid, start_dt, end_dt):
    """拉取 [start_dt, end_dt) 内全部数据，按 (wid_原始, pc) 分组返回。

    返回 {(wid, pc): [(ts_str, epoch, value_float), ...]}，每组内按 epoch 升序。
    """
    cols = [wa.POINT_CONFIG[pc]['column'] for pc in wa.POINT_CODES]
    sql = f"""
        SELECT device_id, ts, {', '.join(cols)}
        FROM {wa.TABLE_SECONDS}
        WHERE ts >= %s AND ts < %s
          AND device_id = ANY(%s)
        ORDER BY device_id, ts
    """
    cur = conn.cursor()
    cur.execute(sql, (start_dt, end_dt, wid_upper))
    grouped = defaultdict(list)
    for dev_upper, ts, *values in cur:
        wid = upper2wid.get(dev_upper)
        if wid is None:
            continue
        ts_str = ts.strftime(wa.TS_FMT)
        ts_epoch = ts.timestamp()
        for pc, value in zip(wa.POINT_CODES, values):
            if value is None:
                continue
            grouped[(wid, pc)].append((ts_str, ts_epoch, float(value)))
    cur.close()
    for k in grouped:
        grouped[k].sort(key=lambda x: x[1])
    return grouped


def get_max_alarm_id(conn):
    """回放前告警表的最大 id，作为旧事件的切换水位。"""
    cur = conn.cursor()
    cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {wa.TABLE_ALARM}")
    val = cur.fetchone()[0]
    cur.close()
    return int(val)


def seed_engine_state(conn, engine, devices, start_dt):
    """把窗口开始前每个 (设备,测点,规则) 的最后一条事件恢复到引擎。

    这样窗口内写出的 RECOVER 能正确对应窗口前已存在的 ALARM，避免删除窗口旧事件
    后出现“只有 ALARM 没有 RECOVER”的悬挂状态；同时让冷却从真实事件时刻连续。
    """
    wid_list = [d['wid'] for d in devices]
    cur = conn.cursor()
    cur.execute(f"""
        SELECT DISTINCT ON (device_id, point_code, rule_code)
               device_id, point_code, rule_code, event, ts
        FROM {wa.TABLE_ALARM}
        WHERE ts < %s AND device_id = ANY(%s) AND point_code = ANY(%s)
        ORDER BY device_id, point_code, rule_code,
                 ts DESC NULLS LAST, id DESC
    """, (start_dt, wid_list, list(wa.POINT_CODES)))
    rows = cur.fetchall()
    cur.close()
    n_active = 0
    for wid, pc, code, event, etime in rows:
        key = (wid, pc, code)
        engine.last_alarm[key] = etime.timestamp() if etime else start_dt.timestamp()
        if event == 'ALARM':
            engine.active[key] = True
            n_active += 1
    print(f"窗口前状态恢复：{len(rows)} 个序列，其中 {n_active} 条活跃告警")


def replace_window_in_pg(conn, start_dt, end_dt, wid_list, max_id):
    """回放成功后切换：只删白名单设备/测点、且 id <= 回放前最大 id 的窗口旧事件。

    回放新写入的行 id > max_id，会被保留；实时进程在回放期间写入的行 ts 在窗口外，
    也不会被误删。单条 DELETE 原子生效，中途失败不会留下“删了还没回填”的状态。
    另外清理本次回放中“沿用窗口前旧点判定”而落到窗口之前的新事件（ts < start），
    保证窗口外不会残留重复行、也不会污染下一次回放的窗口前状态。
    """
    cur = conn.cursor()
    cur.execute(
        f"DELETE FROM {wa.TABLE_ALARM} "
        f"WHERE ts >= %s AND ts < %s AND id <= %s "
        f"AND device_id = ANY(%s) AND point_code = ANY(%s)",
        (start_dt, end_dt, max_id, wid_list, list(wa.POINT_CODES)))
    n_old = cur.rowcount
    cur.execute(
        f"DELETE FROM {wa.TABLE_ALARM} "
        f"WHERE ts < %s AND id > %s "
        f"AND device_id = ANY(%s) AND point_code = ANY(%s)",
        (start_dt, max_id, wid_list, list(wa.POINT_CODES)))
    n_out = cur.rowcount
    conn.commit()
    cur.close()
    return n_old, n_out


def evaluate_tick(series, buffers, engine, tick_epoch):
    """按实时主循环的口径判定所有序列（C01 -> C02 -> C03）。"""
    for wid, pc in series:
        buf = buffers[(wid, pc)]
        latest = buf.latest()
        if latest is None:
            continue

        stats = wa.compute_stats(buf, tick_epoch)
        pc_cfg = wa.POINT_CONFIG[pc]

        # 规则01：越限（与实时一致，只推进三个正常越限码）
        hit_code = wa.classify_rule01(latest[1], pc_cfg)
        for code in ('C01_FAULT', 'C01_HIGH', 'C01_LOW'):
            engine.update(wid, pc, code, code == hit_code, tick_epoch, stats=stats)

        # 规则02：数据中断防误报 + 卡死（lag 用合成时钟，语义与实时一致）
        lag = tick_epoch - latest[0]
        if lag <= wa.FROZEN_FRESHNESS:
            active, _meta = wa.eval_frozen(
                buf.window_values(latest[0], wa.W_FROZEN), tick_epoch)
            engine.update(wid, pc, 'C02_FROZEN', active, tick_epoch, stats=stats)
        elif lag > wa.C02_STALE_RECOVER and engine.is_active(wid, pc, 'C02_FROZEN'):
            engine.update(wid, pc, 'C02_FROZEN', False, tick_epoch, stats=stats,
                          detail='断采>30min')

        # 规则03：突变（内部有 streak_start 跨 tick 状态）
        wa.eval_fluct_tick(buf, tick_epoch, engine, wid, pc, stats)


def replay_window(load_batch, devices, dev_map, upper2wid, buffers, engine, exceed,
                  start_dt, end_dt):
    """分段加载 + 30 秒合成 tick 回放，返回 (总点数, 预热秒数)。

    load_batch(批次开始, 批次结束) -> {(wid,pc): [(ts_str, epoch, value), ...]}
    """
    series = [(d['wid'], pc) for d in devices for pc in wa.POINT_CODES]

    warm_start = start_dt - timedelta(seconds=WARMUP_SECONDS)
    batch_start = warm_start
    carry = {}              # 上一批次未消费的点（ts 跨批次时用，量很小）
    total_points = 0
    ticks = 0
    tick0 = time.time()

    while batch_start < end_dt:
        batch_end = min(batch_start + timedelta(seconds=BATCH_SECONDS), end_dt)
        grouped = load_batch(batch_start, batch_end)

        # 拼上上一批遗留（ts 小于本批，直接前置，保持升序）
        data = {}
        for key, items in grouped.items():
            prev = carry.get(key)
            data[key] = (prev + items) if prev else items
        carry = {}
        pos = {key: 0 for key in data}

        t_dt = batch_start
        while t_dt < batch_end:
            t_epoch = t_dt.timestamp()
            in_window = t_dt >= start_dt

            # 送入 ts < t_epoch 的新点（与实时 fetch 的 '< end' 语义一致）
            for key, items in data.items():
                i = pos[key]
                n = len(items)
                new_items = []
                while i < n and items[i][1] < t_epoch:
                    ts_str, _ep, val = items[i]
                    new_items.append({'ts': ts_str, 'pointValue': val})
                    i += 1
                pos[key] = i
                if not new_items:
                    continue
                wid, pc = key
                recs = buffers[key].add_raw(new_items, t_epoch)
                total_points += len(recs)
                if in_window:
                    dev = dev_map.get(wid, {})
                    cfg = wa.POINT_CONFIG[pc]
                    for ts_str, _ep, pv, v in recs:
                        if wa.classify_rule01(v, cfg) is not None:
                            exceed.write(dev, pc, ts_str, pv)

            if in_window:
                evaluate_tick(series, buffers, engine, t_epoch)
                ticks += 1
                if ticks % 20 == 0:   # 每 10 分钟（模拟时间）打印一次进度
                    elapsed = time.time() - tick0
                    rate = total_points / elapsed if elapsed > 0 else 0
                    print(f"  ... 回放至 {t_dt.strftime(wa.TS_FMT)} "
                          f"已处理 {total_points:,} 点（{rate:,.0f} 点/s，含预热），"
                          f"模拟 tick={ticks}，活跃告警={engine.active_count()}",
                          flush=True)
            t_dt += timedelta(seconds=TICK_SECONDS)

        # 剩余未消费的点留到下一批（最多 TICK_SECONDS 跨度）
        carry = {key: data[key][pos[key]:] for key in data
                 if pos[key] < len(data[key])}
        batch_start = batch_end

    return total_points, WARMUP_SECONDS


def main():
    # ---- 1. 解析参数 ----
    if len(sys.argv) < 3:
        print("用法: python wind_alarm_regenerate.py '开始时间' '结束时间'")
        print("  例: python wind_alarm_regenerate.py "
              "'2026-11-03 17:24:00' '2026-11-04 09:40:00'")
        sys.exit(1)

    try:
        start_dt = datetime.strptime(sys.argv[1], wa.TS_FMT)
        end_dt = datetime.strptime(sys.argv[2], wa.TS_FMT)
    except ValueError:
        print("时间格式错误，应为 'YYYY-MM-DD HH:MM:SS'")
        sys.exit(1)
    if end_dt <= start_dt:
        print("结束时间必须晚于开始时间")
        sys.exit(1)

    # connect() 读取相对路径 data_config.json，固定到脚本目录
    os.chdir(_BASE_DIR)

    print(f"{'=' * 64}")
    print(f"告警回放: {start_dt} ~ {end_dt}")
    print(f"预热: {WARMUP_SECONDS}s | 批次: {BATCH_SECONDS}s | "
          f"tick: {TICK_SECONDS}s（与实时一致）")
    print(f"{'=' * 64}")

    # ---- 2. 白名单设备 ----
    devices = wa.discover_devices()
    dev_map = {d['wid']: d for d in devices}
    upper2wid = {d['wid'].upper(): d['wid'] for d in devices}

    # ---- 3. 初始化滑窗 + 引擎；CSV 重建，避免重复叠加 ----
    buffers = {(d['wid'], pc): wa.SlidingBuffer()
               for d in devices for pc in wa.POINT_CODES}
    _reset_file(REGEN_ALARM_CSV)
    _reset_file(REGEN_EXCEED_CSV)
    engine = wa.AlarmEngine(REGEN_ALARM_CSV, dev_map)
    exceed = wa.ExceedCSV(REGEN_EXCEED_CSV)

    conn = wa.connect()
    conn.autocommit = True      # 只读查询不持长事务，避免阻碍 autovacuum

    wid_upper = list(upper2wid)

    def load_batch(batch_start, batch_end):
        """分批查询；连接失效（服务器重启/网络抖动）时自动重连重试。"""
        nonlocal conn
        delay = 3
        last_err = None
        for attempt in range(1, 7):
            try:
                return load_window(conn, wid_upper, upper2wid,
                                   batch_start, batch_end)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last_err = e
                print(f"批次查询连接异常（第 {attempt} 次），{delay}s 后重连重试: {e}",
                      flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(delay)
                delay = min(delay * 2, 30)
                conn = wa.connect()
                conn.autocommit = True
        raise last_err

    try:
        # ---- 4. 记录 id 水位 + 恢复窗口前状态 ----
        max_id = get_max_alarm_id(conn)
        print(f"回放前 wind_alarm 最大 id={max_id}")
        seed_engine_state(conn, engine, devices, start_dt)

        # ---- 5. 分段 + 合成 tick 回放 ----
        print(f"开始回放（预热 {WARMUP_SECONDS}s + 窗口内按 {TICK_SECONDS}s tick 判定）...")
        total_points, _ = replay_window(
            load_batch, devices, dev_map, upper2wid, buffers, engine, exceed,
            start_dt, end_dt)

        # ---- 6. 切换窗口旧事件（仅成功回放后执行）----
        if total_points == 0:
            print("窗口内没有数据，未改动 wind_alarm，退出。")
            return

        wid_list = [d['wid'] for d in devices]
        del_n, del_out = replace_window_in_pg(
            conn, start_dt, end_dt, wid_list, max_id)
        print(f"切换完成：删除窗口内旧事件 {del_n} 行（id <= {max_id}），"
              f"清理窗口前新事件 {del_out} 行，保留本次回放新写入的窗口内事件")

        cur = conn.cursor()
        cur.execute(
            f"SELECT event, count(*) FROM {wa.TABLE_ALARM} "
            f"WHERE id > %s AND ts >= %s AND ts < %s "
            f"GROUP BY event ORDER BY event",
            (max_id, start_dt, end_dt))
        counts = cur.fetchall()
        cur.close()
        print("本次生成事件：" + (
            "，".join(f"{ev}={n:,}" for ev, n in counts) if counts else "无"))
    finally:
        engine.close()
        exceed.close()
        try:
            conn.close()
        except Exception:
            pass

    print(f"{'=' * 64}")
    print("回放完成")
    print(f"  处理数据点: {total_points:,}（含 {WARMUP_SECONDS}s 预热）")
    print(f"  PG 表:      {wa.TABLE_ALARM}")
    print(f"  告警 CSV:   {REGEN_ALARM_CSV}")
    print(f"  超温 CSV:   {REGEN_EXCEED_CSV}")
    print(f"{'=' * 64}")


if __name__ == '__main__':
    main()
