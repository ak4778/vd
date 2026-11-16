-- ============================================================
-- 逆变器支路电流离散率统计 (PostgreSQL 统一口径版)
-- ============================================================
-- 数据源 : pv_string_branch        (按天分区, 秒级支路电流; 11-02~11-04 实测 204,824,350 行, 12,300 台)
--   device_id    text        逆变器 ID (大写 hex 或 8 位短码)
--   point_code   text        只有 40 种: STRING_BRANCH1_A ~ STRING_BRANCH40_A (大写)
--   point_value  float8      支路电流; 实测无 NULL, "未上报"表现为整行不存在 (不是 NULL, 也不是 '/')
--   ts           timestamptz 秒级时间戳
-- 维表   : pv_station_invert     (去重后 14,037 台逆变器)
--   constituent_name / region_company_name / station_id / station_name /
--   station_type / inverter_id / inverter_name / agreement_id
--   ★ inverter_id 为小写, pv_string_branch.device_id 为大写 -> 关联必须 lower() 两侧
--   ★ inverter_id 可能重复 (同一逆变器多行) -> 关联前用 DISTINCT ON (lower(inverter_id)) 去重,
--     避免一个逆变器被放大成多行
--   ★ 维表无 site_id / device_type: station_id 作为 site_id, station_type 作为 device_type
--   ★ constituent_name 全表只有 '国电电力', 但仍显式过滤以保持原口径
--
-- 【视图 v_branch_current_sec 口径】
--   1) 只保留 constituent_name='国电电力' 的场站 (经维表关联)
--   2) 只保留每日 10:00:00 ~ 16:00:00 (左闭右开, 按 Asia/Shanghai)
--   3) 按 (site_id, device_id, device_type, point_code, 秒) 分组, point_value 取 AVG
--      (同秒多条补传/重传去重归一), 并输出 raw_count
--   4) 视图不物理存数据, 查询时实时聚合; 用 CREATE OR REPLACE 保证可反复执行
--
-- 【离散率定义】 离散率 = 变异系数 CV = 总体标准差 σ / 算术平均值 x̄ × 100%  (分母 n)
--   统一表达式 (x = point_value):
--     CASE WHEN COUNT(x) >= 2 AND AVG(x) > 0
--          THEN sqrt(GREATEST(0::float8,
--                          (SUM(x*x) - SUM(x)*SUM(x)/COUNT(x)) / COUNT(x)))
--               / abs(AVG(x)) * 100.0
--          ELSE NULL END AS dispersion
--   ★ GREATEST(0, ...) 必需: 方差由 (SUM(x²) - SUM(x)²/n)/n 计算, 浮点消去可能产生极小负数,
--     直接 sqrt 会报 "cannot take square root of a negative number"
--   ★ abs() 必需: 均值为负时 CV 不应为负数
--   查询 20/21/22 的保护条件是 (有效支路 >= 2 且均值 != 0), 见各查询注释
--
-- 【稳健性修订 2026-11-05】(基于实测数据校验)
--   1. 所有 sqrt() 标准差项统一加 GREATEST(0::float8, ...) —— 浮点消去会产出负方差
--      (实测: 设备×秒 快照中 150,912 个里有 1 个方差为负, 会直接中断查询)
--   2. 所有 CV 分母统一加 abs() —— 实测 2 分钟窗口内 323 个快照均值为负
--   3. 查询 20/21/22 的 CV 补上 "均值 != 0" 保护 —— 实测 2 分钟窗口内 308 个快照均值为 0,
--      PG 对 float8 除零同样抛错 (division by zero), 不是返回 Infinity
--   4. 查询 20 的 i_std 由无条件求值改为 CASE 保护 —— 整桶无有效支路时分母为 0
--   5. 查询 23 的 3 个时间窗口整体加括号 —— 原来 AND 只作用于窗口 1, AND/OR 优先级导致
--      窗口 2/3 绕过日期与测点过滤, EXPLAIN 实测会有 10 个分区参与 (含 11-05~11-11 空分区)
--   6. 查询 4 的相关子查询 (O(n²)) 改为 DISTINCT ON (site_id, device_id) 一次扫描
--   7. 查询 16 保留 0.5A 最低电流门槛(低电流时 CV 噪声大), 不与查询 20/21/22 的均值!=0 混用
--
-- 【与原 SQLite 版的差异清单】
--   1. 事实表 "odps...dwd_rsc_pv_seconds_point_di" -> pv_string_branch;
--      电站维表 -> pv_station_invert; 删除 ds 分区与 MAX(ds) 取最新分区的逻辑
--   2. 事实表无 site_id / device_type -> 从维表带出 station_id (site_id)、station_type (device_type)
--   3. 无支路配置表 dwd_sis_pv_branch_df:
--      - 删除 devcfg 的 FAKE 行过滤与 branch_code NOT LIKE 'FAKE-%'
--      - 删除支路白名单 JOIN (cfg) 与 'string_' || branch_code || '_A' 匹配
--      - inverter_name 改为从 pv_station_invert 取 (DISTINCT ON 去重)
--      - branchTotal (配置组串数) 无对应字段 -> 查询 20/21/22/23 中该列整体删除
--      - 查询 11/12 的"口径 A (配置表)"删除, 仅保留口径 B (全周期并集) 与 C (单秒最大在线)
--   4. 支路编码大写: GLOB 'string_BRANCH[0-9]+_A' -> upper(point_code) ~ '^STRING_BRANCH[0-9]+_A$'
--   5. point_value 无 NULL、"未上报"=整行不存在; 删除 TEXT->REAL 的 CAST 与空串防护
--   6. 已剔除查询 17 (inverter_efficiency) 与查询 18 (INV.CurPh1/2/3):
--      pv_string_branch 没有这些测点; 原文件本来就没有查询 7 / 19, 编号照旧跳号
--   7. 方言: printf -> to_char / round, strftime/substr(ts,...) -> to_char/date_trunc,
--      所有时间比较与格式化显式 AT TIME ZONE 'Asia/Shanghai'
--
-- ============================================================
-- ⚙ 改这里: 统计日期区间 (左闭右开, 按 Asia/Shanghai 自然日)
--   DATE '2026-11-02' = 起始日 00:00:00 (含)
--   DATE '2026-11-05' = 结束日 00:00:00 (不含)  -> 实际统计 11-02 / 11-03 / 11-04
--   Part 0 视图只在这里写一次; 查询 16/20/21/22/23 为自包含 CTE (内联视图逻辑),
--   其日期条件与本参数块保持一致, 改日期时请搜索 "⚙ 日期" 同步 (共 5 处)。
-- ============================================================


-- ############################################################
-- Part 0: 秒级视图 (先执行, 可反复执行)
--   ① 经维表 INNER JOIN, 全量只保留 constituent_name='国电电力' 的场站
--   ② 秒级去重归一 (同秒同支路多条取 AVG) 并输出 raw_count
-- ############################################################
CREATE OR REPLACE VIEW v_branch_current_sec AS                 -- 可反复执行, 不需要 DROP
SELECT
    d.station_id                        AS site_id,            -- 电站ID (维表 station_id)
    p.device_id                         AS device_id,         -- 逆变器ID (事实表大写)
    d.station_type                      AS device_type,       -- 设备类型 (维表 station_type)
    p.point_code                        AS point_code,        -- 支路测点 STRING_BRANCH*_A
    date_trunc('second', p.ts)          AS ts,                 -- 秒级时间戳 (tz 保留)
    AVG(p.point_value)                  AS point_value,        -- 同秒多条取平均 (float8)
    COUNT(*)                            AS raw_count           -- 该秒同支路原始条数 (通常=1)
FROM pv_string_branch p
JOIN (                                                         -- 逆变器维表去重: 每台逆变器恰好一行
    SELECT DISTINCT ON (lower(inverter_id))
           station_id, station_name, station_type, region_company_name,
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
      AND inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, station_name, station_type,
             region_company_name, inverter_name
) d ON lower(d.inverter_id) = lower(p.device_id)               -- ★大小写不敏感关联
WHERE upper(p.point_code) ~ '^STRING_BRANCH[0-9]+_A$'          -- 只取支路电流测点 (大写)
  AND p.ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Shanghai'   -- ⚙ 日期下界 (含)
  AND p.ts <  (DATE '2026-11-05')::timestamp AT TIME ZONE 'Asia/Shanghai'   -- ⚙ 日期上界 (不含)
  AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '10:00:00'          -- 每日时段起点 (含)
  AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '16:00:00'          -- 每日时段终点 (不含)
