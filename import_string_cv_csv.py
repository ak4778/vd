"""把按日导出的 dispersionRatio CSV 导入 PostgreSQL 的 string_cv 表。

CSV 列（17 列）：
  stationName,stationId,data_date,inverterName,dispersionRatio,deviceType,
  inverterId,branchTotal,normal_cnt,abnormal_cnt,exception_cnt,opencircuit_cnt,
  generateElectricity,openCircuit,lowPowerCount,exceptionCount,sum
其中后 5 列（运行/开路/低效/异常/场站总数）已从 string_cv 删除，导入时忽略。
CSV 第 2 行是中文说明行，导入时跳过。

注意：源 CSV 的 dispersionRatio 是小数（如 0.2545 表示 25.45%），
入库时统一乘以 100 转成百分数，与 cv_pct 量纲一致。

用法：
  python import_string_cv_csv.py                 # 默认导入 C:\\s\\20261113~15
  python import_string_cv_csv.py C:\\s\\20261116  # 指定目录（可多个）或单个 csv 文件
  python import_string_cv_csv.py C:\\s\\20261116 --dry-run   # 只统计不写库

冲突处理：string_cv 上已有唯一索引 (data_date, inverter_id)。
  - 新行：插入 12 个业务列，cv_pct / similarity_pct 留空，等刷新脚本回填
  - 已存在行：只更新这 12 列，不动 cv_pct / similarity_pct
"""
import argparse
import glob
import io
import json
import os
import sys

import psycopg2

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'data_config.json')
DEFAULT_DIRS = [r'C:\s\20261113', r'C:\s\20261114', r'C:\s\20261115']

# CSV 列 -> string_cv 列（顺序即 staging 表顺序）
COLMAP = [
    ('stationName', 'station_name'),
    ('stationId', 'station_id'),
    ('data_date', 'data_date'),
    ('inverterName', 'inverter_name'),
    ('dispersionRatio', 'dispersion_ratio'),
    ('deviceType', 'device_type'),
    ('inverterId', 'inverter_id'),
    ('branchTotal', 'branch_total'),
    ('normal_cnt', 'normal_cnt'),
    ('abnormal_cnt', 'abnormal_cnt'),
    ('exception_cnt', 'exception_cnt'),
    ('opencircuit_cnt', 'opencircuit_cnt'),
]

STG_DDL = """
CREATE TEMP TABLE stg_string_cv (
    station_name      text,
    station_id        text,
    data_date         timestamptz,
    inverter_name     text,
    dispersion_ratio  double precision,
    device_type       text,
    inverter_id       text,
    branch_total      integer,
    normal_cnt        integer,
    abnormal_cnt      integer,
    exception_cnt     integer,
    opencircuit_cnt   integer
)
"""

INSERT_SQL = """
INSERT INTO string_cv (
    station_name, station_id, data_date, inverter_name, dispersion_ratio,
    device_type, inverter_id, branch_total, normal_cnt, abnormal_cnt,
    exception_cnt, opencircuit_cnt)
SELECT station_name, station_id, data_date, inverter_name, dispersion_ratio * 100,
       device_type, inverter_id, branch_total, normal_cnt, abnormal_cnt,
       exception_cnt, opencircuit_cnt
FROM stg_string_cv
ON CONFLICT (data_date, inverter_id) DO UPDATE SET
    station_name     = EXCLUDED.station_name,
    station_id       = EXCLUDED.station_id,
    inverter_name    = EXCLUDED.inverter_name,
    dispersion_ratio = EXCLUDED.dispersion_ratio,
    device_type      = EXCLUDED.device_type,
    branch_total     = EXCLUDED.branch_total,
    normal_cnt       = EXCLUDED.normal_cnt,
    abnormal_cnt     = EXCLUDED.abnormal_cnt,
    exception_cnt    = EXCLUDED.exception_cnt,
    opencircuit_cnt  = EXCLUDED.opencircuit_cnt
"""


