"""统一入口：从本地 wind_device 白名单发现设备 -> 按时间窗拉取 -> upsert 到 wind_seconds_di。

合并了原 get_fjmjsj.py（设备清单发现 type=1022 + 逐 tagName 分页拉取 type=1029）、
fetch_and_ingest.py（30s 轮询 + 水位线窗口 + 不丢数据机制）与
ingest_wind.py（tagName 解析 + 按 (device_id, ts) 归并 + 批量 upsert）的能力，
输出从「写 CSV」改为「直接 upsert 到 wind_seconds_di」。

不丢数据机制（每秒产生多少条无需预知）：
1. 水位线文件 fetch_watermark.json（与脚本同目录，*.json 已被 .gitignore
   忽略）：记录上一轮成功入库的窗口右边界；进程重启后从水位线继续，
   而不是从 now() 开始。只在入库成功后写文件（临时文件 + os.replace
   原子替换）；若崩溃在“入库成功、水位线未写”之间，下一轮重叠重拉，
   MERGE 幂等不会产生重复行。
2. 每轮窗口半开区间 [last_ts - overlap, end)，默认回退 50s 重叠拉取，
   覆盖时钟漂移与迟到/补传数据。
3. 每个 tag 查询按 pageSize 分页，翻页直到取完——窗口内数据量再大也
   不丢，只影响请求次数。
4. 拉取/入库失败则不写水位线，下一轮从原位重拉。
5. 停机很久后恢复时，单窗被限制在 MAX_WINDOW_SECONDS，逐窗快速追平，
   避免一个覆盖几天的超大请求；未追平时本轮结束后立即进入下一轮
   （不 sleep 30s）。

拉取方式（FETCH_MODE，None 时启动交互选择）：
    window = 整窗拉取：queryCriteria 只给 ts 范围，一次拉回窗口内所有
             设备/测点（请求数与设备数无关；未映射测点入库前丢弃）
    like   = 按测点后缀 like：每个 Point_Codes 后缀发一次
             tag_name LIKE '%{后缀}' 查询（请求数 = 后缀数，不受设备数影响）
    tag    = 按 tag 精准拉取：设备发现 x Point_Codes 逐 tag 等值查询
             （请求数 = 设备数 x 后缀数，设备多时最慢）

设备清单：本地 wind_device 表即白名单，只有表中存在的设备才允许入库。
    表内 ID 统一转小写；远端 tagName 使用大写，parse_tag 时转小写入库，
    白名单匹配也用小写，保证 join 时 device_id 直接相等无需 upper()。
"""
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler

import psycopg2
import requests
from psycopg2.extras import execute_values

log = logging.getLogger('wind_fetch')

# ---------------- 表名配置（改表名只改这里） ----------------
TABLE_DATA = 'wind_seconds_di'           # 风机秒级数据明细表（按 ts 日分区）
TABLE_DEVICE = 'wind_device'             # 风机白名单台账表
PARTITION_PREFIX = TABLE_DATA + '_'      # 分区命名前缀 wind_seconds_di_

# ---------------- 接口配置 ----------------
API_URL = 'http://10.65.78.65:18082/queryData'
API_TOKEN = '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9'
DATA_STB = 'ods_ly_data_hub_fjmjsj_new'          # type=1029 数据表
PAGE_SIZE = 1000   # 服务端 2026-10-10 11:34 起对大响应(~>150KB)中途断连
                   # (IncompleteRead)，5000→1000 实测稳定；恢复后可改回
TIMEOUT = 30
MAX_RETRIES = 3
RETRY_INTERVAL = 5

# ---------------- 入库：tagName 解析 + 归并 + 批量 upsert ----------------
# 批量 upsert（execute_values 把多行注入 VALUES %s）。
# 同一 (device_id, ts) 的多条消息必须先在 Python 侧按到达顺序归并成
# 一行（见 merge_points）：PG 不允许单条 INSERT 的 ON CONFLICT DO
# UPDATE 命中同一行两次。
#
# Merge 语义：
# - each fixed column is overwritten only when a new value arrives (COALESCE
#   keeps the old value when this message lacks it).
# - every other point is merged into other_points: new keys are appended,
#   existing keys are overwritten, keys absent from this message stay untouched.
#
# 固定列清单、INSERT 列序、占位符模板均由下方 TAG_MAP 自动推导，
# 新增固定列只需：① 给表加列 ② 在 TAG_MAP 里加映射，其余全部自动跟随。


def connect():
    with open('data_config.json', encoding='utf-8') as f:
        return psycopg2.connect(json.load(f)['pgConnStr'])


