# -*- coding: utf-8 -*-
"""
检测三相支路电流不平衡：cur1/cur2/cur3
按 windDeviceId 分组，按 ts 匹配三相，
unbalance = max(|cur_i - avg|) / |avg| * 100 > 20% 则输出
"""
import csv
import os
import sys
import shutil
from collections import defaultdict
import time

# ====================== 配置 ======================
INPUT_FILES = [
    r"e:\data\d1\fjmjsj_cur1.csv",
    r"e:\data\d1\fjmjsj_cur2.csv",
    r"e:\data\d1\fjmjsj_cur3.csv",
]
OUTPUT_FILE  = r"e:\data\d1\curve_unbalance.csv"
TMP_DIR      = r"e:\data\d1\_cur_tmp"

UNBALANCE_THRESHOLD = 20.0   # 不平衡度阈值 (%)
TIME_TOLERANCE_SEC  = 0      # 时间戳匹配容差（秒），0=严格相等(推荐，避免跨时刻误配)
PROGRESS_INTERVAL   = 100_000  # 每 N 行打印一次进度

# ====================== 工具函数 ======================
def parse_ts(s):
    """解析 '2026-08-22 06:47:11' → epoch 秒"""
    try:
        t = time.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
        return time.mktime(t)
    except (ValueError, TypeError):
        return None

def shortest_angle_diff(a, b):
    """角度最短差（本脚本不用，保留备用）"""
    d = abs(a - b) % 360
    return min(d, 360 - d)

