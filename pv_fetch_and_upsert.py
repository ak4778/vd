"""统一入口：从本地 pv_station_invert 白名单发现逆变器 -> 按时间窗拉取 -> upsert 到 pv_string_branch。

光伏组串/支路秒级数据采集（长表：每个测点一行）。

不丢数据机制（每秒产生多少条无需预知）：
1. 水位线文件 pv_watermark.json：记录上一轮成功入库的窗口右边界；进程重启
   从水位线继续。只在入库成功后写文件（临时文件 + os.replace 原子替换）。
2. 每轮窗口半开区间 [last_ts - overlap, end)，回退 OVERLAP_SECONDS 重叠拉取。
3. 每个 like 模式查询按 pageSize 分页，翻页直到取完。
4. 拉取/入库失败则不写水位线，下一轮从原位重拉。
5. 停机很久后恢复时，单窗被限制在 MAX_WINDOW_SECONDS，逐窗快速追平。
6. 采集时段：每天仅在 COLLECT_WINDOWS（默认 [(10:00, 16:00)]，两端整点均含，
   支持多段如 10-11、12-13、14-16）内采集，其余时间空闲等待；
   窗口起点/终点均裁剪到所在时段内。

拉取方式（like）：每个测点前缀发一次 tag_name LIKE 'GFMJ1_%{pattern}' 查询，
请求数 = 模式数，不受设备数影响。入库前按 pv_station_invert 白名单过滤。

tagName 格式：GFMJ1_{大写inverter_id}{point_code}
  例：GFMJ1_4967E9E443874D76AE4298D57AE5162ESTRING_BRANCH3_A
      inverter_id = 4967E9E443874D76AE4298D57AE5162E
      point_code  = STRING_BRANCH3_A
parse_tag 用 POINT_PREFIXES 里的前缀定位 inverter_id 与 point_code 的分割点。
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler

import psycopg2
import requests
from psycopg2.extras import execute_values

log = logging.getLogger('pv_fetch')

# ---------------- 表名配置 ----------------
TABLE_DATA = 'pv_string_branch'             # 秒级数据明细表（按 ts 日分区，长表）
TABLE_DEVICE = 'pv_station_invert'           # 白名单台账表（逆变器清单）
PARTITION_PREFIX = TABLE_DATA + '_'
# 分区 fillfactor：留 10% 页内空间，让重复 upsert 的 UPDATE 尽量走 HOT
# （不新增索引条目），显著降低表与索引膨胀。
FILLFACTOR = 90

# ---------------- 接口配置 ----------------
API_URL = 'http://10.65.78.65:18082/queryData'
API_TOKEN = '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9'
DATA_STB = 'ods_ly_data_hub_gfmjsj_new'          # type=1029 数据表（光伏）
PAGE_SIZE = 1000   # 服务端对大响应中途断连，1000 实测稳定
TIMEOUT = 60        # 40 个 pattern 下回补块数据量翻倍，上游单查询需 60s+，放宽超时减少无意义重试
MAX_RETRIES = 3
RETRY_INTERVAL = 5

# ---------------- 测点配置 ----------------
# 测点 LIKE 模式列表（不含 GFMJ1_ 前缀），接口 parameter = GFMJ1_%{pattern}
# 每个 pattern 中的字面前缀（% 之前的部分）用于 parse_tag 定位 inverter_id
#
# 拆成 40 个精确组串号（不用单通配 STRING_BRANCH%_A）：
# 单通配模式下一个请求匹配全部 14037 台逆变器的所有组串，响应体过大，
# 早上数据高峰期触发 API 大响应断连/ReadTimeout；拆分后单请求响应小 40 倍，
# 与 wind_fetch 的 19 后缀分散模式一致。
# 2026-10 确认接口存在 33~40 号组串（24 台 8 位短码逆变器，均在白名单内），扩展到 40。
POINT_PATTERNS = [f'STRING_BRANCH{i}_A' for i in range(1, 41)]  # 组串电流 1~40
# 从 patterns 自动提取测点前缀（% 之前的字面部分），用于 tagName 解析
POINT_PREFIXES = [p[:p.index('%')] if '%' in p else p for p in POINT_PATTERNS]

TAG_PREFIX_LEN = 6  # 'GFMJ1_' 的长度

# ---------------- 入库 SQL ----------------
UPSERT_SQL = f"""
INSERT INTO {TABLE_DATA} (device_id, point_code, point_value, ts)
VALUES %s
ON CONFLICT (device_id, ts, point_code) DO UPDATE SET
    point_value = EXCLUDED.point_value
