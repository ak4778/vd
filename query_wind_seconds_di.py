import json
import psycopg2

with open('data_config.json', encoding='utf-8') as f:
    conn = psycopg2.connect(json.load(f)['pgConnStr'])
cur = conn.cursor()

sql = """
SELECT
    ts,
    device_id,
    tem_gen_driend,
    other_points->>'status'              AS status,
    (other_points->>'power_kw')::numeric AS power_kw
FROM wind_seconds_di
WHERE other_points->>'status' = 'running'
  AND (other_points->>'power_kw')::numeric > 1000
ORDER BY power_kw DESC, device_id ASC, ts DESC
"""
cur.execute(sql)
rows = cur.fetchall()

headers = ['ts', 'device_id', 'tem_gen_driend', 'status', 'power_kw']
widths = [19, 9, 14, 8, 9]
print(' | '.join(h.ljust(w) for h, w in zip(headers, widths)))
print('-+-'.join('-' * w for w in widths))
for r in rows:
    vals = [
        r[0].strftime('%Y-%m-%d %H:%M:%S'),
        r[1],
        '' if r[2] is None else str(r[2]),
        r[3],
        str(r[4]),
    ]
    print(' | '.join(v.ljust(w) for v, w in zip(vals, widths)))
print(f'\n{len(rows)} rows')

cur.close()
conn.close()
