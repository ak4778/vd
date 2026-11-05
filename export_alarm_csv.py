#!/usr/bin/env python3
"""直接从 PG 导出告警 CSV，绕过 gddl_alarm.exe"""
import psycopg2, csv, sys
from datetime import datetime

# PG 连接
conn = psycopg2.connect(
    host="10.65.80.103", port=5432,
    dbname="gddl_yjyw", user="gddl_yjyw",
    password="2hQ!@t[N+6=J", connect_timeout=5
)
cur = conn.cursor()

start = sys.argv[1] if len(sys.argv) > 1 else "2026-11-04 00:00:00"
end   = sys.argv[2] if len(sys.argv) > 2 else "2026-11-04 23:59:59"
out   = sys.argv[3] if len(sys.argv) > 3 else f"c:\\s\\vd\\alarms_{start[:10]}.csv"

sql = """SELECT
    COALESCE(to_char(ts, 'YYYY-MM-DD HH24:MI:SS'), ''),
    COALESCE(region_company_name, ''),
    COALESCE(station_name, ''),
    COALESCE(device_id, ''),
    COALESCE(device_name, ''),
    COALESCE(point_code, ''),
    COALESCE(point_name, ''),
    COALESCE(to_char(temperature, 'FM9999.00'), ''),
    COALESCE(to_char(range_1h, 'FM9999.00'), ''),
    COALESCE(to_char(range_20s, 'FM9999.00'), ''),
    COALESCE(alarm_type, ''),
    CASE WHEN alarm_value IS NOT NULL THEN to_char(alarm_value, 'FM9999.00') ELSE '' END
FROM wind_alarm
WHERE ts >= %s AND ts < (%s::timestamp + interval '1 second')
AND event = 'ALARM'
ORDER BY id ASC"""

print(f"查询时间范围: {start} ~ {end}")
cur.execute(sql, (start, end))
rows = cur.fetchall()
print(f"共 {len(rows)} 条 ALARM 记录")

headers = ["时间档", "公司", "场站", "机组编号", "机组名称",
           "测点编码", "测点名称", "温度", "1小时温度极差",
           "20秒温度极差", "告警类型", "告警值"]

with open(out, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(headers)
    w.writerows(rows)

cur.close()
conn.close()
print(f"已导出: {out}")
