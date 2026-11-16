# -*- coding: utf-8 -*-
"""按日期刷新光伏支路全链路：秒级原始 -> 5 分钟汇总 -> 查询A -> string_cv。

链路（全部幂等，可重复执行）：
  1) pv_string_branch_5min 当日分区兜底（pv_string_branch_5min_ensure_partition）
  2) 从 pv_string_branch 聚合当日 10:00-16:00 写入 pv_string_branch_5min（ON CONFLICT 覆盖）
  3) ANALYZE pv_string_branch_5min
  4) 查询 A：设备 × 5 分钟快照 的组串整体偏差率 cv_pct
  5) 更新 string_cv.cv_pct，并重算 similarity_pct

口径与 pv_string_branch.sql 保持一致：
  - 剔除 point_value = 0（zero_cnt 仅计数，不参与均值）
  - 支路均值 i_avg 非空、|i_avg| < 50、单桶有效采样 >= 3
  - 剔除 pv_inverter_branch_abnormal 已标记的逆变器+支路
  - 快照支路数 < 2 或 |组串均值| < 0.5A 记 low_current，cv_pct 记 NULL
  - cv_pct = stddev_samp(i_avg) / avg(i_avg) * 100（带符号，不取 abs）
  - similarity_pct = GREATEST(0, 100 - abs(dispersion_ratio - cv_pct))

用法：
  python pv_string_cv_refresh.py 2026-11-12
  python pv_string_cv_refresh.py 2026-11-12 --skip-5min     # 汇总表已刷新，只重跑查询A与更新
  python pv_string_cv_refresh.py 2026-11-12 --dry-run       # 只统计不提交
"""
import argparse
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
               count(*) FILTER (WHERE point_value <> 0) AS sample_cnt,
               count(*) FILTER (WHERE point_value  = 0) AS zero_cnt,
               avg(point_value) FILTER (WHERE point_value <> 0) AS i_avg,
               max(point_value) FILTER (WHERE point_value <> 0) AS sec_max,
               min(point_value) FILTER (WHERE point_value <> 0) AS sec_min
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


def build_qa(cur, day):
    """查询 A：设备 × 5 分钟快照 的 cv_pct，落临时表 qa。"""
    sql = f"""
    CREATE TEMP TABLE qa ON COMMIT PRESERVE ROWS AS
    WITH ex AS (
        SELECT DISTINCT upper(inverter_id) AS dev_key,
               'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A' AS point_code
        FROM pv_inverter_branch_abnormal
        WHERE branch_name ~ '支路[0-9]+'
    ),
    br AS (
        SELECT b.device_id, b.point_code, b.bucket AS snap, b.i_avg AS i
        FROM pv_string_branch_5min b
        WHERE b.bucket >= {ts(day, DAY_START)}
          AND b.bucket <  {ts(day, DAY_END)}
          AND ({window_clause(day, 'b.bucket')})
          AND b.i_avg IS NOT NULL
          AND abs(b.i_avg) < 50
          AND b.sample_cnt >= 3
          AND NOT EXISTS (
                SELECT 1 FROM ex
                WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
          )
    ),
    ds AS (
        SELECT device_id, snap,
               avg(i)         AS i_mean,
               stddev_samp(i) AS i_std,
               count(*)       AS branch_cnt,
               CASE WHEN count(*) >= 2 AND abs(avg(i)) >= 0.5
                    THEN NULL ELSE 'low_current' END AS snap_flag
        FROM br
        GROUP BY device_id, snap
    )
    SELECT device_id, snap,
           CASE WHEN snap_flag IS NULL THEN i_std / i_mean * 100 END AS cv_pct
    FROM ds
    """
    cur.execute('DROP TABLE IF EXISTS qa')
    cur.execute(sql)


def update_string_cv(cur):
    """按 (inverter_id, 5分钟桶) 对齐全量更新 cv_pct，再重算 similarity_pct。"""
    cur.execute("""
    UPDATE string_cv s
    SET cv_pct = q.cv_pct
    FROM qa q
    WHERE upper(s.inverter_id) = q.device_id
      AND s.data_date >= q.snap
      AND s.data_date <  q.snap + interval '5 min'
      AND s.cv_pct IS DISTINCT FROM q.cv_pct
    """)
    cv_updated = cur.rowcount

    cur.execute("""
    UPDATE string_cv
    SET similarity_pct = CASE WHEN cv_pct IS NOT NULL
                              THEN GREATEST(0, 100 - abs(dispersion_ratio - cv_pct)) END
    WHERE similarity_pct IS DISTINCT FROM
          CASE WHEN cv_pct IS NOT NULL
               THEN GREATEST(0, 100 - abs(dispersion_ratio - cv_pct)) END
    """)
    sim_updated = cur.rowcount
    return cv_updated, sim_updated


def stats(cur):
    cur.execute("""
    SELECT (SELECT count(*) FROM qa) AS qa_rows,
           (SELECT count(*) FROM qa WHERE cv_pct IS NOT NULL) AS qa_with_cv,
           count(*) AS matched,
           count(*) FILTER (WHERE q.cv_pct IS NOT NULL AND s.cv_pct IS NOT NULL) AS both_cv
    FROM qa q
    JOIN string_cv s
      ON upper(s.inverter_id) = q.device_id
     AND s.data_date >= q.snap
     AND s.data_date <  q.snap + interval '5 min'
    """)
    return cur.fetchone()


def main():
    ap = argparse.ArgumentParser(description='按日期刷新 秒级 -> 5分钟汇总 -> 查询A -> string_cv')
    ap.add_argument('day', help='日期，例如 2026-11-12')
    ap.add_argument('--skip-5min', action='store_true', help='跳过汇总表刷新，直接跑查询A与更新')
    ap.add_argument('--dry-run', action='store_true', help='只统计不提交（回滚）')
    args = ap.parse_args()

    day = datetime.strptime(args.day, '%Y-%m-%d').strftime('%Y-%m-%d')

    conn = psycopg2.connect(load_pg_conn(), connect_timeout=10)
    conn.set_client_encoding('UTF8')
    cur = conn.cursor()
    cur.execute('SET statement_timeout = 0')

    t_all = time.time()
    try:
        if not args.skip_5min:
            t0 = time.time()
            part = ensure_partition(cur, day)
            print(f'[1/4] 分区: {part}', flush=True)
            n = refresh_5min(cur, day)
            conn.commit()
            print(f'[2/4] 5分钟汇总 UPSERT {n} 行，用时 {time.time() - t0:.1f}s', flush=True)

            t0 = time.time()
            cur.execute('ANALYZE pv_string_branch_5min')
            conn.commit()
            print(f'[3/4] ANALYZE 完成，用时 {time.time() - t0:.1f}s', flush=True)
        else:
            print('[1-3/4] 已跳过汇总表刷新', flush=True)

        t0 = time.time()
        build_qa(cur, day)
        print(f'[4/4] 查询A 完成，用时 {time.time() - t0:.1f}s', flush=True)

        qa_rows, qa_with_cv, matched, both_cv = stats(cur)
        print(f'      查询A {qa_rows} 行（有 cv {qa_with_cv}），匹配 string_cv {matched} 行'
              f'（双方均有 cv {both_cv}）', flush=True)

        cv_updated, sim_updated = update_string_cv(cur)
        if args.dry_run:
            conn.rollback()
            print(f'      [dry-run] 将更新 cv_pct {cv_updated} 行、similarity_pct {sim_updated} 行，已回滚', flush=True)
        else:
            conn.commit()
            print(f'      已提交：cv_pct 更新 {cv_updated} 行、similarity_pct 更新 {sim_updated} 行', flush=True)

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
