# -*- coding: utf-8 -*-
"""按日期生成光伏支路 5 分钟汇总，或按查询A 导出支路明细 CSV（不再更新 string_cv）。

链路（幂等，可重复执行）：
  1) pv_string_branch_5min 当日分区兜底（pv_string_branch_5min_ensure_partition）
  2) 从 pv_string_branch 聚合当日 10:00-16:00 写入 pv_string_branch_5min（ON CONFLICT 覆盖）
  3) ANALYZE pv_string_branch_5min

汇总口径与 pv_string_branch.sql 保持一致：
  - 剔除 |point_value| <= 1（含 0、±1）；zero_cnt 仅计数，不参与均值
  - 支路均值 |i_avg| > 1（不设上限）、单桶有效采样 >= 3
  - 剔除 pv_inverter_branch_abnormal 已标记的逆变器+支路

用法：
  python pv_string_cv_refresh.py 2026-11-12                 # 生成当日 5 分钟汇总
  python pv_string_cv_refresh.py 2026-11-12 --export-csv     # 按 SQL §3 查询A 导出支路明细 CSV，不改库
  python pv_string_cv_refresh.py 2026-11-12 --export-csv --start 10 --end 11   # 只导出 10:00-11:00
"""
import argparse
import codecs
import json
import os
import time
from datetime import datetime

import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'data_config.json')
PG_CONN_ENV = 'PG_CONN_STR'

# 采集窗口（与 pv_string_branch.sql 的 4 个窗口一致，左闭右开）
WINDOWS = [('10:00:00', '11:00:00'),
           ('12:00:00', '13:00:00'),
           ('14:00:00', '15:00:00'),
           ('15:00:00', '16:00:00')]
DAY_START, DAY_END = '10:00:00', '16:00:00'

BRANCH_RE = r'^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'


def load_pg_conn():
    conn_str = os.environ.get(PG_CONN_ENV, '').strip()
    if conn_str:
        return conn_str
    with open(CONFIG_PATH, encoding='utf-8') as f:
        conn_str = (json.load(f).get('pgConnStr') or '').strip()
    if not conn_str:
        raise RuntimeError(f'{CONFIG_PATH} 缺少 pgConnStr，或设置环境变量 {PG_CONN_ENV}')
    return conn_str


def ts(day, hms):
    """生成显式东八区时间字面量，例如 2026-11-12 10:00:00+08。"""
    return f"TIMESTAMPTZ '{day} {hms}+08'"


def window_clause(day, col):
    """4 个采集窗口的 OR 条件。"""
    parts = [f"({col} >= {ts(day, s)} AND {col} < {ts(day, e)})" for s, e in WINDOWS]
    return '\n         OR '.join(parts)


def parse_hms(text):
    """把 10 / 10:00 / 10:00:00 统一成 HH:MM:SS。"""
    parts = str(text).strip().split(':')
    if not 1 <= len(parts) <= 3:
        raise ValueError(f'时间格式错误: {text}，示例 10 或 10:30 或 10:30:00')
    try:
        h, m, s = [int(x) for x in (parts + ['0', '0'][len(parts) - 1:])[:3]]
    except ValueError:
        raise ValueError(f'时间格式错误: {text}，应为 小时[:分钟[:秒]]') from None
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError(f'时间超出范围: {text}')
    return f'{h:02d}:{m:02d}:{s:02d}'


def range_clause(day, col, start, end):
    """自定义时间区间（左闭右开）。"""
    return f'{col} >= {ts(day, start)} AND {col} < {ts(day, end)}'


def ensure_partition(cur, day):
    cur.execute('SELECT pv_string_branch_5min_ensure_partition(%s::date)', (day,))
    return cur.fetchone()[0]


