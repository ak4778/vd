"""导出 wind_alarm 告警流水表（按 ts 时间范围）。

用法示例：
  python wind_alarm_export.py                                # 导出今天（Asia/Shanghai）
  python wind_alarm_export.py --date 2026-10-24              # 导出指定 1 天
  python wind_alarm_export.py --start "2026-10-01 00:00:00" --end "2026-10-03 23:59:59"
  python wind_alarm_export.py --date 2026-10-24 --device gdfdc001 --event ALARM
  python wind_alarm_export.py --date 2026-10-24 -o out.csv   # 指定输出文件

时间语义：
  - 所有时间均按 Asia/Shanghai（UTC+8）解释；数据库中 ts 为 timestamptz。
  - --date 为自然日，区间为 [当天 00:00:00, 次日 00:00:00)，左闭右开。
  - --start/--end 为闭区间 [start, end]；--end 只给日期时自动取当天 23:59:59；
    --end 省略时截止到当前时刻。
  - ts 为 NULL 的异常行不会被时间过滤命中（不导出）。

输出：
  - CSV，utf-8-sig 编码（Excel 直接双击不乱码），ts 按东八区格式化。
  - 默认文件名 wind_alarm_<区间>.csv，落在脚本所在目录。
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

TABLE = 'wind_alarm'
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai，无夏令时

COLUMNS = [
    'id', 'event', 'ts', 'region_company_name', 'station_name',
    'device_id', 'device_name', 'point_code', 'point_name',
    'rule_code', 'alarm_type',
    'temperature', 'temp_rate', 'range_1h', 'range_20s', 'alarm_value',
    'detail',
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'data_config.json')

# 兼容日期、分钟、秒三种写法
DT_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')


def connect():
    """读取 data_config.json 的 pgConnStr 连接 PostgreSQL。"""
    with open(CONFIG_PATH, encoding='utf-8') as f:
        dsn = json.load(f)['pgConnStr']
    conn = psycopg2.connect(dsn)
    conn.set_client_encoding('UTF8')
    return conn


def parse_dt(text, is_end=False):
    """把命令行时间解析为东八区 aware datetime；日期型 end 取当天最后一秒。"""
    for fmt in DT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if is_end and fmt == '%Y-%m-%d':
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.replace(tzinfo=TZ)
    raise SystemExit(f'时间格式无法识别：{text}（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS）')


def build_range(args):
    """返回 (start, end, 左闭是否右开, 输出文件名标签)。"""
    if args.date and args.start:
        raise SystemExit('--date 与 --start 不能同时使用')

    if args.date:
        day = parse_dt(args.date)
        start = day.replace(hour=0, minute=0, second=0)
        end = start + timedelta(days=1)  # 右开
        return start, end, True, start.strftime('%Y%m%d')

    if args.start:
        start = parse_dt(args.start)
        if args.end:
            end = parse_dt(args.end, is_end=True)
            inclusive = True
            label = f'{start:%Y%m%d_%H%M%S}_{end:%Y%m%d_%H%M%S}'
        else:
            end = datetime.now(TZ)
            inclusive = True
            label = f'{start:%Y%m%d_%H%M%S}_now'
        if end < start:
            raise SystemExit(f'结束时间早于开始时间：{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S}')
        return start, end, not inclusive, label

    # 未指定时间：默认导出今天
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end, True, start.strftime('%Y%m%d')


def fmt_ts(val):
    return '' if val is None else val.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')


def fmt_cell(val):
    if val is None:
        return ''
    if isinstance(val, datetime):
        return fmt_ts(val)
    return val


def build_sql(args, right_open):
    """拼接查询语句：时间区间 + 可选过滤条件，全部参数化。"""
    op = '<' if right_open else '<='
    where = [f'ts >= %s AND ts {op} %s']
    params = []
    if args.device:
        where.append('device_id = %s')
        params.append(args.device)
    if args.event:
        where.append('event = %s')
        params.append(args.event.upper())
    if args.rule:
        where.append('rule_code = %s')
        params.append(args.rule)
    sql = (f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE "
           + ' AND '.join(where) + ' ORDER BY ts ASC, id ASC')
    return sql, params


def export(args):
    start, end, right_open, label = build_range(args)
    sql, extra_params = build_sql(args, right_open)

    print(f'导出区间：{start:%Y-%m-%d %H:%M:%S} ~ {end:%Y-%m-%d %H:%M:%S} '
          f'（{"左闭右开" if right_open else "闭区间"}，Asia/Shanghai）')

    conn = connect()
    try:
        with conn.cursor() as probe:
            probe.execute('SELECT to_regclass(%s)', ('public.' + TABLE,))
            if probe.fetchone()[0] is None:
                raise SystemExit(f'表 {TABLE} 不存在：请先启动监控端建表')

        # 服务端游标流式读取，避免大区间一次性载入内存
        cur = conn.cursor(name='wind_alarm_export')
        cur.itersize = 5000
        cur.execute(sql, [start, end] + extra_params)

        out_path = args.out or os.path.join(
            BASE_DIR, f'wind_alarm_{label}.csv')
        out_path = os.path.abspath(out_path)
        parent = os.path.dirname(out_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        total = alarm = recover = 0
        first_ts = last_ts = None
        with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(COLUMNS)
            for row in cur:
                writer.writerow([fmt_cell(v) for v in row])
                total += 1
                if row[1] == 'ALARM':
                    alarm += 1
                elif row[1] == 'RECOVER':
                    recover += 1
                if row[2] is not None:
                    first_ts = first_ts or row[2]
                    last_ts = row[2]
        cur.close()
        conn.rollback()  # 只读查询，直接释放事务
    finally:
        conn.close()

    print(f'已导出 {total} 行：ALARM {alarm} 行，RECOVER {recover} 行')
    if first_ts:
        print(f'数据时刻范围：{fmt_ts(first_ts)} ~ {fmt_ts(last_ts)}')
    print(f'输出文件：{out_path}')
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description='导出 wind_alarm 告警表（按 ts 时间范围，东八区）')
    ap.add_argument('--date', help='导出某一天，如 2026-10-24')
    ap.add_argument('--start', help='起始时间，如 "2026-10-01 00:00:00"')
    ap.add_argument('--end', help='结束时间（含），省略则截止到当前时刻')
    ap.add_argument('--device', help='按设备 device_id 过滤')
    ap.add_argument('--event', help='按事件类型过滤：ALARM / RECOVER')
    ap.add_argument('--rule', help='按规则 rule_code 过滤，如 C01_HIGH')
    ap.add_argument('-o', '--out', help='输出 CSV 路径')
    args = ap.parse_args()
    export(args)


if __name__ == '__main__':
    sys.exit(main())
