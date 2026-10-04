"""统一入口：从 queryData 接口发现设备 -> 按时间窗拉取 -> upsert 到 wind_seconds_di。

合并了原 get_fjmjsj.py（设备清单发现 type=1022 + 逐 tagName 分页拉取 type=1029）
与 fetch_and_ingest.py（30s 轮询 + 水位线窗口 + 不丢数据机制）的能力，
输出从「写 CSV」改为「直接 upsert 到 wind_seconds_di」。

不丢数据机制（每秒产生多少条无需预知）：
1. 水位线文件 fetch_watermark.json（与脚本同目录，*.json 已被 .gitignore
   忽略）：记录上一轮成功入库的窗口右边界；进程重启后从水位线继续，
   而不是从 now() 开始。只在入库成功后写文件（临时文件 + os.replace
   原子替换）；若崩溃在“入库成功、水位线未写”之间，下一轮重叠重拉，
   MERGE 幂等不会产生重复行。
2. 每轮窗口半开区间 [last_ts - overlap, end)，默认回退 300s 重叠拉取，
   覆盖时钟漂移与迟到/补传数据。
3. 每个 tag 查询按 pageSize 分页，翻页直到取完——窗口内数据量再大也
   不丢，只影响请求次数。
4. 拉取/入库失败则不写水位线，下一轮从原位重拉。
5. 停机很久后恢复时，单窗被限制在 MAX_WINDOW_SECONDS，逐窗快速追平，
   避免一个覆盖几天的超大请求；未追平时本轮结束后立即进入下一轮
   （不 sleep 30s）。

设备清单（type=1022）：
    启动时按 regionCompanyName / stationName 黑名单过滤，得到场站下的
    windDeviceId 列表；与 SUFFIXES 笛卡尔积拼成完整 tagName
    （FJMJ1_{windDeviceId}{suffix}）作为逐 tag 查询目标。
    TARGETS 非空时跳过设备发现，直接使用给定的完整 tagName 列表。
"""
import json
import os
import random
import time
from datetime import datetime, timedelta

import requests

from ingest_wind import ingest_raw, parse_message

# ---------------- 接口配置 ----------------
API_URL = 'http://10.65.78.65:18082/queryData'
API_TOKEN = '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9'
DATA_STB = 'ods_ly_data_hub_fjmjsj_new'          # type=1029 数据表
DEVICE_STB = 'gddl_ods_ly.ods_ly_ads_gd_wind_device_df1016'  # type=1022 设备清单表
PAGE_SIZE = 5000
TIMEOUT = 30
MAX_RETRIES = 3
RETRY_INTERVAL = 5

# ---------------- 拉取范围 ----------------
# 测点后缀（可增删）；与设备 windDeviceId 笛卡尔积拼成完整 tagName
SUFFIXES = [
    'WGEN.TEMGENDRIEND',     # 发电机驱动端温度 -> 固定列 gen_tem_driend
    'WGEN.TEMGENNONDRIEND',  # 发电机非驱动端温度 -> 固定列 gen_tem_nonde
    'WGEN.SPEED',            # 转速 -> other_points
]
# 非空时跳过设备发现，直接使用这些完整 tagName（如 ['FJMJ1_XXXWGEN.SPEED']）
TARGETS = []