GROUP BY d.station_id, p.device_id, d.station_type, p.point_code, date_trunc('second', p.ts);
                                                               -- 分组粒度: 电站 + 设备 + 设备类型 + 支路 + 秒


-- ############################################################
-- 查询 1: 每台逆变器每秒的支路电流离散率
--   输出粒度: 电站 + 设备 + 秒 一行, 即"某一秒的离散率快照"
--   原版依赖 branch_df 的 devcfg/cfg, 现全部改为 pv_station_invert
-- ############################################################
WITH t1 AS (                                                   -- 电站维表: 每电站一行
    SELECT DISTINCT ON (station_id)
           constituent_name, station_id, station_name, station_type
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name, station_type
),
devcfg AS (                                                    -- 设备级配置: 每台逆变器恰好一行
    SELECT DISTINCT ON (lower(inverter_id))
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, inverter_name
),
base AS (                                                      -- 先输出数值型统计量 (未经格式化, 供精确排序)
    SELECT
        t1.constituent_name                            AS constituent_name,
        t1.station_name                                AS station_name,
        v.site_id                                      AS site_id,
        v.device_id                                    AS device_id,
        v.device_type                                  AS device_type,
        d.inverter_name                                AS inverter_name,
        v.ts                                           AS ts,
        COUNT(v.point_value)                           AS branch_cnt,   -- 有效支路数 n
        MIN(v.point_value)                             AS i_min,
        MAX(v.point_value)                             AS i_max,
        AVG(v.point_value)                             AS i_avg,
        CASE WHEN COUNT(v.point_value) >= 2 AND AVG(v.point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(v.point_value * v.point_value) - SUM(v.point_value) * SUM(v.point_value) / COUNT(v.point_value)) / COUNT(v.point_value)))
                  / abs(AVG(v.point_value)) * 100.0          -- σ/x̄×100% = CV (未格式化)
             ELSE NULL END                             AS cv_numeric
    FROM v_branch_current_sec v
    JOIN t1 ON t1.station_id = v.site_id
    JOIN devcfg d ON lower(d.inverter_id) = lower(v.device_id)
    WHERE v.point_value >= 0                                   -- 只统计发电时段
    GROUP BY t1.constituent_name, t1.station_name, v.site_id, v.device_id,
             v.device_type, d.inverter_name, v.ts
)
SELECT
    constituent_name,
    station_name,
    site_id,
    device_id,
    device_type,
    inverter_name,
    ts AT TIME ZONE 'Asia/Shanghai'                        AS ts,       -- 秒级时刻 (上海本地)
    branch_cnt,
    to_char(i_min, 'FM999999990.000')                      AS i_min,     -- 3 位小数
    to_char(i_max, 'FM999999990.000')                      AS i_max,
    to_char(i_avg, 'FM999999990.000')                      AS i_avg,
    CASE WHEN cv_numeric IS NOT NULL
         THEN to_char(cv_numeric, 'FM999999990.00')
         ELSE NULL END                                     AS dispersionRatio  -- 离散率(%) 2 位小数
FROM base
WHERE cv_numeric > 15.0                                        -- 只保留离散率 > 15% 的秒
ORDER BY cv_numeric DESC                                       -- 按真实离散率降序
LIMIT 10000;                                                   -- 演示上限, 全量时删除

-- ############################################################
-- 查询 2: 全天跨时刻聚合的离散率 (参考, 数值会虚高)
--   与查询 1 的区别: 不按秒分组, 全天所有秒级样本混在一起算一个 CV
--   (早晚电流差异也计入波动, 结果偏高, 仅作参考)
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    COUNT(DISTINCT point_code)                      AS branch_count,   -- 全天出现过的支路总数
    COUNT(*)                                        AS samples,        -- 秒级样本行数
    to_char(MIN(point_value), 'FM999999990.000')    AS i_min,
    to_char(MAX(point_value), 'FM999999990.000')    AS i_max,
    to_char(AVG(point_value), 'FM999999990.000')    AS i_avg,
    CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
         THEN to_char(
                  sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0,
                  'FM999999990.00')
         ELSE NULL END                              AS overall_dispersion_pct  -- 全天聚合离散率(%)
FROM v_branch_current_sec
WHERE point_value >= 0
GROUP BY site_id, device_id, device_type
ORDER BY site_id, device_id;

-- ############################################################
-- 查询 3 (推荐): 每台逆变器当日 平均/最大/最小 离散率
--   两层聚合: 内层逐秒离散率, 外层对全天各秒的离散率汇总 (平台日报常用口径)
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    COUNT(*)                                        AS ts_count,            -- 有效秒数
    to_char(AVG(dispersion), 'FM999999990.00')      AS avg_dispersion_pct,
    to_char(MAX(dispersion), 'FM999999990.00')      AS max_dispersion_pct,
    to_char(MIN(dispersion), 'FM999999990.00')      AS min_dispersion_pct
FROM (                                                         -- 内层: 逐秒离散率
    SELECT
        site_id, device_id, device_type, ts,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
) x
WHERE dispersion IS NOT NULL                                   -- 剔除被保护规则跳过的秒
GROUP BY site_id, device_id, device_type
ORDER BY site_id, device_id;

-- ############################################################
-- 查询 4: 每台逆变器当日最大离散率出现的时刻 (定位异常)
--   内层逐秒离散率, 外层用相关子查询找该设备全天最大值, 保留达到最大值的秒
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    ts AT TIME ZONE 'Asia/Shanghai'                 AS worst_ts,
    to_char(dispersion, 'FM999999990.00')           AS worst_dispersion_pct,
    to_char(i_min, 'FM999999990.000')               AS i_min,
    to_char(i_max, 'FM999999990.000')               AS i_max,
    to_char(i_avg, 'FM999999990.000')               AS i_avg,
    branch_cnt
FROM (                                                         -- 每台设备取离散率最高的那一秒
    SELECT DISTINCT ON (site_id, device_id)                    -- ★ 一次扫描取代原来的相关子查询(O(n²))
        site_id, device_id, device_type, ts,
        COUNT(point_value)                          AS branch_cnt,
        MIN(point_value)                            AS i_min,
        MAX(point_value)                            AS i_max,
        AVG(point_value)                            AS i_avg,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value)
                        - SUM(point_value) * SUM(point_value) / COUNT(point_value))
                       / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
    ORDER BY site_id, device_id,
             dispersion DESC NULLS LAST,           -- 离散率最大
             ts                                    -- 并列时取最早的一秒, 保证结果可复现
) worst
ORDER BY site_id, device_id;

-- ############################################################
-- 查询 5: 带可信度分级的离散率
--   有效支路 < 2 -> N/A (跳过); 离散率 > 15% -> HIGH; 其余 OK
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    ts AT TIME ZONE 'Asia/Shanghai'                 AS ts,
    branch_cnt,
    to_char(i_avg, 'FM999999990.000')               AS x_bar,            -- 平均电流 x̄
    CASE WHEN dispersion IS NOT NULL
         THEN to_char(dispersion, 'FM999999990.00')
         ELSE NULL END                              AS dispersionRatio,
    CASE
        WHEN branch_cnt < 2         THEN 'N/A  (n<2, 跳过)'
        WHEN dispersion > 15.0      THEN 'HIGH (>15%)'
        ELSE                            'OK'
    END                                             AS reliability
FROM (                                                         -- 内层: 逐秒离散率 + 支路数
    SELECT
        site_id, device_id, device_type, ts,
        COUNT(point_value)                          AS branch_cnt,
        AVG(point_value)                            AS i_avg,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
) x
ORDER BY site_id, device_id, ts
LIMIT 10000;

