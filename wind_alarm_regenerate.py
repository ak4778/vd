# -*- coding: utf-8 -*-
"""历史告警回放：遍历指定时间段的秒级数据，按时间顺序喂给告警引擎，
补生成那段时间漏掉的告警事件（ALARM / RECOVER）。

复用 wind_alarm.py 的 SlidingBuffer / AlarmEngine / ExceedCSV / 规则判定，
数据源 = 本地 PG wind_seconds_di 表，数据源连接方式与实时监控完全一致。

用法示例：
  python wind_alarm_regenerate.py                           # 回放 11-02 02:20 ~ 04:49（丢失段）
  python wind_alarm_regenerate.py "2026-11-01 13:00:00" "2026-11-02 00:00:00"
  python wind_alarm_regenerate.py "2026-10-30 00:00:00" "2026-10-30 23:59:59"
"""
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime

# 复用实时监控模块（不触发它的 if __name__ == '__main__' 分支）
import wind_alarm as wa


# ============================== 输出配置 ==============================
# 带 _regen 后缀，避免与实时进程正在写的 CSV 冲突
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGEN_ALARM_CSV = os.path.join(_BASE_DIR, "alarm_temgende_regen.csv")
REGEN_EXCEED_CSV = os.path.join(_BASE_DIR, "fjmjsj_temgende_exceed_regen.csv")