WHERE {TABLE_DATA}.point_value IS DISTINCT FROM EXCLUDED.point_value
"""
_ROW_TEMPLATE = '(%s, %s, %s, %s)'

TS_FMT = '%Y-%m-%d %H:%M:%S'


def connect():
    with open('data_config.json', encoding='utf-8') as f:
        return psycopg2.connect(json.load(f)['pgConnStr'])


def parse_tag(tag_name):
    """GFMJ1_{大写inverter_id}{point_code} -> (inverter_id_upper, point_code)。

    用 POINT_PREFIXES 里的前缀在去掉 GFMJ1_ 后的 body 中定位分割点：
    前缀之前是 inverter_id，前缀及之后是 point_code。
    无匹配前缀返回 None。
    """
    body = tag_name[TAG_PREFIX_LEN:]
    for prefix in POINT_PREFIXES:
        idx = body.find(prefix)
        if idx > 0:
            return body[:idx].upper(), body[idx:]
    return None


def parse_message(msg):
    """解析一条原始消息 -> (device_id, point_code, point_value(float), ts) 或 None。

    pointValue 转 float，转换失败（None/非数值）的丢弃。
    """
    parsed = parse_tag(msg.get('tagName', ''))
    if parsed is None:
        return None
    dev, pc = parsed
    val = msg.get('pointValue')
    try:
        val = float(val) if val is not None else None
    except (ValueError, TypeError):
        return None  # 非数值丢弃（point_value 是 double precision，无法存文本）
    return dev, pc, val, msg.get('ts')


def ingest_messages(messages, wl=None):
    """解析原始消息，过滤白名单与未来时间，批量 upsert 到 pv_string_branch。

    返回实际入库条数（丢弃的不计）。可选传入白名单集合，避免每条流式调用都重读。
    """
    if wl is None:
        wl = load_whitelist()
    future_limit = datetime.now() + timedelta(seconds=FUTURE_FILTER_SECONDS)
    rows = []
    future_dropped = 0
    for msg in messages:
        parsed = parse_message(msg)
        if parsed is None:
            continue
        dev, pc, val, ts = parsed
        if dev not in wl:
            continue
        # 33 号及以上组串仅绥中屋顶场站（load_extended_whitelist）入库，其余丢弃
        m = _BRANCH_RE.match(pc)
        if m and int(m.group(1)) > BASE_MAX_BRANCH \
                and dev not in load_extended_whitelist():
            continue
        # 未来时间防御 + 采集时段硬过滤
        try:
            ts_dt = datetime.strptime(ts, TS_FMT)
            if ts_dt > future_limit:
                future_dropped += 1
                continue
            # 只保留落在当天采集时段内的 ts，设备时钟飘移导致的时段外数据一律丢弃
            t = ts_dt.time()
            if not any(o <= t < c for o, c in COLLECT_WINDOWS):
                future_dropped += 1
                continue
        except ValueError:
            pass  # 解析失败的保守保留
        rows.append((dev, pc, val, ts))
    if future_dropped:
        log.warning('discard %d 条异常时间数据（未来时间或非采集时段），阈值=%ds',
                    future_dropped, FUTURE_FILTER_SECONDS)
    if not rows:
        return 0
    # 按 (device_id, ts, point_code) 去重：后到覆盖先到，避免 execute_values
    # 批量插入时同一批内重复键触发 CardinalityViolation
    dedup = {}
    for dev, pc, val, ts in rows:
        dedup[(dev, pc, ts)] = (dev, pc, val, ts)
    rows = list(dedup.values())
    conn = connect()
    try:
        cur = conn.cursor()
        execute_values(cur, UPSERT_SQL, rows,
                       template=_ROW_TEMPLATE, page_size=10000)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------- 窗口与轮询 ----------------
OVERLAP_SECONDS = 120
# 重叠 2 分钟：上游按设备分批发布（同一秒的数据可能先到一部分），
# 每轮重拉近 2 分钟，晚到的设备行在后续轮次被补齐（upsert 幂等）。
INITIAL_LOOKBACK_SECONDS = 300
# 必须大于 OVERLAP_SECONDS：窗口终点是 start + MAX_WINDOW，
# 若 MAX_WINDOW <= OVERLAP，终点会落回水位线之前，导致水位线不前进、
# 原地无限重拉同一个窗口。重叠越大重复写入越多，膨胀越严重。
MAX_WINDOW_SECONDS = 240
FETCH_INTERVAL_SECONDS = 30
FETCH_WORKERS = 4
FUTURE_FILTER_SECONDS = 30 * 60
_empty_streak = 0                           # 连续「拉取 0 条」的轮数（仅用于告警计数）

# ---------------- 采集时段（每天仅此时段采集，其余时间空闲；支持多段） ----------------
# 每段为 (开始, 结束)，两端整点均含（结束整点那一秒也采集）。
# 例：[(dtime(10), dtime(11)), (dtime(12), dtime(13)), (dtime(14), dtime(16))]
COLLECT_WINDOWS = [
    (dtime(10, 0, 0), dtime(11, 0, 0)),
    (dtime(12, 0, 0), dtime(13, 0, 0)),
    (dtime(14, 0, 0), dtime(16, 0, 0)),
]
IDLE_SLEEP_SECONDS = 60                    # 非采集时段的轮询间隔
IDLE_HEARTBEAT_SECONDS = 600               # 空闲期每多少秒输出一次心跳（证明进程活着，且提示下一时段）
WINDOW_FLUSH_SECONDS = MAX_WINDOW_SECONDS   # 时段结束后收尾补采的宽限秒数

MOCK = False
LOG_LEVEL = 'INFO'

SOURCE = 'gfmjsj'
WATERMARK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'pv_watermark.json')
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'pv_fetch.log')
LOG_TO_FILE = False
LOG_BACKUP_DAYS = 30

# ---------------- 历史分区空间回收 ----------------
# upsert 会把同一行反复 UPDATE，autovacuum 只回收可复用空间、不缩小文件，
# 所以每天凌晨对「昨天」的分区做一次 VACUUM FULL，把空间真正还给磁盘。
VACUUM_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'pv_vacuum_state.json')
VACUUM_HOUR_RANGE = (2, 5)      # 仅在 02:00-04:59 执行，避开采集时段
VACUUM_LOCK_TIMEOUT = '10s'     # 拿不到排他锁就跳过，下一个空闲周期重试

# ---------------- 数据完整性自检 ----------------
INTEGRITY_CHECK_SECONDS = 3600
INTEGRITY_LOOKBACK_MIN = 5
INTEGRITY_MIN_RATIO = 0.5

# ---------------- 延迟自动回补 ----------------
# 上游按设备分批发布：实时采集时绝大多数行尚未发布。实测同一 2 分钟窗口，
# 实时采集只入库 3010 行，71 分钟后同一接口能拉到 664285 行。
# 因此在空闲期把「已过发布稳定期」的历史块按同一接口重新回拉，由 upsert
# 幂等补齐。只在非采集时段执行，避免与实时采集抢接口和数据库连接。
REFETCH_DELAYS = (120 * 60,)      # 块结束需早于 now-该值（秒）；可配多轮
REFETCH_BLOCK_SECONDS = 60        # 每个回补块 1 分钟（块小、可随时中断续补）
REFETCH_WORKERS = 4               # 回补并发上限（与实时采集 FETCH_WORKERS 一致，实测上游可承受）
REFETCH_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'pv_refetch.json')


def setup_logging():
    level = logging.getLevelName(str(LOG_LEVEL).upper())
    if not isinstance(level, int):
        raise ValueError(f'非法 LOG_LEVEL={LOG_LEVEL!r}')
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


def integrity_check():
    """对比最近 N 分钟与基线的每分钟行数/设备覆盖，低于阈值告警。"""
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
        log.warning('完整性自检: 最近 %s 分钟与基线均无数据', INTEGRITY_LOOKBACK_MIN)
        return
    r_rows = recent_rows / base_rows if base_rows else 0.0
    r_devs = recent_devs / base_devs if base_devs else 0.0
    log.info('完整性自检: 最近%s分钟 %.0f行/分 %.0f台/分 | 基线 %.0f行 %.0f台 | %s%% %s%%',
             INTEGRITY_LOOKBACK_MIN, recent_rows, recent_devs,
             base_rows, base_devs, f'{r_rows*100:.0f}', f'{r_devs*100:.0f}')
    if r_rows < INTEGRITY_MIN_RATIO or r_devs < INTEGRITY_MIN_RATIO:
        log.warning('完整性自检: 低于基线 %s%%，疑似源端断供',
                    int(INTEGRITY_MIN_RATIO * 100))

# ---------------- 设备白名单（pv_station_invert 表） ----------------
_whitelist_cache = None


def load_whitelist():
    """读取 pv_station_invert 表的 inverter_id，返回小写 ID 集合（进程内缓存）。"""
    global _whitelist_cache
    if _whitelist_cache is not None:
        return _whitelist_cache
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT inverter_id FROM {TABLE_DEVICE}')
        ids = {r[0].strip().upper() for r in cur.fetchall()
               if r[0] and r[0].strip()}  # 内存中转大写，不改数据库原始值
        cur.close()
    finally:
        conn.close()
    if not ids:
        raise RuntimeError(f'{TABLE_DEVICE} 白名单为空，终止拉取')
    _whitelist_cache = ids
    log.info('设备白名单: %s 表共 %s 台逆变器（ID 已转大写）',
             TABLE_DEVICE, len(ids))
    return ids


# ---------------- 扩展组串（33~40）仅限绥中屋顶分布式场站 ----------------
# 该场站（3 箱变 × 8 台 = 24 台）为 40 路组串机型；其他场站只采 1~32 号组串，
# 即便将来其他场站出现 >32 号组串的数据也直接丢弃，不入库。
EXTENDED_BRANCH_STATION_ID = 'c12sYPBD'   # 绥中电厂场内屋顶分布式光伏
BASE_MAX_BRANCH = 32
_BRANCH_RE = re.compile(r'^STRING_BRANCH(\d+)_A$')
_extended_wl_cache = None


def load_extended_whitelist():
    """读取允许 33~40 号组串的逆变器集合（绥中屋顶场站，进程内缓存）。"""
    global _extended_wl_cache
    if _extended_wl_cache is not None:
        return _extended_wl_cache
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT inverter_id FROM {TABLE_DEVICE} WHERE station_id = %s',
                    (EXTENDED_BRANCH_STATION_ID,))
        ids = {r[0].strip().upper() for r in cur.fetchall()
               if r[0] and r[0].strip()}
        cur.close()
    finally:
        conn.close()
    _extended_wl_cache = ids
    log.info('扩展组串白名单: 场站 %s 共 %s 台逆变器可采 33~%d 号组串',
             EXTENDED_BRANCH_STATION_ID, len(ids), len(POINT_PATTERNS))
    return ids


# ---------------- 分区维护（7 天保留） ----------------
RETENTION_DAYS = 7


def ensure_partitions(future_days=7):
    """维护 pv_string_branch 的 ts 日分区（幂等）：DROP 超期 + 补建未来。"""
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
                continue
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
                f'{TABLE_DATA} FOR VALUES FROM (%s) TO (%s) '
                f'WITH (fillfactor={FILLFACTOR})',
                (start, end))
            created += name not in existing
        cur.close()
        if dropped:
            log.info('分区保留 %s 天: DROP %s 个旧分区', RETENTION_DAYS, len(dropped))
        log.info('分区检查: 新建 %s 个，保留自 %s，已确保至 %s',
                 created, keep_from, today + timedelta(days=future_days))
    finally:
        conn.close()


def load_vacuum_state():
    try:
        with open(VACUUM_STATE_FILE, encoding='utf-8') as f:
            return json.load(f).get('last_vacuumed')
    except Exception:
        return None


def save_vacuum_state(day_s):
    tmp = VACUUM_STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'last_vacuumed': day_s}, f, ensure_ascii=False)
    os.replace(tmp, VACUUM_STATE_FILE)


def vacuum_yesterday_partition(now):
    """每天凌晨对「昨天」的分区执行一次 VACUUM FULL（幂等，按日期记状态）。

    历史分区已停止写入，排他锁不影响采集；成功后才落状态，失败则下个空闲
    周期自动重试。
    """
    if not (VACUUM_HOUR_RANGE[0] <= now.hour < VACUUM_HOUR_RANGE[1]):
        return
    tz = timezone(timedelta(hours=8))
    day_s = (now.replace(tzinfo=tz).date() - timedelta(days=1)).strftime('%Y%m%d')
    if load_vacuum_state() == day_s:
        return
    name = PARTITION_PREFIX + day_s
    conn = connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("""SELECT 1 FROM pg_inherits
                       WHERE inhrelid = to_regclass(%s)::oid""", (name,))
        if cur.fetchone() is None:
            log.info('历史分区 %s 不存在，跳过 VACUUM FULL', name)
            save_vacuum_state(day_s)
            return
        cur.execute(f"SET lock_timeout = '{VACUUM_LOCK_TIMEOUT}'")
        # 服务器默认 statement_timeout=10min，大分区重写会超时被取消
        cur.execute('SET statement_timeout = 0')
        # 历史分区不再写入：恢复 fillfactor=100 让重写后页面装得更满
        cur.execute(f'ALTER TABLE {name} RESET (fillfactor)')
        cur.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{name}'))")
        before = cur.fetchone()[0]
        t0 = time.time()
        log.info('开始 VACUUM FULL %s（当前 %s）', name, before)
        cur.execute(f'VACUUM (FULL, ANALYZE) {name}')
        cur.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{name}'))")
        after = cur.fetchone()[0]
        log.info('VACUUM FULL %s 完成: %s -> %s，耗时 %.0fs',
                 name, before, after, time.time() - t0)
        save_vacuum_state(day_s)
    finally:
        conn.close()


# ---------------- 水位线 ----------------
def load_watermark():
    if not os.path.exists(WATERMARK_FILE):
        return None
    with open(WATERMARK_FILE, encoding='utf-8') as f:
        return json.load(f).get(SOURCE)


def save_watermark(last_ts):
    tmp = WATERMARK_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({SOURCE: last_ts}, f, ensure_ascii=False)
    os.replace(tmp, WATERMARK_FILE)


# ---------------- 接口客户端 ----------------
def post_json(payload):
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


def criterion_like_pattern(pattern):
    """tag_name LIKE 条件：parameter = GFMJ1_%{pattern}"""
    return {'columnType': 'String', 'columnName': 'tag_name',
            'condition': 'like', 'parameter': f'GFMJ1_%{pattern}'}


def fetch_target(criteria, tag_criterion=None, workers=None):
    """按 criteria(+可选 tag 条件) 查询并翻页取全，返回 (items, total)。

    workers 为空时用全局 FETCH_WORKERS；回补线程传入独立值，避免与实时采集互相影响。
    """
    w = workers or FETCH_WORKERS
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
    n_pages = -(-total // PAGE_SIZE)
    if n_pages > 1:
        page_map = {}
        with ThreadPoolExecutor(
                max_workers=min(w, n_pages - 1)) as pool:
            futures = {
                pool.submit(post_json, {'type': 1029, 'map': {
                    **base_map, 'pageNum': p, 'pageSize': PAGE_SIZE}}): p
                for p in range(2, n_pages + 1)}
            for fut in as_completed(futures):
                page_map[futures[fut]] = fut.result()
        for p in range(2, n_pages + 1):
            items.extend(page_map[p].get('data', {}).get('list', []) or [])
    return items, total


def fetch_and_ingest_window(start, end, workers=None):
    """按 like 模式拉取窗口 [start, end) 内的原始消息并流式入库。

    每个 pattern 拉完自己的全部页立刻 ingest，避免「全部 pattern 拉完再统一入库」
    的串行等待，让拉取与入库在并发线程内交错进行，缩短单轮总耗时。
    workers 为空时用全局 FETCH_WORKERS；回补线程传入独立值，避免改全局影响实时采集。
    返回 (fetched, ingested)。任一请求/入库失败即整体抛异常：本轮不入库、水位线不推进。
    """
    w = workers or FETCH_WORKERS
    if MOCK:
        items = mock_window(start, end)
        return len(items), ingest_messages(items)
    criteria = [
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '>=', 'parameter': start},
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '<', 'parameter': end},
    ]
    jobs = [(criterion_like_pattern(p), p) for p in POINT_PATTERNS]
    if not jobs:
        return 0, 0
    wl = load_whitelist()  # 提前填充缓存，避免子线程首次 ingest 时竞争查询
    load_extended_whitelist()
    total_fetched = 0
    total_ingested = 0
    with ThreadPoolExecutor(max_workers=min(w, len(jobs))) as pool:
        futures = {pool.submit(_fetch_and_ingest_one, jc, label, criteria, wl, w): label
                   for jc, label in jobs}
        for fut in as_completed(futures):
            label, fetched, ingested = fut.result()
            log.debug('%s: fetched=%s, ingested=%s', label, fetched, ingested)
            total_fetched += fetched
            total_ingested += ingested
    return total_fetched, total_ingested


def _fetch_and_ingest_one(tag_criterion, label, criteria, wl, workers=None):
    """拉取单个 pattern 的全部页并立即入库该 pattern 的数据。"""
    items, total = fetch_target(criteria, tag_criterion, workers=workers)
    n = ingest_messages(items, wl)
    return label, len(items), n


def mock_window(start, end):
    """离线模拟：2 逆变器 x 测点前缀。"""
    devices = ['4967e9e443874d76ae4298d57ae5162e',
               '10c016a2571f4031b7407e02337d7330']
    items = []
    for dev in devices:
        for i in range(1, 4):
            items.append({
                'pointValue': round(5.0 + i * 0.5, 1),
                'description': '光伏组串电流',
                'tagName': f'GFMJ1_{dev.upper()}STRING_BRANCH{i}_A',
                'ts': end,
            })
    return items


# ---------------- 采集时段 ----------------
def _schedule_desc():
    """采集时段的可读描述，如 '10:00:00-11:00:00, 12:00:00-13:00:00'。"""
    return ', '.join(f'{s.strftime("%H:%M:%S")}-{e.strftime("%H:%M:%S")}'
                     for s, e in COLLECT_WINDOWS)


def _next_segment(now):
    """返回严格晚于 now 的下一个采集时段起点，跨天则取次日首段。"""
    today = now.date()
    for s, _e in COLLECT_WINDOWS:
        t = datetime.combine(today, s)
        if t > now:
            return t
    return datetime.combine(today + timedelta(days=1), COLLECT_WINDOWS[0][0])


def _active_window(now):
    """返回 now 所处的采集时段 [open, close)（close 已 +1s，排他）；无则 None。

    命中：open <= now < close，或处于时段结束后的收尾宽限内
    （close <= now < close + WINDOW_FLUSH_SECONDS，用于补完尾部）。
    close = 结束整点 + 1s，故结束整点那一秒也被采集。
    """
    for s, e in COLLECT_WINDOWS:
        open_ = datetime.combine(now.date(), s)
        close = datetime.combine(now.date(), e) + timedelta(seconds=1)
        if open_ <= now < close + timedelta(seconds=WINDOW_FLUSH_SECONDS):
            return open_, close
    return None


def collect_state(now):
    """判断当前是否应采集，返回 (active, win_open, win_close)。

    active=False：不在任何采集时段（含收尾宽限）；或所在时段已采集完毕
    （水位线已达该时段右边界）。win_open/win_close 为 None 表示无时段。
    """
    win = _active_window(now)
    if win is None:
        return False, None, None
    win_open, win_close = win
    wm = load_watermark()
    if wm and datetime.strptime(wm, TS_FMT) >= win_close:
        return False, win_open, win_close
    return True, win_open, win_close


# ---------------- 延迟自动回补 ----------------
def load_refetch_state():
    """读取各轮回补的游标（下一待回补块起点）；缺失的位置返回 None。"""
    try:
        with open(REFETCH_STATE_FILE, encoding='utf-8') as f:
            saved = json.load(f).get('cursors') or []
    except (OSError, ValueError):
        saved = []
    cursors = []
    for i in range(len(REFETCH_DELAYS)):
        s = saved[i] if i < len(saved) else None
        cursors.append(datetime.strptime(s, TS_FMT) if s else None)
    return cursors


def save_refetch_state(cursors):
    """原子写回补游标（临时文件 + os.replace）。"""
    tmp = REFETCH_STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'cursors': [c.strftime(TS_FMT) if c else None
                               for c in cursors]}, f)
    os.replace(tmp, REFETCH_STATE_FILE)


def _align_block(dt):
    """把时间对齐到回补块边界（向下取整）。"""
    step = REFETCH_BLOCK_SECONDS // 60
    return dt.replace(minute=dt.minute - dt.minute % step,
                      second=0, microsecond=0)


def _first_block_of_day(day):
    """当天第一个采集时段的起点（块对齐）。"""
    return _align_block(datetime.combine(day, COLLECT_WINDOWS[0][0]))


def _block_in_collect_window(block_start):
    """block 起点是否落在当天某个采集时段内（含端点）。"""
    d = block_start.date()
    for open_t, close_t in COLLECT_WINDOWS:
        open_ = datetime.combine(d, open_t)
        close = datetime.combine(d, close_t)
        if open_ <= block_start < close:
            return True
    return False


def _advance_to_first_block(cur):
    """把游标推进到「当天下一个合法采集时段的对齐块起点」。
    若当前已超出当天最后一个时段，跳到明天第一块。
    """
    d = cur.date()
    # 先试今天
    for open_t, _close_t in COLLECT_WINDOWS:
        open_ = datetime.combine(d, open_t)
        aligned = _align_block(open_)
        if aligned >= cur:
            return aligned
    # 今天所有时段都已过，跳到明天首块
    return _first_block_of_day(d + timedelta(days=1))


def refetch_step(now):
    """空闲期执行一步延迟回补：每轮最多拉一个块。

    整块必须早于 now-REFETCH_DELAYS[i] 才算「已过发布稳定期」。
    回补块严格限在 COLLECT_WINDOWS 内——时段之外的 ts 即使上游接口有残留
    数据也不入库，与实时采集口径一致。
    成功或无需拉取后推进游标并落盘；异常时游标不动，下次空闲重试。
    返回是否执行了拉取。
    """
    cursors = load_refetch_state()
    for i, delay in enumerate(REFETCH_DELAYS):
        cur = cursors[i]
        if cur is None:
            cur = cursors[i] = _first_block_of_day(now.date())
        # 跨天时保留往日游标继续补，直到该日采集块全部补完才跳到下一天；
        # 直接重置为今天会让昨天没补完的块永久丢失。
        while not _block_in_collect_window(cur) and cur.date() < now.date():
            cur = _advance_to_first_block(cur)
        cursors[i] = cur
        # 若 block 起点已超出当天所有采集时段，跳到明天
        if not _block_in_collect_window(cur):
            cur = cursors[i] = _advance_to_first_block(cur)
            continue
        block_end = cur + timedelta(seconds=REFETCH_BLOCK_SECONDS)
        if block_end > _align_block(now - timedelta(seconds=delay)):
            continue                         # 该块尚未过发布稳定期
        a, b = cur.strftime(TS_FMT), block_end.strftime(TS_FMT)
        try:
            fetched, ingested = fetch_and_ingest_window(
                a, b, workers=REFETCH_WORKERS)
            log.info('延迟回补(延迟 %s 分钟) [%s, %s) 拉取 %s 条，补写 %s 条',
                     delay // 60, a, b, fetched, ingested)
        except Exception as e:
            log.warning('延迟回补失败 [%s, %s)，保持游标下次重试: %r', a, b, e)
            save_refetch_state(cursors)
            return False
        # 下一块：先 +1min，若已跨出当前时段，推进到当天下一个时段的起点；
        # 只有当天的时段全部补完才跳到次日首块（不能直接跳次日，否则会整段
        # 跳过同一天的后续采集时段，例如 12:59 -> 次日 10:00 会漏掉 14:00-16:00）。
        nxt = cur + timedelta(seconds=REFETCH_BLOCK_SECONDS)
        if not _block_in_collect_window(nxt):
            nxt = _advance_to_first_block(nxt)
        cursors[i] = nxt
        save_refetch_state(cursors)
        return True
    save_refetch_state(cursors)
    return False


def _refetch_worker():
    """后台回补线程：只在非采集时段补历史块，跑在主循环之外，绝不阻塞实时采集。"""
    while True:
        try:
            now = datetime.now()
            if not collect_state(now)[0] and refetch_step(now):
                continue                 # 补完一块立刻补下一块
        except Exception as e:
            log.warning('延迟回补异常（稍后重试）: %r', e)
        time.sleep(IDLE_SLEEP_SECONDS)


# ---------------- 主循环 ----------------
def tick(now=None):
    """执行一轮「算窗口 -> 拉取 -> 入库 -> 落水位线」。

    窗口裁剪到当前采集时段：起点不早于该时段左边界（避免空闲期间水位线
    停在上一时段/昨日而回拉旧数据），终点不晚于该时段右边界（含结束整点）。
    """
    if now is None:
        now = datetime.now().replace(microsecond=0)
    win = _active_window(now)
    if win is None:  # 不在采集时段（主循环已判 active，此处兜底）
        return now.strftime(TS_FMT), now.strftime(TS_FMT), 0, 0, True
    win_open, win_close = win
    wm = load_watermark()
    if wm:
        start = datetime.strptime(wm, TS_FMT) \
            - timedelta(seconds=OVERLAP_SECONDS)
    else:
        start = now - timedelta(seconds=INITIAL_LOOKBACK_SECONDS)
    if start < win_open:
        start = win_open
    end = min(now, start + timedelta(seconds=MAX_WINDOW_SECONDS), win_close)
    start_s, end_s = start.strftime(TS_FMT), end.strftime(TS_FMT)

    raw = fetch_and_ingest_window(start_s, end_s)
    fetched, ingested = raw
    global _empty_streak
    if fetched == 0:
        _empty_streak += 1
        if now < win_close:
            # 上游发布有 30~80 分钟延迟：时段未结束时拉到 0 条只说明数据尚未发布。
            # 水位线绝不推进，原地等待下一轮重拉同一窗口，数据发布后自愈式追平；
            # 只有时段结束后才放行——宁可整窗停滞也不静默丢数据。
            log.warning('本轮拉取 0 条（上游尚未发布或源端断供），水位线不推进，'
                        '原地等待重拉（已连续 %s 轮空拉取）: [%s, %s)',
                        _empty_streak, start_s, end_s)
            return start_s, end_s, fetched, ingested, True
        log.warning('采集时段已结束仍连续 %s 轮拉取 0 条，[%s, %s) 数据缺失，请事后回补',
                    _empty_streak, start_s, end_s)
    _empty_streak = 0
    save_watermark(end_s)
    return start_s, end_s, fetched, ingested, end >= now


def main():
    setup_logging()
    mode = '模拟' if MOCK else '真实接口'
    scope = f'{len(POINT_PATTERNS)} 个 like 模式 {POINT_PATTERNS}，按 {TABLE_DEVICE} 白名单过滤入库'
    wm = load_watermark()
    wm_desc = wm if wm else f'无（首次回看 {INITIAL_LOOKBACK_SECONDS}s）'
    if LOG_TO_FILE:
        log.info('日志同时输出到控制台和 %s（按天轮转，保留 %s 天）',
                 LOG_FILE, LOG_BACKUP_DAYS)
    else:
        log.info('日志只输出到控制台')
    log.info('开始拉取（%s，like 模式，%s），采集时段 每天 [%s]，'
             '每轮间隔 %ss，重叠 %ss，水位线 %s（文件 %s），Ctrl+C 终止',
             mode, scope, _schedule_desc(),
             FETCH_INTERVAL_SECONDS,
             OVERLAP_SECONDS, wm_desc, WATERMARK_FILE)
    try:
        ensure_partitions()
    except Exception as e:
        log.warning('分区维护失败（不影响采集）: %r', e)
    last_integrity = time.time()
    threading.Thread(target=_refetch_worker, name='refetch', daemon=True).start()
    idle = None  # None=未知, True=空闲中, False=采集时段内
    idle_cycles = 0  # 空闲循环累计轮数，用于周期性心跳
    while True:
        now = datetime.now()
        active, _wo, _wc = collect_state(now)
        if not active:
            if idle is not True:
                log.info('非采集时段（每天 [%s]），空闲等待', _schedule_desc())
                idle = True
                idle_cycles = 0
            else:
                idle_cycles += 1
                if idle_cycles * IDLE_SLEEP_SECONDS >= IDLE_HEARTBEAT_SECONDS:
                    log.info('空闲中，下一时段 %s',
                             _next_segment(now).strftime('%m-%d %H:%M:%S'))
                    idle_cycles = 0
            try:
                vacuum_yesterday_partition(now)
            except Exception as e:
                log.warning('历史分区 VACUUM FULL 失败（下个空闲周期重试）: %r', e)
            time.sleep(IDLE_SLEEP_SECONDS)
            continue
        if idle is not False:
            log.info('进入采集时段（每天 [%s]），恢复采集', _schedule_desc())
            idle = False
            idle_cycles = 0
            try:
                ensure_partitions()
            except Exception as e:
                log.warning('分区维护失败（不影响采集）: %r', e)
        try:
            start, end, fetched, ingested, caught_up = tick(now)
            log.info('窗口 [%s, %s) 拉取 %s 条，入库 %s 条，水位线 -> %s',
                     start, end, fetched, ingested, end)
        except Exception as e:
            caught_up = True
            log.warning('本轮失败，水位线不推进，%ss 后从原位重拉: %r',
                        FETCH_INTERVAL_SECONDS, e)
        if time.time() - last_integrity >= INTEGRITY_CHECK_SECONDS:
            last_integrity = time.time()
            try:
                integrity_check()
            except Exception as e:
                log.warning('完整性自检失败: %r', e)
        if caught_up:
            time.sleep(FETCH_INTERVAL_SECONDS)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('已停止拉取')