-- ############################################################
-- 查询 6: 支路传感器缺失检测
--   基线 = 该设备历史出现过的最大支路数 (口径 B)
--   某秒在线支路数 < 基线 -> 判定该秒有传感器缺失
-- ############################################################
SELECT
    cur.site_id,
    cur.device_id,
    cur.device_type,
    cur.ts AT TIME ZONE 'Asia/Shanghai'             AS ts,
    expected.expected_branch_cnt                   AS expected,       -- 应有支路数 (历史基线)
    cur.actual_branch_cnt                          AS actual,
    expected.expected_branch_cnt - cur.actual_branch_cnt AS missing,
    CASE WHEN cur.dispersion IS NOT NULL
         THEN to_char(cur.dispersion, 'FM999999990.00')
         ELSE NULL END                              AS dispersionRatio,
    CASE
        WHEN expected.expected_branch_cnt - cur.actual_branch_cnt > 0
                                            THEN 'MISSING SENSORS'
        WHEN cur.actual_branch_cnt < 2      THEN 'N/A'
        ELSE                                     'OK'
    END                                             AS status
FROM (                                                         -- cur: 逐秒在线支路数 + 离散率
    SELECT
        site_id, device_id, device_type, ts,
        COUNT(point_value)                          AS actual_branch_cnt,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
) cur
JOIN (                                                         -- expected: 每设备历史支路基线
    SELECT site_id, device_id, MAX(branch_cnt) AS expected_branch_cnt
    FROM (
        SELECT site_id, device_id, ts, COUNT(DISTINCT point_code) AS branch_cnt
        FROM v_branch_current_sec                              -- 不加 >0 过滤, 夜间数据也计入基线更稳
        GROUP BY site_id, device_id, ts
    ) x
    GROUP BY site_id, device_id
) expected
  ON expected.site_id = cur.site_id
 AND expected.device_id = cur.device_id
ORDER BY missing DESC, cur.site_id, cur.device_id, cur.ts
LIMIT 10000;

-- ############################################################
-- 查询 8: 统计离散率 > 15% 的秒数 (超标告警计数)
--   基于逐秒离散率, 按 15% 阈值统计每台设备的超标情况
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    COUNT(*)                                                       AS total_ts,
    SUM(CASE WHEN dispersion > 15.0 THEN 1 ELSE 0 END)             AS exceed_count,
    to_char(SUM(CASE WHEN dispersion > 15.0 THEN 1 ELSE 0 END) * 100.0
            / COUNT(*), 'FM999999990.00')                          AS exceed_pct,
    to_char(MAX(dispersion), 'FM999999990.00')                     AS max_dispersion,
    to_char(AVG(dispersion), 'FM999999990.00')                     AS avg_dispersion
FROM (                                                         -- 内层: 逐秒离散率
    SELECT
        site_id, device_id, device_type, ts,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
) x
WHERE dispersion IS NOT NULL
GROUP BY site_id, device_id, device_type
ORDER BY exceed_count DESC, max_dispersion DESC;

-- ############################################################
-- 查询 9: 超标秒按小时分布 (定位异常集中时段)
--   把超标秒聚合到小时粒度, 看异常集中在早晨/正午/傍晚哪个时段
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    to_char(ts AT TIME ZONE 'Asia/Shanghai', 'HH24') || ':00'  AS hour,  -- '13:27:41' -> '13:00'
    COUNT(*)                                            AS exceed_count,
    to_char(MIN(dispersion), 'FM999999990.00')          AS min_in_hour,
    to_char(MAX(dispersion), 'FM999999990.00')          AS max_in_hour
FROM (                                                         -- 内层: 逐秒离散率
    SELECT
        site_id, device_id, device_type, ts,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
) x
WHERE dispersion > 15.0                                        -- 只保留超标秒
GROUP BY site_id, device_id, device_type, hour                 -- PG 允许按输出列别名分组
ORDER BY site_id, device_id, hour;

-- ############################################################
-- 查询 10: 秒级平均明细 (核对同秒多条如何被合并)
--   调试用途: 查看 Part 0 视图把同秒多条平均成秒级后的中间结果
-- ############################################################
SELECT
    site_id,
    device_id,
    device_type,
    point_code,
    ts AT TIME ZONE 'Asia/Shanghai'                 AS ts_sec,
    raw_count,
    to_char(point_value, 'FM999999990.0000')        AS point_value_sec   -- 4 位小数
FROM v_branch_current_sec
ORDER BY site_id, device_id, point_code, ts
LIMIT 10000;


-- ############################################################
-- 如何确定一台设备"应有"多少条支路? (查询 11~12)
--   B. 全周期测点并集
--   C. 单秒最大在线支路数
--   原版口径 A 来自支路配置表 dwd_sis_pv_branch_df (PG 不存在), 已删除;
--   推荐口径改为 B (历史并集), 缺失场景与 C 对比暴露。
-- ############################################################

-- ----------------------------------------------------------------
-- 查询 11: 两种口径对比 (数据质量体检)
--   B = 全周期并集口径, C = 单秒最大在线口径
--   B <> C -> 有支路间歇上报 (历史出现过但单秒凑不齐)
--   注: 原版口径 A (branch_df 声明的应有支路数) 因无配置表已整体删除
-- ----------------------------------------------------------------
SELECT
    d.site_id,
    d.device_id,
    d.device_type,
    u.union_cnt                                       AS b_union,      -- 口径 B: 全周期测点并集数
    m.persec_max                                      AS c_persec_max, -- 口径 C: 单秒最大在线支路数
    CASE
        WHEN u.union_cnt <> m.persec_max
             THEN 'B<>C 有支路间歇上报'
        ELSE '一致'
    END                                               AS diagnosis
FROM (SELECT DISTINCT site_id, device_id, device_type FROM v_branch_current_sec) d
LEFT JOIN (                                                    -- 口径 B: 全周期并集
    SELECT site_id, device_id, COUNT(DISTINCT point_code) AS union_cnt
    FROM v_branch_current_sec
    GROUP BY site_id, device_id
) u ON u.site_id = d.site_id AND u.device_id = d.device_id
LEFT JOIN (                                                    -- 口径 C: 单秒最大在线数
    SELECT site_id, device_id, MAX(c) AS persec_max
    FROM (
        SELECT site_id, device_id, ts, COUNT(DISTINCT point_code) AS c
        FROM v_branch_current_sec
        GROUP BY site_id, device_id, ts
    ) x
    GROUP BY site_id, device_id
) m ON m.site_id = d.site_id AND m.device_id = d.device_id
ORDER BY d.site_id, d.device_id;

-- ----------------------------------------------------------------
-- 查询 12 (推荐): 缺失检测的正确版本
--   应有数 = 全周期并集 B (原版 COALESCE(A,B) 中的 A 已随配置表删除)
-- ----------------------------------------------------------------
SELECT
    cur.site_id,
    cur.device_id,
    cur.device_type,
    cur.ts AT TIME ZONE 'Asia/Shanghai'             AS ts,
    exp.expected_branch_cnt                         AS expected,      -- 应有支路数 (口径 B)
    cur.actual_branch_cnt                           AS actual,
    exp.expected_branch_cnt - cur.actual_branch_cnt AS missing,
    CASE WHEN cur.dispersion IS NOT NULL
         THEN to_char(cur.dispersion, 'FM999999990.00')
         ELSE NULL END                              AS dispersionRatio,
    CASE
        WHEN exp.expected_branch_cnt - cur.actual_branch_cnt > 0
            THEN 'MISSING SENSORS'
        WHEN cur.actual_branch_cnt < 2 THEN 'N/A'
        ELSE 'OK'
    END                                             AS status
FROM (                                                         -- cur: 逐秒在线支路数 + 离散率
    SELECT
        site_id, device_id, device_type, ts,
        COUNT(point_value)                          AS actual_branch_cnt,
        CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
             THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                  / abs(AVG(point_value)) * 100.0
             ELSE NULL END                          AS dispersion
    FROM v_branch_current_sec
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, ts
) cur
JOIN (                                                         -- exp: 每设备应有支路数 (口径 B)
    SELECT site_id, device_id,
           COUNT(DISTINCT point_code) AS expected_branch_cnt
    FROM v_branch_current_sec
    GROUP BY site_id, device_id
) exp
  ON exp.site_id = cur.site_id
 AND exp.device_id = cur.device_id
WHERE exp.expected_branch_cnt - cur.actual_branch_cnt > 0      -- 只看存在缺失的秒
ORDER BY missing DESC, cur.site_id, cur.device_id, cur.ts
LIMIT 10000;


-- ############################################################
-- 电站 / 逆变器信息统一来自 pv_station_invert (查询 13~15)
--   关联关系: 维表 station_id = 视图 site_id, lower(inverter_id) = lower(device_id)
--   维表同一 station_id / inverter_id 可能多行, 用 DISTINCT ON 取一行
-- ############################################################