def load_window(conn, wid_upper, upper2wid, start_dt, end_dt):
    """一次性拉取时间窗口内全部数据，按 (wid_原始, pc) 分组返回。

    返回 {(wid, pc): [(ts_str, epoch, value_float), ...]}，每组内按 epoch 升序。
    SQL 结构与 wind_alarm.fetch_round_db 完全一致，复用 POINT_CONFIG['column']。
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
    # 同组内 SQL 已 ORDER BY ts，理论上已排好，稳一点再排一次
    for k in grouped:
        grouped[k].sort(key=lambda x: x[1])
    return grouped


def clear_window_in_pg(conn, start_dt, end_dt):
    """在 wind_alarm 表中删除该时间窗口内已有的事件，避免回放后重复。

    注意：只删 (device_id=ANY白名单, point_code=ANY监控点) 范围内的行，
    以防误伤其他来源写入的记录（虽然不太可能）。"""
    cur = conn.cursor()
    cur.execute(
        f"DELETE FROM {wa.TABLE_ALARM} "
        f"WHERE ts >= %s AND ts < %s",
        (start_dt, end_dt))
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def main():
    # ---- 1. 解析参数 ----
    if len(sys.argv) < 3:
        print("用法: python wind_alarm_regenerate.py '开始时间' '结束时间'")
        print("  例: python wind_alarm_regenerate.py '2026-11-02 02:20:00' '2026-11-02 04:49:00'")
        sys.exit(1)

    try:
        start_dt = datetime.strptime(sys.argv[1], wa.TS_FMT)
        end_dt = datetime.strptime(sys.argv[2], wa.TS_FMT)
    except ValueError:
        print("时间格式错误，应为 'YYYY-MM-DD HH:MM:SS'")
        sys.exit(1)

    print(f"{'='*60}")
    print(f"告警回放: {start_dt} ~ {end_dt}")
    print(f"{'='*60}")

    # ---- 2. 发现白名单设备 ----
    devices = wa.discover_devices()
    dev_map = {d['wid']: d for d in devices}
    upper2wid = {d['wid'].upper(): d['wid'] for d in devices}
    wid_upper = list(upper2wid)

    # ---- 3. 初始化滑窗 + 引擎 ----
    buffers = {(d['wid'], pc): wa.SlidingBuffer()
               for d in devices for pc in wa.POINT_CODES}

    # 回放模式：AlarmEngine 不做 load_state（不从 PG 恢复活跃状态），
    # 因为回放起始时刻的活跃状态我们没有精确快照（且这段数据本来就缺失）。
    # 回放会从"全静态"起步，等到数据喂入后自动产生 ALARM/RECOVER 边沿。
    engine = wa.AlarmEngine(REGEN_ALARM_CSV, dev_map)
    exceed = wa.ExceedCSV(REGEN_EXCEED_CSV)

    # ---- 4. 拉数据 ----
    conn = wa.connect()
    t0 = time.time()
    print(f"从 {wa.TABLE_SECONDS} 拉取数据中...")
    grouped = load_window(conn, wid_upper, upper2wid, start_dt, end_dt)
    fetch_sec = time.time() - t0

    total_points = sum(len(v) for v in grouped.values())
    active_seqs = sum(1 for v in grouped.values() if v)
    print(f"拉取完成：{len(grouped)} 个 (设备×测点) 序列，"
          f"{active_seqs} 个有数据，共 {total_points:,} 个点，耗时 {fetch_sec:.1f}s")

    if total_points == 0:
        print("窗口内没有数据，退出。")
        conn.close()
        engine.close()
        exceed.close()
        return

    # ---- 5. 清 PG 内该窗口的旧告警（避免回放后重复） ----
    t1 = time.time()
    del_n = clear_window_in_pg(conn, start_dt, end_dt)
    print(f"清理 {wa.TABLE_ALARM} 旧事件 {del_n} 行，耗时 {time.time() - t1:.1f}s")
    conn.close()

    # ---- 6. 合并成全局有序时间序列 ----
    all_ticks = []   # (epoch, wid, pc, ts_str, value)
    for (wid, pc), pts in grouped.items():
        for ts_str, epoch, value in pts:
            all_ticks.append((epoch, wid, pc, ts_str, value))
    all_ticks.sort(key=lambda x: x[0])   # epoch 升序

    print(f"开始回放 {len(all_ticks):,} 个数据点（全局时间顺序）...")

    # ---- 7. 逐点回放，规则判定 ----
    t2 = time.time()
    # engine.update 内部用 ALARM_COOLDOWN（秒）比对 now_epoch，
    # 回放时把数据点自身 epoch 当作 now_epoch，冷却按真实时间间隔工作
    for i, (epoch, wid, pc, ts_str, value) in enumerate(all_ticks):
        buf = buffers[(wid, pc)]
        # SlidingBuffer.add_raw 会做 future-check 和窗口裁剪；
        # 回放时 now_epoch = 数据点自身 epoch，future-check 不会触发
        buf.add_raw([{'ts': ts_str, 'pointValue': str(value)}], epoch)

        stats = wa.compute_stats(buf, epoch)
        if stats is None:
            continue

        pc_cfg = wa.POINT_CONFIG[pc]
        latest = buf.latest()

        # 规则01：越限
        hit_code = wa.classify_rule01(value, pc_cfg)
        for code in ('C01_FAULT', 'C01_HIGH', 'C01_LOW'):
            engine.update(wid, pc, code, code == hit_code, epoch, stats=stats)

        # 规则02：卡死
        # 回放模式下 lag=0（数据点即 now），恒满足 FROZEN_FRESHNESS
        if latest is not None:
            active, _ = wa.eval_frozen(
                buf.window_values(latest[0], wa.W_FROZEN), epoch)
            engine.update(wid, pc, 'C02_FROZEN', active, epoch, stats=stats)

        # 规则03：突变（内部有 streak_start 跨 tick 状态）
        wa.eval_fluct_tick(buf, epoch, engine, wid, pc, stats)

        # 超温原始点文件（命中 C01 越限时追加）
        if wa.classify_rule01(value, pc_cfg) is not None:
            dev = dev_map.get(wid, {})
            exceed.write(dev, pc, ts_str, str(value))

        # 进度心跳
        if (i + 1) % 50000 == 0:
            elapsed = time.time() - t2
            rate = (i + 1) / elapsed
            print(f"  ... 回放 {i+1:,}/{len(all_ticks):,} "
                  f"({(i+1)/len(all_ticks)*100:.1f}%) "
                  f"速度 {rate:,.0f} 点/s 活跃告警={engine.active_count()}")

    replay_sec = time.time() - t2

    # ---- 8. 收尾 ----
    engine.close()
    exceed.close()

    print(f"\n{'='*60}")
    print(f"回放完成！")
    print(f"  回放数据点: {len(all_ticks):,}")
    print(f"  耗时:       {fetch_sec:.1f}s(拉取) + {replay_sec:.1f}s(回放) = {fetch_sec+replay_sec:.1f}s")
    print(f"  活跃告警数: {engine.active_count()}")
    print(f"  PG 表:      {wa.TABLE_ALARM}")
    print(f"  告警 CSV:   {REGEN_ALARM_CSV}")
    print(f"  超温 CSV:   {REGEN_EXCEED_CSV}")


if __name__ == '__main__':
    main()