# --- Raw tag message format -------------------------------------------
# Message example:
# {"pointValue": 46.6, "description": "风机秒级数据",
#  "tagName": "FJMJ1_B524F6D0B8FF4B2DBC0C102FC4B032B9WGEN.TEMGENDRIEND",
#  "ts": "2026-09-30 13:57:20"}
#
# tagName layout (1-based positions):
#   chars 1-6              -> source prefix, discarded (e.g. FJMJ1_)
#   char 7 .. key start    -> device_id
#   4 chars before '.' .. end -> key (e.g. WGEN.TEMGENDRIEND)

TAG_PREFIX_LEN = 6  # characters skipped at the start of tagName

# Parsed tagName key -> fixed column mapping. 本字典是测点的唯一事实源：
#   - Point_Codes（下方）直接取这里的 key 作为"去接口拉哪些测点"
#   - key maps to a column name -> value goes into that fixed column
#   - key maps to None          -> value goes into other_points
#   - key not in the map        -> the message is ignored (not inserted)
# 因此加/删一个测点只改这里（+表结构/聚合），拉取与入库同时生效。
# 加一个固定列：给表加列后在这里把映射指向新列名即可，顺序即 INSERT 列序。
TAG_MAP = {
    'WGEN.TEMGENDRIEND': 'tem_gen_driend',  # 发电机驱动端温度
    'WGEN.TEMGENNONDE': 'tem_gen_nonde',  # 发电机非驱动端温度（实测真实 tag）
    'WGEN.GENSENTMP1': 'tem_gen_sen1',  # 发电机定子U相线圈温度
    'WGEN.GENSENTMP2': 'tem_gen_sen2',  # 发电机定子V相线圈温度
    'WGEN.GENSENTMP3': 'tem_gen_sen3',  # 发电机定子W相线圈温度
    'WGEN.TEMGENSTAU': 'tem_gen_stau',  # 发电机定子U相线圈温度
    'WGEN.TEMGENSTAV': 'tem_gen_stav',  # 发电机定子V相线圈温度
    'WGEN.TEMGENSTAW': 'tem_gen_staw',  # 发电机定子W相线圈温度
    'WTRM.TEMMAINBEARING': 'tem_main_bearing',  # 主轴前轴承温度
    'WTRM.TEMMAINBEARING2': 'tem_main_bearing2',  # 主轴后轴承温度
    'WVIB.VIBRATIONLFIL': 'vibration_lateral',  # 侧向震动
    'WVIB.VIBRATIONVFIL': 'vibration_vertical',  # 轴向震动
    'WCNV.CURCONL1': 'cur_conl1',  # 网侧三相电流(Ia)
    'WCNV.CURCONL2': 'cur_conl2',  # 网侧三相电流(Ib)
    'WCNV.CURCONL3': 'cur_conl3',  # 网侧三相电流(Ic)
    'WTRM.TEMGEAOIL': 'tem_gea_oil',  # 齿轮箱油池温度
    'WTRM.TEMGEAMSDE': 'tem_gea_msde',  # 齿轮箱高速轴驱动端温度
    'WTRM.TEMGEAMSND': 'tem_gea_msnd',  # 齿轮箱高速轴非驱动端温度
    'WGEN.GENSPD': None,            # 发电机转速 -> other_points
    #'WNAC.WINDSPEED': None,         # 机舱风速 -> other_points
}

# 固定列 = TAG_MAP 中映射到非 None 的列名，按 TAG_MAP 出现顺序去重
# （dict.fromkeys 保序；顺序必须与 MERGE_SQL_BATCH 的 INSERT 列序一致）。
# 其余测点（value 为 None）一律进 other_points。
FIXED_POINTS = tuple(dict.fromkeys(c for c in TAG_MAP.values() if c))


def _build_merge_sql():
    """根据 FIXED_POINTS 动态生成批量 upsert SQL。"""
    insert_cols = ', '.join(('device_id', 'ts', *FIXED_POINTS, 'other_points'))
    set_clause = ',\n    '.join(
        f'{c} = COALESCE(EXCLUDED.{c}, {TABLE_DATA}.{c})' for c in FIXED_POINTS)
    return f"""
INSERT INTO {TABLE_DATA}
    ({insert_cols})
VALUES %s
ON CONFLICT (device_id, ts) DO UPDATE
SET {set_clause},
    other_points = COALESCE({TABLE_DATA}.other_points, '{{}}'::jsonb)
            || COALESCE(EXCLUDED.other_points, '{{}}'::jsonb)
"""


MERGE_SQL_BATCH = _build_merge_sql()

