"""在 PostgreSQL 创建风机设备清单表 wind_device。

连接串读取 data_config.json 的 pgConnStr；使用 CREATE TABLE IF NOT EXISTS，
不删除已有表/数据。字段口径与设备清单接口（type=1022,
ods_ly_ads_gd_wind_device_df）一致，全部为字符串 -> text。
"""
import json

import psycopg2

TABLE = 'wind_device'

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
conn.autocommit = True
cur = conn.cursor()

cur.execute(f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    wind_device_id           text PRIMARY KEY,
    wind_device_wt           text,
    wind_device_name         text,
    station_id               text,
    jd_num                   text,
    wd_num                   text,
    station_name             text,
    province                 text,
    phase_name               text,
    brand_name               text,
    type_name                text,
    constituent_name         text,
    region_company_name      text,
    wind_device_agreement_id text
)
""")
print(f'table {TABLE} ready')

cur.execute(f"COMMENT ON TABLE {TABLE} IS '风机设备清单（来源: 设备清单接口 type=1022 ods_ly_ads_gd_wind_device_df）'")
comments = {
    'wind_device_id': '风机ID',
    'wind_device_wt': '风机WT编码',
    'wind_device_name': '风机名称',
    'station_id': '场站ID',
    'jd_num': '经度',
    'wd_num': '纬度',
    'station_name': '所属风电场',
    'province': '省份',
    'phase_name': '项目名称',
    'brand_name': '品牌',
    'type_name': '机型',
    'constituent_name': '总公司名称',
    'region_company_name': '区域公司名称',
    'wind_device_agreement_id': '风机协议id',
}
for col, comment in comments.items():
    cur.execute(f"COMMENT ON COLUMN {TABLE}.{col} IS %s", (comment,))
print('comments added')

# 设备发现按区域公司/风电场过滤（见 get_fj_realtime.py 黑白名单），建对应索引
cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_region '
            f'ON {TABLE} (region_company_name)')
cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE}_station '
            f'ON {TABLE} (station_id)')
print('indexes ready')

cur.execute("""
    SELECT a.attname, format_type(a.atttypid, a.atttypmod),
           col_description(a.attrelid, a.attnum)
    FROM pg_attribute a
    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
""", (TABLE,))
print('\ncolumns:')
for name, typ, comment in cur.fetchall():
    print(f'  {name:26s} {typ:10s} {comment or ""}')

cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (TABLE,))
print('indexes:', [r[0] for r in cur.fetchall()])

cur.close()
conn.close()