-- ----------------------------------------------------------------
-- 查询 13: 离散率结果关联电站名称
--   在查询 3 (每台逆变器当日离散率) 基础上 JOIN 维表, 输出 station_name (别名 site_name)
-- ----------------------------------------------------------------
SELECT
    c.station_name AS site_name,
    d.site_id,
    d.device_id,
    d.device_type,
    d.ts_count,
    d.avg_dispersion_pct,
    d.max_dispersion_pct,
    d.min_dispersion_pct
FROM (                                                         -- d = 查询 3 的当日汇总
    SELECT
        site_id, device_id, device_type,
        COUNT(*)                                        AS ts_count,
        to_char(AVG(dispersion), 'FM999999990.00')      AS avg_dispersion_pct,
        to_char(MAX(dispersion), 'FM999999990.00')      AS max_dispersion_pct,
        to_char(MIN(dispersion), 'FM999999990.00')      AS min_dispersion_pct,
        MAX(dispersion)                                 AS max_disp_raw
    FROM (                                                     -- 内层: 逐秒离散率
        SELECT
            site_id, device_id, device_type, ts,
            CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
                 THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                      / abs(AVG(point_value)) * 100.0
                 ELSE NULL END                      AS dispersion
        FROM v_branch_current_sec
        WHERE point_value >= 0
        GROUP BY site_id, device_id, device_type, ts
    ) x
    WHERE dispersion IS NOT NULL
    GROUP BY site_id, device_id, device_type
) d
JOIN (
    SELECT DISTINCT ON (station_id)
           station_id, station_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name
) c ON c.station_id = d.site_id
ORDER BY d.max_disp_raw DESC;

-- ############################################################
-- 查询 14: 先在电站维表过滤站点, 再计算离散率
--   适用: 按电站名称等维表属性筛选, 筛选逻辑集中在维表
--   写法: 维表过滤结果作为驱动集合 (CTE site_filter), 事实子查询用 site_id IN (...) 下推
-- ############################################################
WITH site_filter AS (                                          -- CTE: 维表过滤出的目标站点
    SELECT DISTINCT ON (station_id)
           station_id AS site_id, station_name AS site_name
    FROM pv_station_invert
    WHERE station_name = '国电电力蓝海2号光伏电站'              -- 换成目标电站; 多站用 IN (...)
    ORDER BY station_id, station_name
)
SELECT
    f.site_name,
    d.site_id,
    d.device_id,
    d.device_type,
    d.ts_count,
    d.avg_dispersion_pct,
    d.max_dispersion_pct
FROM site_filter f
JOIN (                                                         -- d = 仅目标站点的当日汇总
    SELECT
        v.site_id, v.device_id, v.device_type,
        COUNT(*)                                        AS ts_count,
        to_char(AVG(v.dispersion), 'FM999999990.00')    AS avg_dispersion_pct,
        to_char(MAX(v.dispersion), 'FM999999990.00')    AS max_dispersion_pct,
        MAX(v.dispersion)                               AS max_disp_raw
    FROM (                                                     -- v = 逐秒离散率 (已下推站点过滤)
        SELECT
            site_id, device_id, device_type, ts,
            CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
                 THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                      / abs(AVG(point_value)) * 100.0
                 ELSE NULL END                      AS dispersion
        FROM v_branch_current_sec
        WHERE site_id IN (SELECT site_id FROM site_filter)     -- 维表过滤下推
          AND point_value >= 0
        GROUP BY site_id, device_id, device_type, ts
    ) v
    WHERE v.dispersion IS NOT NULL
    GROUP BY v.site_id, v.device_id, v.device_type
) d ON d.site_id = f.site_id
ORDER BY d.max_disp_raw DESC;

-- ############################################################
-- 查询 15: 维表过滤的另一等价写法 —— CTE 驱动 INNER JOIN
--   与查询 14 同口径同结果, 区别仅在过滤方式:
--     Q14: 事实子查询内 site_id IN (维表过滤集合) 下推
--     Q15: 先在 t1 过滤维表, t2 再 INNER JOIN 事实视图
--   注意: 必须 INNER JOIN, LEFT JOIN 不会剔除其他电站
-- ############################################################
WITH t1 AS (                                                   -- t1: 维表过滤出的目标站点
    SELECT DISTINCT ON (station_id)
           station_id AS site_id, station_name AS site_name
    FROM pv_station_invert
    WHERE station_name = '国电电力蓝海2号光伏电站'
    ORDER BY station_id, station_name
),
t2 AS (                                                        -- t2: 目标站点的事实数据
    SELECT v.site_id, v.device_id, v.device_type, v.ts, v.point_value, t1.site_name
    FROM v_branch_current_sec v
    JOIN t1 ON t1.site_id = v.site_id
),
t3 AS (                                                        -- t3: 过滤后的逐秒离散率
    SELECT site_id, device_id, device_type, site_name, ts,
           CASE WHEN COUNT(point_value) >= 2 AND AVG(point_value) > 0
                THEN sqrt(GREATEST(0::float8, (SUM(point_value * point_value) - SUM(point_value) * SUM(point_value) / COUNT(point_value)) / COUNT(point_value)))
                     / abs(AVG(point_value)) * 100.0
           END AS dispersion
    FROM t2
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, site_name, ts
)
SELECT
    site_name,
    site_id,
    device_id,
    device_type,
    COUNT(*)                                        AS ts_count,
    to_char(AVG(dispersion), 'FM999999990.00')      AS avg_dispersion_pct,
    to_char(MAX(dispersion), 'FM999999990.00')      AS max_dispersion_pct,
    MAX(dispersion)                                 AS max_disp_raw
FROM t3
WHERE dispersion IS NOT NULL
GROUP BY site_name, site_id, device_id, device_type
ORDER BY max_disp_raw DESC;


-- ############################################################
-- 查询 16: 问题逆变器判定 (快照粒度可调: 秒/分钟/5分钟/10分钟)
--   口径: 变异系数 CV = σ/x̄×100 (同查询 1~15, 非极差法)
--   告警逻辑: 全周期有效快照 N 个, 其中 CV > 15% 的 M 个, M/N > 20% -> 问题逆变器
--     (不按天拆分, 整个统计周期合并计算一个总占比)
--   自包含: Part 0 视图逻辑已内联到 v_branch CTE, 无需预建视图即可独立运行
--   三处可调: ① 快照粒度 (bm 的 snap_bucket, SELECT 与 GROUP BY 两处同步)
--             ② 超标阈值 15.0 (CASE) ③ 占比阈值 20 (HAVING)
-- ############################################################
WITH v_branch AS (                                             -- ★ 内联 Part 0: 秒级支路电流 (电站白名单+时段+去重)
    SELECT
        d.station_id                        AS site_id,
        p.device_id                         AS device_id,
        d.station_type                      AS device_type,
        p.point_code                        AS point_code,
        date_trunc('second', p.ts)          AS ts,
        AVG(p.point_value)                  AS point_value,
        COUNT(*)                            AS raw_count
    FROM pv_string_branch p
    JOIN (
        SELECT DISTINCT ON (lower(inverter_id))
               station_id, station_type, inverter_id
        FROM pv_station_invert
        WHERE constituent_name = '国电电力'
          AND inverter_id IS NOT NULL
          AND inverter_id <> ''
        ORDER BY lower(inverter_id), station_id, station_type
    ) d ON lower(d.inverter_id) = lower(p.device_id)
    WHERE upper(p.point_code) ~ '^STRING_BRANCH[0-9]+_A$'
      AND p.ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期下界
      AND p.ts <  (DATE '2026-11-05')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期上界
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '10:00:00'
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '16:00:00'
    GROUP BY d.station_id, p.device_id, d.station_type, p.point_code, date_trunc('second', p.ts)
),
bm AS (                                                        -- 第 1 层: 每支路每快照桶的均值
    SELECT
        site_id,
        device_id,
        device_type,
        point_code,
        date_trunc('second', ts)                            AS snap_bucket,   -- ★粒度① 秒级 (默认)
        -- date_trunc('minute', ts)                         AS snap_bucket,   -- ★粒度② 分钟级
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 5) * interval '5 minute'
        --                                                    AS snap_bucket,   -- ★粒度③ 5分钟
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 10) * interval '10 minute'
        --                                                    AS snap_bucket,   -- ★粒度④ 10分钟
        AVG(point_value)                                    AS i
    FROM v_branch                                              -- 内联视图 (电站白名单+时段过滤已内置)
    WHERE point_value >= 0
    GROUP BY site_id, device_id, device_type, point_code,
             date_trunc('second', ts)                          -- ★与上面选中的粒度表达式保持一致
),
snap AS (                                                      -- 第 2 层: 每设备每桶的快照 CV (跨支路)
    SELECT
        site_id,
        device_id,
        device_type,
        snap_bucket,
        CASE WHEN COUNT(i) >= 2 AND AVG(i) > 0.5     -- 保护: n<2 或均值<=0.5A -> NULL
             THEN sqrt(GREATEST(0::float8, (SUM(i * i) - SUM(i) * SUM(i) / COUNT(i)) / COUNT(i)))
                  / abs(AVG(i)) * 100.0
             ELSE NULL END                            AS cv
    FROM bm
    GROUP BY site_id, device_id, device_type, snap_bucket
)
-- 第 3 层: 全周期超标占比 + 问题逆变器判定 (补省公司/场站名称 / 逆变器名称)
SELECT
    c.region_company_name,
    c.station_name,
    d.site_id,
    d.device_id,
    d.device_type,
    inv.inverter_name,
    d.total_valid_points,
    d.over_points,
    d.over_ratio