def refresh_5min(cur, day):
    """从秒级原始表聚合当日 10:00-16:00，UPSERT 到 5 分钟汇总表。"""
    sql = f"""
    INSERT INTO pv_string_branch_5min AS t
    SELECT device_id, point_code, bucket, sample_cnt, zero_cnt, i_avg, sec_max, sec_min
    FROM (
        SELECT device_id,
               point_code,
               to_timestamp(floor(extract(epoch FROM ts) / 300) * 300) AS bucket,
               count(*) FILTER (WHERE abs(point_value) > 1) AS sample_cnt,
               count(*) FILTER (WHERE point_value  = 0)     AS zero_cnt,
               avg(point_value) FILTER (WHERE abs(point_value) > 1) AS i_avg,
               max(point_value) FILTER (WHERE abs(point_value) > 1) AS sec_max,
               min(point_value) FILTER (WHERE abs(point_value) > 1) AS sec_min
        FROM pv_string_branch
        WHERE ts >= {ts(day, DAY_START)}
          AND ts <  {ts(day, DAY_END)}
          AND point_code ~ '{BRANCH_RE}'
        GROUP BY 1, 2, 3
    ) s
    ON CONFLICT (device_id, point_code, bucket) DO UPDATE SET
        sample_cnt = EXCLUDED.sample_cnt,
        zero_cnt   = EXCLUDED.zero_cnt,
        i_avg      = EXCLUDED.i_avg,
        sec_max    = EXCLUDED.sec_max,
        sec_min    = EXCLUDED.sec_min
    """
    cur.execute(sql)
    return cur.rowcount


