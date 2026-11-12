-- ============================================================================
-- pv_string_branch.sql  光伏支路偏差率与报警（5 分钟快照 / 4 个采集窗口）
-- ----------------------------------------------------------------------------
-- 数据源 : pv_string_branch（秒级原始，ts 日分区）
-- 架构   : 原始秒级 -> pv_string_branch_5min（支路×5分钟汇总, bucket 按天分区）-> 快照明细 / 窗口报警
-- 窗口   : 10:00-11:00 / 12:00-13:00 / 14:00-15:00 / 15:00-16:00 (Asia/Shanghai)
-- 快照   : 每个窗口 12 个 5 分钟桶，桶边界 :00/:05/.../:55
-- 20 分钟: 每个窗口固定 3 块（:00-:20 / :20-:40 / :40-:00），不滚动
--
-- 定稿口径（务必与业务确认一致后再改）:
--   1. 电流 = 0 的原始行最先剔除，不参与均值/标准差/偏差率/任何条数；
--      但 zero_cnt 仍写入汇总表，用于单独的零电流（断路）清单。
--   2. 不设上限，所有非零电流（含负值、>15A）均参与计算。
--   3. 组串平均电流 = 同一快照下各支路均值的等权平均（一支路一票）。
--   4. CV 分母带符号：cv = stddev_pop(i_b)/i_mean*100，不做 abs。
--   5. 异常条数分档互斥：>=5% 列只统计 5%<=dev<10%，>=10% 列单独统计。
--   6. 零值判定 = 偏差率为 0（|dev_pct| <= 1e-6 个百分点，等价于 i_b = i_mean，
--      留容差以免 float8 累加误差 1e-16~1e-14 造成漏判），且同组串同快照存在其他异常支路才计入。
--   7. 逆变器即组串（device_id / inverter_name）。
--   8. 已被 pv_inverter_branch_abnormal 标记的逆变器+支路（branch_name 含"支路N"）
--      不参与统计计算与输出（参考 pv_to_excel.py）；在查询层 NOT EXISTS 排除，
--      标记状态实时生效，汇总表 pv_string_branch_5min 仍保留其数据以备核查。
--
-- 执行顺序:
--   §1 建表（一次性）
--   §2 汇总表刷新（采集时段每 5 分钟；窗口结束后再补一次最近 90 分钟）
--   §3 查询 A：快照明细（支路 × 5 分钟）
--   §3-A 查询 A 原始表版：直接聚合 pv_string_branch，仅出快照级指标（不含 20 分钟块）
--   §4 查询 B：窗口报警（支路 × 1 小时）
--   §5 查询 C：零电流（断路）清单（支路 × 20 分钟块）
--
-- ★ 所有日期字面量、阈值均为占位符，换日期/调阈值只需全局替换。
-- ★ 大范围查询前执行: SET statement_timeout = 0;
-- ============================================================================


-- ============================================================================
-- 0. 参数与时间表达式（★ 可调，SQL 内以字面量出现）
-- ----------------------------------------------------------------------------
--   低电流门槛        |i_mean| < 0.5 A        -> 快照标记 low_current，不计算偏差率
--   最小有效支路数    2
--   单支路单桶最少采样 3（5 分钟桶通常 15~22 个点）
--   偏差率分档        5% / 10% / -5%
--   20 分钟极差阈值   20%
--   20 分钟突变阈值   10 个百分点（rate = 10/15 ≈ 0.667 %/m，分母取块内首末快照实际跨度 15 分钟）
-- ============================================================================


-- ============================================================================
-- 1. 5 分钟汇总表 DDL（一次性执行）
-- ----------------------------------------------------------------------------
-- 粒度必须保持 5 分钟：窗口报警的条数按 5 分钟快照计数，变化速率需要块首/块尾快照。
-- 20 分钟固定块由 5 分钟桶派生（floor(epoch/1200)），不单独建表。
-- 汇总表按 bucket 天级 RANGE 分区（pv_string_branch_5min_YYYYMMDD），ensure_partition 幂等自动建分区。
-- ============================================================================
CREATE TABLE IF NOT EXISTS pv_string_branch_5min (
    device_id   text        NOT NULL,
    point_code  text        NOT NULL,
    bucket      timestamptz NOT NULL,   -- 5 分钟桶起点 :00/:05/...
    sample_cnt  int         NOT NULL,   -- point_value <> 0 的采样数（参与均值，无上限）
    zero_cnt    int         NOT NULL,   -- point_value = 0 的采样数（仅记录，不参与计算）
    i_avg       float8,                 -- 支路桶均值；全零 / 全脏 -> NULL
    sec_max     float8,                 -- 桶内非零有效采样最大值
    sec_min     float8,                 -- 桶内非零有效采样最小值
    PRIMARY KEY (device_id, point_code, bucket)
) PARTITION BY RANGE (bucket);  -- 按天分区；分区键 bucket 已包含在主键内

CREATE INDEX IF NOT EXISTS idx_pv_string_branch_5min_bucket
    ON pv_string_branch_5min (bucket);      -- 父表建索引, 自动传播到所有分区

-- 日分区自动创建（幂等）：§2.1 每次刷新前调用；历史回填前对目标日期调用
CREATE OR REPLACE FUNCTION pv_string_branch_5min_ensure_partition(p_day date)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    v_part text := 'pv_string_branch_5min_' || to_char(p_day, 'YYYYMMDD');
BEGIN
    IF to_regclass(v_part) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF pv_string_branch_5min FOR VALUES FROM (%L) TO (%L)',
            v_part, p_day, p_day + 1);
        RETURN v_part || ' (created)';
    END IF;
    RETURN v_part || ' (exists)';
END;
$$;

COMMENT ON TABLE  pv_string_branch_5min IS '光伏支路 5 分钟汇总：非零电流参与统计，零电流仅计数用于断路清单';
COMMENT ON COLUMN pv_string_branch_5min.i_avg      IS '支路桶均值，来源 point_value<>0；可含负值、可>15A';
COMMENT ON COLUMN pv_string_branch_5min.zero_cnt   IS 'point_value=0 的采样数，不参与任何均值/偏差率计算';

-- ============================================================================
-- 2. 汇总表刷新
-- ----------------------------------------------------------------------------
-- 2.1 常规刷新（采集时段内每 5 分钟执行一次；窗口结束后再执行一次覆盖迟到数据）
--     上游发布延迟 30~80 分钟，因此回看 90 分钟，ON CONFLICT 幂等覆盖。
-- ============================================================================
-- 分区兜底：凌晨跨天窗口需今日与前一日两个分区（幂等, 已存在则跳过）
SELECT pv_string_branch_5min_ensure_partition(CURRENT_DATE);
SELECT pv_string_branch_5min_ensure_partition(CURRENT_DATE - 1);

INSERT INTO pv_string_branch_5min AS t
SELECT device_id, point_code, bucket,
       sample_cnt, zero_cnt, i_avg, sec_max, sec_min
