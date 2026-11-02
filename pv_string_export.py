"""导出 pv_string_branch 支路电流明细（按 device_id + ts 时间范围）。

用法示例：
  python pv_string_export.py                         # 默认：3F5973E5... 2026-10-28 10:00~11:00
  python pv_string_export.py --device <id> --start "2026-10-28 14:00:00" --end "2026-10-28 15:00:00"
  python pv_string_export.py --format csv            # 输出 CSV（utf-8-sig）

时间语义：Asia/Shanghai（UTC+8），区间左闭右开 [start, end)，与 pv_to_excel.py 口径一致。
输出：xlsx（默认，含维度信息，数据行高 23）或 csv。
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'data_config.json')
TZ = timezone(timedelta(hours=8))

DEFAULT_DEVICE = '3F5973E59E494733A1B2FB4DF4493449'
DEFAULT_START = '2026-10-28 10:00:00'
DEFAULT_END = '2026-10-28 11:00:00'

COLUMNS = [
    ('device_id', '逆变器ID'),
    ('inverter_name', '逆变器名称'),
    ('station_name', '场站'),
    ('region_company_name', '公司'),
    ('point_code', '测点编码'),
    ('ts', '时间'),
    ('point_value', '电流(A)'),
]


def connect():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        dsn = json.load(f)['pgConnStr']
    conn = psycopg2.connect(dsn)
    conn.set_client_encoding('UTF8')
    return conn


def parse_dt(text):
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    raise SystemExit(f'时间格式无法识别：{text}')


SQL = r"""
SELECT b.device_id, i.inverter_name, i.station_name, i.region_company_name,
       b.point_code, b.ts, b.point_value
FROM pv_string_branch b
LEFT JOIN pv_station_invert i ON upper(i.inverter_id) = upper(b.device_id)
WHERE upper(b.device_id) = %s
  AND b.ts >= %s
  AND b.ts <  %s
ORDER BY b.ts, b.point_code
"""


def fetch(device, start, end):
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute('SET statement_timeout = 0')
        cur.execute(SQL, (device.upper(), start, end))
        rows = cur.fetchall()
        cur.close()
        conn.rollback()
    finally:
        conn.close()
    return rows


def fmt_ts(val):
    return '' if val is None else val.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')


def write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow([h for _, h in COLUMNS])
        for r in rows:
            r = list(r)
            r[5] = fmt_ts(r[5])
            w.writerow(r)


def write_xlsx(path, rows):
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = '支路电流明细'
    ws.append([h for _, h in COLUMNS])
    for r in rows:
        r = list(r)
        r[5] = fmt_ts(r[5])
        ws.append(r)
        ws.row_dimensions[ws.max_row].height = 23

    for i, width in enumerate([36, 18, 30, 16, 24, 21, 12], 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = 'A2'
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description='导出 pv_string_branch 支路电流明细')
    ap.add_argument('--device', default=DEFAULT_DEVICE, help='逆变器 device_id')
    ap.add_argument('--start', default=DEFAULT_START, help='起始时间（含）')
    ap.add_argument('--end', default=DEFAULT_END, help='结束时间（不含）')
    ap.add_argument('--format', default='xlsx', choices=['xlsx', 'csv'], help='输出格式')
    ap.add_argument('-o', '--out', help='输出文件路径')
    args = ap.parse_args()

    start, end = parse_dt(args.start), parse_dt(args.end)
    if end <= start:
        raise SystemExit('结束时间必须晚于开始时间')

    print(f'查询 device_id={args.device.upper()}  '
          f'{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}（左闭右开，东八区）')
    rows = fetch(args.device, start, end)
    print(f'命中 {len(rows)} 行，测点 {len({r[4] for r in rows})} 个')

    label = f"{args.device.upper()[:8]}_{start:%Y%m%d_%H%M}_{end:%H%M}"
    out_path = os.path.abspath(args.out or os.path.join(
        BASE_DIR, f'pv_string_{label}.{args.format}'))
    if args.format == 'csv':
        write_csv(out_path, rows)
    else:
        write_xlsx(out_path, rows)
    print(f'已输出: {out_path}')


if __name__ == '__main__':
    main()