# ====================== Phase 1: 分桶 ======================
def phase1_split_by_device():
    """三个文件分别按 windDeviceId 分桶写入临时文件"""
    os.makedirs(TMP_DIR, exist_ok=True)

    # 检查是否已完成
    marker = os.path.join(TMP_DIR, "_phase1_done.marker")
    if os.path.exists(marker):
        print("Phase 1 已完成（标记文件存在），跳过分桶", flush=True)
        return

    # 设备 → 文件句柄缓存
    dev_fps = {}   # { (file_idx, dev_id): file_handle }
    dev_meta = {}   # { dev_id: (windDeviceName, typeName, phaseName) }
    total_lines = 0
    file_stats = []

    for file_idx, fpath in enumerate(INPUT_FILES):
        if not os.path.exists(fpath):
            print(f"  警告: 文件不存在 {fpath}", flush=True)
            file_stats.append(0)
            continue

        f_lines = 0
        print(f"\n--- 分桶文件 {file_idx+1}/3: {fpath} ---", flush=True)
        print(f"    大小: {os.path.getsize(fpath)/(1024**3):.2f} GB", flush=True)

        with open(fpath, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            for row in reader:
                if len(row) < 7:
                    continue
                dev_id = row[0]
                val = row[4]
                ts_str = row[6]
                dev_meta[dev_id] = (row[1], row[2], row[3])

                key = (file_idx, dev_id)
                if key not in dev_fps:
                    fname = f"f{file_idx}_{dev_id}.csv"
                    fpath_out = os.path.join(TMP_DIR, fname)
                    dev_fps[key] = open(fpath_out, "w", encoding="utf-8", newline="")
                    w = csv.writer(dev_fps[key])
                    w.writerow(["ts", "val"])

                w = csv.writer(dev_fps[key])
                w.writerow([ts_str, val])

                f_lines += 1
                total_lines += 1
                if total_lines % PROGRESS_INTERVAL == 0:
                    print(f"    file{file_idx+1} scanned {f_lines:>10,} | total {total_lines:>12,} | devs {len(dev_meta):,}",
                          flush=True)

        file_stats.append(f_lines)
        print(f"    file{file_idx+1} done: {f_lines:,} lines", flush=True)

    # 关闭所有文件句柄
    for fp in dev_fps.values():
        fp.close()

    # 写元数据文件
    meta_path = os.path.join(TMP_DIR, "_device_meta.csv")
    with open(meta_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["windDeviceId", "windDeviceName", "typeName", "phaseName"])
        for dev_id in sorted(dev_meta.keys()):
            nm, typ, ph = dev_meta[dev_id]
            w.writerow([dev_id, nm, typ, ph])

    # 写完成标记
    with open(marker, "w") as f:
        f.write("done")

    print(f"\nPhase 1 done: total {total_lines:,} lines, {len(dev_meta):,} devices", flush=True)
    print(f"  file1: {file_stats[0]:,}  file2: {file_stats[1]:,}  file3: {file_stats[2]:,}", flush=True)

# ====================== Phase 2: 检测不平衡 ======================
def load_device_file(fpath):
    """加载单个设备临时文件 → [(ts_epoch, val_float, ts_str), ...]"""
    rows = []
    if not os.path.exists(fpath):
        return rows
    with open(fpath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        for row in reader:
            if len(row) < 2:
                continue
            ts_epoch = parse_ts(row[0])
            if ts_epoch is None:
                continue
            try:
                val = float(row[1])
            except (ValueError, TypeError):
                continue
            rows.append((ts_epoch, val, row[0]))
    rows.sort(key=lambda x: x[0])
    return rows

def check_unbalance_for_device(dev_id, dev_info, fout, csv_writer):
    """检测单个设备的三相不平衡"""
    base = TMP_DIR
    cur1 = load_device_file(os.path.join(base, f"f0_{dev_id}.csv"))
    cur2 = load_device_file(os.path.join(base, f"f1_{dev_id}.csv"))
    cur3 = load_device_file(os.path.join(base, f"f2_{dev_id}.csv"))

    if not cur1 or not cur2 or not cur3:
        return 0

    # 用三指针匹配时间戳（带容差）
    i1 = i2 = i3 = 0
    n1, n2, n3 = len(cur1), len(cur2), len(cur3)
    anom_count = 0

    while i1 < n1 and i2 < n2 and i3 < n3:
        t1 = cur1[i1][0]
        t2 = cur2[i2][0]
        t3 = cur3[i3][0]

        # 找三者的最小时间
        tmin = min(t1, t2, t3)

        # 在容差范围内匹配
        matched1 = abs(t1 - tmin) <= TIME_TOLERANCE_SEC
        matched2 = abs(t2 - tmin) <= TIME_TOLERANCE_SEC
        matched3 = abs(t3 - tmin) <= TIME_TOLERANCE_SEC

        if matched1 and matched2 and matched3:
            # 三相都匹配
            v1 = cur1[i1][1]
            v2 = cur2[i2][1]
            v3 = cur3[i3][1]

            avg = (v1 + v2 + v3) / 3.0
            if abs(avg) > 1e-9:  # 避免除零
                max_dev = max(abs(v1 - avg), abs(v2 - avg), abs(v3 - avg))
                unbalance = max_dev / abs(avg) * 100.0

                if unbalance > UNBALANCE_THRESHOLD:
                    csv_writer.writerow([
                        dev_id, dev_info[0], dev_info[1], dev_info[2],
                        cur1[i1][2],  # ts_str
                        round(v1, 4), round(v2, 4), round(v3, 4),
                        round(avg, 4), round(unbalance, 2),
                        round(max(abs(v1-v2), abs(v2-v3), abs(v1-v3)), 4)
                    ])
                    anom_count += 1

            i1 += 1
            i2 += 1
            i3 += 1
        else:
            # 推进最小时间的指针
            if t1 == tmin:
                i1 += 1
            if t2 == tmin:
                i2 += 1
            if t3 == tmin:
                i3 += 1

    return anom_count

def phase2_detect():
    """逐设备检测不平衡"""
    meta_path = os.path.join(TMP_DIR, "_device_meta.csv")
    if not os.path.exists(meta_path):
        print("错误: 设备元数据文件不存在，请先运行 Phase 1", flush=True)
        return

    # 加载设备元数据
    devices = []
    with open(meta_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if len(row) >= 4:
                devices.append((row[0], (row[1], row[2], row[3])))

    total_devs = len(devices)
    print(f"\n{'='*60}", flush=True)
    print(f"Phase 2: 逐设备检测三相不平衡", flush=True)
    print(f"  设备数: {total_devs:,}", flush=True)
    print(f"  不平衡阈值: >{UNBALANCE_THRESHOLD}%", flush=True)
    print(f"  时间匹配容差: {TIME_TOLERANCE_SEC}s", flush=True)
    print(f"{'='*60}", flush=True)

    total_anom = 0
    devs_done = 0

    with open(OUTPUT_FILE, "w", encoding="utf-8-sig", newline="") as fout:
        writer = csv.writer(fout)
        writer.writerow([
            "windDeviceId", "windDeviceName", "typeName", "phaseName",
            "ts",
            "cur1", "cur2", "cur3",
            "avg", "unbalance_pct", "max_pair_diff"
        ])

        for dev_id, dev_info in devices:
            cnt = check_unbalance_for_device(dev_id, dev_info, fout, writer)
            total_anom += cnt
            devs_done += 1
            if devs_done % 100 == 0 or devs_done == total_devs:
                print(f"  [{devs_done:>5}/{total_devs}] anom records so far = {total_anom:,}", flush=True)

    print(f"\nPhase 2 done: {total_anom:,} unbalance records → {OUTPUT_FILE}", flush=True)

# ====================== 主函数 ======================
def main():
    print(f"输入文件:", flush=True)
    for i, f in enumerate(INPUT_FILES):
        exists = os.path.exists(f)
        size = os.path.getsize(f)/(1024**3) if exists else 0
        print(f"  cur{i+1}: {f} ({size:.2f} GB) {'✓' if exists else '✗ 不存在'}", flush=True)
    print(f"输出文件: {OUTPUT_FILE}", flush=True)
    print(f"不平衡阈值: >{UNBALANCE_THRESHOLD}%", flush=True)
    print(f"时间匹配容差: {TIME_TOLERANCE_SEC}s", flush=True)
    print()

    phase1_split_by_device()
    phase2_detect()
    print("\n全部完成。", flush=True)

if __name__ == "__main__":
    main()
