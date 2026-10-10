"""重建 wind_seconds_di：按 ts 的 RANGE 日分区表（PostgreSQL 12 原生分区）。

全量重建：先 DROP 再 CREATE（表内数据将全部清空）。

分区设计：
- 父表 PARTITION BY RANGE (ts)，每天一个子分区 wind_seconds_di_YYYYMMDD，
  边界按 +08:00（Asia/Shanghai）自然日划分。
- 主键 (device_id, ts) 包含分区键 ts，故各分区上的唯一约束与
  ingest_wind.py 的 ON CONFLICT (device_id, ts) UPSERT 均无需改动。
- 索引建在父表上，PG 自动在每个子分区创建对应索引。
- 不建 DEFAULT 分区：插入没有对应分区的数据会直接报错（fail loud），
  避免数据静默落入 DEFAULT 后无法拆分；靠预建未来分区保证写入。
  预建范围：昨天 ~ 未来 14 天，采集脚本启动时会按需补建。
"""
import json
from datetime import datetime, timedelta, timezone

import psycopg2

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
PAST_DAYS = 1     # 预建昨天起的分区（覆盖重启回灌）
FUTURE_DAYS = 14  # 预建未来 14 天分区

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
conn.autocommit = True
cur = conn.cursor()

cur.execute('DROP TABLE IF EXISTS wind_seconds_di')
cur.execute("""
CREATE TABLE wind_seconds_di (
    device_id           text             NOT NULL,
    ts                  timestamptz      NOT NULL,
    tem_gen_driend      double precision,   -- 发电机驱动端温度
    tem_gen_nonde       double precision,   -- 发电机非驱动端温度
    tem_main_bearing    double precision,   -- 主轴前轴承温度
    tem_main_bearing2   double precision,   -- 主轴后轴承温度
    tem_gen_sen1        double precision,   -- 发电机定子U相线圈温度
    tem_gen_sen2        double precision,   -- 发电机定子V相线圈温度
    tem_gen_sen3        double precision,   -- 发电机定子W相线圈温度
    tem_gen_stau        double precision,   -- 发电机定子U相线圈温度
    tem_gen_stav        double precision,   -- 发电机定子V相线圈温度
    tem_gen_staw        double precision,   -- 发电机定子W相线圈温度
    vibration_lateral   double precision,   -- 侧向震动
    vibration_vertical  double precision,   -- 轴向震动
    cur_conl1           double precision,   -- 网侧三相电流(Ia)
    cur_conl2           double precision,   -- 网侧三相电流(Ib)
    cur_conl3           double precision,   -- 网侧三相电流(Ic)
    tem_gea_oil         double precision,   -- 齿轮箱油池温度
    tem_gea_msde        double precision,   -- 齿轮箱高速轴驱动端温度
    tem_gea_msnd        double precision,   -- 齿轮箱高速轴非驱动端温度
    other_points        jsonb,
    PRIMARY KEY (device_id, ts)
) PARTITION BY RANGE (ts)
""")
print('partitioned parent wind_seconds_di rebuilt')

# 固定列中文注释（CREATE TABLE 内联注释仅用于源码可读性，
# 真正落入数据库的注释走 COMMENT ON，重建后自动同步）
COMMENTS = [
    ('tem_gen_driend', '发电机驱动端温度'),
    ('tem_gen_nonde', '发电机非驱动端温度'),
    ('tem_main_bearing', '主轴前轴承温度'),
    ('tem_main_bearing2', '主轴后轴承温度'),
    ('tem_gen_sen1', '发电机定子U相线圈温度'),
    ('tem_gen_sen2', '发电机定子V相线圈温度'),
    ('tem_gen_sen3', '发电机定子W相线圈温度'),
    ('tem_gen_stau', '发电机定子U相线圈温度'),
    ('tem_gen_stav', '发电机定子V相线圈温度'),
    ('tem_gen_staw', '发电机定子W相线圈温度'),
    ('vibration_lateral', '侧向震动'),
    ('vibration_vertical', '轴向震动'),
    ('cur_conl1', '网侧三相电流(Ia)'),
    ('cur_conl2', '网侧三相电流(Ib)'),
    ('cur_conl3', '网侧三相电流(Ic)'),
    ('tem_gea_oil', '齿轮箱油池温度'),
    ('tem_gea_msde', '齿轮箱高速轴驱动端温度'),
    ('tem_gea_msnd', '齿轮箱高速轴非驱动端温度'),
]
for col, comment in COMMENTS:
    cur.execute(f"COMMENT ON COLUMN wind_seconds_di.{col} IS %s", (comment,))
cur.execute("COMMENT ON TABLE wind_seconds_di IS '风机秒级数据明细表（按 ts 日分区，30天保留）'")
print(f'added {len(COMMENTS)} column comments + table comment')


def ensure_partition(day):
    """为某一天（+08:00 自然日）创建日分区，已存在则跳过。"""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    end = start + timedelta(days=1)
    name = 'wind_seconds_di_' + start.strftime('%Y%m%d')
    cur.execute(
        f'CREATE TABLE IF NOT EXISTS {name} PARTITION OF wind_seconds_di '
        'FOR VALUES FROM (%s) TO (%s)', (start, end))


today = datetime.now(TZ).date()
for offset in range(-PAST_DAYS, FUTURE_DAYS + 1):
    ensure_partition(today + timedelta(days=offset))
print(f'daily partitions ready: {today - timedelta(days=PAST_DAYS)} '
      f'~ {today + timedelta(days=FUTURE_DAYS)}')

# 与普通表版本一致：索引声明在父表，自动下推到每个分区。
# (device_id, ts) 的主键索引服务按设备查询；ts DESC 全局索引服务
# 不带 device_id 的全局时间序查询。
cur.execute('CREATE INDEX idx_wind_seconds_di_ts ON wind_seconds_di (ts DESC)')
print('indexes created')

# 分区维护函数：每分钟由 wind_rollup_minute.sql 调用，负责 DROP 超期分区（30天保留）
# 和补建未来分区。用 right(relname,8) 取末尾 8 位日期，避免 substring 错位。
cur.execute('DROP FUNCTION IF EXISTS ensure_wind_partitions(integer)')
cur.execute("""
CREATE FUNCTION ensure_wind_partitions(days_ahead int DEFAULT 14)
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
        WHERE i.inhparent = 'wind_seconds_di'::regclass
          AND c.relname ~ '^wind_seconds_di_[0-9]{8}$'
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
        pname := 'wind_seconds_di_' || to_char(d_start, 'YYYYMMDD');
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF wind_seconds_di
             FOR VALUES FROM (%L::timestamptz) TO (%L::timestamptz)',
            pname, d_start, d_start + 1);
    END LOOP;
END;
$$
""")
print('ensure_wind_partitions function created (30-day retention)')

cur.execute("""
    SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
    FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid
    WHERE i.inhparent = 'wind_seconds_di'::regclass
    ORDER BY c.relname
""")
parts = cur.fetchall()
print(f'\npartitions ({len(parts)}):')
for name, bound in parts:
    print(f'  {name}: {bound}')

cur.execute("""
    SELECT a.attname, format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a
    WHERE a.attrelid = 'wind_seconds_di'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
""")
print('parent columns:', [(r[0], r[1]) for r in cur.fetchall()])

cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'wind_seconds_di'")
print('parent indexes:', [r[0] for r in cur.fetchall()])

cur.close()
conn.close()