# execute_values 的单行模板：device_id, ts, 每个固定列, other_points
_ROW_TEMPLATE = '(' + ', '.join(
    ['%s', '%s', *(f'%s' for _ in FIXED_POINTS), '%s::jsonb']) + ')'


def parse_tag(tag_name):
    """Split a raw tagName into (device_id, key). device_id 统一转大写入库。"""
    dot = tag_name.find('.')
    if dot == -1 or dot - 4 <= TAG_PREFIX_LEN:
        raise ValueError(f'bad tagName: {tag_name!r}')
    key = tag_name[dot - 4:]
    device_id = tag_name[TAG_PREFIX_LEN:dot - 4].upper()
    return device_id, key


def parse_message(msg):
    """Convert one raw message dict into (device_id, ts, points dict).

    Returns None when the key is not in TAG_MAP (message must be dropped).
    """
    device_id, key = parse_tag(msg['tagName'])
    if key not in TAG_MAP:
        return None
    column = TAG_MAP[key]
    if column is None:
        return device_id, msg['ts'], {key: msg['pointValue']}
    return device_id, msg['ts'], {column: msg['pointValue']}


def merge_points(messages):
    """把 (device_id, ts, points) 序列按到达顺序归并到 (device_id, ts) 上。

    归并语义与 MERGE_SQL_BATCH 的 SQL 合并完全一致：
    - 固定列：后到的非 None 值覆盖；后到 None 不覆盖已有值；
    - other_points：键值对按到达顺序合并，后到覆盖先到。
    返回 [(device_id, ts, point_map)]，键唯一。
    """
    merged = {}
    for dev, ts, points in messages:
        point_map = points if isinstance(points, dict) else dict([points])
        dst = merged.setdefault((dev, ts), {})
        for k, v in point_map.items():
            if k in FIXED_POINTS:
                if v is not None or k not in dst:
                    dst[k] = v
            else:
                dst[k] = v
    return [(dev, ts, merged[(dev, ts)]) for dev, ts in merged]


def ingest_raw(messages):
    """messages: iterable of raw dicts with tagName/pointValue/ts.

    全部解析后按 (device_id, ts) 归并，execute_values 批量 upsert
    （同一事务）；TAG_MAP 之外的 key 丢弃。返回实际入库的消息条数
    （丢弃的不计）。
    """
    parsed = [p for p in (parse_message(m) for m in messages) if p is not None]
    if not parsed:
        return 0
    # 未来时间防御：接口偶发吐带未来 ts 的占位记录（数值多为 NULL），
    # 入库前过滤掉，避免数据库残留"接口查不到但表里有"的脏数据。
    future_limit = datetime.now() + timedelta(seconds=FUTURE_FILTER_SECONDS)
    filtered = []
    future_dropped = 0
    for dev, ts, points in parsed:
        try:
            ts_dt = datetime.strptime(ts, TS_FMT)
        except ValueError:
            filtered.append((dev, ts, points))  # 解析失败的不过滤，保守保留
            continue
        if ts_dt > future_limit:
            future_dropped += 1
        else:
            filtered.append((dev, ts, points))
    if future_dropped:
        log.warning('discard %d 条未来时间脏数据（ts > %s），阈值=%ds',
                    future_dropped, future_limit.strftime(TS_FMT),
                    FUTURE_FILTER_SECONDS)
    parsed = filtered
    if not parsed:
        return 0
    rows = []
    for dev, ts, point_map in merge_points(parsed):
        other = {k: v for k, v in point_map.items() if k not in FIXED_POINTS}
        rows.append((dev, ts,
                     *(point_map.get(fp) for fp in FIXED_POINTS),
                     json.dumps(other, ensure_ascii=False)))
    conn = connect()
    try:
        cur = conn.cursor()
        execute_values(cur, MERGE_SQL_BATCH, rows,
                       template=_ROW_TEMPLATE, page_size=1000)
        conn.commit()
        return len(parsed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------- 拉取范围 ----------------
# 要拉取的测点后缀直接复用上方入库映射 TAG_MAP 的 key（唯一事实源）：
# 在 TAG_MAP 里加/删一个测点，拉取清单与入库映射同步生效，无需维护两份。
Point_Codes = tuple(TAG_MAP.keys())
# 仅 like 模式生效的额外模糊模式（% / _ 为 SQL 通配符）。
# 用途：用一个模式一次捞回同系列多个测点（如 'WGEN.TEMGEN%' 同时覆盖
# DRIEND/NONDE），减少请求数。只影响"怎么查"，不影响"怎么存"——
# 入库仍按 TAG_MAP 对返回的真实测点精确匹配，模式拉回的未映射测点会被丢弃。
# 已被某模式覆盖的精确测点在 like 模式下自动跳过（结果幂等，纯省请求）。
# tag/window/mock 模式忽略此项（tag 是等值查询，% 是字面字符，不能用）。
EXTRA_LIKE_PATTERNS = [
    # 'WGEN.TEMGEN%',
]
# 非空时跳过设备发现，直接使用这些完整 tagName（如 ['FJMJ1_XXXWGEN.GENSPD']）
TARGETS = []

# ---------------- 拉取方式 ----------------
# 'window'=整窗拉取  'like'=按测点后缀like  'tag'=按tag精准
FETCH_MODE = 'like'

# ---------------- 窗口与轮询 ----------------
OVERLAP_SECONDS = 50            # 每轮回退重叠，防迟到/钟漂
INITIAL_LOOKBACK_SECONDS = 300  # 首次运行（无水位线文件）回看多久
MAX_WINDOW_SECONDS = 600        # 追历史时单窗上限；过大的窗口易触发接口
                                # ChunkedEncodingError（响应中途断开），10 分钟窗更稳
FETCH_INTERVAL_SECONDS = 30     # 每轮间隔
FETCH_WORKERS = 8               # 并发拉取 tag 的线程数
FUTURE_FILTER_SECONDS = 30 * 60  # 未来时间防御阈值：ts 超过 now+30min 的点丢弃
MOCK = False                    # True = 不请求真实接口，用模拟数据
LOG_LEVEL = 'INFO'               # DEBUG=打印每条入库/每个查询明细；INFO=只留主干；WARNING=只留异常

TS_FMT = '%Y-%m-%d %H:%M:%S'
SOURCE = 'fjmjsj'
WATERMARK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'fetch_watermark.json')
ROLLUP_SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'wind_rollup_minute.sql')
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fetch.log')
LOG_TO_FILE = False             # True=同时写文件+控制台；False=只打印到屏幕
LOG_BACKUP_DAYS = 30


