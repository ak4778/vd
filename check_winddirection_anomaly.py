"""
检测输入文件夹中风向数据（fjmjsj_winddirection*.csv）里连续跳变持续满 3 分钟（180s）的异常段。
针对大文件（~33GB）采用两阶段流式处理：
  Phase 1: 逐行读取输入文件夹中全部匹配 CSV，按 windDeviceId 分桶写入临时文件（仅1次顺序扫描）
  Phase 2: 逐个设备文件加载 → 时间升序排序 → 连续跳变链检测 → 异常段直写输出文件

角度差计算采用环形最短差（0°/360°视为同一点，差<=180）。
异常段定义: 首次跳变后每次跳变间隔≤20s 持续出现，跳变链持续时长≥180s，
且末次跳变后点角度绝对值>90°。
"""

import csv
import glob
import os
from datetime import datetime
from collections import defaultdict

#INPUT_DIR = r"e:\data\i"
#OUTPUT_FILE = r"e:\data\e1.csv"
INPUT_DIR = r"e:\data\d3"   # 输入文件夹：读取其中匹配模式的所有 CSV
INPUT_PATTERN = "fjmjsj_winddirection*.csv"  # 输入文件名匹配模式（支持多个分片文件）
OUTPUT_FILE = r"e:\data\d3\winddirection_anomalies.csv"
TMP_DIR = r"e:\data\d3\_wd_tmp"
TIME_WINDOW_SEC = 180   # 跳变链需持续满的时长（秒）：连续跳变链持续≥此值才输出
ANGLE_THRESHOLD = 90.0  # 跳变阈值（度）：相邻两点角度差超过此值视为跳变
ANGLE_ABS_THRESHOLD = 90.0  # 角度绝对值阈值（度）：末次跳变后点角度绝对值需超过此值才输出
JUMP_GAP_TIMEOUT_SEC = 20  # 连续跳变间隔上限：超过此值无新跳变则链结束

CSV_HEADER = [
    "windDeviceId", "windDeviceName", "typeName", "phaseName",
    "ts_jump1", "ts_jump2", "gap_sec",
    "angle_before1", "angle_after1", "diff1_deg",
    "angle_before2", "angle_after2", "diff2_deg",
    "angle_before1_norm", "angle_after1_norm",
    "angle_before2_norm", "angle_after2_norm",
]


def normalize_angle(deg):
    try:
        return float(deg) % 360.0
    except (TypeError, ValueError):
        return None


def is_valid_range(v):
    """量程合法性校验：v ∈ [0, 360] 或 v ∈ [-180, 180] 任一即可。"""
    return (0.0 <= v <= 360.0) or (-180.0 <= v <= 180.0)


def shortest_angle_diff(a, b):
    diff = abs(a - b)
    return diff if diff <= 180 else 360.0 - diff


def parse_ts(ts_str):
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")


def safe_filename(dev_id):
    return f"dev_{abs(hash(dev_id)) & 0xFFFFFFFF:x}.csv"


