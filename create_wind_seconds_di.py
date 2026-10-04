import json
import psycopg2

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
conn.autocommit = True
cur = conn.cursor()

# 全量重建：先 DROP 再 CREATE（表内数据将全部清空）
cur.execute('DROP TABLE IF EXISTS wind_seconds_di')
cur.execute("""
CREATE TABLE wind_seconds_di (
    device_id       text             NOT NULL,
    ts              timestamptz      NOT NULL,
    tem_gen_driend    double precision,
    tem_gen_nonde     double precision,
    tem_main_bearing  double precision,
    other_points      jsonb,
    PRIMARY KEY (device_id, ts)
)
""")
print('table wind_seconds_di rebuilt')

# Global time-order queries (ORDER BY ts DESC / ts range without device_id)
# are served by this index; (device_id, ts) alone cannot since ts is not
# the leading column.
cur.execute('CREATE INDEX idx_wind_seconds_di_ts ON wind_seconds_di (ts DESC)')
# other_points 暂不建索引：默认 jsonb GIN 只支持 @>/? 操作符，与现有
# ->> 等值/范围查询形态不匹配（EXPLAIN 实测为 Seq Scan），而本表是秒级
# 高频写入表，空转 GIN 只增加写入维护成本。待真实查询模式明确后再按需
# 建针对性索引，例如：
#   CREATE INDEX ... ON wind_seconds_di ((other_points->>'status'));
#   CREATE INDEX ... ON wind_seconds_di (((other_points->>'power_kw')::numeric));
print('indexes created')

cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'wind_seconds_di'
    ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(' ', r)

cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'wind_seconds_di'")
print('indexes:', [r[0] for r in cur.fetchall()])

cur.close()
conn.close()