FROM (
    SELECT
        site_id, device_id, device_type,
        COUNT(*)                                                       AS total_valid_points,
        SUM(CASE WHEN cv > 15.0 THEN 1 ELSE 0 END)                     AS over_points,
        round(SUM(CASE WHEN cv > 15.0 THEN 1 ELSE 0 END) * 100.0
              / COUNT(*), 2)                                           AS over_ratio
    FROM snap
    WHERE cv IS NOT NULL
    GROUP BY site_id, device_id, device_type
    HAVING round(SUM(CASE WHEN cv > 15.0 THEN 1 ELSE 0 END) * 100.0
                 / COUNT(*), 2) > 20                                   -- ★占比阈值 (PG 的 HAVING 不能引用别名, 需重写表达式)
) d
JOIN (                                                         -- 电站维表: 省公司/场站名称
    SELECT DISTINCT ON (station_id)
           station_id, station_name, region_company_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name, region_company_name
) c ON c.station_id = d.site_id
LEFT JOIN (                                                    -- 逆变器: 每设备一行
    SELECT DISTINCT ON (lower(inverter_id))
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, inverter_name
) inv ON lower(inv.inverter_id) = lower(d.device_id)
ORDER BY c.region_company_name, d.over_ratio DESC;


-- ############################################################
-- 查询 20: 全量逐秒(可调桶)离散率明细 (不做 >15% 过滤)
--   与查询 1 的区别:
--     1) inverter_name 来自 pv_station_invert (LEFT JOIN), 未登记设备为 NULL, 行不丢
--     2) 去掉 cv_numeric > 15 过滤; 离散率 NULL 的单支路桶也保留 (normal_cnt=1)
--     3) 不用 WHERE 过滤电流, 改为条件聚合: 0A 计 opencircuit_cnt, <0 或 >15A 计 exceptionCount
--        ★bm 层仅对有效秒(x<=15, 含 0A 开路与 <0 负值)取桶均值; 仅 >15A 异常秒不参与 CV
--   三类计数口径 (同设备同桶跨支路):
--     currentBranch_count = 该桶上报支路总数 (含 0A 开路与异常)
--     normal_cnt      = 桶内有有效秒(x<=15)的支路数
--     opencircuit_cnt = 桶内全为 0A 的支路数 (已参与 CV, point_value=0)
--     exceptionCount  = 该桶异常秒总数 (SUM(exc_cnt), 按秒计)
--   整桶全为异常值也保留输出 (normal_cnt=0, i_min/i_max/i_avg/CV 为 NULL)
--   ★快照粒度开关在 bm CTE (SELECT 与 GROUP BY 两处同步, 4 选 1): 当前 1 小时档
--   ★原版 branchTotal(配置组串数) 无对应字段, 已整体删除
--   离散率公式 (总体标准差, 分母 n) 与查询 1 一致
--   电流窗口: x<=15 均参与 CV (含 0A 与负值); 仅 normal_cnt<2 或均值=0(除零) 时 CV 为 NULL
-- ############################################################
WITH t1 AS (                                                   -- 电站维表: 每电站一行
    SELECT DISTINCT ON (station_id)
           station_id, station_name, station_type, region_company_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name, station_type, region_company_name
),
v AS (                                                         -- ★ 内联 Part 0 秒级层
    SELECT
        d.station_id                        AS site_id,
        p.device_id                         AS device_id,
        d.station_type                      AS device_type,
        p.point_code                        AS point_code,
        date_trunc('second', p.ts)          AS ts,
        AVG(p.point_value)                  AS point_value,
        COUNT(DISTINCT p.ts)                AS ms_bucket_count   -- 该秒不同毫秒时刻数 (秒级数据恒=1)
    FROM pv_string_branch p
    JOIN (
        SELECT DISTINCT ON (lower(inverter_id))
               station_id, station_type, inverter_id
        FROM pv_station_invert
        WHERE constituent_name = '国电电力'
          AND inverter_id IS NOT NULL
          AND inverter_id <> ''
        ORDER BY lower(inverter_id), station_id, station_type
    ) d ON lower(d.inverter_id) = lower(p.device_id)
    WHERE upper(p.point_code) ~ '^STRING_BRANCH[0-9]+_A$'
      AND p.ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期下界
      AND p.ts <  (DATE '2026-11-05')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期上界
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '10:00:00'
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '16:00:00'
    GROUP BY d.station_id, p.device_id, d.station_type, p.point_code, date_trunc('second', p.ts)
),
bm AS (                                                        -- ★快照粒度桶化: 每支路每桶把多秒平均成 1 值
    SELECT
        site_id,
        device_id,
        device_type,
        point_code,
        -- date_trunc('second', ts) AS ts,                        -- ★粒度开关① 秒级
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 5) * interval '5 minute'
        --                                 AS ts,                 -- ★粒度开关② 5 分钟
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 10) * interval '10 minute'
        --                                 AS ts,                 -- ★粒度开关③ 10 分钟
        date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
            AT TIME ZONE 'Asia/Shanghai'            AS ts,        -- ★粒度开关④ 1 小时 (当前)
        AVG(CASE WHEN point_value <= 15 THEN point_value END)
            AS point_value,                                        -- 支路桶均值: 仅有效秒; 全无效->NULL
        SUM(CASE WHEN point_value <= 15 THEN 1 ELSE 0 END)
            AS valid_cnt,
        SUM(CASE WHEN point_value = 0 THEN 1 ELSE 0 END)
            AS zero_cnt,
        SUM(CASE WHEN point_value < 0 OR point_value > 15 THEN 1 ELSE 0 END)
            AS exc_cnt,
        COUNT(DISTINCT date_trunc('second', ts)) AS bucket_num,    -- 该支路桶内不同秒数
        MAX(ms_bucket_count)                     AS ms_bucket_count,
        MAX(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_max,
        MIN(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_min
    FROM v
    GROUP BY site_id, device_id, device_type, point_code,
             date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
                 AT TIME ZONE 'Asia/Shanghai'                      -- ★与 SELECT 粒度同步
),
devcfg AS (                                                    -- 设备级配置: 每台逆变器恰好一行
    SELECT DISTINCT ON (lower(inverter_id))
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, inverter_name
),
base AS (                                                      -- 按电站+设备+桶聚合, 输出数值型统计量
    SELECT
        t1.region_company_name                          AS region_company_name,
        t1.station_name                                 AS station_name,
        v.site_id                                       AS site_id,
        v.device_id                                     AS device_id,
        v.device_type                                   AS device_type,
        d.inverter_name                                 AS inverter_name,
        v.ts                                            AS ts,
        MAX(v.bucket_num)                               AS bucket_num,
        MAX(v.ms_bucket_count)                          AS ms_bucket_count,
        COUNT(DISTINCT v.point_code)                    AS currentBranch_count,
        SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)
                                                        AS normal_cnt,
        SUM(CASE WHEN v.bucket_num = v.zero_cnt AND v.zero_cnt > 0 THEN 1 ELSE 0 END)
                                                        AS opencircuit_cnt,
        SUM(v.exc_cnt)                                  AS exceptionCount,
        MIN(v.sec_min)                                  AS i_min,
        MAX(v.sec_max)                                  AS i_max,
        AVG(v.point_value)                              AS i_avg,
        CASE WHEN SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END) >= 1
             THEN sqrt(GREATEST(0::float8, (SUM(v.point_value * v.point_value)
                        - SUM(v.point_value) * SUM(v.point_value)
                          / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END))
                       / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)))
             ELSE NULL END                              AS i_std,
        CASE WHEN SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END) >= 2
               AND AVG(v.point_value) <> 0           -- ★ 防除零 (0A 整桶)
             THEN sqrt(GREATEST(0::float8, (SUM(v.point_value * v.point_value) - SUM(v.point_value) * SUM(v.point_value) / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)) / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)))
                  / abs(AVG(v.point_value)) * 100.0
             ELSE NULL END                              AS cv_numeric
    FROM bm v
    JOIN t1 ON t1.station_id = v.site_id
    LEFT JOIN devcfg d ON lower(d.inverter_id) = lower(v.device_id)
    GROUP BY t1.region_company_name, t1.station_name, v.site_id, v.device_id,
             v.device_type, d.inverter_name, v.ts
)
SELECT                                                         -- 外层只负责展示格式化
    region_company_name,
    station_name,
    site_id,
    device_id,
    device_type,
    inverter_name,
    ts AT TIME ZONE 'Asia/Shanghai'                 AS ts,        -- 快照桶起始时刻 (上海本地)
    bucket_num,
    ms_bucket_count,
    currentBranch_count,
    normal_cnt,
    opencircuit_cnt,
    exceptionCount,
    CASE WHEN i_min IS NOT NULL THEN to_char(i_min, 'FM999999990.000') ELSE NULL END AS i_min,
    CASE WHEN i_max IS NOT NULL THEN to_char(i_max, 'FM999999990.000') ELSE NULL END AS i_max,
    CASE WHEN i_avg IS NOT NULL THEN to_char(i_avg, 'FM999999990.000') ELSE NULL END AS i_avg,
    CASE WHEN i_std IS NOT NULL THEN to_char(i_std, 'FM999999990.000') ELSE NULL END AS i_std,
    CASE WHEN cv_numeric IS NOT NULL
         THEN to_char(cv_numeric, 'FM999999990.00')
         ELSE NULL END                              AS dispersionRatio