def build_query_a_detail_sql(day, start=None, end=None):
    """pv_string_branch.sql §3 查询A：支路 × 5 分钟快照明细（含 20 分钟块指标）。

    start/end 为空时用 4 个采集窗口；否则用 [start, end) 单区间。
    """
    if start and end:
        bucket_filter = range_clause(day, 'bucket', start, end)
    else:
        bucket_filter = f"bucket >= {ts(day, DAY_START)}\n          AND bucket <  {ts(day, DAY_END)}\n          AND ({window_clause(day, 'bucket')})"
    return f"""
    WITH ex AS (
        SELECT DISTINCT
            upper(inverter_id)                                                    AS dev_key,
            'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A' AS point_code
        FROM pv_inverter_branch_abnormal
        WHERE branch_name ~ '支路[0-9]+'
    ),
    br AS (
        SELECT
            device_id,
            point_code,
            bucket      AS snap,
            i_avg       AS i,
            sample_cnt  AS n,
            zero_cnt
        FROM pv_string_branch_5min b
        WHERE {bucket_filter}
          AND i_avg IS NOT NULL
          AND abs(i_avg) > 1
          AND sample_cnt >= 3
          AND NOT EXISTS (
                SELECT 1 FROM ex
                WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
          )
    ),
    ds AS (
        SELECT
            device_id,
            snap,
            avg(i)         AS i_mean,
            stddev_samp(i) AS i_std,
            count(*)       AS branch_cnt,
            min(i)         AS i_min,
            max(i)         AS i_max,
            CASE WHEN count(*) >= 2 AND abs(avg(i)) >= 0.5
                 THEN NULL ELSE 'low_current' END AS snap_flag
        FROM br
        GROUP BY device_id, snap
    ),
    bs AS (
        SELECT
            b.device_id,
            b.point_code,
            b.snap,
            b.i,
            b.zero_cnt,
            d.i_mean,
            d.i_std,
            d.branch_cnt,
            d.i_min,
            d.i_max,
            d.snap_flag,
            to_timestamp(floor(extract(epoch FROM b.snap) / 1200) * 1200) AS block_20m,
            extract(hour FROM b.snap AT TIME ZONE 'Asia/Shanghai')          AS hh,
            CASE WHEN d.snap_flag IS NULL
                 THEN (b.i - d.i_mean) / d.i_mean * 100 END AS dev_pct,
            CASE WHEN d.snap_flag IS NULL
                 THEN d.i_std / d.i_mean * 100 END          AS cv_pct,
            CASE WHEN d.snap_flag IS NULL
                 THEN (d.i_max - d.i_min) / d.i_mean * 100 END AS range_pct
        FROM br b
        JOIN ds d USING (device_id, snap)
    ),
    bok AS (
        SELECT * FROM bs
        WHERE snap_flag IS NULL AND dev_pct IS NOT NULL
    ),
    bb_win AS (
        SELECT
            device_id, point_code, block_20m, snap, i, i_mean, dev_pct,
            first_value(dev_pct) OVER w AS dev_first,
            last_value(dev_pct)  OVER w AS dev_last,
            count(dev_pct)       OVER w AS dev_cnt
        FROM bok
        WINDOW w AS (
            PARTITION BY device_id, point_code, block_20m
            ORDER BY snap
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )
    ),
    bb AS (
        SELECT
            device_id,
            point_code,
            block_20m,
            max(dev_first) AS dev_first,
            max(dev_last)  AS dev_last,
            max(dev_cnt)   AS dev_cnt,
            min(snap)      AS block_start,
            max(snap)      AS block_end,
            extract(hour FROM block_20m AT TIME ZONE 'Asia/Shanghai') AS hh,
            CASE WHEN max(dev_cnt) >= 2 THEN max(dev_last) - max(dev_first) END AS chg_20m,
            CASE WHEN max(dev_cnt) >= 2 THEN (max(dev_last) - max(dev_first)) / 15 END AS rate_pm
        FROM bb_win
        GROUP BY device_id, point_code, block_20m
    ),
    dblk AS (
        SELECT
            device_id,
            point_code,
            block_20m,
            max(i)               AS blk_i_max,
            min(i)               AS blk_i_min,
            count(DISTINCT snap) AS blk_snap_cnt
        FROM bok
        GROUP BY device_id, point_code, block_20m
    ),
    dblk_mean AS (
        SELECT
            d.device_id,
            d.point_code,
            d.block_20m,
            d.blk_i_max,
            d.blk_i_min,
            d.blk_snap_cnt,
            avg(s.i_mean) AS blk_mean_i
        FROM dblk d
        JOIN (SELECT DISTINCT device_id, snap, block_20m, i_mean FROM bok) s
          ON s.device_id = d.device_id AND s.block_20m = d.block_20m
        GROUP BY 1, 2, 3, 4, 5, 6
    ),
    dblk_dev AS (
        SELECT
            *,
            CASE WHEN blk_snap_cnt >= 2 AND abs(blk_mean_i) >= 0.5
                 THEN (blk_i_max - blk_i_min) / blk_mean_i * 100 END AS range20_pct
        FROM dblk_mean
    ),
    st AS (
        SELECT station_id, station_name, region_company_name
        FROM pv_station_info
        WHERE constituent_name = '国电电力'
        GROUP BY station_id, station_name, region_company_name
    ),
    si AS (
        SELECT DISTINCT ON (upper(inverter_id))
               upper(inverter_id) AS dev_key,
               station_id,
               inverter_name
        FROM pv_station_invert
        WHERE inverter_id IS NOT NULL
          AND inverter_id <> ''
        ORDER BY upper(inverter_id), station_id, inverter_name
    )
    SELECT
        bs.snap AT TIME ZONE 'Asia/Shanghai'                            AS "时间戳",
        st.region_company_name                                          AS "公司",
        st.station_name                                                 AS "场站",
        si.inverter_name                                                AS "组串编号",
        bs.device_id                                                    AS "逆变器ID",
        substring(bs.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int    AS "支路编号",
        bs.point_code                                                   AS "支路点位",
        round(bs.i::numeric, 3)                                         AS "支路电流",
        round(bs.i_mean::numeric, 3)                                    AS "组串平均电流",
        round(bs.dev_pct::numeric, 2)                                   AS "单支路当前偏差率(%)",
        round(bs.cv_pct::numeric, 2)                                    AS "组串整体偏差率(%)",
        round(bs.range_pct::numeric, 2)                                 AS "组串电流相对极差(%)",
        bb.block_20m AT TIME ZONE 'Asia/Shanghai'                       AS "20分钟块起始",
        round(bb.chg_20m::numeric, 2)                                   AS "20分钟偏差变化(pp)",
        round(bb.rate_pm::numeric, 3)                                   AS "偏差率变化速率(%/m)",
        round(dd.range20_pct::numeric, 2)                               AS "20分钟相对极差(%)"
    FROM bs
    LEFT JOIN bb
           ON bb.device_id = bs.device_id
          AND bb.point_code = bs.point_code
          AND bb.block_20m = bs.block_20m
    LEFT JOIN dblk_dev dd
           ON dd.device_id = bs.device_id
          AND dd.point_code = bs.point_code
          AND dd.block_20m = bs.block_20m
    LEFT JOIN si ON si.dev_key = bs.device_id
    LEFT JOIN st ON st.station_id = si.station_id
    ORDER BY
        st.region_company_name,
        st.station_name,
        si.inverter_name,
        bs.device_id,
        bs.snap,
        substring(bs.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int
    """


