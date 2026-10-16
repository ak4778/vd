"""在 PostgreSQL 创建光伏电站清单表 pv_station_info 并导入 pv_station_info.xlsx。

连接串读取 data_config.json 的 pgConnStr；使用 CREATE TABLE IF NOT EXISTS，
不删除已有表/数据。使用 execute_values 批量 UPSERT（ON CONFLICT DO UPDATE），
空单元格转 NULL，主键为空的行跳过并计数。
"""
import json

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

TABLE = 'pv_station_info'
EXCEL = 'pv_station_info.xlsx'

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
conn.autocommit = True
cur = conn.cursor()

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    constituent_name     text,
    station_id           text PRIMARY KEY,
    station_name         text,
    station_type         text,
    region_company_name  text
)
""")
print(f'table {TABLE} ready')

cur.execute(f"COMMENT ON TABLE {TABLE} IS '光伏电站清单（来源: pv_station_info.xlsx）'")
comments = {
    'constituent_name':    '总公司名称',
    'station_id':          '电站ID',
    'station_name':        '电站名称',
    'station_type':        '电站类型（分布式/集中式）',
    'region_company_name': '区域公司名称',
}
for col, comment in comments.items():
    cur.execute(f"COMMENT ON COLUMN {TABLE}.{col} IS %s", (comment,))
print('comments added')

cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_region '
            f'ON {TABLE} (region_company_name)')
cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_type '
            f'ON {TABLE} (station_type)')
print('indexes ready')

# ---- 导入 xlsx ----
df = pd.read_excel(EXCEL, dtype=str)          # 全部读为字符串
df = df.where(pd.notna(df), None)             # NaN -> None -> NULL

# 主键为空的行跳过并计数
mask = df['station_id'].notna() & (df['station_id'].str.strip() != '')
skipped = len(df) - mask.sum()
df = df[mask].copy()
df['station_id'] = df['station_id'].str.strip()

cols = list(df.columns)
rows = df[cols].values.tolist()

execute_values(
    cur,
    f"""INSERT INTO {TABLE} ({', '.join(cols)}) VALUES %s
        ON CONFLICT (station_id) DO UPDATE SET
            {', '.join(f'{c}=EXCLUDED.{c}' for c in cols if c != 'station_id')}""",
    rows,
    page_size=500,
)
print(f'imported {len(rows)} rows (skipped {skipped} empty-pk rows)')

# ---- 验证 ----
cur.execute(f"SELECT count(*) FROM {TABLE}")
print(f'total rows in {TABLE}: {cur.fetchone()[0]}')

cur.execute("""
    SELECT a.attname, format_type(a.atttypid, a.atttypmod),
           col_description(a.attrelid, a.attnum)
    FROM pg_attribute a
    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
""", (TABLE,))
print('\ncolumns:')
for name, typ, comment in cur.fetchall():
    print(f'  {name:22s} {typ:10s} {comment or ""}')

cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (TABLE,))
print('indexes:', [r[0] for r in cur.fetchall()])

cur.close()
conn.close()
