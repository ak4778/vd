import json
import psycopg2

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS wind_seconds_di (
    device_id       text             NOT NULL,
    ts              timestamptz      NOT NULL,
    gen_tem_driend    double precision,
    gen_tem_nonde     double precision,
    extra             jsonb,
    PRIMARY KEY (device_id, ts)
)
""")
print('table wind_seconds_di created')

# Global time-order queries (ORDER BY ts DESC / ts range without device_id)
# are served by this index; (device_id, ts) alone cannot since ts is not
# the leading column.
cur.execute('CREATE INDEX IF NOT EXISTS idx_wind_seconds_di_ts ON wind_seconds_di (ts DESC)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_wind_seconds_di_extra ON wind_seconds_di USING gin (extra)')
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