def setup_logging():
    """按全局 LOG_LEVEL 配置日志（DEBUG/INFO/WARNING/...，不区分大小写）。

    LOG_TO_FILE=True：同时输出到控制台和按天轮转文件（保留 LOG_BACKUP_DAYS 天）；
    LOG_TO_FILE=False：只输出到控制台。
    DEBUG 时打印每条入库/每个查询的明细；INFO 只留主干；WARNING 只留异常。
    """
    level = logging.getLevelName(str(LOG_LEVEL).upper())
    if not isinstance(level, int):
        raise ValueError(f'非法 LOG_LEVEL={LOG_LEVEL!r}，'
                         f'可选 DEBUG/INFO/WARNING/ERROR/CRITICAL')
    log.setLevel(level)
    log.handlers.clear()
    fmt = logging.Formatter('%(asctime)s %(levelname)-5s %(message)s', TS_FMT)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if LOG_TO_FILE:
        fh = TimedRotatingFileHandler(LOG_FILE, when='midnight',
                                      backupCount=LOG_BACKUP_DAYS,
                                      encoding='utf-8')
        fh.setFormatter(fmt)
        log.addHandler(fh)
    log.propagate = False


def rollup_minute():
    """执行分钟聚合 SQL（每分钟跨分钟时触发一次，SQL 自身幂等）。"""
    conn = connect()
    try:
        with open(ROLLUP_SQL_FILE, encoding='utf-8') as f, \
                conn.cursor() as cur:
            cur.execute(f.read())  # 建表 + 建索引 + 最近3分钟 upsert
        conn.commit()
    finally:
        conn.close()


# ---------------- 数据完整性自检 ----------------
INTEGRITY_CHECK_SECONDS = 3600   # 自检周期（秒）
INTEGRITY_LOOKBACK_MIN = 5       # 对比最近 N 分钟
INTEGRITY_MIN_RATIO = 0.5        # 低于基线 50% 告警


