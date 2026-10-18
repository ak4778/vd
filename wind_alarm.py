# -*- coding: utf-8 -*-
"""
发电机温度多测点实时监控
========================
监控测点见 POINT_CONFIG（驱动端温度 WGEN.TEMGENDRIEND / 非驱动端温度 WGEN.TEMGENNONDE），
每台风机 × 每个测点独立滑窗与告警判定。
设备白名单：本地 wind_device 表即监控白名单，只拉取表内风机（含元数据，不走 1022 接口）。
数据来源（DATA_SOURCE 切换）：
  - 'db'（默认）：本地 PG wind_seconds_di 表（wind_fetch_and_upsert.py 采集入库），
    两个监控测点对应固定列 tem_gen_driend / tem_gen_nonde；device_id 入库时统一小写。
  - 'api'：queryData HTTP 接口（1029 查询），设备×测点并发拉取。
两种来源都归一为 [{'ts': str, 'pointValue': float}, ...] 喂同一套滑窗与告警引擎。

监控规则（每台风机×测点独立判定，绝对值阈值按测点配置）：
  01 越限：v >= c01_fault(传感器故障) / v > c01_high(高温) / v <= c01_low(探头失效码)，逐点即时判定
  02 卡死：1 小时内所有点完全相同(max-min=0)，且窗口真实覆盖近1小时、数据新鲜
  03 波动：20 秒内 max-min > 10℃，且该状态持续 >= 5 秒（按数据点 ts 计量）
  数据中断：不单独输出告警；断采>15分钟时显式恢复 C02_FROZEN，防止告警永久挂起

数据存放：
  - 原始测点：内存滑窗（每台×测点 deque 保留近 1 小时），不落盘
  - 告警记录：① 追加写 ALARM_CSV（utf-8-sig），13列：ts, regionCompanyName,
    stationName, windDeviceId, windDeviceName, pointCode, pointName, temperature,
    temp_rate, range_1h, range_20s, alarm_type, alarm_value
    ② 同时 INSERT 到 PG 表 wind_alarm（事件流水：ALARM/RECOVER 各一行，
    含 event_time 写入时刻、data_ts 触发数据点时刻、rule_code、明细数值）；
    PG 写失败仅记日志，不影响 CSV 与监控主循环。启动时从该表最后事件回放
    活跃告警与冷却起点，重启不重复告警。
  - 超温原始点：追加写 EXCEED_CSV（utf-8-sig），8列含 pointCode
"""

import csv
import json
import os
import sys
import time
import threading
import psycopg2
import requests
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# 让 Windows 控制台中文输出尽量不报错
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================== 配置区 ==============================
URL = "http://10.65.78.65:18082/queryData"
ZB_TOKEN = '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9'
HEADERS = {'zb-token': ZB_TOKEN, 'Content-Type': 'application/json'}

DATA_STB = "ods_ly_data_hub_fjmjsj_new"                        # 测点数据表(1029)
TABLE_DEVICE = 'wind_device'   # 风机白名单台账表（本地 PG，即监控白名单）
TABLE_SECONDS = 'wind_seconds_di'  # DATA_SOURCE='db' 时的秒级明细表
TABLE_ALARM = 'wind_alarm'        # 告警事件流水表（ALARM/RECOVER）

# 数据来源切换：'db' 读本地 PG 秒级表（默认）；'api' 走 queryData 接口直拉。
DATA_SOURCE = 'db'

# 监控测点配置：每测点独立 suffix/中文名/入库列名/绝对值阈值。
# suffix 仅 api 模式拼 tagName 用；column 仅 db 模式从秒级表取数用。
# 注意：非驱动端真实测点为 WGEN.TEMGENNONDE（TEMGENNONDRIEND 接口恒返回 0 条）。
POINT_CONFIG = {
    'TEMGENDRIEND': {'suffix': 'WGEN.TEMGENDRIEND', 'column': 'tem_gen_driend',
                     'name': '驱动端温度',
                     'c01_fault': 850.0, 'c01_high': 85.0, 'c01_low': -50.0},
    'TEMGENNONDE':  {'suffix': 'WGEN.TEMGENNONDE',  'column': 'tem_gen_nonde',
                     'name': '非驱动端温度',
                     'c01_fault': 850.0, 'c01_high': 85.0, 'c01_low': -50.0},
}
POINT_CODES = tuple(POINT_CONFIG.keys())


def make_tag(wid, pc):
    """按设备 id + 测点后缀拼接 tagName，与 wind_fetch_and_upsert.py 一致。"""
    return f"FJMJ1_{wid.upper()}{POINT_CONFIG[pc]['suffix']}"

OUTPUT_DIR = r"e:\data\0929"
ALARM_CSV = os.path.join(OUTPUT_DIR, "alarm_temgende.csv")
EXCEED_CSV = os.path.join(OUTPUT_DIR, "fjmjsj_temgende_exceed.csv")  # 超温原始数据文件(>85/<=-50/>=850)