# ================================================================
# Phase 1: 按设备分桶写临时文件
# ================================================================
def phase1_split_by_device():
    print("=" * 60)
    print("PHASE 1: 扫描输入文件夹全部匹配CSV，按设备分桶写入临时文件...")
    print("=" * 60)

    index_path = os.path.join(TMP_DIR, "_index.txt")
    # 若临时分桶文件已存在（上次 Phase 1 完成），直接复用，跳过重新扫描
    if os.path.exists(index_path):
        dev_ids = []
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    dev_id = line.split("\t", 1)[0]
                    dev_ids.append(dev_id)
        if dev_ids:
            print(f"  检测到已有临时分桶文件（{len(dev_ids)} 设备），跳过 Phase 1 重新扫描")
            print(f"  如需重新扫描，请删除 {TMP_DIR} 目录后重跑")
            return dev_ids, 0, 0
        print("  临时索引为空，重新扫描...")

    # 列出输入文件夹中所有匹配的 CSV 文件
    input_files = sorted(glob.glob(os.path.join(INPUT_DIR, INPUT_PATTERN)))
    if not input_files:
        print(f"  输入文件夹中无匹配文件: {os.path.join(INPUT_DIR, INPUT_PATTERN)}")
        return [], 0, 0
    print(f"  共 {len(input_files)} 个输入文件:")
    for p in input_files:
        print(f"    {p}")

    os.makedirs(TMP_DIR, exist_ok=True)
    dev_file_handles = {}
    dev_file_writers = {}
    index_fp = open(index_path, "w", encoding="utf-8")

    total_lines = 0
    kept_lines = 0
    out_of_range = 0

    for fpath in input_files:
        print(f"  -> 读取 {os.path.basename(fpath)} ...", flush=True)
        with open(fpath, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # 跳过表头
            for row in reader:
                total_lines += 1
                if len(row) < 7:
                    continue
                dev_id = row[0]
                deg = normalize_angle(row[4])
                if deg is None:
                    continue
                # 量程合法性校验（数据检测前）：超量程数据直接丢弃
                if not is_valid_range(float(row[4])):
                    out_of_range += 1
                    continue
                # 精简字段，减小临时文件体积
                out_row = [row[0], row[1], row[2], row[3], row[4], row[6]]
                if dev_id not in dev_file_handles:
                    path = os.path.join(TMP_DIR, safe_filename(dev_id))
                    fh = open(path, "w", encoding="utf-8", newline="")
                    w = csv.writer(fh)
                    dev_file_handles[dev_id] = fh
                    dev_file_writers[dev_id] = w
                    index_fp.write(f"{dev_id}\t{path}\n")
                dev_file_writers[dev_id].writerow(out_row)
                kept_lines += 1

                if total_lines % 100_000 == 0:
                    print(f"  scanned {total_lines:>12,} lines | kept {kept_lines:>12,} | oor {out_of_range:>10,} | devices = {len(dev_file_handles)}", flush=True)

    index_fp.close()
    for fh in dev_file_handles.values():
        fh.close()

    dev_ids = list(dev_file_handles.keys())
    print(f"\nPhase 1 done: scanned {total_lines:,} lines, kept {kept_lines:,} lines, out-of-range {out_of_range:,}, devices = {len(dev_ids)}")
    return dev_ids, total_lines, kept_lines


# ================================================================
# Phase 2: 逐个设备排序后检测连续跳变链持续满 180s
#   跳变: 连续两点间隔≤180s 且角度差>90°
#   连续性: 每次跳变后 20s 内必须再出现跳变，否则链结束
#   输出: 连续跳变链持续满 180s，且末次跳变后点绝对值 >90° 且量程合法
# ================================================================
def phase2_detect_and_write(dev_ids, out_writer):
    print("\n" + "=" * 60)
    print("PHASE 2: 逐设备排序 + 检测连续跳变链持续满 180s...")
    print(f"  跳变定义: 连续两点间隔≤{TIME_WINDOW_SEC}s 且角度差>{ANGLE_THRESHOLD}°")
    print(f"  连续性: 每次跳变后 {JUMP_GAP_TIMEOUT_SEC}s 内必须再出现跳变，否则链结束")
    print(f"  输出条件: 连续跳变链持续满 {TIME_WINDOW_SEC}s，且末次跳变后点绝对值>{ANGLE_ABS_THRESHOLD}°")
    print("=" * 60)

    path_map = {}
    idx_path = os.path.join(TMP_DIR, "_index.txt")
    with open(idx_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            dev_id, fp = line.split("\t", 1)
            path_map[dev_id] = fp

    total_anom_segs = 0
    devs_with_anom = 0
    devs_done = 0
    total_devs = len(dev_ids)
    total_phase2_rows = 0  # 跨设备累计处理行数（用于进度日志）
    next_log_at = 100_000  # 下一次打印进度时的累计行数阈值

    for dev_id in dev_ids:
        devs_done += 1
        fp = path_map.get(dev_id)
        if not fp or not os.path.exists(fp):
            continue

        # ---- 加载单个设备文件 ----
        rows = []
        with open(fp, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            for r in reader:
                if len(r) < 6:
                    continue
                deg = normalize_angle(r[4])
                if deg is None:
                    continue
                # 量程合法性校验（数据检测前, 兜底）：即使复用旧分桶也过滤超量程点
                if not is_valid_range(float(r[4])):
                    continue
                try:
                    ts = parse_ts(r[5])
                except ValueError:
                    continue
                rows.append({
                    "name": r[1], "typ": r[2], "ph": r[3],
                    "raw": r[4], "ts_str": r[5], "ts": ts, "ang": deg,
                })

        if len(rows) < 2:
            if devs_done % 50 == 0:
                print(f"  [{devs_done:>5}/{total_devs}] <2 rows, skip")
            continue

        rows.sort(key=lambda x: x["ts"])
        n = len(rows)
        total_phase2_rows += n
        anom_this = 0

        # 累计行数跨越阈值时打印进度
        if total_phase2_rows >= next_log_at:
            print(f"  [{devs_done:>5}/{total_devs}] cumulative rows = {total_phase2_rows:>12,} | anom segs so far = {total_anom_segs:,}", flush=True)
            next_log_at = ((total_phase2_rows // 100_000) + 1) * 100_000

        # ---- 检测连续跳变链持续满 180s ----
        # 跳变定义: 连续两点(i, i+1)间隔≤180s 且角度差>90°
        # 连续性: 每次跳变后 JUMP_GAP_TIMEOUT_SEC(20s)内必须再出现跳变，否则链结束
        # 输出条件: 跳变链持续时长≥180s，且末次跳变后点有 >90° 跳变且绝对值 >90
        i = 0
        while i < n - 1:
            # 检查连续两点 (i, i+1) 是否为跳变
            pair_gap1 = (rows[i + 1]["ts"] - rows[i]["ts"]).total_seconds()
            if pair_gap1 > TIME_WINDOW_SEC:
                i += 1
                continue
            diff1 = shortest_angle_diff(rows[i]["ang"], rows[i + 1]["ang"])
            if diff1 <= ANGLE_THRESHOLD:
                i += 1
                continue

            # 第一次跳变找到：rows[i] → rows[i+1]，跳变链从此开始
            jump1_before = rows[i]
            jump1_after = rows[i + 1]
            last_jump_ts = jump1_after["ts"]  # 最近一次跳变时间
            last_jump_before = jump1_before   # 最近一次跳变的前点
            last_jump_after = jump1_after     # 最近一次跳变的后点
            last_diff = diff1                 # 最近一次跳变的角度差

            # 追踪跳变链：每次跳变后 20s 内必须再出现跳变，否则链结束（不设窗口上限）
            j = i + 1
            while j < n - 1:
                ts_j1 = rows[j + 1]["ts"]
                # 20s 超时检查：距上次跳变超过 20s 则链结束
                if (ts_j1 - last_jump_ts).total_seconds() > JUMP_GAP_TIMEOUT_SEC:
                    break
                # 检查 (j, j+1) 是否为跳变
                pair_gap = (ts_j1 - rows[j]["ts"]).total_seconds()
                if pair_gap <= TIME_WINDOW_SEC and pair_gap > 0:
                    d = shortest_angle_diff(rows[j]["ang"], rows[j + 1]["ang"])
                    if d > ANGLE_THRESHOLD:
                        last_jump_ts = ts_j1  # 更新最近跳变时间
                        last_jump_before = rows[j]
                        last_jump_after = rows[j + 1]
                        last_diff = d
                j += 1

            # 链结束：链持续时长 = 末次跳变后点 - 首次跳变后点
            chain_sec = (last_jump_ts - jump1_after["ts"]).total_seconds()
            # 输出阶段量程合法性校验：末次跳变后点与首次跳变后点均须在量程内
            ep_raw = float(last_jump_after["raw"])
            j1_raw = float(jump1_after["raw"])
            if (chain_sec >= TIME_WINDOW_SEC
                    and abs(ep_raw) > ANGLE_ABS_THRESHOLD
                    and is_valid_range(ep_raw)
                    and is_valid_range(j1_raw)):
                gap_sec = int(chain_sec)
                rec = [
                    dev_id, jump1_before["name"], jump1_before["typ"], jump1_before["ph"],
                    jump1_after["ts_str"], last_jump_after["ts_str"], gap_sec,
                    jump1_before["raw"], jump1_after["raw"], round(diff1, 3),
                    last_jump_before["raw"], last_jump_after["raw"], round(last_diff, 3),
                    round(jump1_before["ang"], 3), round(jump1_after["ang"], 3),
                    round(last_jump_before["ang"], 3), round(last_jump_after["ang"], 3),
                ]
                out_writer.writerow(rec)
                anom_this += 1
                total_anom_segs += 1
            # 从链结束位置继续扫描（超时点对可作为新链的起点）
            i = max(j, i + 1)

        if anom_this > 0:
            devs_with_anom += 1
            print(f"  [{devs_done:>5}/{total_devs}] {rows[0]['name']:>6}  rows={n:,}  ANOM_SEGS={anom_this:,}")
        elif devs_done % 10 == 0:
            print(f"  [{devs_done:>5}/{total_devs}] {rows[0]['name']:>6}  rows={n:,}  segs=0")

    return total_anom_segs, devs_with_anom


def cleanup_tmp():
    try:
        import shutil
        if os.path.exists(TMP_DIR):
            shutil.rmtree(TMP_DIR)
            print(f"  (临时目录 {TMP_DIR} 已清理)")
    except Exception as e:
        print(f"  (临时目录清理跳过: {e})")


def main():
    print(f"输入文件夹 : {INPUT_DIR}")
    print(f"匹配模式   : {INPUT_PATTERN}")
    print(f"跳变定义 : 连续两点间隔≤{TIME_WINDOW_SEC}s 且角度差 > {ANGLE_THRESHOLD}°")
    print(f"连续性   : 每次跳变后 {JUMP_GAP_TIMEOUT_SEC}s 内必须再出现跳变，否则链结束")
    print(f"输出条件 : 连续跳变链持续满 {TIME_WINDOW_SEC}s，且末次跳变后点绝对值>{ANGLE_ABS_THRESHOLD}°")
    print(f"输出文件 : {OUTPUT_FILE}")
    print()

    try:
        dev_ids, total_lines, kept_lines = phase1_split_by_device()
        if not dev_ids:
            print("无可分析数据。")
            cleanup_tmp()
            return

        # 提前打开输出文件，Phase 2 直写
        with open(OUTPUT_FILE, "w", encoding="utf-8-sig", newline="") as fout:
            out_writer = csv.writer(fout)
            out_writer.writerow(CSV_HEADER)
            total_anom, devs_anom = phase2_detect_and_write(dev_ids, out_writer)

        print("\n" + "=" * 60)
        print("FINISHED  SUMMARY")
        print("=" * 60)
        print(f"  源文件总行数     : {total_lines:,}")
        print(f"  有效数据行       : {kept_lines:,}")
        print(f"  设备总数         : {len(dev_ids)} 台")
        print(f"  存在异常设备     : {devs_anom} 台")
        print(f"  双跳变异常段数   : {total_anom:,}")
        print(f"  结果文件         : {OUTPUT_FILE}")
        # 仅成功时清理临时文件
        cleanup_tmp()
    except Exception as e:
        print(f"\nERROR: {e}")
        print(f"  临时文件保留在 {TMP_DIR}，修复后可直接重跑 Phase 2")
        raise


if __name__ == "__main__":
    main()