FROM (
    SELECT
        device_id,
        point_code,
        to_timestamp(floor(extract(epoch FROM ts) / 300) * 300) AS bucket,
        count(*) FILTER (WHERE point_value <> 0) AS sample_cnt,
        count(*) FILTER (WHERE point_value  = 0) AS zero_cnt,
        avg(point_value) FILTER (WHERE point_value <> 0) AS i_avg,
        max(point_value) FILTER (WHERE point_value <> 0) AS sec_max,
        min(point_value) FILTER (WHERE point_value <> 0) AS sec_min
    FROM pv_string_branch                       -- ★ 换日期时保留父表名即可自动裁剪分区
    WHERE ts >= to_timestamp(floor(extract(epoch FROM now()) / 300) * 300) - interval '90 min'
      AND ts <  to_timestamp(floor(extract(epoch FROM now()) / 300) * 300)
      AND point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'   -- 1~40（[1-40] 是字符类, 只匹配单数字 1-4）
    GROUP BY 1, 2, 3
) s
ON CONFLICT (device_id, point_code, bucket) DO UPDATE SET
    sample_cnt = EXCLUDED.sample_cnt,
    zero_cnt   = EXCLUDED.zero_cnt,
    i_avg      = EXCLUDED.i_avg,
    sec_max    = EXCLUDED.sec_max,
    sec_min    = EXCLUDED.sec_min;

-- ----------------------------------------------------------------------------
-- 2.2 历史回填模板（按需执行；建议按单日、单窗口分批，避免长事务）
--     把 WHERE 换成目标区间即可；同样是幂等 UPSERT。
--     ★ 回填前先确保目标日分区存在（幂等, 换日期时同步改）：
-- SELECT pv_string_branch_5min_ensure_partition(DATE '2026-11-04');
-- ----------------------------------------------------------------------------
-- INSERT INTO pv_string_branch_5min AS t
-- SELECT device_id, point_code, bucket,
--        sample_cnt, zero_cnt, i_avg, sec_max, sec_min
-- FROM (
--     SELECT device_id, point_code,
--            to_timestamp(floor(extract(epoch FROM ts) / 300) * 300) AS bucket,
--            count(*) FILTER (WHERE point_value <> 0) AS sample_cnt,
--            count(*) FILTER (WHERE point_value  = 0) AS zero_cnt,
--            avg(point_value) FILTER (WHERE point_value <> 0) AS i_avg,
--            max(point_value) FILTER (WHERE point_value <> 0) AS sec_max,
--            min(point_value) FILTER (WHERE point_value <> 0) AS sec_min
--     FROM pv_string_branch_20261104              -- ★ 指定日分区
--     WHERE ts >= TIMESTAMPTZ '2026-11-04 10:00:00+08'
--       AND ts <  TIMESTAMPTZ '2026-11-04 16:00:00+08'
--       AND point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'   -- 1~40
--     GROUP BY 1, 2, 3
-- ) s
-- ON CONFLICT (device_id, point_code, bucket) DO UPDATE SET
--     sample_cnt = EXCLUDED.sample_cnt, zero_cnt = EXCLUDED.zero_cnt,
--     i_avg     = EXCLUDED.i_avg,
--     sec_max    = EXCLUDED.sec_max,    sec_min   = EXCLUDED.sec_min;

-- ★ 大批量回填完成后必须收集一次统计信息；否则规划器误判行数会选嵌套循环，
--   同一查询会从 10 秒级退化到 10 分钟以上（11-04 回填后实测）。
ANALYZE pv_string_branch_5min;


