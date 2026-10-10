"""重建 pv_seconds_di：光伏秒级测点明细长表（按 ts 的 RANGE 日分区表）。

长表结构（每个测点一行），与 wind_seconds_di 宽表不同：
- 一行 = 一个设备的一个测点在某一时刻的值
- 加测点不需要改表结构，直接插入新 point_code 即可

全量重建：先 DROP 再 CREATE（表内数据将全部清空）。

分区设计：
- 父表 PARTITION BY RANGE (ts)，每天一个子分区
  pv_seconds_di_YYYYMMDD，边界按 +08:00（Asia/Shanghai）自然日划分。
- 主键 (device_id, ts, point_code) 包含分区键 ts，故各分区上的唯一约束与
  采集脚本的 ON CONFLICT (device_id, ts, point_code) UPSERT 均无需改动。
- 索引建在父表上，PG 自动在每个子分区创建对应索引。
- 不建 DEFAULT 分区：插入没有对应分区的数据会直接报错（fail loud），
  避免数据静默落入 DEFAULT 后无法拆分；靠预建未来分区保证写入。
  预建范围：昨天 ~ 未来 14 天，采集脚本启动时会按需补建。

字段说明：
- site_id       光伏站编码
- device_id     设备id（主键组成部分）
- device_type   设备类型（逆变器/汇流箱/箱变等，用于过滤）
- point_code    测点编码（如 PCC.POWER, INV.DCVOLT 等，主键组成部分）
- point_value   测点值（text 类型，保留原始字符串，下游按需 CAST）
- ts            测点采集时间（主键组成部分，分区键）
"""
import json
from datetime import datetime, timedelta, timezone

import psycopg2

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
PAST_DAYS = 1     # 预建昨天起的分区（覆盖重启回灌）
FUTURE_DAYS = 14  # 预建未来 14 天分区

# 表名常量（改表名只改这里）
TABLE_DATA = 'pv_seconds_di'
PARTITION_PREFIX = TABLE_DATA + '_'

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
conn.autocommit = True
cur = conn.cursor()

cur.execute(f'DROP TABLE IF EXISTS {TABLE_DATA}')
cur.execute(f"""
CREATE TABLE {TABLE_DATA} (
    site_id        text             NOT NULL,
    device_id      text             NOT NULL,
    device_type    text,
    point_code     text             NOT NULL,
    point_value    text,
    ts             timestamptz      NOT NULL,
    PRIMARY KEY (device_id, ts, point_code)
) PARTITION BY RANGE (ts)
""")
print(f'partitioned parent {TABLE_DATA} rebuilt')

# 列注释（CREATE TABLE 内联注释仅用于源码可读性，
# 真正落入数据库的注释走 COMMENT ON，重建后自动同步）
COMMENTS = [
    ('site_id',     '光伏站编码'),
    ('device_id',   '设备id'),
    ('device_type', '设备类型（逆变器/汇流箱/箱变等）'),
    ('point_code',  '测点编码'),
    ('point_value', '测点值（保留原始字符串，下游按需 CAST）'),
    ('ts',          '测点采集时间'),
]
for col, comment in COMMENTS:
    cur.execute(f"COMMENT ON COLUMN {TABLE_DATA}.{col} IS %s", (comment,))
cur.execute(f"COMMENT ON TABLE {TABLE_DATA} IS "
             f"'光伏秒级测点明细长表（按 ts 日分区，30天保留）'")
print(f'added {len(COMMENTS)} column comments + table comment')


def ensure_partition(day):
    """为某一天（+08:00 自然日）创建日分区，已存在则跳过。"""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    name = PARTITION_PREFIX + start.strftime('%Y%m%d')
    cur.execute(
        f'CREATE TABLE IF NOT EXISTS {name} PARTITION OF {TABLE_DATA} '
        'FOR VALUES FROM (%s) TO (%s)', (start, end))


today = datetime.now(TZ).date()
for offset in range(-PAST_DAYS, FUTURE_DAYS + 1):
    ensure_partition(today + timedelta(days=offset))
print(f'daily partitions ready: {today - timedelta(days=PAST_DAYS)} '
      f'~ {today + timedelta(days=FUTURE_DAYS)}')

# 索引声明在父表，自动下推到每个分区。
# (device_id, ts, point_code) 的主键索引服务按设备查询；
# ts DESC 全局索引服务不带 device_id 的全局时间序查询；
# site_id 索引服务按光伏站过滤；point_code 索引服务按测点过滤。
cur.execute(f'CREATE INDEX idx_{TABLE_DATA}_ts ON {TABLE_DATA} (ts DESC)')
cur.execute(f'CREATE INDEX idx_{TABLE_DATA}_site ON {TABLE_DATA} (site_id)')
cur.execute(f'CREATE INDEX idx_{TABLE_DATA}_code ON {TABLE_DATA} (point_code)')
print('indexes created')

# 分区维护函数：负责 DROP 超期分区（30天保留）和补建未来分区。
# 用 right(relname,8) 取末尾 8 位日期，避免 substring 错位。
func_name = 'ensure_pv_partitions'
cur.execute(f'DROP FUNCTION IF EXISTS {func_name}(integer)')
cur.execute(f"""
CREATE FUNCTION {func_name}(days_ahead int DEFAULT 14)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    i int;
    d_start date;
    pname text;
    retention int := 30;
    keep_from date;
    r record;
BEGIN
    keep_from := current_date - (retention - 1);
    FOR r IN
        SELECT c.relname FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        WHERE i.inhparent = '{TABLE_DATA}'::regclass
          AND c.relname ~ '^{PARTITION_PREFIX}[0-9]{{8}}$'
    LOOP
        BEGIN
            d_start := to_date(right(r.relname, 8), 'YYYYMMDD');
            IF d_start < keep_from THEN
                EXECUTE format('DROP TABLE IF EXISTS %I', r.relname);
            END IF;
        EXCEPTION WHEN others THEN NULL;
        END;
    END LOOP;
    FOR i IN -1..days_ahead LOOP
        d_start := current_date + i;
        pname := '{PARTITION_PREFIX}' || to_char(d_start, 'YYYYMMDD');
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF {TABLE_DATA}
             FOR VALUES FROM (%L::timestamptz) TO (%L::timestamptz)',
            pname, d_start, d_start + 1);
    END LOOP;
END;
$$
""")
print(f'{func_name} function created (30-day retention)')

cur.execute(f"""
    SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
    FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid
    WHERE i.inhparent = '{TABLE_DATA}'::regclass
    ORDER BY c.relname
""")
parts = cur.fetchall()
print(f'\npartitions ({len(parts)}):')
for name, bound in parts:
    print(f'  {name}: {bound}')

cur.execute(f"""
    SELECT a.attname, format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a
    WHERE a.attrelid = '{TABLE_DATA}'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
""")
print('parent columns:', [(r[0], r[1]) for r in cur.fetchall()])

cur.execute(f"SELECT indexname FROM pg_indexes WHERE tablename = '{TABLE_DATA}'")
print('parent indexes:', [r[0] for r in cur.fetchall()])

cur.close()
conn.close()