def integrity_check():
    """对比最近 N 分钟与更早一小时（5~65分钟前）的每分钟行数/设备覆盖均值，
    低于 INTEGRITY_MIN_RATIO 时打 WARNING——源端大面积断供早发现。
    仅读库打日志，不影响采集。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            WITH m AS (
                SELECT date_trunc('minute', ts) AS mi,
                       COUNT(*) AS rows, COUNT(DISTINCT device_id) AS devs
                FROM {TABLE_DATA}
                WHERE ts >= now() - interval '{INTEGRITY_LOOKBACK_MIN} minutes'
                GROUP BY 1)
            SELECT COALESCE(avg(rows), 0), COALESCE(avg(devs), 0) FROM m
        """)
        recent_rows, recent_devs = cur.fetchone()
        cur.execute(f"""
            WITH m AS (
                SELECT date_trunc('minute', ts) AS mi,
                       COUNT(*) AS rows, COUNT(DISTINCT device_id) AS devs
                FROM {TABLE_DATA}
                WHERE ts >= now() - interval '{INTEGRITY_LOOKBACK_MIN + 60} minutes'
                  AND ts <  now() - interval '{INTEGRITY_LOOKBACK_MIN} minutes'
                GROUP BY 1)
            SELECT COALESCE(avg(rows), 0), COALESCE(avg(devs), 0) FROM m
        """)
        base_rows, base_devs = cur.fetchone()
    finally:
        conn.close()
    if base_rows == 0 and recent_rows == 0:
        log.warning('完整性自检: 最近 %s 分钟与基线均无数据（可能尚未采集或源端断供）',
                    INTEGRITY_LOOKBACK_MIN)
        return
    r_rows = recent_rows / base_rows if base_rows else 0.0
    r_devs = recent_devs / base_devs if base_devs else 0.0
    log.info('完整性自检: 最近%s分钟均值 %.0f 行/分 %.0f 台/分 | 基线 %.0f 行 %.0f 台'
             ' | 行数 %s%% 设备 %s%%', INTEGRITY_LOOKBACK_MIN, recent_rows,
             recent_devs, base_rows, base_devs,
             f'{r_rows * 100:.0f}', f'{r_devs * 100:.0f}')
    if r_rows < INTEGRITY_MIN_RATIO or r_devs < INTEGRITY_MIN_RATIO:
        log.warning('完整性自检: 行数或设备覆盖低于基线 %s%%，疑似源端断供，请检查！',
                    int(INTEGRITY_MIN_RATIO * 100))


# ---------------- 设备白名单（本地 wind_device 表） ----------------
# wind_device.wind_device_id 即白名单：只有表中存在的设备才允许入库。
# 表内 ID 与入库 device_id 统一小写，join 时直接相等。
_whitelist_cache = None


def load_whitelist():
    """读取 wind_device 表的 wind_device_id，返回小写 ID 集合（进程内缓存）。"""
    global _whitelist_cache
    if _whitelist_cache is not None:
        return _whitelist_cache
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT wind_device_id FROM {TABLE_DEVICE}')
        ids = {r[0].strip().lower() for r in cur.fetchall()
               if r[0] and r[0].strip()}
        cur.close()
    finally:
        conn.close()
    if not ids:
        raise RuntimeError('wind_device 白名单为空，终止拉取')
    _whitelist_cache = ids
    log.info('设备白名单: wind_device 表共 %s 台（ID 已转小写）', len(ids))
    return ids


def build_targets():
    """TARGETS 非空时直接使用；否则白名单设备 x Point_Codes 拼完整 tagName。

    远端 tagName 使用大写 ID，故拼 tagName 时白名单 ID 转大写。
    """
    if MOCK or TARGETS:
        return list(TARGETS)
    devices = sorted(load_whitelist())
    return [f'FJMJ1_{dev.upper()}{point_code}'
            for dev in devices for point_code in Point_Codes]


def filter_by_whitelist(items):
    """只保留 tagName 中设备 ID 命中白名单的消息（tag/like 模式入库前过滤）。

    wind_device 表保留原始大小写，白名单存小写；parse_tag 返回大写，
    比较时统一转小写，忽略大小写。
    """
    wl = load_whitelist()
    kept, dropped = [], 0
    for msg in items:
        try:
            dev, _ = parse_tag(msg.get('tagName', ''))
        except (KeyError, ValueError):
            dropped += 1
            continue
        if dev.lower() in wl:  # 白名单是小写集合，忽略大小写比较
            kept.append(msg)
        else:
            dropped += 1
    if dropped:
        log.info('白名单过滤: 丢弃 %s 条非白名单设备数据，保留 %s 条',
                 dropped, len(kept))
    return kept


# ---------------- 分区维护（30 天保留） ----------------
RETENTION_DAYS = 30