def iter_csv_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(glob.glob(os.path.join(p, '*.csv'))):
                yield f
        elif os.path.isfile(p) and p.lower().endswith('.csv'):
            yield p
        else:
            print(f'跳过（不存在或非 csv）: {p}')


def import_file(cur, path, dry_run=False):
    """导入一个 CSV，返回 (csv 数据行数, 新增行数, 更新行数)。"""
    import csv
    import itertools

    with open(path, encoding='utf-8-sig', newline='') as f:
        rd = csv.reader(f)
        header = next(rd)
        header = [h.strip() for h in header]
        missing = [c for c, _ in COLMAP if c not in header]
        if missing:
            raise ValueError(f'{os.path.basename(path)} 缺少列: {missing}')
        idx = [header.index(c) for c, _ in COLMAP]
        first = next(rd, None)
        if first is None:
            return 0, 0, 0
        # 第 2 行是中文说明行（首列"名称"），跳过；否则当作数据
        rows_iter = rd if first and first[0].strip() == '名称' \
            else itertools.chain([first], rd)

        buf = io.StringIO()
        wr = csv.writer(buf, lineterminator='\n')
        n_rows = 0
        for parts in rows_iter:
            if not parts or not any(p.strip() for p in parts):
                continue
            wr.writerow([parts[i] if i < len(parts) else '' for i in idx])
            n_rows += 1
        if n_rows == 0:
            return 0, 0, 0
        buf.seek(0)

        if dry_run:
            return n_rows, 0, 0

        cur.execute('DROP TABLE IF EXISTS stg_string_cv')
        cur.execute(STG_DDL)
        cur.copy_expert(
            'COPY stg_string_cv FROM STDIN WITH (FORMAT csv, NULL \'\')', buf)
        cur.execute('SELECT count(*) FROM stg_string_cv')
        n_stg = cur.fetchone()[0]
        if n_stg != n_rows:
            raise RuntimeError(f'{os.path.basename(path)} 行数不一致: '
                               f'csv {n_rows}, staging {n_stg}')
        cur.execute("""
            SELECT count(*) FROM stg_string_cv s
            WHERE EXISTS (SELECT 1 FROM string_cv t
                          WHERE t.data_date = s.data_date
                            AND t.inverter_id = s.inverter_id)
        """)
        n_exist = cur.fetchone()[0]
        cur.execute(INSERT_SQL)
        return n_rows, n_rows - n_exist, n_exist


def main():
    ap = argparse.ArgumentParser(description='导入 dispersionRatio CSV 到 string_cv')
    ap.add_argument('paths', nargs='*', default=DEFAULT_DIRS,
                    help='目录或 csv 文件，默认 11-13/14/15 三天目录')
    ap.add_argument('--dry-run', action='store_true', help='只统计不写库')
    args = ap.parse_args()

    cfg = json.load(open(CONFIG_PATH, encoding='utf-8'))
    conn = psycopg2.connect(cfg['pgConnStr'])
    cur = conn.cursor()

    total_rows = total_new = total_upd = 0
    files = list(iter_csv_files(args.paths or DEFAULT_DIRS))
    print(f'待导入文件 {len(files)} 个' + ('（dry-run）' if args.dry_run else ''))
    for i, path in enumerate(files, 1):
        try:
            n, new, upd = import_file(cur, path, args.dry_run)
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
            total_rows += n
            total_new += new
            total_upd += upd
            print(f'[{i}/{len(files)}] {os.path.basename(path)}: '
                  f'{n} 行（新增 {new}，覆盖 {upd}）')
        except Exception as e:
            conn.rollback()
            print(f'[{i}/{len(files)}] {os.path.basename(path)} 失败: {e!r}')
            raise
    print(f'\n合计: {total_rows} 行，新增 {total_new}，覆盖 {total_upd}')
    conn.close()


if __name__ == '__main__':
    main()