-- ============================================================================
-- 3. 查询 A：快照明细（支路 × 5 分钟）
-- ----------------------------------------------------------------------------
-- 每支路每个 5 分钟快照一行；块级特征（20 分钟变化/速率/极差）来自固定 20 分钟块。
-- 排序：公司 -> 场站 -> 组串 -> 时间 -> 支路号（数值）。
-- ============================================================================
WITH ex AS (                                    -- 已标记异常的逆变器+支路（参考 pv_to_excel.py）
    SELECT DISTINCT
        upper(inverter_id)                                                     AS dev_key,
        'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A'  AS point_code
    FROM pv_inverter_branch_abnormal
    WHERE branch_name ~ '支路[0-9]+'
),
br AS (                                         -- 支路 × 5 分钟（仅有效桶, 排除已标记支路）
    SELECT
        device_id,
        point_code,
        bucket      AS snap,
        i_avg       AS i,
        sample_cnt  AS n,
        zero_cnt
    FROM pv_string_branch_5min b
    WHERE bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08'     -- ★ 日期下界
      AND bucket <  TIMESTAMPTZ '2026-11-04 16:00:00+08'     -- ★ 日期上界
      AND (
            (bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 11:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 12:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 13:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 14:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 15:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 15:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 16:00:00+08')
      )
      AND i_avg IS NOT NULL
      AND sample_cnt >= 3                       -- ★ 支路单桶最少有效采样
      AND NOT EXISTS (                          -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
),
ds AS (                                         -- 设备 × 5 分钟：组串均值 / 标准差 / 极值
    SELECT
        device_id,
        snap,
        avg(i)        AS i_mean,                -- 支路等权平均
        stddev_pop(i) AS i_std,                 -- 总体标准差（分母 n）
        count(*)      AS branch_cnt,
        min(i)        AS i_min,
        max(i)        AS i_max,
        CASE WHEN count(*) >= 2 AND abs(avg(i)) >= 0.5
             THEN NULL ELSE 'low_current' END   AS snap_flag   -- ★ 低电流门槛 0.5A
    FROM br
    GROUP BY device_id, snap
),
bs AS (                                         -- 支路偏差率 / 整体偏差率 / 快照极差
    SELECT
        b.device_id,
        b.point_code,
        b.snap,
        b.i,
        b.zero_cnt,
        d.i_mean,
        d.i_std,
        d.branch_cnt,
        d.i_min,
        d.i_max,
        d.snap_flag,
        to_timestamp(floor(extract(epoch FROM b.snap) / 1200) * 1200) AS block_20m,
        extract(hour FROM b.snap AT TIME ZONE 'Asia/Shanghai')           AS hh,
        CASE WHEN d.snap_flag IS NULL
             THEN (b.i - d.i_mean) / d.i_mean * 100 END AS dev_pct,      -- 带符号
        CASE WHEN d.snap_flag IS NULL
             THEN d.i_std / d.i_mean * 100 END          AS cv_pct,       -- 带符号，不做 abs
        CASE WHEN d.snap_flag IS NULL
             THEN (d.i_max - d.i_min) / d.i_mean * 100 END AS range_pct
    FROM br b
    JOIN ds d USING (device_id, snap)
),
bok AS (                                        -- 仅参与统计的快照（排除 low_current）
    SELECT * FROM bs
    WHERE snap_flag IS NULL AND dev_pct IS NOT NULL
),
bb_win AS (                                     -- 块首 / 块尾 偏差率
    SELECT
        device_id, point_code, block_20m, snap, i, i_mean, dev_pct,
        first_value(dev_pct) OVER w AS dev_first,
        last_value(dev_pct)  OVER w AS dev_last,
        count(dev_pct)       OVER w AS dev_cnt
    FROM bok
    WINDOW w AS (
        PARTITION BY device_id, point_code, block_20m
        ORDER BY snap
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
),
bb AS (                                         -- 支路 × 20 分钟块：变化量 / 变化速率
    SELECT
        device_id,
        point_code,
        block_20m,
        max(dev_first) AS dev_first,
        max(dev_last)  AS dev_last,
        max(dev_cnt)   AS dev_cnt,
        min(snap)      AS block_start,
        max(snap)      AS block_end,
        extract(hour FROM block_20m AT TIME ZONE 'Asia/Shanghai') AS hh,
        CASE WHEN max(dev_cnt) >= 2 THEN max(dev_last) - max(dev_first) END AS chg_20m,
        CASE WHEN max(dev_cnt) >= 2 THEN (max(dev_last) - max(dev_first)) / 15 END AS rate_pm
    FROM bb_win
    GROUP BY device_id, point_code, block_20m
),
dblk AS (                                       -- 支路 × 20 分钟块：单支路时间极值
    SELECT
        device_id,
        point_code,
        block_20m,
        max(i)              AS blk_i_max,
        min(i)              AS blk_i_min,
        count(DISTINCT snap) AS blk_snap_cnt
    FROM bok
    GROUP BY device_id, point_code, block_20m
),
dblk_mean AS (                                  -- 块内组串均值 = 各快照 i_mean 的等权平均
    SELECT
        d.device_id,
        d.point_code,
        d.block_20m,
        d.blk_i_max,
        d.blk_i_min,
        d.blk_snap_cnt,
        avg(s.i_mean) AS blk_mean_i
    FROM dblk d
    JOIN (SELECT DISTINCT device_id, snap, block_20m, i_mean FROM bok) s
      ON s.device_id = d.device_id AND s.block_20m = d.block_20m
    GROUP BY 1, 2, 3, 4, 5, 6
),
dblk_dev AS (                                   -- 20 分钟固定块相对极差
    SELECT
        *,
        CASE WHEN blk_snap_cnt >= 2 AND abs(blk_mean_i) >= 0.5
             THEN (blk_i_max - blk_i_min) / blk_mean_i * 100 END AS range20_pct
    FROM dblk_mean
),
st AS (                                         -- 电站维表
    SELECT station_id, station_name, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, region_company_name
),
si AS (                                         -- 逆变器维表（逆变器 = 组串）
    SELECT DISTINCT ON (upper(inverter_id))
           upper(inverter_id) AS dev_key,
           station_id,
           inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY upper(inverter_id), station_id, inverter_name
)
SELECT
    bs.snap AT TIME ZONE 'Asia/Shanghai'                            AS "时间戳",
    st.region_company_name                                          AS "公司",
    st.station_name                                                 AS "场站",
    si.inverter_name                                                AS "组串编号",
    bs.device_id                                                    AS "逆变器ID",
    substring(bs.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int    AS "支路编号",
    bs.point_code                                                   AS "支路点位",
    round(bs.i::numeric, 3)                                         AS "支路电流",
    round(bs.i_mean::numeric, 3)                                    AS "组串平均电流",
    round(bs.dev_pct::numeric, 2)                                   AS "单支路当前偏差率(%)",
    round(bs.cv_pct::numeric, 2)                                    AS "组串整体偏差率(%)",
    round(bs.range_pct::numeric, 2)                                 AS "组串电流相对极差(%)",
    bb.block_20m AT TIME ZONE 'Asia/Shanghai'                       AS "20分钟块起始",
    round(bb.chg_20m::numeric, 2)                                   AS "20分钟偏差变化(pp)",
    round(bb.rate_pm::numeric, 3)                                   AS "偏差率变化速率(%/m)",
    round(dd.range20_pct::numeric, 2)                               AS "20分钟相对极差(%)"
FROM bs
LEFT JOIN bb
       ON bb.device_id = bs.device_id
      AND bb.point_code = bs.point_code
      AND bb.block_20m = bs.block_20m
LEFT JOIN dblk_dev dd
       ON dd.device_id = bs.device_id
      AND dd.point_code = bs.point_code
      AND dd.block_20m = bs.block_20m
LEFT JOIN si ON si.dev_key = bs.device_id
LEFT JOIN st ON st.station_id = si.station_id
ORDER BY
    st.region_company_name,
    st.station_name,
    si.inverter_name,
    bs.device_id,
    bs.snap,
    substring(bs.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int;


-- ============================================================================
-- 3-A. 查询 A（原始表版）：明细（支路 × 原始采样）
-- ----------------------------------------------------------------------------
-- 与 §3 的区别：
--   ① 数据来源：§3   -> pv_string_branch_5min（5 分钟汇总表，依赖 §2 刷新过）
--                §3-A -> pv_string_branch（原始秒级表）
--   ② 不做 5 分钟分桶：每条原始记录一行，snap = ts、i = point_value 原值，不取平均；
--      point_value = 0 的记录不输出、也不参与组串统计（与 §3 的零值口径一致）
--   ③ 组串指标按"同一时间戳"对齐：仅当多个支路 ts 完全相同时才能算出
--      组串平均电流 / 单支路偏差率 / 组串整体偏差率 / 相对极差；
--      单支路时刻这些列为空。实测各支路采样时点大多不一致，
--      故大部分行组串指标为空属正常现象。
--   ④ 不计算 20 分钟块指标（块起始 / 20 分钟偏差变化 / 速率 / 20 分钟相对极差）。
-- 适用场景：查看原始逐点电流、追溯某时刻各支路读数、或汇总表未覆盖的日期。
--
-- ★ 原始表缺少 §2.1 写入时施加的两道过滤，br 内必须自带：
--     ① point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'
--        （否则电压/功率/其他测点会被一并输出）
--     ② 时间过滤写在 ts 上（触发 ts 日分区裁剪）
--
-- 性能：不再做大聚合与排序；主要成本是扫日分区（实测单日 1.37 亿行 / 27GB）。
--   现有索引（见 create_pv_string_branch.py）：
--     PK (device_id, ts, point_code) / idx_(ts DESC) / idx_(point_code)
--   本查询无 device_id 等值条件，正则 ~ 又不走 point_code 索引，
--   实际只有 idx_(ts DESC) 做范围扫，device_id/point_code/point_value 逐行回表。
--   → 需要提速时加一条覆盖索引（前导列取过滤列 ts，其余列 INCLUDE 免回表）。
--     父表建索引会自动下推到全部现有与未来分区；非 CONCURRENTLY，
--     建索引期间阻塞写入，务必在低峰执行：
--   CREATE INDEX IF NOT EXISTS idx_pv_string_branch_ts_cover
--       ON pv_string_branch (ts) INCLUDE (device_id, point_code, point_value);
--   → 之后必须 ANALYZE（大分区缺统计信息时规划器易选错，退化比缺索引更严重）：
--   ANALYZE pv_string_branch;
--   → 若只是偶发查询、单次几分钟可接受，可不加索引直接跑；先
--     EXPLAIN (ANALYZE, BUFFERS) 看实际耗时与 Heap Fetches 再决定。
-- ============================================================================
WITH ex AS (                                    -- 已标记异常的逆变器+支路（与 §3 一致）
    SELECT DISTINCT
        upper(inverter_id)                                                     AS dev_key,
        'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A'  AS point_code
    FROM pv_inverter_branch_abnormal
    WHERE branch_name ~ '支路[0-9]+'
),
br AS (                                         -- 支路 × 原始采样：不分桶，保持原始时间戳
    SELECT
        b.device_id,
        b.point_code,
        b.ts           AS snap,                 -- ★ 原始时间戳
        b.point_value  AS i                     -- ★ 原始电流值，不做平均
    FROM pv_string_branch b                     -- ★ 原始秒级表（ts 日分区）
    WHERE b.ts >= TIMESTAMPTZ '2026-11-04 10:00:00+08'     -- ★ 日期下界
      AND b.ts <  TIMESTAMPTZ '2026-11-04 16:00:00+08'     -- ★ 日期上界
      AND (
            (b.ts >= TIMESTAMPTZ '2026-11-04 10:00:00+08' AND b.ts < TIMESTAMPTZ '2026-11-04 11:00:00+08')
         OR (b.ts >= TIMESTAMPTZ '2026-11-04 12:00:00+08' AND b.ts < TIMESTAMPTZ '2026-11-04 13:00:00+08')
         OR (b.ts >= TIMESTAMPTZ '2026-11-04 14:00:00+08' AND b.ts < TIMESTAMPTZ '2026-11-04 15:00:00+08')
         OR (b.ts >= TIMESTAMPTZ '2026-11-04 15:00:00+08' AND b.ts < TIMESTAMPTZ '2026-11-04 16:00:00+08')
      )
      AND b.point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'   -- ★ 1~40（原表无此过滤）
      AND b.point_value <> 0                    -- ★ 保留零值过滤：0A 不输出、不参与统计
      AND NOT EXISTS (                          -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
),
ds AS (                                         -- 设备 × 同一时间戳：组串均值 / 标准差 / 极值
    SELECT
        device_id,
        snap,
        avg(i)        AS i_mean,                -- 支路等权平均（仅同 ts 的支路）
        stddev_pop(i) AS i_std,                 -- 总体标准差（分母 n）
        count(*)      AS branch_cnt,
        min(i)        AS i_min,
        max(i)        AS i_max,
        CASE WHEN count(*) >= 2 AND abs(avg(i)) >= 0.5
             THEN NULL ELSE 'low_current' END   AS snap_flag   -- 同 ts 支路不足 2 条或均值过小
    FROM br
    GROUP BY device_id, snap
),
bs AS (                                         -- 支路偏差率 / 整体偏差率 / 快照极差
    SELECT
        b.device_id,
        b.point_code,
        b.snap,
        b.i,
        d.i_mean,
        d.i_std,
        d.branch_cnt,
        d.i_min,
        d.i_max,
        d.snap_flag,
        CASE WHEN d.snap_flag IS NULL
             THEN (b.i - d.i_mean) / d.i_mean * 100 END AS dev_pct,      -- 带符号
        CASE WHEN d.snap_flag IS NULL
             THEN d.i_std / d.i_mean * 100 END          AS cv_pct,       -- 带符号，不做 abs
        CASE WHEN d.snap_flag IS NULL
             THEN (d.i_max - d.i_min) / d.i_mean * 100 END AS range_pct
    FROM br b
    JOIN ds d USING (device_id, snap)
),
st AS (                                         -- 电站维表
    SELECT station_id, station_name, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, region_company_name
),
si AS (                                         -- 逆变器维表（逆变器 = 组串）
    SELECT DISTINCT ON (upper(inverter_id))
           upper(inverter_id) AS dev_key,
           station_id,
           inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY upper(inverter_id), station_id, inverter_name
)
SELECT
    bs.snap AT TIME ZONE 'Asia/Shanghai'                            AS "时间戳",
    st.region_company_name                                          AS "公司",
    st.station_name                                                 AS "场站",
    si.inverter_name                                                AS "组串编号",
    bs.device_id                                                    AS "逆变器ID",
    substring(bs.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int    AS "支路编号",
    bs.point_code                                                   AS "支路点位",
    round(bs.i::numeric, 3)                                         AS "支路电流",
    round(bs.i_mean::numeric, 3)                                    AS "组串平均电流",
    round(bs.dev_pct::numeric, 2)                                   AS "单支路当前偏差率(%)",
    round(bs.cv_pct::numeric, 2)                                    AS "组串整体偏差率(%)",
    round(bs.range_pct::numeric, 2)                                 AS "组串电流相对极差(%)"
FROM bs
LEFT JOIN si ON si.dev_key = bs.device_id
LEFT JOIN st ON st.station_id = si.station_id
ORDER BY
    st.region_company_name,
    st.station_name,
    si.inverter_name,
    bs.device_id,
    bs.snap,
    substring(bs.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int;


-- ============================================================================
-- 4. 查询 B：窗口报警（支路 × 1 小时）
-- ----------------------------------------------------------------------------
-- 行集合以汇总表中出现过的支路为准（含整窗口全 0A、无有效均值的支路），
-- 保证“断路”支路不会因为被排除在均值计算之外而从报警表消失。
-- ============================================================================
WITH ex AS (                                    -- 已标记异常的逆变器+支路（参考 pv_to_excel.py）
    SELECT DISTINCT
        upper(inverter_id)                                                     AS dev_key,
        'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A'  AS point_code
    FROM pv_inverter_branch_abnormal
    WHERE branch_name ~ '支路[0-9]+'
),
br AS (
    SELECT
        device_id,
        point_code,
        bucket      AS snap,
        i_avg       AS i,
        sample_cnt  AS n,
        zero_cnt
    FROM pv_string_branch_5min b
    WHERE bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08'     -- ★ 日期下界
      AND bucket <  TIMESTAMPTZ '2026-11-04 16:00:00+08'     -- ★ 日期上界
      AND (
            (bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 11:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 12:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 13:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 14:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 15:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 15:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 16:00:00+08')
      )
      AND i_avg IS NOT NULL
      AND sample_cnt >= 3
      AND NOT EXISTS (                          -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
),
ds AS (
    SELECT
        device_id,
        snap,
        avg(i)        AS i_mean,
        stddev_pop(i) AS i_std,
        count(*)      AS branch_cnt,
        min(i)        AS i_min,
        max(i)        AS i_max,
        CASE WHEN count(*) >= 2 AND abs(avg(i)) >= 0.5
             THEN NULL ELSE 'low_current' END AS snap_flag
    FROM br
    GROUP BY device_id, snap
),
bs AS (
    SELECT
        b.device_id,
        b.point_code,
        b.snap,
        b.i,
        d.i_mean,
        d.i_std,
        d.snap_flag,
        to_timestamp(floor(extract(epoch FROM b.snap) / 1200) * 1200) AS block_20m,
        extract(hour FROM b.snap AT TIME ZONE 'Asia/Shanghai')           AS hh,
        CASE WHEN d.snap_flag IS NULL
             THEN (b.i - d.i_mean) / d.i_mean * 100 END AS dev_pct,
        CASE WHEN d.snap_flag IS NULL
             THEN d.i_std / d.i_mean * 100 END          AS cv_pct,
        CASE WHEN d.snap_flag IS NULL
             THEN (d.i_max - d.i_min) / d.i_mean * 100 END AS range_pct
    FROM br b
    JOIN ds d USING (device_id, snap)
),
bok AS (
    SELECT * FROM bs
    WHERE snap_flag IS NULL AND dev_pct IS NOT NULL
),
snap_abn AS (                                   -- 每个快照的异常支路数（用于零值条件⑥）
    SELECT device_id, snap, count(*) AS abn_cnt
    FROM bok
    WHERE dev_pct >= 5 OR dev_pct <= -5 OR dev_pct >= 100 OR dev_pct < -1e-6
                                          -- ★ 负值判定留 1e-6 个百分点容差：
                                          --   avg(float8) 累加误差可达 1e-14，恒流支路会被误判为负值
    GROUP BY device_id, snap
),
bi AS (
    SELECT b.*, COALESCE(a.abn_cnt, 0) AS abn_cnt
    FROM bok b
    LEFT JOIN snap_abn a USING (device_id, snap)
),
bb_win AS (
    SELECT
        device_id, point_code, block_20m, snap, i, i_mean, dev_pct,
        first_value(dev_pct) OVER w AS dev_first,
        last_value(dev_pct)  OVER w AS dev_last,
        count(dev_pct)       OVER w AS dev_cnt
    FROM bok
    WINDOW w AS (
        PARTITION BY device_id, point_code, block_20m
        ORDER BY snap
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
    )
),
bb AS (
    SELECT
        device_id,
        point_code,
        block_20m,
        max(dev_first) AS dev_first,
        max(dev_last)  AS dev_last,
        max(dev_cnt)   AS dev_cnt,
        min(snap)      AS block_start,
        max(snap)      AS block_end,
        extract(hour FROM block_20m AT TIME ZONE 'Asia/Shanghai') AS hh,
        CASE WHEN max(dev_cnt) >= 2 THEN max(dev_last) - max(dev_first) END AS chg_20m,
        CASE WHEN max(dev_cnt) >= 2 THEN (max(dev_last) - max(dev_first)) / 15 END AS rate_pm
    FROM bb_win
    GROUP BY device_id, point_code, block_20m
),
bwin AS (                                       -- 支路 × 窗口：变化速率极值
    SELECT
        device_id,
        point_code,
        hh,
        max(abs(chg_20m)) AS chg_20m_max,
        max(abs(rate_pm)) AS rate_max_pm
    FROM bb
    GROUP BY device_id, point_code, hh
),
dblk AS (                                       -- 支路 × 20 分钟块：单支路时间极值
    SELECT
        device_id,
        point_code,
        block_20m,
        max(i)               AS blk_i_max,
        min(i)               AS blk_i_min,
        count(DISTINCT snap) AS blk_snap_cnt
    FROM bok
    GROUP BY device_id, point_code, block_20m
),
dblk_mean AS (
    SELECT
        d.device_id, d.point_code, d.block_20m,
        d.blk_i_max, d.blk_i_min, d.blk_snap_cnt,
        avg(s.i_mean) AS blk_mean_i
    FROM dblk d
    JOIN (SELECT DISTINCT device_id, snap, block_20m, i_mean FROM bok) s
      ON s.device_id = d.device_id AND s.block_20m = d.block_20m
    GROUP BY 1, 2, 3, 4, 5, 6
),
dblk_dev AS (
    SELECT
        *,
        CASE WHEN blk_snap_cnt >= 2 AND abs(blk_mean_i) >= 0.5
             THEN (blk_i_max - blk_i_min) / blk_mean_i * 100 END AS range20_pct
    FROM dblk_mean
),
jump AS (                                       -- 支路 × 窗口：条件③ 触发块数（0~3）
    SELECT
        device_id,
        point_code,
        extract(hour FROM block_20m AT TIME ZONE 'Asia/Shanghai') AS hh,
        count(*) AS jump_cnt
    FROM dblk_dev
    WHERE range20_pct >= 20                     -- ★ 20 分钟相对极差阈值
    GROUP BY device_id, point_code, hh
),
win AS (                                        -- 支路 × 窗口：特征汇总
    SELECT
        device_id,
        point_code,
        hh,
        count(*)                                                AS snap_cnt,
        min(i)                                                  AS i_min_win,   -- 窗口内最小支路电流
        avg(dev_pct)                                            AS dev_avg,
        max(dev_pct)                                            AS dev_max,
        min(dev_pct)                                            AS dev_min,
        avg(cv_pct)                                             AS cv_avg,
        max(cv_pct)                                             AS cv_max,
        count(*) FILTER (WHERE dev_pct >= 5 AND dev_pct < 10)   AS ge5_cnt,   -- ★ 互斥 5%~10%
        count(*) FILTER (WHERE dev_pct >= 10)                   AS ge10_cnt,
        count(*) FILTER (WHERE dev_pct <= -5)                   AS le_neg5_cnt,
        count(*) FILTER (WHERE abs(dev_pct) >= 5)               AS abn_win_cnt,  -- 异常条数（|偏差率|>=5%）
        count(*) FILTER (WHERE dev_pct < -1e-6)                 AS neg_cnt,   -- ★ 1e-6 容差，滤掉浮点噪声假负值
        count(*) FILTER (WHERE dev_pct >= 100)                  AS over100_cnt,
        count(*) FILTER (WHERE abs(dev_pct) <= 1e-6 AND abn_cnt > 0) AS zero_cnt
        -- ★ i = i_mean 的容差等价写法：avg(float8) 累加误差会让 0.6 vs 0.6000000000000001
        --   判为不等而漏报；dev_pct = 0 与 i = i_mean 同义，1e-6 个百分点容差足够安全
    FROM bi
    GROUP BY device_id, point_code, hh
),
ball AS (                                       -- 窗口内出现过的支路（含全 0A 支路, 排除已标记支路）
    SELECT DISTINCT
        device_id,
        point_code,
        extract(hour FROM bucket AT TIME ZONE 'Asia/Shanghai') AS hh
    FROM pv_string_branch_5min b
    WHERE bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08'
      AND bucket <  TIMESTAMPTZ '2026-11-04 16:00:00+08'
      AND (
            (bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 11:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 12:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 13:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 14:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 15:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 15:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 16:00:00+08')
      )
      AND (sample_cnt >= 1 OR zero_cnt > 0)
      AND NOT EXISTS (                          -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
),
zc AS (                                         -- 支路 × 窗口：零电流采样数（断路线索）
    SELECT
        device_id,
        point_code,
        extract(hour FROM bucket AT TIME ZONE 'Asia/Shanghai') AS hh,
        sum(zero_cnt) AS zero_current_samples
    FROM pv_string_branch_5min
    WHERE bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08'
      AND bucket <  TIMESTAMPTZ '2026-11-04 16:00:00+08'
      AND (
            (bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 11:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 12:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 13:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 14:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 15:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 15:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 16:00:00+08')
      )
      AND zero_cnt > 0
    GROUP BY device_id, point_code, hh
),
st AS (
    SELECT station_id, station_name, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, region_company_name
),
si AS (
    SELECT DISTINCT ON (upper(inverter_id))
           upper(inverter_id) AS dev_key,
           station_id,
           inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY upper(inverter_id), station_id, inverter_name
)
SELECT
    (DATE '2026-11-04' + make_interval(hours => b.hh::int))::timestamp AS "时间戳",   -- ★ 日期
    CASE b.hh WHEN 10 THEN '10:00-11:00'
              WHEN 12 THEN '12:00-13:00'
              WHEN 14 THEN '14:00-15:00'
              WHEN 15 THEN '15:00-16:00' END                          AS "时间窗口",
    st.region_company_name                                            AS "公司",
    st.station_name                                                   AS "场站",
    si.inverter_name                                                  AS "组串编号",
    b.device_id                                                       AS "逆变器ID",
    substring(b.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int      AS "支路编号",
    b.point_code                                                      AS "支路点位",

    round(w.dev_avg::numeric, 2)                                      AS "单支路当前偏差率(%)",   -- 无有效快照则为 NULL
    round(w.cv_avg::numeric, 2)                                       AS "组串整体偏差率(%)",
    round(w.dev_max::numeric, 2)                                      AS "最大偏差率(%)",
    round(w.dev_min::numeric, 2)                                      AS "最小偏差率(%)",
    round(w.cv_max::numeric, 2)                                       AS "最大组串整体偏差率(%)",
    round(brw.chg_20m_max::numeric, 2)                                AS "20分钟最大偏差变化(pp)",
    round(brw.rate_max_pm::numeric, 3)                                AS "偏差率变化速率(%/m)",

    COALESCE(w.ge5_cnt, 0)                                            AS "偏差率>=5%且<10%条数",
    COALESCE(w.ge10_cnt, 0)                                           AS "偏差率>=10%条数",
    COALESCE(w.le_neg5_cnt, 0)                                        AS "偏差率<=-5%条数",
    COALESCE(w.abn_win_cnt, 0)                                        AS "异常数据条数",     -- |偏差率|>=5% 的条数
    COALESCE(j.jump_cnt, 0)                                           AS "突变数据条数",     -- 20分钟块触发次数
    COALESCE(w.neg_cnt, 0)                                            AS "负值数据条数",
    COALESCE(w.over100_cnt, 0)                                        AS "超限数据条数",
    COALESCE(w.zero_cnt, 0)                                           AS "零值数据条数",
    COALESCE(z.zero_current_samples, 0)                               AS "零电流采样数",

    -- 条件标识
    (COALESCE(w.ge5_cnt, 0) > 0 OR COALESCE(w.ge10_cnt, 0) > 0 OR COALESCE(w.le_neg5_cnt, 0) > 0) AS "①绝对值报警",
    (COALESCE(w.snap_cnt, 0) >= 2 AND w.dev_max = w.dev_min)                                        AS "②死值校验",
    (COALESCE(j.jump_cnt, 0) >= 1)                                                                   AS "③突变报警",
    (COALESCE(w.neg_cnt, 0) > 0)                                                                     AS "④负值异常",
    (COALESCE(w.over100_cnt, 0) > 0)                                                                 AS "⑤超限异常",
    (COALESCE(w.zero_cnt, 0) > 0)                                                                    AS "⑥零值异常",
    (COALESCE(z.zero_current_samples, 0) > 0)                                                        AS "零电流(断路兜底)",

    -- 报警 = ① OR ② OR ③ OR ④ OR ⑤ OR ⑥ OR 零电流兜底
    -- 注：0A 原始数据被排除在偏差率计算之外，dev=-100% 不再可能出现，
    --     若不把零电流并入报警，开路支路将永远不报警。若要求严格 6 条件，可删去最后一项。
    (COALESCE(z.zero_current_samples, 0) > 0
     OR COALESCE(w.ge5_cnt, 0) > 0
     OR COALESCE(w.ge10_cnt, 0) > 0
     OR COALESCE(w.le_neg5_cnt, 0) > 0
     OR (COALESCE(w.snap_cnt, 0) >= 2 AND w.dev_max = w.dev_min)
     OR COALESCE(j.jump_cnt, 0) >= 1
     OR COALESCE(w.neg_cnt, 0) > 0
     OR COALESCE(w.over100_cnt, 0) > 0
     OR COALESCE(w.zero_cnt, 0) > 0)                                 AS "报警",

    -- 运维建议（按优先级取第一条命中）
    CASE
        WHEN COALESCE(z.zero_current_samples, 0) > 0
            THEN '支路存在0A采样，疑似断路/未接入：检查汇流箱熔断器、MC4接头、组串是否断开'
        WHEN COALESCE(w.snap_cnt, 0) = 0
            THEN '窗口内无有效电流采样（全0A或数据缺失）：按零电流支路核查'
        WHEN w.i_min_win < 0
            THEN '电流反向：检查电流传感器接线极性，排查组件反接/反灌'
        WHEN w.dev_min <= -5
            THEN '组串低效：排查遮挡、组件老化、二极管故障，红外热像查热斑，测支路开路电压'
        WHEN w.dev_max >= 10
            THEN '电流偏高：校验CT精度，对比相邻支路实测电流'
        WHEN COALESCE(j.jump_cnt, 0) >= 1
            THEN '组串严重失配：重点排查偏差率最低的1-2条支路（隐裂、接线盒烧毁）'
        WHEN COALESCE(w.snap_cnt, 0) >= 2 AND w.dev_max = w.dev_min
            THEN '数据死值：结合电压判断传感器卡死或通信假数据'
        WHEN w.dev_avg >= 5 AND w.dev_avg < 10
            THEN '轻度偏高（亚健康）：记录在案，下次清洗/巡检重点观察'
        WHEN w.dev_avg > -5 AND w.dev_avg <= 0
            THEN '轻度偏低（亚健康）：记录在案，下次清洗/巡检重点观察'
        ELSE '正常'
    END                                                               AS "运维建议"
FROM ball b
LEFT JOIN win  w   USING (device_id, point_code, hh)
LEFT JOIN bwin brw USING (device_id, point_code, hh)
LEFT JOIN jump j   USING (device_id, point_code, hh)
LEFT JOIN zc   z   USING (device_id, point_code, hh)
LEFT JOIN si ON si.dev_key = b.device_id
LEFT JOIN st ON st.station_id = si.station_id
ORDER BY
    "报警" DESC,
    st.region_company_name,
    st.station_name,
    si.inverter_name,
    b.device_id,
    b.hh,
    substring(b.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int;


-- ============================================================================
-- 5. 查询 C：零电流（断路）清单（支路 × 20 分钟固定块）
-- ----------------------------------------------------------------------------
-- 统计原始 point_value = 0 的采样数；这些采样不参与任何偏差率计算，
-- 仅用于补足“电流=0 被剔除后无法由 dev=-100% 发现断路”的盲区。
-- ============================================================================
WITH ex AS (                                    -- 已标记异常的逆变器+支路（参考 pv_to_excel.py）
    SELECT DISTINCT
        upper(inverter_id)                                                     AS dev_key,
        'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A'  AS point_code
    FROM pv_inverter_branch_abnormal
    WHERE branch_name ~ '支路[0-9]+'
),
z AS (
    SELECT
        device_id,
        point_code,
        to_timestamp(floor(extract(epoch FROM bucket) / 1200) * 1200) AS block_20m,
        sum(zero_cnt)                                        AS zero_current_samples,
        count(*) FILTER (WHERE sample_cnt > 0)               AS valid_bucket_cnt,
        count(*)                                             AS bucket_cnt,
        sum(sample_cnt)                                      AS valid_sample_cnt
    FROM pv_string_branch_5min b
    WHERE bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08'     -- ★ 日期下界
      AND bucket <  TIMESTAMPTZ '2026-11-04 16:00:00+08'     -- ★ 日期上界
      AND zero_cnt > 0
      AND NOT EXISTS (                          -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
      AND (
            (bucket >= TIMESTAMPTZ '2026-11-04 10:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 11:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 12:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 13:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 14:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 15:00:00+08')
         OR (bucket >= TIMESTAMPTZ '2026-11-04 15:00:00+08' AND bucket < TIMESTAMPTZ '2026-11-04 16:00:00+08')
      )
    GROUP BY device_id, point_code, block_20m
),
st AS (
    SELECT station_id, station_name, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, region_company_name
),
si AS (
    SELECT DISTINCT ON (upper(inverter_id))
           upper(inverter_id) AS dev_key,
           station_id,
           inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY upper(inverter_id), station_id, inverter_name
)
SELECT
    z.block_20m AT TIME ZONE 'Asia/Shanghai'                     AS "20分钟块起始",
    st.region_company_name                                        AS "公司",
    st.station_name                                               AS "场站",
    si.inverter_name                                              AS "组串编号",
    z.device_id                                                   AS "逆变器ID",
    substring(z.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int  AS "支路编号",
    z.point_code                                                  AS "支路点位",
    z.zero_current_samples                                        AS "0A采样数",
    z.valid_sample_cnt                                            AS "有效采样数",
    CASE WHEN z.valid_bucket_cnt = 0
         THEN '整块全为0A，疑似断路/未接入'
         ELSE '块内存在0A采样，疑似间歇开路' END                  AS "判断"
FROM z
LEFT JOIN si ON si.dev_key = z.device_id
LEFT JOIN st ON st.station_id = si.station_id
ORDER BY
    st.region_company_name,
    st.station_name,
    si.inverter_name,
    z.device_id,
    z.block_20m,
    substring(z.point_code FROM 'STRING_BRANCH([0-9]+)_A')::int;

-- ============================================================================
-- 6. 三特征独立查询（已验证：Excel 源头重算 288/288 一致，详见会话验证记录）
--    特征1 单支路偏差率 = (支路电流 - 组串平均电流) / 组串平均电流 * 100%
--    特征2 组串整体偏差率 = stddev_pop(支路电流) / 组串平均电流 * 100%   （总体标准差 ÷n）
--    特征3 组串电流相对极差 = (组串最大电流 - 组串最小电流) / 组串平均电流 * 100%
--    口径：剔除 0A（i_avg IS NOT NULL）、单桶有效采样 ≥3、
--          快照 ≥2 条支路且 |组串均值| ≥0.5A（低电流防御，与 §3 ds 一致）
--          支路级 |i| < 0.5A 只打标记「支路低电流」，不剔除（仍参与统计，口径 2）；
--          zero_cnt 仅用于暴露 0A，不参与任何均值/偏差率计算（口径 1）；
--          6.2 额外把被 i_avg IS NULL 剔除的全零支路计入 零值采样数/零值占比，
--          并单列「全零支路数」，避免断路支路被静默丢弃
--          已被 pv_inverter_branch_abnormal 标记的逆变器+支路不参与计算与输出
--          （口径 8；ex CTE + NOT EXISTS，与 §3 完全一致）
--          6.2 以窗口内全部 5 分钟桶为骨架，全零快照也会输出一行
--          （支路数=0、零值占比=100），不会因 br 无行而整行消失
--    正则：point_code 用锚定的 1~40 支路白名单，与 §2.1 完全一致
--    时区：过滤用显式 +08 timestamptz，显示固定 Asia/Shanghai
--    ★ 6.1 与 6.2 共用同一份 br/ds 定义（两条独立语句各写一遍），改口径必须两处同步
-- ============================================================================

-- 6.1 支路级明细（每快照 × 每支路一行，含三个特征）
--     全零快照在支路级无明细行，其 0A 信息见 6.2 的「全零支路数 / 零值占比」
WITH ex AS (                                    -- 已标记异常的逆变器+支路（与 §3 完全一致）
    SELECT DISTINCT
        upper(inverter_id)                                                     AS dev_key,
        'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A'  AS point_code
    FROM pv_inverter_branch_abnormal
    WHERE branch_name ~ '支路[0-9]+'
),
br AS (
    SELECT b.device_id, b.point_code, b.bucket,
           b.i_avg AS i, b.sample_cnt, b.zero_cnt
    FROM pv_string_branch_5min b
    WHERE b.device_id = 'AUFP24YH'                                -- ← 设备
      AND b.bucket >= timestamptz '2026-11-04 10:00:00+08'        -- ← 时间范围（半开区间）
      AND b.bucket <  timestamptz '2026-11-04 11:00:00+08'
      AND b.i_avg IS NOT NULL
      AND b.sample_cnt >= 3
      AND b.point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$' -- 1~40（锚定，与 §2.1 一致）
      AND NOT EXISTS (                            -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
),
ds AS (
    SELECT device_id, bucket,
           avg(i)        AS i_mean,
           count(*)      AS branch_cnt,
           sum(zero_cnt) AS zero_cnt,
           sum(sample_cnt) AS sample_cnt,
           count(*) FILTER (WHERE abs(i) < 0.5) AS low_cnt
    FROM br
    GROUP BY device_id, bucket
),
st AS (                                         -- 电站维表（与 §3 一致）
    SELECT station_id, station_name, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, region_company_name
),
si AS (                                         -- 逆变器维表（与 §3 一致）
    SELECT DISTINCT ON (upper(inverter_id))
           upper(inverter_id) AS dev_key,
           station_id,
           inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY upper(inverter_id), station_id, inverter_name
)
SELECT to_char(br.bucket AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI') AS "时间戳",
       st.region_company_name                                               AS "公司",
       st.station_name                                                      AS "场站",
       si.inverter_name                                                     AS "组串编号",
       br.device_id                                                         AS "逆变器ID",
       substring(br.point_code FROM '^STRING_BRANCH([0-9]+)_A$')::int        AS "支路编号",
       br.point_code                                                        AS "支路点位",
       round(br.i::numeric, 3)                                              AS "支路电流(A)",
       round(d.i_mean::numeric, 3)                                          AS "组串平均电流(A)",
       round(((br.i - d.i_mean) / d.i_mean * 100)::numeric, 2)               AS "单支路偏差率(%)",
       br.sample_cnt                                                        AS "有效采样数",
       br.zero_cnt                                                          AS "零值采样数",
       CASE WHEN abs(br.i) < 0.5 THEN '是' ELSE '' END                       AS "支路低电流"
FROM br
JOIN ds d USING (device_id, bucket)
LEFT JOIN si ON si.dev_key = br.device_id
LEFT JOIN st ON st.station_id = si.station_id
WHERE d.branch_cnt >= 2 AND abs(d.i_mean) >= 0.5
ORDER BY br.bucket, "支路编号";

-- 6.2 组串级汇总（每快照一行，仅特征 2、3；br/ds/zbr 与 6.1 完全一致）
WITH ex AS (                                    -- 已标记异常的逆变器+支路（与 §3 完全一致）
    SELECT DISTINCT
        upper(inverter_id)                                                     AS dev_key,
        'STRING_BRANCH' || substring(branch_name FROM '支路([0-9]+)') || '_A'  AS point_code
    FROM pv_inverter_branch_abnormal
    WHERE branch_name ~ '支路[0-9]+'
),
br AS (
    SELECT b.device_id, b.point_code, b.bucket,
           b.i_avg AS i, b.sample_cnt, b.zero_cnt
    FROM pv_string_branch_5min b
    WHERE b.device_id = 'AUFP24YH'                                -- ← 设备
      AND b.bucket >= timestamptz '2026-11-04 10:00:00+08'        -- ← 时间范围（半开区间）
      AND b.bucket <  timestamptz '2026-11-04 11:00:00+08'
      AND b.i_avg IS NOT NULL
      AND b.sample_cnt >= 3
      AND b.point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$' -- 1~40（锚定，与 §2.1 一致）
      AND NOT EXISTS (                            -- 排除已标记异常的逆变器+支路
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
),
ds AS (
    SELECT device_id, bucket,
           avg(i)        AS i_mean,
           stddev_pop(i) AS i_std,
           min(i)        AS i_min,
           max(i)        AS i_max,
           count(*)      AS branch_cnt,
           sum(zero_cnt) AS zero_cnt,
           sum(sample_cnt) AS sample_cnt,
           count(*) FILTER (WHERE abs(i) < 0.5) AS low_cnt
    FROM br
    GROUP BY device_id, bucket
),
zbr AS (                                        -- 被剔除的全零支路（i_avg IS NULL），仅用于暴露 0A
    SELECT b.device_id, b.bucket,
           count(*)                    AS allzero_cnt,
           coalesce(sum(b.zero_cnt), 0) AS allzero_samples
    FROM pv_string_branch_5min b
    WHERE b.device_id = 'AUFP24YH'                                -- ← 设备
      AND b.bucket >= timestamptz '2026-11-04 10:00:00+08'        -- ← 时间范围（半开区间）
      AND b.bucket <  timestamptz '2026-11-04 11:00:00+08'
      AND b.i_avg IS NULL
      AND b.point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'
      AND NOT EXISTS (                            -- 已标记支路同样不参与 0A 统计
            SELECT 1 FROM ex
            WHERE ex.dev_key = b.device_id AND ex.point_code = b.point_code
      )
    GROUP BY b.device_id, b.bucket
),
snap AS (                                       -- 窗口内全部 5 分钟桶：全零快照的骨架
    SELECT DISTINCT device_id, bucket
    FROM pv_string_branch_5min
    WHERE device_id = 'AUFP24YH'                                  -- ← 设备
      AND bucket >= timestamptz '2026-11-04 10:00:00+08'          -- ← 时间范围（半开区间）
      AND bucket <  timestamptz '2026-11-04 11:00:00+08'
      AND point_code ~ '^STRING_BRANCH([1-9]|[1-3][0-9]|40)_A$'
),
st AS (                                         -- 电站维表（与 §3 一致）
    SELECT station_id, station_name, region_company_name
    FROM pv_station_info
    WHERE constituent_name = '国电电力'
    GROUP BY station_id, station_name, region_company_name
),
si AS (                                         -- 逆变器维表（与 §3 一致）
    SELECT DISTINCT ON (upper(inverter_id))
           upper(inverter_id) AS dev_key,
           station_id,
           inverter_name
    FROM pv_station_invert
    WHERE inverter_id IS NOT NULL
      AND inverter_id <> ''
    ORDER BY upper(inverter_id), station_id, inverter_name
)
SELECT to_char(s.bucket AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM-DD HH24:MI') AS "时间戳",
       st.region_company_name                                               AS "公司",
       st.station_name                                                      AS "场站",
       si.inverter_name                                                     AS "组串编号",
       s.device_id                                                          AS "逆变器ID",
       coalesce(ds.branch_cnt, 0)                                           AS "支路数",
       round(ds.i_mean::numeric, 3)                                          AS "组串平均电流(A)",
       round((ds.i_std / ds.i_mean * 100)::numeric, 2)                        AS "组串整体偏差率(%)",
       round(((ds.i_max - ds.i_min) / ds.i_mean * 100)::numeric, 2)           AS "组串电流相对极差(%)",
       coalesce(ds.zero_cnt, 0) + coalesce(z.allzero_samples, 0)              AS "零值采样数",
       round(((coalesce(ds.zero_cnt, 0) + coalesce(z.allzero_samples, 0))::numeric
              / nullif(coalesce(ds.zero_cnt, 0) + coalesce(z.allzero_samples, 0)
                       + coalesce(ds.sample_cnt, 0), 0) * 100), 2)            AS "零值占比(%)",
       coalesce(ds.low_cnt, 0)                                               AS "低电流支路数",
       coalesce(z.allzero_cnt, 0)                                            AS "全零支路数"
FROM snap s
LEFT JOIN ds     ON ds.device_id = s.device_id AND ds.bucket = s.bucket
LEFT JOIN zbr z  ON z.device_id  = s.device_id AND z.bucket  = s.bucket
LEFT JOIN si ON si.dev_key = s.device_id
LEFT JOIN st ON st.station_id = si.station_id
WHERE ds.branch_cnt IS NULL                     -- 全零快照：保留一行（支路数=0）
   OR (ds.branch_cnt >= 2 AND abs(ds.i_mean) >= 0.5)
ORDER BY s.bucket;