POLL_INTERVAL = 30        # 主循环轮询周期（秒）
FETCH_LOOKBACK = 90       # 每轮拉取最近多少秒（重叠窗口，防延迟/漏点）
BOOTSTRAP_HOURS = 1       # 启动回灌历史小时数（规则02启动即可判）
MAX_WORKERS = 24          # 拉取并发数
MAX_RETRIES = 3
RETRY_INTERVAL = 5
WINDOW_KEEP = 3650        # 内存滑窗保留秒数（1小时+余量）
PAGE_SIZE = 5000

# ---- 规则阈值 ----
# C01 绝对值阈值（c01_fault/high/low）已迁入 POINT_CONFIG，按测点独立配置。
# 以下为检测时序类参数，各测点共享。
W_FROZEN = 3600           # 规则02窗口(秒)
FROZEN_MIN_SPAN = 2700    # 规则02窗口实际跨度下限(秒)。须≤W_FROZEN-FROZEN_FRESHNESS(=3300)：
                          # 断采时span=3600-lag，阈值过高会在lag≈100s先跌破阈值提前RECOVER，
                          # 导致lag>900的"断采>15min"恢复分支不可达。取2700=W_FROZEN-C02_STALE_RECOVER对齐
FROZEN_MIN_POINTS = 100   # 规则02最少样本数
EPS = 1e-9
W_FLUCT = 20              # 规则03窗口(秒)
FLUCT_MIN_SPAN = 15       # 规则03窗口实际跨度下限(秒)
FLUCT_MIN_POINTS = 3      # 规则03最少样本数（实测平均8.3s间隔，防两点毛刺）
FLUCT_RANGE = 10.0        # max-min 阈值(℃)
SUSTAIN_SEC = 5           # 规则03持续秒数
FROZEN_FRESHNESS = 300    # 规则02仅在最新点滞后<5分钟时判定（正常gap p99≈188s）
C02_STALE_RECOVER = 900   # C02告警后断采超过15分钟 -> 显式恢复（滞后5~15分钟保持状态防抖动）
ALARM_COOLDOWN = 600      # 同类告警冷却(秒)，恢复后可重新告警
HEARTBEAT_TICKS = 5       # 每5轮(约5分钟)打印一次心跳

# 报警类型统一格式：01-绝对值报警 / 02-死值校验 / 03-突变报警
ALARM_TYPE_MAP = {
    'C01_FAULT': '01-绝对值报警',
    'C01_HIGH': '01-绝对值报警',
    'C01_LOW': '01-绝对值报警',
    'C02_FROZEN': '02-死值校验',
    'C03_FLUCT': '03-突变报警',
}

TS_FMT = "%Y-%m-%d %H:%M:%S"


def log(msg):
    print(f"[{datetime.now().strftime(TS_FMT)}] {msg}", flush=True)


# ============================== 网络拉取 ==============================
_thread_local = threading.local()


def _session():
    s = getattr(_thread_local, 's', None)
    if s is None:
        s = requests.Session()
        _thread_local.s = s
    return s


def post_with_retry(payload):
    """POST 并解析 JSON，仅对网络错误重试。"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _session().post(URL, headers=HEADERS, data=payload, timeout=30)
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_INTERVAL)
            else:
                raise
        except Exception:
            raise
    raise last_err


def query_points(tag, start_str, end_str=None):
    """按 tag_name + ts 范围升序拉取（窗口内点数实测 < 500，单页足够；保险起见分页）。"""
    qc = [
        {"columnType": "String", "columnName": "tag_name", "condition": "=", "parameter": tag},
        {"columnType": "String", "columnName": "ts", "condition": ">=", "parameter": start_str},
    ]
    if end_str:
        qc.append({"columnType": "String", "columnName": "ts", "condition": "<", "parameter": end_str})
    base = {"stb": DATA_STB, "queryCriteria": qc, "sortType": "asc", "sortField": "ts"}

    # 探测 total
    rj = post_with_retry(json.dumps({"type": 1029, "map": {**base, "pageNum": 1, "pageSize": 1}}))
    total = (rj.get('data') or {}).get('total', 0) or 0
    if total == 0:
        return []
    items = []
    page = 1
    while True:
        rj = post_with_retry(json.dumps({"type": 1029,
                                         "map": {**base, "pageNum": page, "pageSize": PAGE_SIZE}}))
        items.extend((rj.get('data') or {}).get('list', []) or [])
        if page * PAGE_SIZE >= total:
            break
        page += 1
    return items


def connect():
    """读取 data_config.json 的 pgConnStr 连接 PostgreSQL（与 wind_fetch_and_upsert.py 一致）。"""
    with open('data_config.json', encoding='utf-8') as f:
        return psycopg2.connect(json.load(f)['pgConnStr'])


# 告警事件流水表：ALARM/RECOVER 各一行（bigserial 追加，不做 upsert——
# 冷却后重新告警与恢复都是独立事件）。告警量小（边沿触发），普通表+索引即可。
DDL_ALARM = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ALARM} (
    id                   bigserial PRIMARY KEY,
    event                text NOT NULL,          -- ALARM / RECOVER
    event_time           timestamptz NOT NULL DEFAULT now(),  -- 记录写入时刻
    data_ts              timestamptz,            -- 触发数据点时刻（stats['ts']）
    region_company_name  text,
    station_name         text,
    device_id            text NOT NULL,
    device_name          text,
    point_code           text NOT NULL,
    point_name           text,
    rule_code            text NOT NULL,          -- C01_HIGH / C02_FROZEN / ...
    alarm_type           text,                   -- 01-绝对值报警 / ...
    temperature          double precision,
    temp_rate            double precision,
    range_1h             double precision,
    range_20s            double precision,
    alarm_value          double precision,
    detail               text
)
"""


