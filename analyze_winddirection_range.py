import csv
import os
from collections import defaultdict

INPUT = r"e:\data\fjmjsj_winddirection.csv"

# 每个设备的 pointValue 范围
dev_min = defaultdict(lambda: float('inf'))
dev_max = defaultdict(lambda: float('-inf'))
dev_name = {}
dev_count = defaultdict(int)

# 全局统计
global_min = float('inf')
global_max = float('-inf')
total_rows = 0
negative_count = 0
gt360_count = 0
lt_neg180_count = 0
parse_error = 0

print(f"分析文件: {INPUT}")
print(f"文件大小: {os.path.getsize(INPUT) / (1024**3):.2f} GB")
print()

with open(INPUT, "r", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    header = next(reader)
    for row in reader:
        if len(row) < 7:
            continue
        try:
            val = float(row[4])
        except (ValueError, TypeError):
            parse_error += 1
            continue
        dev_id = row[0]
        dev_name[dev_id] = (row[1], row[2], row[3])
        dev_count[dev_id] += 1

        if val < dev_min[dev_id]:
            dev_min[dev_id] = val
        if val > dev_max[dev_id]:
            dev_max[dev_id] = val

        if val < global_min:
            global_min = val
        if val > global_max:
            global_max = val

        if val < 0:
            negative_count += 1
        if val > 360:
            gt360_count += 1
        if val < -180:
            lt_neg180_count += 1

        total_rows += 1
        if total_rows % 10_000_000 == 0:
            print(f"  已处理 {total_rows:,} 行, 全局范围=[{global_min:.2f}, {global_max:.2f}], 设备数={len(dev_min)}", flush=True)

print()
print("=" * 80)
print("全局统计")
print("=" * 80)
print(f"总行数: {total_rows:,}")
print(f"解析失败: {parse_error:,}")
print(f"设备数: {len(dev_min):,}")
print(f"全局最小值: {global_min:.4f}")
print(f"全局最大值: {global_max:.4f}")
print(f"负数记录: {negative_count:,} ({negative_count/total_rows*100:.2f}%)")
print(f">360 记录: {gt360_count:,} ({gt360_count/total_rows*100:.4f}%)")
print(f"<-180 记录: {lt_neg180_count:,} ({lt_neg180_count/total_rows*100:.4f}%)")

print()
print("=" * 80)
print("设备范围分类")
print("=" * 80)

# 分类统计
all_pos_0_360 = 0   # 全部在 0-360
has_neg_180_180 = 0  # 有负数，在 -180~180
has_neg_other = 0    # 有负数，超出 -180~180
out_of_range = 0     # 超出任何范围

for dev_id in dev_min:
    vmin = dev_min[dev_id]
    vmax = dev_max[dev_id]
    if vmin >= 0 and vmax <= 360:
        all_pos_0_360 += 1
    elif vmin >= -180 and vmax <= 180:
        has_neg_180_180 += 1
    elif vmin >= -360 and vmax <= 360:
        has_neg_other += 1
    else:
        out_of_range += 1

print(f"全部在 0~360 之间:     {all_pos_0_360:,} 台 ({all_pos_0_360/len(dev_min)*100:.1f}%)")
print(f"有负数，在 -180~180:   {has_neg_180_180:,} 台 ({has_neg_180_180/len(dev_min)*100:.1f}%)")
print(f"有负数，超 -180~180:   {has_neg_other:,} 台 ({has_neg_other/len(dev_min)*100:.1f}%)")
print(f"超出任何范围:          {out_of_range:,} 台 ({out_of_range/len(dev_min)*100:.1f}%)")

print()
print("=" * 80)
print("异常设备（超出 -180~360 范围）")
print("=" * 80)
if out_of_range > 0:
    print(f"{'设备ID':<40} {'名称':<8} {'min':>10} {'max':>10} {'count':>10}")
    print("-" * 80)
    for dev_id in sorted(dev_min.keys(), key=lambda d: dev_min[d]):
        vmin = dev_min[dev_id]
        vmax = dev_max[dev_id]
        if vmin < -180 or vmax > 360:
            nm, typ, ph = dev_name[dev_id]
            print(f"{dev_id:<40} {nm:<8} {vmin:>10.2f} {vmax:>10.2f} {dev_count[dev_id]:>10,}")
else:
    print("  无")

print()
print("=" * 80)
print("前 20 个设备的 pointValue 范围（按最小值排序）")
print("=" * 80)
print(f"{'设备ID':<40} {'名称':<8} {'min':>10} {'max':>10} {'count':>10}")
print("-" * 80)
for dev_id in sorted(dev_min.keys(), key=lambda d: dev_min[d])[:20]:
    vmin = dev_min[dev_id]
    vmax = dev_max[dev_id]
    nm, typ, ph = dev_name[dev_id]
    print(f"{dev_id:<40} {nm:<8} {vmin:>10.2f} {vmax:>10.2f} {dev_count[dev_id]:>10,}")

print()
print("=" * 80)
print("前 20 个设备的 pointValue 范围（按最大值排序）")
print("=" * 80)
print(f"{'设备ID':<40} {'名称':<8} {'min':>10} {'max':>10} {'count':>10}")
print("-" * 80)
for dev_id in sorted(dev_max.keys(), key=lambda d: dev_max[d], reverse=True)[:20]:
    vmin = dev_min[dev_id]
    vmax = dev_max[dev_id]
    nm, typ, ph = dev_name[dev_id]
    print(f"{dev_id:<40} {nm:<8} {vmin:>10.2f} {vmax:>10.2f} {dev_count[dev_id]:>10,}")