FROM base
-- WHERE device_id = 'b718d258677145edb1a5e901fe5568a9'       -- 可选: 定位单台逆变器
-- WHERE cv_numeric > 15.0                                     -- 可选: 只看超标桶
ORDER BY region_company_name, cv_numeric DESC                  -- 同省内按真实离散率降序
LIMIT 10000;


-- ############################################################
-- 查询 21: 问题逆变器判定 (在查询 20 的快照离散率结果上做全周期占比聚合)
--   口径: WITH 链 (t1→v→bm→devcfg→base) 与查询 20 完全一致, 快照 CV 即查询 20 的 cv_numeric
--   bm 粒度开关随查询 20 同步 (当前 1 小时档)
--   告警逻辑 (同查询 16): 全周期有效快照 N, 其中 CV > 15% 的 M, M/N > 20% -> 问题逆变器
--   有效快照 = cv_numeric 非空 (保护: 有效支路>=2 且均值!=0; 均值为负也计入)
--   两处可调: 超标阈值 15.0 / 占比阈值 20
--   ★原版 branchTotal 列已删除
-- ############################################################
WITH t1 AS (
    SELECT DISTINCT ON (station_id)
           station_id, station_name, station_type, region_company_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name, station_type, region_company_name
),
v AS (
    SELECT
        d.station_id                        AS site_id,
        p.device_id                         AS device_id,
        d.station_type                      AS device_type,
        p.point_code                        AS point_code,
        date_trunc('second', p.ts)          AS ts,
        AVG(p.point_value)                  AS point_value,
        COUNT(DISTINCT p.ts)                AS ms_bucket_count
    FROM pv_string_branch p
    JOIN (
        SELECT DISTINCT ON (lower(inverter_id))
               station_id, station_type, inverter_id
        FROM pv_station_invert
        WHERE constituent_name = '国电电力'
          AND inverter_id IS NOT NULL
          AND inverter_id <> ''
        ORDER BY lower(inverter_id), station_id, station_type
    ) d ON lower(d.inverter_id) = lower(p.device_id)
    WHERE upper(p.point_code) ~ '^STRING_BRANCH[0-9]+_A$'
      AND p.ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期下界
      AND p.ts <  (DATE '2026-11-05')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期上界
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '10:00:00'
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '16:00:00'
    GROUP BY d.station_id, p.device_id, d.station_type, p.point_code, date_trunc('second', p.ts)
),
bm AS (
    SELECT
        site_id,
        device_id,
        device_type,
        point_code,
        -- date_trunc('second', ts) AS ts,                        -- ★粒度开关① 秒级
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 5) * interval '5 minute'
        --                                 AS ts,                 -- ★粒度开关② 5 分钟
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 10) * interval '10 minute'
        --                                 AS ts,                 -- ★粒度开关③ 10 分钟
        date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
            AT TIME ZONE 'Asia/Shanghai'            AS ts,        -- ★粒度开关④ 1 小时 (当前)
        AVG(CASE WHEN point_value <= 15 THEN point_value END)
            AS point_value,
        SUM(CASE WHEN point_value <= 15 THEN 1 ELSE 0 END)
            AS valid_cnt,
        SUM(CASE WHEN point_value = 0 THEN 1 ELSE 0 END)
            AS zero_cnt,
        SUM(CASE WHEN point_value < 0 OR point_value > 15 THEN 1 ELSE 0 END)
            AS exc_cnt,
        COUNT(DISTINCT date_trunc('second', ts)) AS bucket_num,
        MAX(ms_bucket_count)                     AS ms_bucket_count,
        MAX(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_max,
        MIN(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_min
    FROM v
    GROUP BY site_id, device_id, device_type, point_code,
             date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
                 AT TIME ZONE 'Asia/Shanghai'
),
devcfg AS (
    SELECT DISTINCT ON (lower(inverter_id))
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, inverter_name
),
base AS (
    SELECT
        t1.region_company_name                          AS region_company_name,
        t1.station_name                                 AS station_name,
        v.site_id                                       AS site_id,
        v.device_id                                     AS device_id,
        v.device_type                                   AS device_type,
        d.inverter_name                                 AS inverter_name,
        v.ts                                            AS ts,
        MAX(v.bucket_num)                               AS bucket_num,
        MAX(v.ms_bucket_count)                          AS ms_bucket_count,
        COUNT(DISTINCT v.point_code)                    AS currentBranch_count,
        SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)
                                                        AS normal_cnt,
        SUM(CASE WHEN v.bucket_num = v.zero_cnt AND v.zero_cnt > 0 THEN 1 ELSE 0 END)
                                                        AS opencircuit_cnt,
        SUM(v.exc_cnt)                                  AS exceptionCount,
        MIN(v.sec_min)                                  AS i_min,
        MAX(v.sec_max)                                  AS i_max,
        AVG(v.point_value)                              AS i_avg,
        CASE WHEN SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END) >= 2
               AND AVG(v.point_value) <> 0           -- ★ 防除零 (0A 整桶)
             THEN sqrt(GREATEST(0::float8, (SUM(v.point_value * v.point_value) - SUM(v.point_value) * SUM(v.point_value) / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)) / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)))
                  / abs(AVG(v.point_value)) * 100.0
             ELSE NULL END                              AS cv_numeric
    FROM bm v
    JOIN t1 ON t1.station_id = v.site_id
    LEFT JOIN devcfg d ON lower(d.inverter_id) = lower(v.device_id)
    GROUP BY t1.region_company_name, t1.station_name, v.site_id, v.device_id,
             v.device_type, d.inverter_name, v.ts
),
agg AS (                                                       -- 全周期占比聚合: 每台逆变器一行
    SELECT
        region_company_name,
        station_name,
        site_id,
        device_id,
        device_type,
        MAX(inverter_name)                        AS inverter_name,
        COUNT(*)                                  AS total_valid_points,
        SUM(CASE WHEN cv_numeric > 15.0 THEN 1 ELSE 0 END)   AS over_points,
        round(SUM(CASE WHEN cv_numeric > 15.0 THEN 1 ELSE 0 END) * 100.0
              / COUNT(*), 2)                      AS over_ratio
    FROM base
    WHERE cv_numeric IS NOT NULL
    GROUP BY region_company_name, station_name, site_id, device_id, device_type
)
SELECT                                                         -- 只保留占比超阈值的"问题逆变器"
    region_company_name,
    station_name,
    site_id,
    device_id,
    device_type,
    inverter_name,
    total_valid_points,
    over_points,
    over_ratio
