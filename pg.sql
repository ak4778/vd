SELECT * FROM pg_catalog.pg_extension
ORDER BY oid ASC ;
SHOW server_version;
-- 1. 看版本，>= 10 就有原生分区 
SHOW server_version; -- 2. 看有没有装 TimescaleDB 之类的分区扩展 
SELECT * FROM pg_available_extensions WHERE name = 'timescaledb' ;
SELECT ts,
    device_id,
    gen_tem_driend,
    extra->> 'status' AS status,
    (extra->> 'power_kw' ):: numeric AS power_kw 
FROM wind_seconds_di wsd  
WHERE 
extra->> 'status' = 'running' AND 
(extra->> 'power_kw' )::numeric > 1000 
ORDER BY power_kw DESC , device_id ASC , ts DESC ;
SELECT * FROM pg_catalog.pg_tables WHERE schemaname = 'public' ; -- 或省略 schema 前缀，pg_catalog 默认在 search_path 里 SELECT * FROM pg_tables;
-- 查所有用户表 
SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ; 
-- 查 wind_seconds_di 的列定义 
SELECT attname, format_type(atttypid, atttypmod) FROM pg_catalog.pg_attribute WHERE attrelid = 'wind_seconds_di' ::regclass AND attnum > 0 ;
select * from wind_seconds_di WHERE ts >= now() - interval '1 day';
SELECT typname FROM pg_catalog.pg_type WHERE typnamespace = 'pg_catalog' ::regnamespace AND typtype = 'b' ORDER BY typname;
SELECT device_id,
    to_char(ts AT TIME ZONE 'Asia/Shanghai' , 'YYYY-MM-DD HH24:MI:SS' ) AS ta,
	ts,
    gen_tem_driend,
    extra FROM wind_seconds_di ORDER BY ts DESC ;
select * from wind_seconds_di where device_id='gdfdc001';
SELECT * FROM wind_seconds_di WHERE device_id = 'gdfdc001' AND ts = '2026-09-29 12:33:44' ;

INSERT INTO wind_seconds_di (device_id, ts, gen_tem_driend) 
VALUES ( 'gdfdc001' , '2026-09-29 12:33:44' , 222.0 ) 
ON CONFLICT (device_id, ts)
DO UPDATE SET gen_tem_driend = EXCLUDED.gen_tem_driend;
SELECT * FROM wind_seconds_di WHERE device_id = 'gdfdc001' AND ts = '2026-09-29 12:33:44' ;
INSERT INTO wind_seconds_di (device_id, ts, extra) 
VALUES ( 'gdfdc001' , '2026-09-29 12:33:44' , '{"speed": 21}' ::jsonb) 
ON CONFLICT (device_id, ts) 
DO UPDATE SET extra = COALESCE (wind_seconds_di.extra, '{}' ::jsonb) || EXCLUDED.extra;


INSERT INTO wind_seconds_di (device_id, ts, gen_tem_nonde, gen_tem_driend, extra) 
VALUES ( 'gdfdc001' , '2026-09-29 12:33:44' ,666, 222 , '{"rotate": 33,"age":55}' ::jsonb) 
ON CONFLICT (device_id, ts) 
DO UPDATE SET gen_tem_driend = COALESCE (EXCLUDED.gen_tem_driend, wind_seconds_di.gen_tem_driend),
    gen_tem_nonde = COALESCE (EXCLUDED.gen_tem_nonde, wind_seconds_di.gen_tem_nonde),
    extra = COALESCE (wind_seconds_di.extra, '{}' ::jsonb) || COALESCE (EXCLUDED.extra, '{}' ::jsonb);
SELECT * FROM wind_seconds_di WHERE device_id = 'gdfdc001' AND ts = '2026-09-29 12:33:44' ;
--SELECT pg_size_pretty(pg_database_size('device_dashboard')) AS db_size;
