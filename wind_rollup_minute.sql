-- 分钟级滚动聚合表：wind_seconds_di（秒级）-> wind_minutes_di（每分钟每设备一行）。
-- 本文件可重复执行：DDL 用 IF NOT EXISTS；聚合用 INSERT ... ON CONFLICT 幂等覆盖；
-- 末尾视图用 CREATE OR REPLACE（每分钟随文件重跑，仅改定义不涉及数据）。
-- 由采集进程每分钟触发一次，只重算最近 3 个已结束的分钟（当前未结束分钟不参与），
-- 重叠重算用于修正迟到/补传数据；历史回填把 WHERE 条件换成更大的区间即可。
CREATE TABLE IF NOT EXISTS wind_minutes_di (
    device_id             text NOT NULL,
    minute                timestamptz NOT NULL,
    tem_gen_driend_avg    float8,
    tem_gen_driend_max    float8,
    tem_gen_driend_min    float8,
    tem_gen_nonde_avg     float8,
    tem_gen_nonde_max     float8,
    tem_gen_nonde_min     float8,
    tem_main_bearing_avg  float8,
    tem_main_bearing_max  float8,
    tem_main_bearing_min  float8,
    genspd_avg            float8,
    genspd_max            float8,
    windspeed_avg         float8,
    windspeed_max         float8,
    sample_cnt            int NOT NULL,
    PRIMARY KEY (device_id, minute)
);
CREATE INDEX IF NOT EXISTS idx_wind_minutes_di_minute ON wind_minutes_di (minute DESC);
INSERT INTO wind_minutes_di AS t (
    device_id, minute,
    tem_gen_driend_avg, tem_gen_driend_max, tem_gen_driend_min,
    tem_gen_nonde_avg, tem_gen_nonde_max, tem_gen_nonde_min,
    tem_main_bearing_avg, tem_main_bearing_max, tem_main_bearing_min,
    genspd_avg, genspd_max, windspeed_avg, windspeed_max, sample_cnt)
SELECT
    device_id,
    date_trunc('minute', ts) AS minute,
    avg(tem_gen_driend)::float8, max(tem_gen_driend), min(tem_gen_driend),
    avg(tem_gen_nonde)::float8,  max(tem_gen_nonde),  min(tem_gen_nonde),
    avg(tem_main_bearing)::float8, max(tem_main_bearing), min(tem_main_bearing),
    avg((other_points->>'WGEN.GENSPD')::numeric)::float8,
    max((other_points->>'WGEN.GENSPD')::numeric)::float8,
    avg((other_points->>'WNAC.WINDSPEED')::numeric)::float8,
    max((other_points->>'WNAC.WINDSPEED')::numeric)::float8,
    count(*)::int
FROM wind_seconds_di
WHERE ts >= date_trunc('minute', now()) - interval '3 min'
  AND ts <  date_trunc('minute', now())
GROUP BY device_id, date_trunc('minute', ts)
ON CONFLICT (device_id, minute) DO UPDATE SET
    tem_gen_driend_avg   = EXCLUDED.tem_gen_driend_avg,
    tem_gen_driend_max   = EXCLUDED.tem_gen_driend_max,
    tem_gen_driend_min   = EXCLUDED.tem_gen_driend_min,
    tem_gen_nonde_avg    = EXCLUDED.tem_gen_nonde_avg,
    tem_gen_nonde_max    = EXCLUDED.tem_gen_nonde_max,
    tem_gen_nonde_min    = EXCLUDED.tem_gen_nonde_min,
    tem_main_bearing_avg = EXCLUDED.tem_main_bearing_avg,
    tem_main_bearing_max = EXCLUDED.tem_main_bearing_max,
    tem_main_bearing_min = EXCLUDED.tem_main_bearing_min,
    genspd_avg           = EXCLUDED.genspd_avg,
    genspd_max           = EXCLUDED.genspd_max,
    windspeed_avg        = EXCLUDED.windspeed_avg,
    windspeed_max        = EXCLUDED.windspeed_max,
    sample_cnt           = EXCLUDED.sample_cnt;

-- 查询用视图（不存数据，底层即分钟聚合表）：语义化列名、km/h 换算、数据质量标记。
-- sample_cnt>=30 视为完整分钟 ok，1~29 为稀疏 sparse，便于查询时过滤孤点。
CREATE OR REPLACE VIEW v_wind_minutes AS
SELECT
    device_id,
    minute,
    tem_gen_driend_avg   AS gen_drive_temp,
    tem_gen_nonde_avg    AS gen_nondrive_temp,
    tem_main_bearing_avg AS main_bearing_temp,
    tem_gen_driend_max   AS gen_drive_temp_max,
    tem_gen_nonde_max    AS gen_nondrive_temp_max,
    tem_main_bearing_max AS main_bearing_temp_max,
    genspd_avg           AS gen_speed,
    windspeed_avg,
    round((windspeed_avg * 3.6)::numeric, 1)::float8 AS windspeed_kmh,
    sample_cnt,
    CASE WHEN sample_cnt >= 30 THEN 'ok'
         WHEN sample_cnt >= 1  THEN 'sparse'
         ELSE 'empty' END AS data_quality
FROM wind_minutes_di;

-- 自动维护秒级表日分区（函数由 create_wind_seconds_di.py 创建）：
-- 每分钟随本文件调用一次，确保「昨天~未来14天」分区存在，跨天插入不报错。
SELECT ensure_wind_partitions(14);