def ensure_partitions(future_days=7):
    """维护 wind_seconds_di 的 ts 日分区（幂等）：

    1. DROP 早于保留期（RETENTION_DAYS）的旧日分区，只保留最近
       RETENTION_DAYS 个自然日分区（含今天）；DROP 分区即瞬间释放磁盘。
    2. 补建今天起未来 future_days 天的分区——表无 DEFAULT 分区，
       不预建会导致跨天/长跑写入直接报错。
    分区命名：wind_seconds_di_YYYYMMDD。
    """
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).date()
    keep_from = today - timedelta(days=RETENTION_DAYS - 1)
    conn = connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT c.relname FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            WHERE i.inhparent = '{TABLE_DATA}'::regclass
        """)
        existing = {r[0] for r in cur.fetchall()}

        dropped = []
        for name in existing:
            suffix = name[len(PARTITION_PREFIX):]
            try:
                day = datetime.strptime(suffix, '%Y%m%d').date()
            except ValueError:
                continue  # 非标准命名分区不自动处理
            if day < keep_from:
                cur.execute(f'DROP TABLE IF EXISTS {name}')
                dropped.append(name)

        created = 0
        for offset in range(future_days + 1):
            day = today + timedelta(days=offset)
            start = datetime(day.year, day.month, day.day, tzinfo=tz)
            end = start + timedelta(days=1)
            name = PARTITION_PREFIX + start.strftime('%Y%m%d')
            cur.execute(
                f'CREATE TABLE IF NOT EXISTS {name} PARTITION OF '
                f'{TABLE_DATA} FOR VALUES FROM (%s) TO (%s)',
                (start, end))
            created += name not in existing
        cur.close()
        if dropped:
            log.info('分区保留 %s 天: DROP %s 个旧分区 %s ~ %s',
                     RETENTION_DAYS, len(dropped),
                     sorted(dropped)[0], sorted(dropped)[-1])
        log.info('分区检查: 新建 %s 个，保留自 %s，已确保至 %s',
                 created, keep_from, today + timedelta(days=future_days))
    finally:
        conn.close()


# ---------------- 水位线（本地文件，不建数据库表） ----------------
def load_watermark():
    """返回窗口右边界 'YYYY-MM-DD HH:MM:SS'，首次运行返回 None。"""
    if not os.path.exists(WATERMARK_FILE):
        return None
    with open(WATERMARK_FILE, encoding='utf-8') as f:
        return json.load(f).get(SOURCE)


def save_watermark(last_ts):
    """原子写入：先写临时文件再 replace，避免半写文件污染下轮启动。"""
    tmp = WATERMARK_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({SOURCE: last_ts}, f, ensure_ascii=False)
    os.replace(tmp, WATERMARK_FILE)


# ---------------- 接口客户端 ----------------
def post_json(payload):
    """发一次请求并解析 JSON。网络错误 / HTTP 非 2xx / JSON 解析失败均重试；
    业务错误（data 为空，重试无意义）直接抛出。"""
    headers = {'zb-token': API_TOKEN, 'Content-Type': 'application/json'}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(API_URL, headers=headers,
                              data=json.dumps(payload), timeout=TIMEOUT)
            r.raise_for_status()
            rj = r.json()
            if rj.get('data') is None:
                raise RuntimeError(f'接口返回异常: {rj}')
            return rj
        except RuntimeError:
            raise
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.HTTPError, ValueError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                log.warning('[retry %s/%s] 请求异常: %s，%ss 后重试...',
                            attempt, MAX_RETRIES, e.__class__.__name__,
                            RETRY_INTERVAL)
                time.sleep(RETRY_INTERVAL)
            else:
                raise
    raise last_err


def criterion_eq_tag(tag):
    """tag_name = 等值条件（按 tag 精准拉取）。"""
    return {'columnType': 'String', 'columnName': 'tag_name',
            'condition': '=', 'parameter': tag}


def criterion_like_point_code(point_code):
    """tag_name LIKE 模糊条件（按测点后缀拉取，% 为 SQL 通配符）。"""
    return {'columnType': 'String', 'columnName': 'tag_name',
            'condition': 'like', 'parameter': f'%{point_code}'}


def _covered_by_like(point_code, patterns):
    """point_code 是否已被某个 SQL LIKE 模式覆盖（% 任意串、_ 单字符）。

    用于 like 模式去重：模式覆盖的精确测点不必再单独发请求。
    实现按 SQL 语义转成正则：先转义字面字符，再把 % / _ 换成通配。
    """
    for p in patterns:
        rx = re.escape(p).replace('%', '.*').replace('_', '.')
        if re.fullmatch(rx, point_code):
            return True
    return False


def fetch_target(criteria, tag_criterion=None):
    """按 criteria(+可选 tag 条件) 查询并翻页取全，返回 (items, total)。

    第 1 页按 PAGE_SIZE 请求并读取 total；总页数 >1 时其余页并发抓取
    （整窗模式数据量大时收益明显）。
    """
    qc = ([tag_criterion] if tag_criterion is not None else []) \
        + list(criteria)
    base_map = {
        'stb': DATA_STB,
        'queryCriteria': qc,
        'sortType': 'desc',
        'sortField': 'ts',
    }
    rj = post_json({'type': 1029, 'map': {
        **base_map, 'pageNum': 1, 'pageSize': PAGE_SIZE}})
    total = rj.get('data', {}).get('total', 0) or 0
    items = list(rj.get('data', {}).get('list', []) or [])
    n_pages = -(-total // PAGE_SIZE)  # ceil(total / PAGE_SIZE)
    if n_pages > 1:
        page_map = {}
        with ThreadPoolExecutor(
                max_workers=min(FETCH_WORKERS, n_pages - 1)) as pool:
            futures = {
                pool.submit(post_json, {'type': 1029, 'map': {
                    **base_map, 'pageNum': p, 'pageSize': PAGE_SIZE}}): p
                for p in range(2, n_pages + 1)}
            for fut in as_completed(futures):
                page_map[futures[fut]] = fut.result()
        for p in range(2, n_pages + 1):  # 按页序拼接，保证顺序稳定
            items.extend(page_map[p].get('data', {}).get('list', []) or [])
    return items, total


def fetch_window(start, end):
    """按 FETCH_MODE 拉取窗口 [start, end) 内的原始消息，汇总返回。

    window: 1 个查询；like: 每后缀 1 个查询并发；tag: 每 tag 1 个查询并发。
    tag/like 模式返回前按白名单过滤；window 模式拉全量不做白名单过滤。
    任一请求失败即整体抛异常：本轮不入库、水位线不推进，
    下一轮整窗重拉（幂等）。
    """
    if MOCK:
        return mock_window(start, end)
    criteria = [
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '>=', 'parameter': start},
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '<', 'parameter': end},
    ]
    all_items = []

    if FETCH_MODE == 'window':
        items, total = fetch_target(criteria)
        log.debug('整窗: total=%s, fetched=%s', total, len(items))
        all_items.extend(items)
        return all_items

    if FETCH_MODE == 'like':
        # 精确测点 + 额外模糊模式；入库端仍由 TAG_MAP 精确归列，未映射安全。
        # 已被某模糊模式覆盖的精确测点跳过（该模式请求会带回同批数据）。
        like_targets = [s for s in Point_Codes
                        if not _covered_by_like(s, EXTRA_LIKE_PATTERNS)] \
                       + list(EXTRA_LIKE_PATTERNS)
        skipped = len(Point_Codes) + len(EXTRA_LIKE_PATTERNS) - len(like_targets)
        if skipped:
            log.debug('like: %d 个精确测点已被模糊模式覆盖，跳过', skipped)
        jobs = [(criterion_like_point_code(s), s) for s in like_targets]
    else:  # tag
        jobs = [(criterion_eq_tag(t), t) for t in build_targets()]
    if not jobs:
        return all_items
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(jobs))) as pool:
        futures = {pool.submit(fetch_target, criteria, jc): label
                   for jc, label in jobs}
        for fut in as_completed(futures):
            label = futures[fut]
            items, total = fut.result()
            log.debug('%s: total=%s, fetched=%s', label, total, len(items))
            all_items.extend(items)
    # tag/like 模式：入库前按白名单过滤
    return filter_by_whitelist(all_items)


def mock_window(start, end):
    """离线模拟：3 设备 x (Point_Codes + 1 个未映射测点)，ts 打窗口右边界。"""
    devices = ['b524f6d0b8ff4b2dbc0c102fc4b032b9',
               'a111f6d0b8ff4b2dbc0c102fc4b032b8',
               'c222f6d0b8ff4b2dbc0c102fc4b032b7']
    tags = list(Point_Codes) + ['WGEN.NOTMAPPED']
    return [{
        'pointValue': round(random.uniform(5, 60), 1),
        'description': '风机秒级数据',
        'tagName': 'FJMJ1_' + dev.upper() + tag,
        'ts': end,
    } for dev in devices for tag in tags]


# ---------------- 主循环 ----------------
def tick(now=None):
    """执行一轮「算窗口 -> 按 FETCH_MODE 拉全 -> 入库 -> 落水位线」。

    返回 (start, end, fetched, ingested, caught_up)；失败时抛出异常，
    水位线文件不变，下一轮从同一位置重拉。
    """
    if now is None:
        now = datetime.now().replace(microsecond=0)
    wm = load_watermark()
    if wm:
        start = datetime.strptime(wm, TS_FMT) \
            - timedelta(seconds=OVERLAP_SECONDS)
    else:
        start = now - timedelta(seconds=INITIAL_LOOKBACK_SECONDS)
    # 单窗封顶，停机很久后逐窗追赶而不是发超大请求
    end = min(now, start + timedelta(seconds=MAX_WINDOW_SECONDS))
    start_s, end_s = start.strftime(TS_FMT), end.strftime(TS_FMT)

    raw = fetch_window(start_s, end_s)
    if log.isEnabledFor(logging.DEBUG):
        printed = 0
        for msg in raw:
            parsed = parse_message(msg)
            if parsed is not None:
                dev, ts, points = parsed
                log.debug('入库 %s  %s  %s', dev, ts, points)
                printed += 1
        log.debug('---- 实际入库 %s 条（拉取 %s 条，丢弃 %s 条未映射测点）',
                  printed, len(raw), len(raw) - printed)
    n = ingest_raw(raw)
    save_watermark(end_s)  # 入库成功后才推进
    return start_s, end_s, len(raw), n, end >= now


MODE_LABELS = {'window': '整窗拉取', 'like': '按测点后缀like', 'tag': '按tag精准'}


def main():
    setup_logging()
    mode = '模拟' if MOCK else '真实接口'
    if FETCH_MODE == 'tag':
        scope = f'TARGETS={len(TARGETS)} 个tag' if TARGETS \
            else f'白名单 {len(load_whitelist())} 台 x {len(Point_Codes)} 个测点'
    elif FETCH_MODE == 'like':
        scope = f'{len(Point_Codes)} 个后缀 like，按白名单 {len(load_whitelist())} 台过滤入库'
        if EXTRA_LIKE_PATTERNS:
            scope += f' + {len(EXTRA_LIKE_PATTERNS)} 个模糊模式 {EXTRA_LIKE_PATTERNS}'
    else:
        scope = '只按 ts 过滤一次拉全（设备数无关，不做白名单过滤）'
    wm = load_watermark()
    wm_desc = wm if wm else f'无（首次回看 {INITIAL_LOOKBACK_SECONDS}s）'
    if LOG_TO_FILE:
        log.info('日志同时输出到控制台和 %s（按天轮转，保留 %s 天）',
                 LOG_FILE, LOG_BACKUP_DAYS)
    else:
        log.info('日志只输出到控制台（LOG_TO_FILE=False）')
    log.info('开始拉取（%s，%s，%s），每轮间隔 %ss，重叠 %ss，'
             '水位线 %s（文件 %s），Ctrl+C 终止',
             mode, MODE_LABELS[FETCH_MODE], scope, FETCH_INTERVAL_SECONDS,
             OVERLAP_SECONDS, wm_desc, WATERMARK_FILE)
    # 启动时维护分区（30 天保留 + 未来 7 天），确保跨天写入不报错
    try:
        ensure_partitions()
    except Exception as e:
        log.warning('分区维护失败（不影响采集，但跨天可能报错）: %r', e)
    # 进循环前先执行一次：初始化聚合表；失败不阻断采集（下一轮跨分钟会重试）。
    startup_min = datetime.now().strftime(TS_FMT)[:16]
    last_rollup_min = startup_min
    try:
        rollup_minute()
        log.info('启动初始化：聚合表就绪')
    except Exception as e:
        log.warning('启动初始化失败（不影响采集，下轮跨分钟重试）: %r', e)
    last_integrity = time.time()  # 启动满一个自检周期后开始
    while True:
        stamp = datetime.now().strftime(TS_FMT)
        try:
            start, end, fetched, ingested, caught_up = tick()
            log.info('窗口 [%s, %s) 拉取 %s 条，入库 %s 条，水位线 -> %s',
                     start, end, fetched, ingested, end)
            this_min = stamp[:16]  # 'YYYY-MM-DD HH:MM'
            if this_min != last_rollup_min:  # 跨分钟即触发，失败不影响水位线
                last_rollup_min = this_min
                try:
                    rollup_minute()
                    log.info('分钟聚合完成（重算最近3个已结束分钟）')
                except Exception as e:
                    log.warning('分钟聚合失败（下轮跨分钟时重试）: %r', e)
        except Exception as e:
            caught_up = True  # 失败时不进入追赶快转
            log.warning('本轮失败，水位线不推进，%ss 后从原位重拉: %r',
                        FETCH_INTERVAL_SECONDS, e)
        if time.time() - last_integrity >= INTEGRITY_CHECK_SECONDS:
            last_integrity = time.time()
            try:
                integrity_check()
            except Exception as e:
                log.warning('完整性自检失败（下个周期重试）: %r', e)
        if caught_up:
            time.sleep(FETCH_INTERVAL_SECONDS)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('已停止拉取')
