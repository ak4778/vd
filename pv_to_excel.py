"""pv_search.sql 查询结果导出为 Excel，格式对齐支路异常20261019.xlsx。

用法: python pv_to_excel.py [日期 YYYY-MM-DD]   # 默认今天
      python pv_to_excel.py 2026-10-19

输出: 支路异常{YYYYMMDD}.xlsx（与支路异常20261019.xlsx 完全同结构同列宽）
"""
import json
import sys
from datetime import date, datetime, time, timedelta, timezone

import psycopg2
from openpyxl import Workbook

# ---------------- 配置 ----------------
COLUMNS = [                                 # 输出列（顺序、表头与支路异常20261019.xlsx 完全一致）
    ('region_company_name',             '公司'),
    ('station_name',                    '场站'),
    ('site_id',                         '场站ID'),
    ('device_id',                       '逆变器ID'),
    ('inverter_name',                   '逆变器名称'),
    ('ts',                              '时间'),
    ('time_window',                     '时间窗口'),
    ('total_branch_cnt',                '支路总数'),
    ('neg_branch_cnt',                  '电流小于0支路数'),
    ('zero_branch_cnt',                 '电流等于0支路数'),
    ('fluct_branch_cnt',                '最大值-最小值>平均值的2倍支路数'),
    ('total_neg_seconds',               '电流小于0检出次数数目'),
    ('total_zero_seconds',              '电流等于0检出次数数目'),
]

COL_WIDTHS = [                            # 列宽（与支路异常20261019.xlsx 一致）
    15.0, 19.875, 22.25, 18.25, 18.5, 21.5, 12.625, 8.875,
    22.375, 13.0, 32.875, 16.125, 13.0,
]

SHEET_NAME = '支路电流异常时次统计'
TZ = timezone(timedelta(hours=8))
WINDOWS = [                               # 固定 3 个采集窗口（Asia/Shanghai）
    (time(10, 0), time(11, 0)),
    (time(14, 0), time(15, 0)),
    (time(15, 0), time(16, 0)),
]

# ---------------- SQL 模板 ----------------
SQL_TEMPLATE = r"""
WITH t1 AS (
    SELECT station_id, station_name, station_type, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, station_type, region_company_name
),
si AS (
    SELECT
        upper(inverter_id)   AS dev_key,
        station_id,
        inverter_name
    FROM pv_station_invert
),
v AS (
    SELECT b.device_id, b.point_code, b.ts, b.point_value
    FROM pv_string_branch b
    WHERE b.point_code ~ '^STRING_BRANCH[0-9]{{1,4}}_A$'
      AND b.ts >= TIMESTAMPTZ '{day} 10:00:00+08'
      AND b.ts <  TIMESTAMPTZ '{day} 16:00:00+08'
      AND (
        (b.ts >= TIMESTAMPTZ '{day} 10:00:00+08' AND b.ts <  TIMESTAMPTZ '{day} 11:00:00+08')
        OR (b.ts >= TIMESTAMPTZ '{day} 14:00:00+08' AND b.ts <  TIMESTAMPTZ '{day} 15:00:00+08')
        OR (b.ts >= TIMESTAMPTZ '{day} 15:00:00+08' AND b.ts <  TIMESTAMPTZ '{day} 16:00:00+08')
      )
),
bm AS (
    SELECT
        v.device_id,
        v.point_code,
        date_trunc('hour', v.ts AT TIME ZONE 'Asia/Shanghai')       AS ts,
        AVG(v.point_value) FILTER (WHERE v.point_value <= 15)       AS point_value,
        COUNT(*) FILTER (WHERE v.point_value < 0)                  AS neg_cnt,
        COUNT(*) FILTER (WHERE v.point_value = 0)                  AS zero_cnt,
        COUNT(*) FILTER (WHERE v.point_value <= 15)                AS valid_cnt,
        MAX(v.point_value) FILTER (WHERE v.point_value <= 15)       AS sec_max,
        MIN(v.point_value) FILTER (WHERE v.point_value <= 15)       AS sec_min
    FROM v
    GROUP BY v.device_id, v.point_code,
             date_trunc('hour', v.ts AT TIME ZONE 'Asia/Shanghai')
),
base AS (
    SELECT
        t1.region_company_name                                     AS region_company_name,
        t1.station_name                                            AS station_name,
        si.station_id                                              AS site_id,
        bm.device_id                                               AS device_id,
        si.inverter_name                                           AS inverter_name,
        bm.ts                                                      AS ts,
        CASE extract(hour FROM bm.ts)
             WHEN 10 THEN '10:00-11:00'
             WHEN 14 THEN '14:00-15:00'
             WHEN 15 THEN '15:00-16:00' END                        AS time_window,
        COUNT(DISTINCT bm.point_code)                              AS total_branch_cnt,
        COUNT(*) FILTER (WHERE bm.neg_cnt  > 0)                   AS neg_branch_cnt,
        COUNT(*) FILTER (WHERE bm.zero_cnt > 0)                   AS zero_branch_cnt,
        COUNT(*) FILTER (WHERE bm.sec_max IS NOT NULL
                           AND bm.sec_min IS NOT NULL
                           AND bm.point_value IS NOT NULL
                           AND (bm.sec_max - bm.sec_min) > 2 * bm.point_value)
                                                                   AS fluct_branch_cnt,
        SUM(bm.neg_cnt)                                            AS total_neg_seconds,
        SUM(bm.zero_cnt)                                           AS total_zero_seconds
    FROM bm
    JOIN si ON si.dev_key = bm.device_id
    JOIN t1 ON t1.station_id = si.station_id
    GROUP BY t1.region_company_name, t1.station_name, si.station_id,
             bm.device_id, si.inverter_name, bm.ts
)
SELECT {cols}
FROM base
WHERE neg_branch_cnt > 0 OR zero_branch_cnt > 0 OR fluct_branch_cnt > 0
ORDER BY region_company_name, station_name, device_id, ts;
"""


def main():
    # 日期参数
    if len(sys.argv) > 1:
        day = sys.argv[1]
        date_obj = datetime.strptime(day, '%Y-%m-%d').date()
    else:
        date_obj = date.today()
    day_s = date_obj.strftime('%Y-%m-%d')
    day_compact = date_obj.strftime('%Y%m%d')
    out_path = f'支路异常{day_compact}.xlsx'

    # 生成 SQL
    sql = SQL_TEMPLATE.format(day=day_s, cols=', '.join(c for c, _ in COLUMNS))

    # 查询
    with open('data_config.json', encoding='utf-8') as f:
        conn = psycopg2.connect(json.load(f)['pgConnStr'])
    try:
        cur = conn.cursor()
        print(f'查询 {day_s} ...')
        # 服务端默认 statement_timeout 会让大分区（上亿行）的聚合被中途取消
        cur.execute('SET statement_timeout = 0')
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    print(f'命中 {len(rows)} 行')

    # 写 Excel
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME

    # 表头
    ws.append([h for _, h in COLUMNS])

    # 数据
    for row in rows:
        out = list(row)
        # ts -> 'YYYY-MM-DD HH:MM:SS' 字符串（xlsx 里存 datetime 对象也可以，原文件就是 datetime）
        if out[5] is not None:
            # ts 是 TIMESTAMPTZ，pg 驱动返回 datetime，直接用
            out[5] = out[5].replace(tzinfo=None) if hasattr(out[5], 'tzinfo') and out[5].tzinfo else out[5]
        ws.append(out)

    # 列宽
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    wb.save(out_path)
    print(f'已输出: {out_path}')


if __name__ == '__main__':
    main()