# regionCompanyName 黑名单：命中直接忽略
EXCLUDE_REGIONS = {
    '宁波风电', '广东新能源', '广西风电', '云南新能源',
    '江西新能源', '湖南新能源', '宁夏新能源', '甘肃新能源',
    '陕西新能源', '国电建投',
}
# stationName 黑名单：命中直接忽略
EXCLUDE_STATIONS = {
    '宿松风电场', '太湖风电场', '瓜州风电场', '岷县一期风电场', '通渭风电场',
    '东源蝉子顶风电场', '东里风电场', '高帮山风电场', '弄好岭风电场', '鱼塘风电场',
    '舸川风电场', '浩宏风电场', '风雨殿风电场', '贤良风电场', '天华山风电场',
    '茶山风电场', '穿山风电场', '大武口光伏电站', '海子井光伏电站', '麻黄山风电场',
    '宣和马场湖光伏电站', '灵武马家滩光伏', '牛首山风电场', '平罗光伏电站', '青山风电场',
    '石板泉风电场', '香山风电场', '定边光伏电站', '皇赵光伏电站', '圣熙风电场',
    '烁光光伏电站', '希恒光伏电站', '旭阳光伏电站', '智亮光伏电站', '大风丫口风电场',
    '凤代光伏电站', '金铜盆风电场', '朗山风电场', '柳树冲光伏电站', '磨刀石光伏电站',
    '西泽风电场', '卓干山风电场', '宝山风电场', '青龙风电场', '郊区风电场', '万发风电场',
}

# ---------------- 窗口与轮询 ----------------
OVERLAP_SECONDS = 300           # 每轮回退重叠，防迟到/钟漂
INITIAL_LOOKBACK_SECONDS = 300  # 首次运行（无水位线文件）回看多久
MAX_WINDOW_SECONDS = 3600       # 追历史时单窗上限，避免超大请求
FETCH_INTERVAL_SECONDS = 3
#MOCK = False                    # True = 不请求真实接口，用模拟数据
MOCK = True                     # True = 不请求真实接口，用模拟数据
PRINT_INGESTED = True           # 打印每条实际入库的数据（device_id/ts/测点）

TS_FMT = '%Y-%m-%d %H:%M:%S'
SOURCE = 'fjmjsj'
WATERMARK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'fetch_watermark.json')


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
    """发一次请求并解析 JSON。仅对网络错误重试；业务错误直接抛出。"""
    headers = {'zb-token': API_TOKEN, 'Content-Type': 'application/json'}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(API_URL, headers=headers,
                              data=json.dumps(payload), timeout=TIMEOUT)
            rj = r.json()
            if rj.get('data') is None:
                raise RuntimeError(f'接口返回异常: {rj}')
            return rj
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                print(f'    [retry {attempt}/{MAX_RETRIES}] 网络错误: '
                      f'{e.__class__.__name__}，{RETRY_INTERVAL}s 后重试...')
                time.sleep(RETRY_INTERVAL)
            else:
                raise
    raise last_err


def discover_devices():
    """type=1022 查设备清单，按黑名单过滤，返回 windDeviceId 列表。"""
    rj = post_json({
        'type': 1022,
        'map': {
            'stb': DEVICE_STB,
            'sortType': 'desc',
            'sortField': '',
            'pageNum': 1,
            'pageSize': 5000,
        },
    })
    lst = rj.get('data', {}).get('list', []) or []
    hits = [x for x in lst
            if x.get('regionCompanyName') not in EXCLUDE_REGIONS
            and x.get('stationName') not in EXCLUDE_STATIONS
            and x.get('stationName')]
    excluded = len(lst) - len(hits)
    print(f'设备清单: 共 {len(lst)} 条，黑名单过滤掉 {excluded} 条，'
          f'保留 {len(hits)} 台')
    return [x.get('windDeviceId', '').upper() for x in hits if x.get('windDeviceId')]


def build_targets():
    """TARGETS 非空时直接使用；否则设备发现 x SUFFIXES 拼完整 tagName。"""
    if MOCK or TARGETS:
        return list(TARGETS)
    devices = discover_devices()
    return [f'FJMJ1_{dev}{suffix}' for dev in devices for suffix in SUFFIXES]