def ensure_alarm_table():
    """启动时幂等建告警表与查询索引。表名/索引名为内部常量，无注入面。"""
    conn = connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(DDL_ALARM)
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE_ALARM}_device_ts '
                    f'ON {TABLE_ALARM} (device_id, event_time DESC)')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{TABLE_ALARM}_rule_ts '
                    f'ON {TABLE_ALARM} (rule_code, event_time DESC)')
        cur.close()
        log(f'PG 告警表 {TABLE_ALARM} 已就绪')
    finally:
        conn.close()


def discover_devices():
    """从本地 wind_device 表读取白名单设备（含元数据），不再走 1022 接口、不做区域/电站过滤。
    wind_device 表即监控白名单：只有表中的风机才被监控。空表则终止，防止误拉全量。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""SELECT wind_device_id, wind_device_name, station_name,
                              region_company_name, phase_name, type_name
                       FROM {TABLE_DEVICE}""")
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    if not rows:
        log("wind_device 白名单为空，终止。请先导入设备台账。")
        sys.exit(1)
    out = []
    for wid, name, station, region, phase, dtype in rows:
        if not wid or not wid.strip():
            continue
        out.append({
            'wid': wid.strip(),
            'name': name or '',
            'station': station or '',
            'region': region or '',
            'phase': phase or '',
            'type': dtype or '',
        })
    log(f"白名单设备 wind_device 表共 {len(out)} 台，全部纳入监控")
    return out


# ============================== 内存滑窗 ==============================
class SlidingBuffer:
    """单台风机的时间序列滑窗，按 ts 去重，保留 WINDOW_KEEP 秒。"""
    __slots__ = ('points', 'seen_ts')

    def __init__(self):
        self.points = deque()      # (epoch:float, value:float)，升序
        self.seen_ts = set()       # ts 原始字符串，用于去重

    def add_raw(self, raw_items, now_epoch):
        """写入接口返回的原始点（按 ts 去重），并裁剪到 WINDOW_KEEP 秒。

        未来时间防御：若数据点 ts 比当前时间晚超过 1 小时（epoch > now + 3600），
        视为接口吐的脏数据（历史上接口曾吐 2026-10-28 未来日期数据，污染 CSV），
        丢弃并打印警告，避免 CSV 再次被污染。
        返回新增记录列表 [(ts_str, epoch, pv_str, value)]。"""
        new_records = []
        dropped_future = 0
        future_threshold = now_epoch + 3600  # 比当前时间晚 1h 视为脏数据
        for it in raw_items:
            ts_str = it.get('ts')
            pv = it.get('pointValue', '')
            if not ts_str or ts_str in self.seen_ts:
                continue
            try:
                epoch = datetime.strptime(ts_str, TS_FMT).timestamp()
                value = float(pv)
            except (TypeError, ValueError):
                self.seen_ts.add(ts_str)  # 坏点不参与计算，但也不再重复处理
                continue
            # 未来时间防御：接口脏数据丢弃，标记已处理避免日志刷屏
            if epoch > future_threshold:
                dropped_future += 1
                self.seen_ts.add(ts_str)
                continue
            self.seen_ts.add(ts_str)
            new_records.append((ts_str, epoch, pv, value))
        if dropped_future:
            log(f"丢弃 {dropped_future} 条未来时间脏数据（ts > now + 1h），"
                f"避免 CSV 被污染")
        if new_records:
            self.points.extend((e, v) for _, e, _, v in new_records)
            self.points = deque(sorted(self.points, key=lambda t: t[0]))
        cutoff = now_epoch - WINDOW_KEEP
        while self.points and self.points[0][0] < cutoff:
            self.points.popleft()
        # seen_ts 周期性重建，防止无限增长
        if len(self.seen_ts) > 5000:
            self.seen_ts = {datetime.fromtimestamp(e).strftime(TS_FMT) for e, _ in self.points}
        return new_records

    def window_values(self, now_epoch, seconds):
        lo = now_epoch - seconds
        return [(e, v) for e, v in self.points if e >= lo]

    def latest(self):
        return self.points[-1] if self.points else None


