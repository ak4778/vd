# -*- coding: utf-8 -*-
"""
从 winddirection_anomalies.csv 读取每段异常，在输入文件夹中所有 fjmjsj_winddirection*.csv
提取该段前后各 EXTEND_RECORDS 条记录，每段绘制一个风向曲线图。
"""
import csv
import glob
import os
import bisect
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.transforms as mtransforms

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

# ====================== 配置 ======================
ANOMALY_FILE   = r"e:\data\d1\winddirection_anomalies.csv"
SOURCE_DIR     = r"e:\data\d1"                       # 源数据文件夹
SOURCE_PATTERN = "fjmjsj_winddirection*.csv"      # 源文件名匹配模式
OUTPUT_DIR     = r"e:\data\d1\anomaly_winddirection"
EXTEND_RECORDS = 10   # 向前/向后拓展的记录数
ANGLE_THRESHOLD = 90.0

# ====================== 工具函数 ======================
def parse_ts(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")

def shortest_angle_diff(a, b):
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d

# ====================== Step 1: 读取异常段 ======================
segments = []
with open(ANOMALY_FILE, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    next(reader)  # skip header
    for row in reader:
        if len(row) < 13:
            continue
        segments.append({
            "dev_id":    row[0],
            "dev_name":  row[1],
            "type_name": row[2],
            "phase_name":row[3],
            "ts1":       parse_ts(row[4]),
            "ts2":       parse_ts(row[5]),
            "ts1_str":   row[4],
            "ts2_str":   row[5],
            "gap_sec":   row[6],
            "diff1":     float(row[9]),
            "diff2":     float(row[12]),
        })

print(f"读取 {len(segments)} 个异常段, 涉及 {len(set(s['dev_id'] for s in segments))} 台设备")

# ====================== Step 2: 流式扫描全部源文件，只收集异常设备记录 ======================
anomaly_dev_ids = set(s["dev_id"] for s in segments)
dev_records = {}   # dev_id -> list of (ts, angle, raw)
total_scanned = 0

PROGRESS_FILE = r"e:\data\plot_progress.txt"

def write_progress(msg):
    """写进度到独立文件 + stdout"""
    with open(PROGRESS_FILE, "a", encoding="utf-8") as pf:
        pf.write(msg + "\n")
    print(msg, flush=True)

# 清空进度文件
with open(PROGRESS_FILE, "w", encoding="utf-8") as pf:
    pass

# 列出输入文件夹中所有匹配的 CSV 文件
source_files = sorted(glob.glob(os.path.join(SOURCE_DIR, SOURCE_PATTERN)))
write_progress(f"源文件夹: {SOURCE_DIR}")
write_progress(f"匹配模式: {SOURCE_PATTERN}")
if not source_files:
    write_progress(f"  错误：未找到匹配的源文件")
    exit(1)
write_progress(f"  共 {len(source_files)} 个源文件:")
for p in source_files:
    write_progress(f"    {p}")
write_progress(f"  目标设备数: {len(anomaly_dev_ids)}")

for fpath in source_files:
    write_progress(f"  -> 读取 {os.path.basename(fpath)} ...")
    with open(fpath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            total_scanned += 1
            if total_scanned % 1_000_000 == 0:
                collected = sum(len(v) for v in dev_records.values())
                write_progress(f"  scanned {total_scanned:>12,} | collected {collected:>10,} | devs {len(dev_records)}")
            if row[0] not in anomaly_dev_ids:
                continue
            try:
                ts = parse_ts(row[6])
                angle = float(row[4])
            except (ValueError, IndexError):
                continue
            dev_records.setdefault(row[0], []).append((ts, angle, row[4]))

collected = sum(len(v) for v in dev_records.values())
write_progress(f"扫描完成: {total_scanned:,} 行, 收集 {collected:,} 条记录 ({len(dev_records)} 台设备)")

# 按时间排序
for dev_id in dev_records:
    dev_records[dev_id].sort(key=lambda x: x[0])

# ====================== Step 3: 逐段绘图 ======================
os.makedirs(OUTPUT_DIR, exist_ok=True)

for idx, seg in enumerate(segments):
    dev_id = seg["dev_id"]
    records = dev_records.get(dev_id, [])
    if len(records) < 2:
        print(f"  段 {idx+1}: 设备 {dev_id} 记录不足, 跳过")
        continue

    ts_list = [r[0] for r in records]

    # 定位 ts1 在记录中的索引（跳变1"后"点的时间戳）
    idx1 = bisect.bisect_left(ts_list, seg["ts1"])
    if idx1 >= len(records):
        idx1 = len(records) - 1

    # 定位 ts2 在记录中的索引（窗口末点时间戳）
    idx2 = bisect.bisect_right(ts_list, seg["ts2"]) - 1
    if idx2 < 0:
        idx2 = 0

    # 向前/向后拓展 EXTEND_RECORDS 条
    start_idx = max(0, idx1 - EXTEND_RECORDS)
    end_idx   = min(len(records) - 1, idx2 + EXTEND_RECORDS)

    seg_records = records[start_idx:end_idx + 1]
    plot_ts = [r[0] for r in seg_records]
    plot_val = [r[1] for r in seg_records]

    # ---- 绘图 ----
    fig, ax = plt.subplots(figsize=(16, 6))

    # 蓝色：全部数据点连线
    ax.plot(plot_ts, plot_val, linewidth=1.0, color="steelblue",
            marker="o", markersize=4, linestyle="-", zorder=1)

    # 红色：跳变 > 90° 的线段
    for i in range(len(plot_ts) - 1):
        diff = shortest_angle_diff(plot_val[i], plot_val[i + 1])
        if diff > ANGLE_THRESHOLD:
            ax.plot([plot_ts[i], plot_ts[i + 1]], [plot_val[i], plot_val[i + 1]],
                    linewidth=2.0, color="red", marker="o", markersize=6,
                    linestyle="-", zorder=2)
            ax.annotate(f"{plot_val[i]:.1f}→{plot_val[i+1]:.1f}",
                        xy=(plot_ts[i + 1], plot_val[i + 1]),
                        fontsize=7, color="red",
                        textcoords="offset points", xytext=(0, 10))

    # 橙色/紫色虚线：异常窗口起止时间
    ax.axvline(x=seg["ts1"], color="orange", linewidth=1.5,
               linestyle="--", alpha=0.7)
    ax.axvline(x=seg["ts2"], color="purple", linewidth=1.5,
               linestyle="--", alpha=0.7)

    # 淡绿色：异常时间窗口高亮（从 ts_jump1 到 ts2 之后的第一个数据点）
    window_start = seg["ts1"]
    # 找到 ts2 之后的第一个数据点作为窗口结束
    window_end = seg["ts2"]
    for r in seg_records:
        if r[0] > seg["ts2"]:
            window_end = r[0]
            break
    duration_sec = (window_end - window_start).total_seconds()
    duration_min = int(duration_sec) // 60
    duration_rem = int(duration_sec) % 60
    duration_str = f"{duration_min}分{duration_rem}秒"
    ax.axvspan(window_start, window_end, alpha=0.18, color="lightgreen", zorder=0)

    # 持续时间标注（窗口中间、图内顶部，避免与标题重叠）
    mid_ts = window_start + (window_end - window_start) / 2
    trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
    ax.text(mid_ts, 0.97, f"窗口持续: {duration_str}",
            transform=trans,
            fontsize=9, color="darkgreen", fontweight="bold",
            ha="center", va="top",
            bbox=dict(boxstyle="round,pad=0.3", fc="lightgreen", alpha=0.7),
            zorder=3)

    # 绿色：±90° 参考线
    ax.axhline(y=90,  color="green", linewidth=0.8, linestyle="--", alpha=0.4)
    ax.axhline(y=-90, color="green", linewidth=0.8, linestyle="--", alpha=0.4)
    ax.axhline(y=0,   color="gray",  linewidth=0.5, alpha=0.3)

    title = (f"{seg['dev_name']} / {seg['type_name']} / {seg['phase_name']}"
             f" — 风向异常段 {idx+1}/{len(segments)}\n"
             f"跳变1: {seg['ts1_str']} ({seg['diff1']:.1f}°)  "
             f"跳变2: {seg['ts2_str']} ({seg['diff2']:.1f}°)  "
             f"间隔: {seg['gap_sec']}s  "
             f"窗口: {duration_str} ({int(duration_sec)}s)")
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("时间")
    ax.set_ylabel("风向角度 (°)")
    ax.legend(["风向角度", "红色：跳变>90°", "橙色虚线：跳变1时间",
               "紫色虚线：跳变2时间", "绿色：±90°参考线",
               "淡绿色：异常时间窗口"],
              loc="upper right", fontsize=7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.grid(True, which="major", alpha=0.4)

    plt.tight_layout()
    out_path = os.path.join(OUTPUT_DIR, f"anomaly_seg_{idx+1:03d}.png")
    plt.savefig(out_path, dpi=150)
    plt.close(fig)

    # ---- 输出该段数据到 CSV ----
    csv_path = os.path.join(OUTPUT_DIR, f"anomaly_seg_{idx+1:03d}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as cf:
        w = csv.writer(cf)
        # 段信息头
        w.writerow(["# segment", idx+1, "of", len(segments)])
        w.writerow(["# dev_id", seg["dev_id"]])
        w.writerow(["# dev_name", seg["dev_name"]])
        w.writerow(["# type_name", seg["type_name"]])
        w.writerow(["# phase_name", seg["phase_name"]])
        w.writerow(["# ts_jump1", seg["ts1_str"]])
        w.writerow(["# ts_jump2", seg["ts2_str"]])
        w.writerow(["# gap_sec", seg["gap_sec"]])
        w.writerow(["# diff1_deg", seg["diff1"]])
        w.writerow(["# diff2_deg", seg["diff2"]])
        w.writerow(["# window_duration_sec", int(duration_sec)])
        w.writerow(["# window_duration_str", duration_str])
        # 数据列
        w.writerow(["ts", "angle", "is_jump", "in_window"])
        for i, (ts, angle, raw) in enumerate(seg_records):
            # is_jump 标记在“跳变后”的点，与检测器 ts_jump 约定一致
            is_jump = 0
            if i > 0:
                d = shortest_angle_diff(seg_records[i - 1][1], angle)
                if d > ANGLE_THRESHOLD:
                    is_jump = 1
            in_window = 1 if seg["ts1"] <= ts <= seg["ts2"] else 0
            w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), raw, is_jump, in_window])

    print(f"  段 {idx+1}/{len(segments)}: {out_path} + {csv_path} ({len(seg_records)} 点, {seg['dev_name']})")

print(f"\n完成! {len(segments)} 个图表+CSV保存到 {OUTPUT_DIR}")