def fetch_target(criteria, tag):
    """按 tag_name + ts 范围查询一个 tag，翻页取全，返回 (items, total)。"""
    qc = [{
        'columnType': 'String',
        'columnName': 'tag_name',
        'condition': '=',
        'parameter': tag,
    }] + list(criteria)
    base_map = {
        'stb': DATA_STB,
        'queryCriteria': qc,
        'sortType': 'desc',
        'sortField': 'ts',
    }
    # 先 pageSize=1 探测 total
    rj = post_json({'type': 1029,
                    'map': {**base_map, 'pageNum': 1, 'pageSize': 1}})
    total = rj.get('data', {}).get('total', 0) or 0
    if total == 0:
        return [], 0

    # 再按 PAGE_SIZE 翻页直到取完
    items = []
    page_num = 1
    while True:
        rj = post_json({'type': 1029, 'map': {
            **base_map, 'pageNum': page_num, 'pageSize': PAGE_SIZE}})
        page = rj.get('data', {}).get('list', []) or []
        items.extend(page)
        if page_num * PAGE_SIZE >= total:
            break
        page_num += 1
    return items, total


def fetch_window(start, end, tags):
    """对窗口 [start, end) 内所有 tag 逐个分页拉取，汇总原始消息。"""
    if MOCK:
        return mock_window(start, end)
    criteria = [
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '>=', 'parameter': start},
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '<', 'parameter': end},
    ]
    all_items = []
    for tag in tags:
        items, total = fetch_target(criteria, tag)
        print(f'    {tag}: total={total}, fetched={len(items)}')
        all_items.extend(items)
    return all_items


def mock_window(start, end):
    """离线模拟：3 设备 x 4 测点，ts 打窗口右边界（仅供联调测试）。"""
    devices = ['B524F6D0B8FF4B2DBC0C102FC4B032B9',
               'A111F6D0B8FF4B2DBC0C102FC4B032B8',
               'C222F6D0B8FF4B2DBC0C102FC4B032B7']
    tags = ['WGEN.TEMGENDRIEND', 'WGEN.TEMGENNONDRIEND',
            'WGEN.SPEED', 'WGEN.NOTMAPPED']
    return [{
        'pointValue': round(random.uniform(5, 60), 1),
        'description': '风机秒级数据',
        'tagName': 'FJMJ1_' + dev + tag,
        'ts': end,
    } for dev in devices for tag in tags]


# ---------------- 主循环 ----------------
def tick(now=None):
    """执行一轮「设备发现/取 targets -> 算窗口 -> 分页拉全 -> 入库 -> 落水位线」。

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

    tags = build_targets()
    raw = fetch_window(start_s, end_s, tags)
    if PRINT_INGESTED:
        printed = 0
        for msg in raw:
            parsed = parse_message(msg)
            if parsed is not None:
                dev, ts, points = parsed
                print(f'    入库 {dev}  {ts}  {points}')
                printed += 1
        print(f'    ---- 实际入库 {printed} 条（拉取 {len(raw)} 条，'
              f'丢弃 {len(raw) - printed} 条未映射测点）')
    n = ingest_raw(raw)
    save_watermark(end_s)  # 入库成功后才推进
    return start_s, end_s, len(raw), n, end >= now


def main():
    mode = '模拟' if MOCK else '真实接口'
    scope = f'TARGETS={len(TARGETS)} 个tag' if TARGETS \
        else f'设备发现 x {len(SUFFIXES)} 个测点'
    print(f'开始拉取（{mode}，{scope}），每轮间隔 {FETCH_INTERVAL_SECONDS}s，'
          f'重叠 {OVERLAP_SECONDS}s，水位线文件 {WATERMARK_FILE}，Ctrl+C 终止')
    while True:
        stamp = datetime.now().strftime(TS_FMT)
        try:
            start, end, fetched, ingested, caught_up = tick()
            print(f'[{stamp}] 窗口 [{start}, {end}) '
                  f'拉取 {fetched} 条，入库 {ingested} 条，水位线 -> {end}')
        except Exception as e:
            caught_up = True  # 失败时不进入追赶快转
            print(f'[{stamp}] 本轮失败，水位线不推进，'
                  f'{FETCH_INTERVAL_SECONDS}s 后从原位重拉: {e!r}')
        if caught_up:
            time.sleep(FETCH_INTERVAL_SECONDS)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已停止拉取')
