-- ############################################################
-- pv_search: 支路电流异常时次统计（PG 版, 口径移植自 dispersion_query.sql 查询 23）
--   统计三种异常（与 ODPS 版完全一致）:
--     (1) 电流 < 0 （负值）             —— 有负值秒的支路数
--     (2) 电流 = 0 （开路）             —— 有 0A 秒的支路数
--     (3) 窗口内 max - min > 2 × avg（波动异常）—— 有此异常的支路数
--   所有统计均限定 x <= 15（含 0A 与负值）; >15A 不参与
--   时间窗口: 10:00-11:00 / 14:00-15:00 / 15:00-16:00（本地时区 Asia/Shanghai）
--   输出粒度: 设备 × 时次（每行 = 一台逆变器在一个 1 小时窗口的异常汇总）
--
-- ★ 复用说明（换日期只改这里）:
--   本文件日期硬编码为 2026-10-26, 复用只需改 v CTE 内的 8 处 TIMESTAMPTZ 字面量:
--     · 2 处日分区边界:   ts >= '2026-10-26 10:00:00+08' / ts < '2026-10-26 16:00:00+08'
--       (与 3 个窗口的外边界重合, 仅用于收紧分区裁剪; 实际过滤仍由下方 3 个窗口决定)
--     · 6 处时间窗口边界: 3 个窗口 × 起止各 1 (10-11 / 14-15 / 15-16)
--   跨多日查询: 把 pv_string_branch 换成具体日分区表 pv_string_branch_YYYYMMDD 并逐日跑,
--   以触发分区裁剪; 参考性能: 单日 3 窗口约 1.8 万行 / 90s
--
-- ODPS → PG 表映射:
--   odps.lydl_gcjs_prod.ods_sis_prd_public_pv_station_info -> pv_station_info   (电站维表)
--   odps.lydl_gcjs_prod.dwd_rsc_pv_seconds_point_di        -> pv_string_branch  (秒级事实表)
--   odps.lydl_gcjs_prod.dwd_sis_pv_branch_df               -> pv_station_invert (逆变器维表)
--
-- 与 ODPS 版的差异（PG 侧结构/数据形态导致, 均已在下方标注）:
--   1. 事实表无 site_id: 改经 pv_station_invert 桥接 station_id ↔ inverter_id 关联电站维表
--   2. 大小写: 事实表 point_code 存的是大写 STRING_BRANCHn_A（ODPS 为小写 string_BRANCHn_A）;
--      pv_station_invert.inverter_id 大小写混杂, 事实表 device_id 全大写
--      -> 连接用 upper(inverter_id) = device_id（维表仅 1.4 万行, upper 只在维表侧算一次）
--   3. PG 秒级表每 (device_id, point_code, ts) 恰一行 -> 无需 ODPS 的毫秒→秒折叠
--      （原 v CTE 的 substr(ts,1,19) + AVG + ms_bucket_count 整段省略）
--   4. PG 无 string_nums(branchTotal) / branch_code -> 不输出 branchTotal, 去掉 devcfg / cfg 两个 CTE
--   5. PG 事实表无 device_type -> 不输出该列
--   6. 方言: GLOB -> ~(正则); printf -> round(); substr(ts,12,8) -> ts 区间比较
-- ############################################################
WITH t1 AS (                                                    -- 电站维表: 只取国电电力, 每电站一行
    SELECT station_id, station_name, station_type, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, station_type, region_company_name
),
si AS (                                                         -- 逆变器维表: 预计算连接键 dev_key(大写), 桥接 station_id
    SELECT
        upper(inverter_id)  AS dev_key,                         -- 对齐事实表 device_id 的大写形态
        station_id,
        inverter_name
    FROM pv_station_invert
),
v AS (                                                          -- 秒级事实表: PG 已是秒级(每 device+point+秒恰一行), 原样取用
    SELECT
        b.device_id,
        b.point_code,
        b.ts,
        b.point_value
    FROM pv_string_branch b
    WHERE b.point_code ~ '^STRING_BRANCH[0-9]{1,4}_A$'          -- 大写正则; 对应 ODPS 的 4 个 GLOB
      AND b.ts >= TIMESTAMPTZ '2026-10-26 10:00:00+08'          -- ★事实表日分区下界 (含)
      AND b.ts <  TIMESTAMPTZ '2026-10-26 16:00:00+08'          -- ★事实表日分区上界 (不含)
      AND ( (b.ts >= TIMESTAMPTZ '2026-10-26 10:00:00+08'
             AND b.ts <  TIMESTAMPTZ '2026-10-26 11:00:00+08')   -- 窗口1: 10-11
         OR (b.ts >= TIMESTAMPTZ '2026-10-26 14:00:00+08'
             AND b.ts <  TIMESTAMPTZ '2026-10-26 15:00:00+08')   -- 窗口2: 14-15
         OR (b.ts >= TIMESTAMPTZ '2026-10-26 15:00:00+08'
             AND b.ts <  TIMESTAMPTZ '2026-10-26 16:00:00+08') ) -- 窗口3: 15-16
),
bm AS (                                                         -- ★1小时窗口桶化: 每支路在每个1小时窗口内把多秒聚合
    SELECT
        v.device_id,
        v.point_code,
        date_trunc('hour', v.ts AT TIME ZONE 'Asia/Shanghai') AS ts,  -- 1小时桶: 分秒归零 (如 2026-10-26 10:00:00)
        AVG(v.point_value) FILTER (WHERE v.point_value <= 15)
            AS point_value,                                     -- 支路窗口均值: x<=15 (含 0A 与负值) 的秒平均; >15A 剔除
        COUNT(*) FILTER (WHERE v.point_value < 0)
            AS neg_cnt,                                         -- 负值秒数 (x<0; 已含在 x<=15 内参与统计)
        COUNT(*) FILTER (WHERE v.point_value = 0)
            AS zero_cnt,                                        -- 0A 开路秒数 (已含在 x<=15 内参与统计)
        COUNT(*) FILTER (WHERE v.point_value <= 15)
            AS valid_cnt,                                       -- 有效秒数 (x<=15, 含 0A 与负值)
        COUNT(*) FILTER (WHERE v.point_value < 0 OR v.point_value > 15)
            AS exc_cnt,                                         -- 异常秒数 (<0A 或 >15A; <0A 仍参与统计, >15A 不参与)
        COUNT(*)                                                -- 该支路窗口内上报秒数 (PG 每秒恰一行 -> COUNT(*) 即不同秒数)
            AS bucket_num,
        MAX(v.point_value) FILTER (WHERE v.point_value <= 15)
            AS sec_max,                                         -- 窗口内有效秒最大值 (含 0A 与负值)
        MIN(v.point_value) FILTER (WHERE v.point_value <= 15)
            AS sec_min                                          -- 窗口内有效秒最小值 (含 0A 与负值)
    FROM v
    GROUP BY v.device_id, v.point_code,
             date_trunc('hour', v.ts AT TIME ZONE 'Asia/Shanghai')
),
base AS (                                                       -- 设备×时次聚合: 跨支路统计三种异常
    SELECT
        t1.region_company_name                                AS region_company_name,
        t1.station_name                                       AS station_name,
        si.station_id                                         AS site_id,
        bm.device_id                                          AS device_id,
        si.inverter_name                                      AS inverter_name,
        bm.ts                                                 AS ts,
        CASE extract(hour FROM bm.ts)
             WHEN 10 THEN '10:00-11:00'
             WHEN 14 THEN '14:00-15:00'
             WHEN 15 THEN '15:00-16:00' END                   AS time_window,
        COUNT(DISTINCT bm.point_code)                         AS total_branch_cnt, -- 上报支路总数
        COUNT(*) FILTER (WHERE bm.neg_cnt  > 0)               AS neg_branch_cnt,   -- (1) 有负值电流的支路数
        COUNT(*) FILTER (WHERE bm.zero_cnt > 0)               AS zero_branch_cnt,  -- (2) 有 0A 电流的支路数
        COUNT(*) FILTER (WHERE bm.sec_max IS NOT NULL
                           AND bm.sec_min IS NOT NULL
                           AND bm.point_value IS NOT NULL
                           AND (bm.sec_max - bm.sec_min) > 2 * bm.point_value)
                                                              AS fluct_branch_cnt, -- (3) max-min>2*avg 的波动异常支路数
        SUM(bm.neg_cnt)                                       AS total_neg_seconds,  -- 负值秒总数 (按秒计)
        SUM(bm.zero_cnt)                                      AS total_zero_seconds, -- 0A秒总数 (按秒计)
        SUM(bm.exc_cnt)                                       AS total_exc_seconds,  -- 异常秒总数 (<0 或 >15)
        MIN(bm.sec_min)                                       AS i_min,        -- 跨支路有效秒最小值
        MAX(bm.sec_max)                                       AS i_max,        -- 跨支路有效秒最大值
        AVG(bm.point_value)                                   AS i_avg         -- 跨支路窗口均值的平均
    FROM bm
    JOIN si ON si.dev_key = bm.device_id                        -- 逆变器维表: upper(inverter_id) = device_id
    JOIN t1 ON t1.station_id = si.station_id                    -- 电站维表: 同时把范围收敛到国电电力
    GROUP BY t1.region_company_name, t1.station_name, si.station_id, bm.device_id,
             si.inverter_name, bm.ts
)
SELECT
    region_company_name,
    station_name,
    site_id,
    device_id,
    inverter_name,
    ts,                                                         -- 窗口起始时刻 (如 2026-10-26 10:00:00)
    time_window,                                                -- 时次标签 ('10:00-11:00' 等)
    total_branch_cnt,                                           -- 上报支路总数
    neg_branch_cnt,                                             -- (1) 有负值电流的支路数
    zero_branch_cnt,                                            -- (2) 有 0A 电流的支路数
    fluct_branch_cnt,                                           -- (3) max-min>2*avg 的波动异常支路数
    total_neg_seconds,                                          -- 负值秒总数
    total_zero_seconds,                                         -- 0A 秒总数
    total_exc_seconds,                                          -- 异常秒总数 (<0 或 >15)
    round(i_min::numeric, 3)::float8 AS i_min,
    round(i_max::numeric, 3)::float8 AS i_max,
    round(i_avg::numeric, 3)::float8 AS i_avg
FROM base
WHERE neg_branch_cnt > 0 OR zero_branch_cnt > 0 OR fluct_branch_cnt > 0  -- ★只保留三类异常至少一项>0的 设备×时次 行
ORDER BY region_company_name, station_name, device_id, ts
;