def export_query_a_csv(cur, day, path, start=None, end=None):
    """按 §3 查询A 导出 CSV（UTF-8 BOM），返回数据行数。"""
    with open(path, 'wb') as f:
        f.write(codecs.BOM_UTF8)
        cur.copy_expert(
            'COPY (' + build_query_a_detail_sql(day, start, end) + ') '
            "TO STDOUT WITH (FORMAT csv, HEADER true)", f)
    with open(path, 'rb') as f:
        return sum(1 for _ in f) - 1


def main():
    ap = argparse.ArgumentParser(description='按日期生成 5 分钟汇总，或按查询A 导出支路明细 CSV')
    ap.add_argument('day', help='日期，例如 2026-11-12')
    ap.add_argument('--export-csv', nargs='?', const='', default=None,
                    help='按 pv_string_branch.sql §3 查询A 导出支路明细 CSV（可指定路径），'
                         '默认 pv_queryA_<日期>.csv；导出后直接退出，不改库')
    ap.add_argument('--start', default=None,
                    help='导出起始时间（左闭），如 10 或 10:30；默认沿用 4 个采集窗口')
    ap.add_argument('--end', default=None,
                    help='导出结束时间（右开），如 11 或 11:30；需与 --start 同时给')
    args = ap.parse_args()

    if args.export_csv is None and (args.start or args.end):
        ap.error('--start/--end 只能与 --export-csv 一起使用')

    start = end = None
    if args.start or args.end:
        if not (args.start and args.end):
            ap.error('--start 和 --end 必须同时指定')
        try:
            start, end = parse_hms(args.start), parse_hms(args.end)
        except ValueError as e:
            ap.error(str(e))
        if start >= end:
            ap.error(f'--start 必须早于 --end（{start} >= {end}）')

    day = datetime.strptime(args.day, '%Y-%m-%d').strftime('%Y-%m-%d')

    conn = psycopg2.connect(load_pg_conn(), connect_timeout=10)
    conn.set_client_encoding('UTF8')
    cur = conn.cursor()
    cur.execute('SET statement_timeout = 0')

    t_all = time.time()
    try:
        if args.export_csv is not None:
            default_name = f'pv_queryA_{day.replace("-", "")}.csv'
            if start:
                default_name = (f'pv_queryA_{day.replace("-", "")}'
                                f'_{start[:5].replace(":", "")}-{end[:5].replace(":", "")}.csv')
            out = os.path.abspath(args.export_csv or os.path.join(BASE_DIR, default_name))
            span = f'{start}~{end}' if start else '4个采集窗口'
            print(f'导出查询A 明细 {day} [{span}] -> {out}', flush=True)
            n = export_query_a_csv(cur, day, out, start, end)
            conn.rollback()
            print(f'已导出 {n} 行，用时 {time.time() - t_all:.1f}s', flush=True)
            return

        t0 = time.time()
        part = ensure_partition(cur, day)
        print(f'[1/3] 分区: {part}', flush=True)
        n = refresh_5min(cur, day)
        conn.commit()
        print(f'[2/3] 5分钟汇总 UPSERT {n} 行，用时 {time.time() - t0:.1f}s', flush=True)

        t0 = time.time()
        cur.execute('ANALYZE pv_string_branch_5min')
        conn.commit()
        print(f'[3/3] ANALYZE 完成，用时 {time.time() - t0:.1f}s', flush=True)
        print(f'完成，总用时 {time.time() - t_all:.1f}s', flush=True)
    except Exception as e:
        conn.rollback()
        print(f'失败已回滚：{type(e).__name__}: {e}', flush=True)
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