# ============================== 告警引擎 ==============================
def open_csv_with_header(csv_path, header):
    """打开 CSV 以追加写；若已存在文件表头列数与期望不符（旧 schema），则先重置文件再写表头。
    返回 (fp, writer)。schema 升级时避免新旧列错位。"""
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    reset = True
    if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
        try:
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                first = next(csv.reader(f), None)
        except Exception:
            first = None
        if first is not None and len(first) == len(header):
            reset = False
    if reset:
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow(header)
    fp = open(csv_path, 'a', encoding='utf-8-sig', newline='')
    return fp, csv.writer(fp)


class AlarmEngine:
    """每(风机,测点,规则)维护告警状态：边沿触发 + 恢复 + 冷却。
    每个事件三路输出：CSV 追加、PG wind_alarm 追加、控制台。"""

    HEADER = ['ts', 'regionCompanyName', 'stationName',
              'windDeviceId', 'windDeviceName',
              'pointCode', 'pointName',
              'temperature', 'temp_rate',
              'range_1h', 'range_20s',
              'alarm_type', 'alarm_value']

    _INSERT_SQL = f"""
        INSERT INTO {TABLE_ALARM} (
            event, data_ts, region_company_name, station_name,
            device_id, device_name, point_code, point_name, rule_code, alarm_type,
            temperature, temp_rate, range_1h, range_20s, alarm_value, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    def __init__(self, csv_path, dev_map):
        self.dev_map = dev_map
        self.active = {}                     # key -> bool
        self.last_alarm = {}                 # key -> epoch
        self.streak_start = {}               # key(仅C03) -> 连续满足起始 epoch
        self.fp, self.writer = open_csv_with_header(csv_path, self.HEADER)
        self.lock = threading.Lock()
        self._pg = None                      # PG 连接懒建，与数据源连接独立

    def update(self, wid, pc, rule_code, is_active, now_epoch, stats=None, detail=''):
        """stats 由主循环计算后传入: {'ts','temp','rate','range_1h','range_20s'}"""
        key = (wid, pc, rule_code)
        was = self.active.get(key, False)
        with self.lock:
            if is_active and not was:
                last = self.last_alarm.get(key, 0)
                if now_epoch - last < ALARM_COOLDOWN:
                    return
                self.active[key] = True
                self.last_alarm[key] = now_epoch
                self._emit('ALARM', wid, pc, rule_code, stats, detail)
            elif (not is_active) and was:
                self.active[key] = False
                self._emit('RECOVER', wid, pc, rule_code, stats, detail)

    def _emit(self, event, wid, pc, rule_code, stats, detail):
        d = self.dev_map.get(wid, {})
        pc_cfg = POINT_CONFIG.get(pc, {})
        s = stats or {}
        ts_str = s.get('ts') or datetime.now().strftime(TS_FMT)
        temp = s.get('temp')
        rate = s.get('rate')
        r1h = s.get('range_1h')
        r20s = s.get('range_20s')
        # 按规则类型取报警值
        if event == 'ALARM':
            if rule_code in ('C01_FAULT', 'C01_HIGH', 'C01_LOW'):
                alarm_val = temp
            elif rule_code == 'C02_FROZEN':
                alarm_val = r1h
            elif rule_code == 'C03_FLUCT':
                alarm_val = r20s
            else:
                alarm_val = temp
        else:
            alarm_val = None
        row = [
            ts_str,
            d.get('region', ''),
            d.get('station', ''),
            wid,
            d.get('name', ''),
            pc,
            pc_cfg.get('name', ''),
            '' if temp is None else round(temp, 2),
            '' if rate is None else round(rate, 4),
            '' if r1h is None else round(r1h, 2),
            '' if r20s is None else round(r20s, 2),
            ALARM_TYPE_MAP.get(rule_code, rule_code) if event == 'ALARM' else '',
            '' if alarm_val is None else round(alarm_val, 2),
        ]
        self.writer.writerow(row)
        self.fp.flush()
        # PG 流水（event_time 用库默认 now()；data_ts 取触发数据点时刻）
        self._write_pg(event, wid, pc, rule_code, d, pc_cfg, s,
                       temp, rate, r1h, r20s, alarm_val, detail)
        color = '\033[91m' if event == 'ALARM' else '\033[92m'
        alarm_type = ALARM_TYPE_MAP.get(rule_code, rule_code)
        log(f"{color}{event}\033[0m {alarm_type}[{rule_code}] {pc_cfg.get('name','')} "
            f"{d.get('station','')} {d.get('name','')} "
            f"temp={round(temp,2) if temp is not None else '-'} rate={round(rate,4) if rate is not None else '-'} "
            f"r1h={round(r1h,2) if r1h is not None else '-'} r20s={round(r20s,2) if r20s is not None else '-'} {detail}")

    def _get_pg(self):
        """懒建 autocommit 只读写连接；失效时调用方负责置空重连。"""
        if self._pg is None or self._pg.closed:
            self._pg = connect()
            self._pg.autocommit = True      # 每条 INSERT 即时提交，不长时间持事务
        return self._pg

    def _write_pg(self, event, wid, pc, rule_code, d, pc_cfg, s,
                  temp, rate, r1h, r20s, alarm_val, detail):
        """追加一行告警事件；任何失败仅记日志，不影响 CSV 与主循环。"""
        data_ts = None
        if s.get('ts'):
            try:
                data_ts = datetime.strptime(s['ts'], TS_FMT)
            except (TypeError, ValueError):
                data_ts = None
        params = (
            event, data_ts,
            d.get('region') or None, d.get('station') or None,
            wid, d.get('name') or None,
            pc, pc_cfg.get('name') or None,
            rule_code, ALARM_TYPE_MAP.get(rule_code, rule_code),
            temp, rate, r1h, r20s,
            alarm_val if event == 'ALARM' else None,
            detail or None,
        )
        try:
            cur = self._get_pg().cursor()
            cur.execute(self._INSERT_SQL, params)
            cur.close()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            log(f"告警写 PG 连接失效，丢弃本条并在下次事件重连: {e}")
            try:
                if self._pg is not None:
                    self._pg.close()
            except Exception:
                pass
            self._pg = None
        except Exception as e:
            log(f"告警写 PG 失败（不影响 CSV）: {e}")
            try:
                self._pg.rollback()
            except Exception:
                pass

    def load_state(self, wid_list):
        """启动回放：从 wind_alarm 每个 (设备,测点,规则) 的最后一条事件恢复内存状态。

        - 最后事件为 ALARM  -> active=True（重启后不再重复告警；规则02 断采恢复、
          其他规则在条件消失时仍会正常写 RECOVER）
        - 最后事件为 RECOVER / ALARM -> last_alarm 一律置为该事件时刻，
          使重启后的 ALARM_COOLDOWN 冷却连续，不被重启清零
        - C03 的 streak_start（连续满足起点）无法从流水精确还原，保持 None：
          重启后需重新连续满足 SUSTAIN_SEC 才会告警（偏保守，不会误报）
        仅回放白名单设备、POINT_CONFIG 内测点；失败则按全新启动处理。
        """
        try:
            cur = self._get_pg().cursor()
            cur.execute("""
                SELECT DISTINCT ON (device_id, point_code, rule_code)
                       device_id, point_code, rule_code, event, event_time
                FROM wind_alarm
                WHERE device_id = ANY(%s) AND point_code = ANY(%s)
                ORDER BY device_id, point_code, rule_code,
                         event_time DESC, id DESC
            """, (wid_list, list(POINT_CODES)))
            rows = cur.fetchall()
            cur.close()
        except Exception as e:
            log(f"回放告警状态失败，按全新启动处理（不影响监控）: {e}")
            try:
                self._pg.rollback()
            except Exception:
                pass
            return
        n_active = 0
        with self.lock:
            for wid, pc, code, event, etime in rows:
                key = (wid, pc, code)
                self.last_alarm[key] = etime.timestamp()  # timestamptz 直接转 epoch
                if event == 'ALARM':
                    self.active[key] = True
                    n_active += 1
        log(f"从 wind_alarm 回放告警状态：{len(rows)} 个序列，"
            f"其中 {n_active} 条活跃告警已恢复")

    def active_count(self):
        return sum(1 for v in self.active.values() if v)

    def is_active(self, wid, pc, rule_code):
        return self.active.get((wid, pc, rule_code), False)

    def close(self):
        self.fp.close()
        if self._pg is not None:
            self._pg.close()


# ============================== 超温数据文件 ==============================
class ExceedCSV:
    """超温原始数据文件（命中 C01 绝对值阈值：故障/高温/低温），格式与 get_fj_realtime.py 输出对齐：
    windDeviceId, windDeviceName, typeName, phaseName, pointCode, pointValue, tagName, ts
    重启时读回已有 (tagName, ts)，防止回灌重复写。"""

    HEADER = ['windDeviceId', 'windDeviceName', 'typeName',
              'phaseName', 'pointCode', 'pointValue', 'tagName', 'ts']

    def __init__(self, csv_path):
        self.fp, self.writer = open_csv_with_header(csv_path, self.HEADER)
        self.seen = set()          # (tagName, ts_str)，跨重启去重
        try:
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                rd = csv.reader(f)
                next(rd, None)
                for row in rd:
                    if len(row) >= 8:
                        self.seen.add((row[6], row[7]))
        except Exception:
            pass

    def write(self, dev, pc, ts_str, pv_str):
        tag = make_tag(dev['wid'], pc)
        key = (tag, ts_str)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.writer.writerow([dev['wid'], dev['name'], dev.get('type', ''),
                              dev.get('phase', ''), pc, pv_str, tag, ts_str])
        self.fp.flush()
        return True

    def close(self):
        self.fp.close()


def write_exceed_points(exceed, dev, pc, new_records):
    """把新增点中命中超温条件的原始值写入超温文件，返回实际写入条数。"""
    pc_cfg = POINT_CONFIG[pc]
    n = 0
    for ts_str, _e, pv_str, value in new_records:
        if classify_rule01(value, pc_cfg) is not None:
            if exceed.write(dev, pc, ts_str, pv_str):
                n += 1
    return n


# ============================== 规则判定 ==============================
def classify_rule01(value, pc_cfg):
    """规则01：返回命中的规则码（优先级 故障>高温>低温），未命中返回 None。
    阈值取自该测点的 POINT_CONFIG（pc_cfg['c01_fault'/'c01_high'/'c01_low']）。"""
    if value >= pc_cfg['c01_fault']:
        return 'C01_FAULT'
    if value > pc_cfg['c01_high']:
        return 'C01_HIGH'
    if value <= pc_cfg['c01_low']:
        return 'C01_LOW'
    return None


def eval_frozen(win, now_epoch):
    """规则02：近1小时窗口所有点完全相同。返回 (active, detail_dict)。"""
    if not win:
        return False, {}
    epochs = [e for e, _ in win]
    vals = [v for _, v in win]
    span = epochs[-1] - epochs[0]
    n = len(win)
    vmin, vmax = min(vals), max(vals)
    if span < FROZEN_MIN_SPAN or n < FROZEN_MIN_POINTS:
        return False, {}
    if (vmax - vmin) < EPS:
        return True, {'win_min': vmin, 'win_max': vmax, 'span': span, 'n': n}
    return False, {'win_min': vmin, 'win_max': vmax, 'span': span, 'n': n}


def compute_stats(buf, now_epoch):
    """计算该风机当前 tick 的各项统计：温度、温升速率、1h极差、20s极差。"""
    latest = buf.latest()
    if not latest:
        return None
    pts = buf.window_values(now_epoch, W_FROZEN) or []
    # 温升速率：当前值 vs 前一个点的差值/时间差
    rate = None
    if len(pts) >= 2:
        prev = pts[-2]
        dt = latest[0] - prev[0]
        if dt > 0:
            rate = (latest[1] - prev[1]) / dt
    # 1小时极差
    r1h = None
    if pts:
        r1h = max(v for _, v in pts) - min(v for _, v in pts)
    # 20秒极差
    pts20 = buf.window_values(now_epoch, W_FLUCT) or []
    r20s = None
    if pts20:
        r20s = max(v for _, v in pts20) - min(v for _, v in pts20)
    return {
        'ts': datetime.fromtimestamp(latest[0]).strftime(TS_FMT),
        'temp': latest[1],
        'rate': rate,
        'range_1h': r1h,
        'range_20s': r20s,
    }


def eval_fluct_tick(buf, now_epoch, engine, wid, pc, stats):
    """规则03：20秒 max-min>10 且持续>=5秒（按数据点ts计量，连续状态保存在引擎上）。"""
    key = (wid, pc, 'C03_FLUCT')
    win = buf.window_values(now_epoch, W_FLUCT)
    latest = buf.latest()
    if not win or latest is None:
        cond = False
    else:
        epochs = [e for e, _ in win]
        vals = [v for _, v in win]
        span = epochs[-1] - epochs[0]
        n = len(win)
        cond = span >= FLUCT_MIN_SPAN and n >= FLUCT_MIN_POINTS and (max(vals) - min(vals)) > FLUCT_RANGE

    latest_epoch = latest[0] if latest else now_epoch
    if cond:
        start = engine.streak_start.get(key)
        if start is None:
            engine.streak_start[key] = latest_epoch          # 候选开始，先不报
            sustained = False
        else:
            sustained = (latest_epoch - start) >= SUSTAIN_SEC
        engine.update(wid, pc, 'C03_FLUCT', sustained, now_epoch, stats=stats)
    else:
        engine.streak_start.pop(key, None)                   # 中断即清零
        engine.update(wid, pc, 'C03_FLUCT', False, now_epoch, stats=stats)


# ============================== 主流程 ==============================
def fetch_one(dev, pc, start_str, end_str=None):
    try:
        return dev['wid'], pc, query_points(make_tag(dev['wid'], pc), start_str, end_str), None
    except Exception as e:
        return dev['wid'], pc, [], e


def fetch_round_db(conn, start_dt, end_dt, wid_lower, lower2wid):
    """db 模式：一条 SQL 拉窗口内全部白名单设备的两个温度固定列。

    相比 api 模式的「设备×测点」N*2 个请求，这里只有一次查询；宽表行
    (device_id, ts, tem_gen_driend, tem_gen_nonde) 在 Python 侧按非 NULL
    列 unpivot 成与接口一致的 {'ts', 'pointValue'} 列表。
    device_id 入库统一小写，经 lower2wid 映射回 wind_device 白名单原始 id。
    返回 {(wid_原始, pc): [{'ts','pointValue'}, ...]}（无数据的 key 不出现）。
    """
    cols = [POINT_CONFIG[pc]['column'] for pc in POINT_CODES]  # 内部配置，无注入面
    sql = f"""
        SELECT device_id, ts, {', '.join(cols)}
        FROM {TABLE_SECONDS}
        WHERE ts >= %s AND ts < %s
          AND device_id = ANY(%s)
        ORDER BY device_id, ts
    """
    grouped = {}
    cur = conn.cursor()
    try:
        cur.execute(sql, (start_dt, end_dt, wid_lower))
        for dev_lower, ts, *values in cur:
            wid = lower2wid.get(dev_lower)
            if wid is None:
                continue
            ts_str = ts.strftime(TS_FMT)
            for pc, value in zip(POINT_CODES, values):
                if value is None:
                    continue
                grouped.setdefault((wid, pc), []).append(
                    {'ts': ts_str, 'pointValue': float(value)})
    finally:
        cur.close()
    return grouped


def run():
    devices = discover_devices()
    ensure_alarm_table()                 # 幂等建 wind_alarm（PG 本就是白名单依赖）
    dev_map = {d['wid']: d for d in devices}
    # wind_seconds_di.device_id 入库统一小写，查询用小写、结果映射回白名单原始 id
    lower2wid = {d['wid'].lower(): d['wid'] for d in devices}
    wid_lower = list(lower2wid)
    # 每台风机 × 每个测点 独立滑窗
    buffers = {(d['wid'], pc): SlidingBuffer() for d in devices for pc in POINT_CODES}
    engine = AlarmEngine(ALARM_CSV, dev_map)
    engine.load_state([d['wid'] for d in devices])  # 重启不重复告警：回放活跃状态与冷却
    exceed = ExceedCSV(EXCEED_CSV)
    exceed_total = 0     # 累计写入超温文件条数

    # 拉取任务 = 设备 × 测点 的笛卡尔积
    tasks = [(d, pc) for d in devices for pc in POINT_CODES]
    n_tasks = len(tasks)

    # 按数据源初始化：api 用线程池并发请求；db 复用一条长连接。
    # 监控连接只做只读 SELECT：autocommit=True 让每条查询独立提交，避免 7x24
    # 长事务持有 backend xmin 阻碍 autovacuum 回收秒表死元组。
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS) if DATA_SOURCE == 'api' else None
    pg_conn = connect() if DATA_SOURCE == 'db' else None
    if pg_conn is not None:
        pg_conn.autocommit = True
    log(f"数据来源 DATA_SOURCE={DATA_SOURCE}"
        + (f"（{TABLE_SECONDS} 固定列）" if DATA_SOURCE == 'db' else '（queryData 接口）'))

    def fetch_all(start_epoch, end_epoch=None):
        """统一拉取入口，返回 ({(wid, pc): items}, 错误数)。
        无数据的 (wid, pc) 由调用方按空列表处理。"""
        if DATA_SOURCE == 'api':
            start_str = datetime.fromtimestamp(start_epoch).strftime(TS_FMT)
            end_str = (datetime.fromtimestamp(end_epoch).strftime(TS_FMT)
                       if end_epoch is not None else None)
            results = list(pool.map(
                lambda t: fetch_one(t[0], t[1], start_str, end_str), tasks))
            out, errs = {}, 0
            for wid, pc, items, err in results:
                if err is not None:
                    errs += 1
                    log(f"  {make_tag(wid, pc)}  ERROR: {err}")
                    out[(wid, pc)] = []
                else:
                    out[(wid, pc)] = items
            return out, errs
        # ---- db ----
        nonlocal pg_conn
        start_dt = datetime.fromtimestamp(start_epoch)
        end_dt = (datetime.fromtimestamp(end_epoch)
                  if end_epoch is not None else datetime.now())
        try:
            return fetch_round_db(pg_conn, start_dt, end_dt, wid_lower, lower2wid), 0
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            # 连接失效：关掉旧连接重连，本轮空过，下轮自动恢复
            log(f"PG 连接异常，重连后下轮重试: {e}")
            try:
                pg_conn.close()
            except Exception:
                pass
            pg_conn = connect()
            return {}, 1
        except Exception as e:
            log(f"PG 查询失败，本轮跳过（保留上一轮滑窗）: {e}")
            pg_conn.rollback()
            return {}, 1

    try:
        # ---- 启动回灌近 1 小时 ----
        log(f"启动回灌近 {BOOTSTRAP_HOURS} 小时历史数据，{len(devices)} 台 × "
            f"{len(POINT_CODES)} 测点 = {n_tasks} 个序列...")
        fetched, fail = fetch_all(time.time() - BOOTSTRAP_HOURS * 3600)
        now_epoch = time.time()
        ok = 0
        for idx, (d, pc) in enumerate(tasks, 1):
            wid = d['wid']
            items = fetched.get((wid, pc), [])
            new_recs = buffers[(wid, pc)].add_raw(items, now_epoch)
            exceed_total += write_exceed_points(exceed, dev_map[wid], pc, new_recs)
            ok += 1
            if not items:
                log(f"  [{idx}/{n_tasks}] {make_tag(wid, pc)}  (no data)")
            else:
                log(f"  [{idx}/{n_tasks}] {make_tag(wid, pc)}  "
                    f"-> fetched={len(items)} rows, new={len(new_recs)}")
        log(f"回灌完成：序列 {ok}，拉取错误 {fail}，超温点 {exceed_total} 条，"
            f"开始实时监控（每 {POLL_INTERVAL}s 一轮）")

        tick = 0
        while True:
            t0 = time.time()
            now_epoch = t0

            fetched, fetch_errs = fetch_all(t0 - FETCH_LOOKBACK, t0)

            log(f"=== round {tick + 1} 拉取最近 {FETCH_LOOKBACK}s 数据 ===")
            for idx, (d, pc) in enumerate(tasks, 1):
                wid = d['wid']
                items = fetched.get((wid, pc), [])
                buf = buffers[(wid, pc)]
                new_recs = buf.add_raw(items, now_epoch)
                exceed_total += write_exceed_points(exceed, dev_map[wid], pc, new_recs)
                if not items:
                    log(f"  [{idx}/{n_tasks}] {make_tag(wid, pc)}  (no data)")
                else:
                    log(f"  [{idx}/{n_tasks}] {make_tag(wid, pc)}  "
                        f"-> fetched={len(items)} rows, new={len(new_recs)}")
                latest = buf.latest()

                # 无任何数据点：跳过
                if latest is None:
                    continue

                # 计算统计数据
                stats = compute_stats(buf, now_epoch)

                # 规则01：越限（阈值取自该测点 POINT_CONFIG）
                pc_cfg = POINT_CONFIG[pc]
                hit_code = classify_rule01(latest[1], pc_cfg)
                for code in ('C01_FAULT', 'C01_HIGH', 'C01_LOW'):
                    engine.update(wid, pc, code, code == hit_code, now_epoch,
                                  stats=stats)

                # 规则02：数据中断防误报 + 卡死
                lag = now_epoch - latest[0]
                if lag <= FROZEN_FRESHNESS:
                    active, meta = eval_frozen(buf.window_values(now_epoch, W_FROZEN), now_epoch)
                    engine.update(wid, pc, 'C02_FROZEN', active, now_epoch, stats=stats)
                elif lag > C02_STALE_RECOVER and engine.is_active(wid, pc, 'C02_FROZEN'):
                    # 断采超15分钟：显式恢复，避免告警永久挂起（滞后5~15分钟保持状态防抖动）
                    engine.update(wid, pc, 'C02_FROZEN', False, now_epoch, stats=stats,
                                  detail='断采>15min')

                # 规则03
                eval_fluct_tick(buf, now_epoch, engine, wid, pc, stats)

            tick += 1
            if tick % HEARTBEAT_TICKS == 0:
                log(f"heartbeat tick={tick} 设备={len(devices)} 测点={len(POINT_CODES)} "
                    f"拉取错误={fetch_errs} 当前活跃告警={engine.active_count()} 超温累计={exceed_total}")

            elapsed = time.time() - t0
            sleep_s = POLL_INTERVAL - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)
            elif sleep_s < -5:
                hint = ("建议增大 MAX_WORKERS 或 POLL_INTERVAL"
                        if DATA_SOURCE == 'api' else "建议增大 POLL_INTERVAL")
                log(f"警告：本轮耗时 {round(elapsed,1)}s 超过轮询周期 {POLL_INTERVAL}s，{hint}")
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出监控。")
    finally:
        if pool is not None:
            pool.shutdown(wait=False)
        if pg_conn is not None:
            pg_conn.close()
        engine.close()
        exceed.close()


if __name__ == '__main__':
    run()