FROM agg
WHERE over_ratio > 20                                          -- ★占比阈值 (同查询 16 的 HAVING)
ORDER BY over_ratio DESC, region_company_name;


-- ############################################################
-- 查询 22: 问题逆变器的超标快照明细 (查询 20 明细 ∩ 查询 21 问题设备 ∩ CV>15)
--   WITH 链 (t1→v→bm→devcfg→base→agg) 与查询 21 逐字一致 (即与查询 20 同源)
--   bad = 查询 21 的输出 (agg WHERE over_ratio > 20), 不重写阈值
--   明细 INNER JOIN bad (仅问题逆变器) AND base.cv_numeric > 15.0
--   JOIN 键 = agg 的 5 个 GROUP BY 维度 (region/station/site/device/device_type);
--   不用 inverter_name (未登记设备为 NULL, NULL=NULL 关联不上); 键在 agg 内唯一, JOIN 不膨胀
--   输出列与查询 20 相同 (17 列, 已删除 branchTotal); 排序: 占比降序 -> 省公司 -> 桶时刻
-- ############################################################
WITH t1 AS (
    SELECT DISTINCT ON (station_id)
           station_id, station_name, station_type, region_company_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name, station_type, region_company_name
),
v AS (
    SELECT
        d.station_id                        AS site_id,
        p.device_id                         AS device_id,
        d.station_type                      AS device_type,
        p.point_code                        AS point_code,
        date_trunc('second', p.ts)          AS ts,
        AVG(p.point_value)                  AS point_value,
        COUNT(DISTINCT p.ts)                AS ms_bucket_count
    FROM pv_string_branch p
    JOIN (
        SELECT DISTINCT ON (lower(inverter_id))
               station_id, station_type, inverter_id
        FROM pv_station_invert
        WHERE constituent_name = '国电电力'
          AND inverter_id IS NOT NULL
          AND inverter_id <> ''
        ORDER BY lower(inverter_id), station_id, station_type
    ) d ON lower(d.inverter_id) = lower(p.device_id)
    WHERE upper(p.point_code) ~ '^STRING_BRANCH[0-9]+_A$'
      AND p.ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期下界
      AND p.ts <  (DATE '2026-11-05')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期上界
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '10:00:00'
      AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '16:00:00'
    GROUP BY d.station_id, p.device_id, d.station_type, p.point_code, date_trunc('second', p.ts)
),
bm AS (
    SELECT
        site_id,
        device_id,
        device_type,
        point_code,
        -- date_trunc('second', ts) AS ts,                        -- ★粒度开关① 秒级
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 5) * interval '5 minute'
        --                                 AS ts,                 -- ★粒度开关② 5 分钟
        -- date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
        --   + floor(extract(minute from ts AT TIME ZONE 'Asia/Shanghai')::int / 10) * interval '10 minute'
        --                                 AS ts,                 -- ★粒度开关③ 10 分钟
        date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
            AT TIME ZONE 'Asia/Shanghai'            AS ts,        -- ★粒度开关④ 1 小时 (当前)
        AVG(CASE WHEN point_value <= 15 THEN point_value END)
            AS point_value,
        SUM(CASE WHEN point_value <= 15 THEN 1 ELSE 0 END)
            AS valid_cnt,
        SUM(CASE WHEN point_value = 0 THEN 1 ELSE 0 END)
            AS zero_cnt,
        SUM(CASE WHEN point_value < 0 OR point_value > 15 THEN 1 ELSE 0 END)
            AS exc_cnt,
        COUNT(DISTINCT date_trunc('second', ts)) AS bucket_num,
        MAX(ms_bucket_count)                     AS ms_bucket_count,
        MAX(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_max,
        MIN(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_min
    FROM v
    GROUP BY site_id, device_id, device_type, point_code,
             date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
                 AT TIME ZONE 'Asia/Shanghai'
),
devcfg AS (
    SELECT DISTINCT ON (lower(inverter_id))
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, inverter_name
),
base AS (
    SELECT
        t1.region_company_name                          AS region_company_name,
        t1.station_name                                 AS station_name,
        v.site_id                                       AS site_id,
        v.device_id                                     AS device_id,
        v.device_type                                   AS device_type,
        d.inverter_name                                 AS inverter_name,
        v.ts                                            AS ts,
        MAX(v.bucket_num)                               AS bucket_num,
        MAX(v.ms_bucket_count)                          AS ms_bucket_count,
        COUNT(DISTINCT v.point_code)                    AS currentBranch_count,
        SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)
                                                        AS normal_cnt,
        SUM(CASE WHEN v.bucket_num = v.zero_cnt AND v.zero_cnt > 0 THEN 1 ELSE 0 END)
                                                        AS opencircuit_cnt,
        SUM(v.exc_cnt)                                  AS exceptionCount,
        MIN(v.sec_min)                                  AS i_min,
        MAX(v.sec_max)                                  AS i_max,
        AVG(v.point_value)                              AS i_avg,
        CASE WHEN SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END) >= 2
               AND AVG(v.point_value) <> 0           -- ★ 防除零 (0A 整桶)
             THEN sqrt(GREATEST(0::float8, (SUM(v.point_value * v.point_value) - SUM(v.point_value) * SUM(v.point_value) / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)) / SUM(CASE WHEN v.valid_cnt > 0 THEN 1 ELSE 0 END)))
                  / abs(AVG(v.point_value)) * 100.0
             ELSE NULL END                              AS cv_numeric
    FROM bm v
    JOIN t1 ON t1.station_id = v.site_id
    LEFT JOIN devcfg d ON lower(d.inverter_id) = lower(v.device_id)
    GROUP BY t1.region_company_name, t1.station_name, v.site_id, v.device_id,
             v.device_type, d.inverter_name, v.ts
),
agg AS (
    SELECT
        region_company_name,
        station_name,
        site_id,
        device_id,
        device_type,
        MAX(inverter_name)                        AS inverter_name,
        COUNT(*)                                  AS total_valid_points,
        SUM(CASE WHEN cv_numeric > 15.0 THEN 1 ELSE 0 END)   AS over_points,
        round(SUM(CASE WHEN cv_numeric > 15.0 THEN 1 ELSE 0 END) * 100.0
              / COUNT(*), 2)                      AS over_ratio
    FROM base
    WHERE cv_numeric IS NOT NULL
    GROUP BY region_company_name, station_name, site_id, device_id, device_type
)
,
bad AS (                                                       -- ★问题逆变器集合 = 查询 21 的输出
    SELECT region_company_name, station_name, site_id, device_id, device_type,
           inverter_name, total_valid_points, over_points, over_ratio
    FROM agg
    WHERE over_ratio > 20                                      -- 占比阈值与查询 21 外层 WHERE 完全一致
)
SELECT                                                         -- 输出列与查询 20 相同 (17 列)
    b.region_company_name,
    b.station_name,
    b.site_id,
    b.device_id,
    b.device_type,
    b.inverter_name,
    b.ts AT TIME ZONE 'Asia/Shanghai'               AS ts,
    b.bucket_num,
    b.ms_bucket_count,
    b.currentBranch_count,
    b.normal_cnt,
    b.opencircuit_cnt,
    b.exceptionCount,
    CASE WHEN b.i_min IS NOT NULL THEN to_char(b.i_min, 'FM999999990.000') ELSE NULL END AS i_min,
    CASE WHEN b.i_max IS NOT NULL THEN to_char(b.i_max, 'FM999999990.000') ELSE NULL END AS i_max,
    CASE WHEN b.i_avg IS NOT NULL THEN to_char(b.i_avg, 'FM999999990.000') ELSE NULL END AS i_avg,
    CASE WHEN b.cv_numeric IS NOT NULL
         THEN to_char(b.cv_numeric, 'FM999999990.00')
         ELSE NULL END                              AS dispersionRatio
