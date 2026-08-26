import csv
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

INPUT = r"e:\data\m.csv"
OUTPUT = r"e:\data\winddirection_plot_m.png"

ts_list = []
val_list = []

with open(INPUT, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    for row in reader:
        if len(row) < 7:
            continue
        try:
            ts = datetime.strptime(row[6], "%Y-%m-%d %H:%M:%S")
            val = float(row[4])
            ts_list.append(ts)
            val_list.append(val)
        except (ValueError, IndexError):
            continue

print(f"读取 {len(ts_list)} 个数据点")

# 按时间排序
combined = sorted(zip(ts_list, val_list), key=lambda x: x[0])
ts_list = [c[0] for c in combined]
val_list = [c[1] for c in combined]

ANGLE_THRESHOLD = 90.0

def shortest_angle_diff(a, b):
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d

# 分段：连续两点间跳变>90°的用红色，否则用蓝色
segments = []  # (start_idx, end_idx, is_jump)
if len(ts_list) >= 2:
    seg_start = 0
    for i in range(len(ts_list) - 1):
        diff = shortest_angle_diff(val_list[i], val_list[i + 1])
        is_jump = diff > ANGLE_THRESHOLD
        if i == 0:
            seg_is_jump = is_jump
        if is_jump != seg_is_jump:
            segments.append((seg_start, i, seg_is_jump))
            seg_start = i
            seg_is_jump = is_jump
    segments.append((seg_start, len(ts_list) - 1, seg_is_jump))

fig, ax = plt.subplots(figsize=(24, 8))
# 线条全部用蓝色
ax.plot(ts_list, val_list, linewidth=1.0, color="steelblue", marker="o", markersize=3, linestyle="-")
# 跳变>90°的线段用红色
for i in range(len(ts_list) - 1):
    diff = shortest_angle_diff(val_list[i], val_list[i + 1])
    if diff > ANGLE_THRESHOLD:
        ax.plot([ts_list[i], ts_list[i + 1]], [val_list[i], val_list[i + 1]],
                linewidth=1.5, color="red", marker="o", markersize=5, linestyle="-")
        ax.annotate(f"{val_list[i]:.1f}→{val_list[i+1]:.1f}",
                    xy=(ts_list[i + 1], val_list[i + 1]), fontsize=7, color="red",
                    textcoords="offset points", xytext=(0, 10))
ax.set_xlabel("时间")
ax.set_ylabel("风向角度 (°)")
ax.set_title("E34 / GWH191-6700 / 劲风风电场二期 — 风向角度曲线 (2026-08-17 15:51~15:58)")
ax.axhline(y=90, color="green", linewidth=0.8, linestyle="--", alpha=0.5, label="±90°")
ax.axhline(y=-90, color="green", linewidth=0.8, linestyle="--", alpha=0.5)
ax.axhline(y=0, color="gray", linewidth=0.5, alpha=0.3)
ax.legend(["风向角度", "红色标记：跳变>90°", "绿色：±90° 参考线"])
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
ax.xaxis.set_major_locator(mdates.SecondLocator(interval=30))
ax.xaxis.set_minor_locator(mdates.SecondLocator(interval=10))
ax.grid(True, which="major", alpha=0.4)
ax.grid(True, which="minor", alpha=0.15)
plt.tight_layout()
plt.savefig(OUTPUT, dpi=150)
print(f"图表已保存: {OUTPUT}")