FROM base b
JOIN bad                                                       -- ★INNER: 只保留问题逆变器
  ON b.region_company_name = bad.region_company_name
 AND b.station_name        = bad.station_name
 AND b.site_id             = bad.site_id
 AND b.device_id           = bad.device_id
 AND b.device_type         = bad.device_type
WHERE b.cv_numeric > 15.0                                       -- ★只保留超标快照
ORDER BY bad.over_ratio DESC,                                   -- 问题最严重的设备排最前
         b.region_company_name,
         b.ts
LIMIT 10000;


-- ############################################################
-- 查询 23: 支路电流异常时次统计 (三种异常, 按设备×1小时窗口)
--   口径: 参考查询 20 的 WITH 链 (t1→v→bm→devcfg→base)
--   统计三种异常:
--     (1) 电流 < 0 (负值) —— 有负值秒的支路数
--     (2) 电流 = 0 (开路) —— 有 0A 秒的支路数
--     (3) 窗口内 max - min > 2 × avg (波动异常) —— 按支路判断
--   所有统计均考虑 x <= 15 (含 0A 和负值), >15A 不参与
--   时间窗口: 10:00-11:00, 14:00-15:00, 15:00-16:00 (3 个 1 小时窗口)
--   输出粒度: 设备 × 时次 (每行 = 一台逆变器在一个 1 小时窗口的异常汇总)
--   ★原版 branchTotal 列已删除
-- ############################################################
WITH t1 AS (
    SELECT DISTINCT ON (station_id)
           station_id, station_name, station_type, region_company_name
    FROM pv_station_invert
    WHERE constituent_name = '国电电力'
    ORDER BY station_id, station_name, station_type, region_company_name
),
v AS (                                                         -- 秒级层: 按"电站+设备+支路+秒"聚合
    SELECT
        d.station_id                        AS site_id,
        p.device_id                         AS device_id,
        d.station_type                      AS device_type,
        p.point_code                        AS point_code,
        date_trunc('second', p.ts)          AS ts,
        AVG(p.point_value)                  AS point_value,
        COUNT(DISTINCT p.ts)                AS ms_bucket_count
    FROM pv_string_branch p
    JOIN (
        SELECT DISTINCT ON (lower(inverter_id))
               station_id, station_type, inverter_id
        FROM pv_station_invert
        WHERE constituent_name = '国电电力'
          AND inverter_id IS NOT NULL
          AND inverter_id <> ''
        ORDER BY lower(inverter_id), station_id, station_type
    ) d ON lower(d.inverter_id) = lower(p.device_id)
    WHERE upper(p.point_code) ~ '^STRING_BRANCH[0-9]+_A$'
      AND p.ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期下界
      AND p.ts <  (DATE '2026-11-05')::timestamp AT TIME ZONE 'Asia/Shanghai'  -- ⚙ 日期上界
      AND ( ( (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '10:00:00'
         AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '11:00:00' )    -- 窗口1: 10-11
       OR ( (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '14:00:00'
        AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '15:00:00' )     -- 窗口2: 14-15
       OR ( (p.ts AT TIME ZONE 'Asia/Shanghai')::time >= TIME '15:00:00'
        AND (p.ts AT TIME ZONE 'Asia/Shanghai')::time <  TIME '16:00:00' ) )   -- 窗口3: 15-16
    GROUP BY d.station_id, p.device_id, d.station_type, p.point_code, date_trunc('second', p.ts)
),
bm AS (                                                        -- ★1小时窗口桶化
    SELECT
        site_id,
        device_id,
        device_type,
        point_code,
        date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
            AT TIME ZONE 'Asia/Shanghai'            AS ts,        -- 1小时桶起始 (如 10:00:00)
        AVG(CASE WHEN point_value <= 15 THEN point_value END)
            AS point_value,
        SUM(CASE WHEN point_value < 0 THEN 1 ELSE 0 END)
            AS neg_cnt,                                           -- 负值秒数
        SUM(CASE WHEN point_value = 0 THEN 1 ELSE 0 END)
            AS zero_cnt,                                          -- 0A 开路秒数
        SUM(CASE WHEN point_value <= 15 THEN 1 ELSE 0 END)
            AS valid_cnt,
        SUM(CASE WHEN point_value < 0 OR point_value > 15 THEN 1 ELSE 0 END)
            AS exc_cnt,
        COUNT(DISTINCT date_trunc('second', ts)) AS bucket_num,
        MAX(ms_bucket_count)                     AS ms_bucket_count,
        MAX(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_max,
        MIN(CASE WHEN point_value <= 15 THEN point_value END)
            AS sec_min
    FROM v
    GROUP BY site_id, device_id, device_type, point_code,
             date_trunc('hour', ts AT TIME ZONE 'Asia/Shanghai')
                 AT TIME ZONE 'Asia/Shanghai'
),
devcfg AS (
    SELECT DISTINCT ON (lower(inverter_id))
           inverter_id, inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY lower(inverter_id), station_id, inverter_name
),
base AS (                                                      -- 设备×时次聚合: 跨支路统计三种异常
    SELECT
        t1.region_company_name                          AS region_company_name,
        t1.station_name                                 AS station_name,
        v.site_id                                       AS site_id,
        v.device_id                                     AS device_id,
        v.device_type                                   AS device_type,
        d.inverter_name                                 AS inverter_name,
        v.ts                                            AS ts,
        CASE WHEN to_char(v.ts AT TIME ZONE 'Asia/Shanghai', 'HH24') = '10' THEN '10:00-11:00'
             WHEN to_char(v.ts AT TIME ZONE 'Asia/Shanghai', 'HH24') = '14' THEN '14:00-15:00'
             WHEN to_char(v.ts AT TIME ZONE 'Asia/Shanghai', 'HH24') = '15' THEN '15:00-16:00'
        END                                             AS time_window,
        COUNT(DISTINCT v.point_code)                    AS total_branch_cnt,
        SUM(CASE WHEN v.neg_cnt > 0 THEN 1 ELSE 0 END)  AS neg_branch_cnt,
        SUM(CASE WHEN v.zero_cnt > 0 THEN 1 ELSE 0 END) AS zero_branch_cnt,
        SUM(CASE WHEN v.sec_max IS NOT NULL AND v.sec_min IS NOT NULL
                  AND v.point_value IS NOT NULL
                  AND (v.sec_max - v.sec_min) > 2 * v.point_value
                 THEN 1 ELSE 0 END)                     AS fluct_branch_cnt,
        SUM(v.neg_cnt)                                  AS total_neg_seconds,
        SUM(v.zero_cnt)                                 AS total_zero_seconds,
        SUM(v.exc_cnt)                                  AS total_exc_seconds,
        MIN(v.sec_min)                                  AS i_min,
        MAX(v.sec_max)                                  AS i_max,
        AVG(v.point_value)                              AS i_avg
    FROM bm v
    JOIN t1 ON t1.station_id = v.site_id
    LEFT JOIN devcfg d ON lower(d.inverter_id) = lower(v.device_id)
    GROUP BY t1.region_company_name, t1.station_name, v.site_id, v.device_id,
             v.device_type, d.inverter_name, v.ts
)
SELECT
    region_company_name,
    station_name,
    site_id,
    device_id,
    device_type,
    inverter_name,
    ts AT TIME ZONE 'Asia/Shanghai'                 AS ts,        -- 窗口起始时刻
    time_window,
    total_branch_cnt,
    neg_branch_cnt,
    zero_branch_cnt,
    fluct_branch_cnt,
    total_neg_seconds,
    total_zero_seconds,
    total_exc_seconds,
    CASE WHEN i_min IS NOT NULL THEN to_char(i_min, 'FM999999990.000') ELSE NULL END AS i_min,
    CASE WHEN i_max IS NOT NULL THEN to_char(i_max, 'FM999999990.000') ELSE NULL END AS i_max,
    CASE WHEN i_avg IS NOT NULL THEN to_char(i_avg, 'FM999999990.000') ELSE NULL END AS i_avg
FROM base
WHERE neg_branch_cnt > 0 OR zero_branch_cnt > 0 OR fluct_branch_cnt > 0  -- ★至少一类异常
ORDER BY region_company_name, station_name, device_id, ts
LIMIT 10000;
