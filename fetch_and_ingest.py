"""从 queryData 接口按时间窗拉取风机秒级数据并写入 wind_seconds_di。

不丢数据的核心机制（每秒产生多少条无需预知）：
1. 水位线文件 fetch_watermark.json（与脚本同目录，*.json 已被
   .gitignore 忽略）：记录上一轮成功入库的窗口右边界；进程重启后从
   水位线继续，而不是从 now() 开始。只在入库成功后写文件，写入采用
   临时文件 + os.replace 原子替换；若崩溃在“入库成功、水位线未写”
   之间，下一轮重叠重拉即可，MERGE 幂等不会产生重复行。
2. 每轮窗口半开区间 [last_ts - overlap, 窗口右边界)，默认回退 300s
   重叠拉取，覆盖时钟漂移与迟到/补传数据。
3. 接口按 ts 条件分页（先 pageSize=1 探测 total，再按 pageSize 翻页
   直到取完）——窗口内数据量再大也不丢，只影响请求次数。
4. 拉取/入库失败则不写水位线，下一轮从原位重拉。
5. 停机很久后恢复时，单窗被限制在 MAX_WINDOW_SECONDS（默认 1 小时），
   逐窗快速追平，避免一个覆盖几天的超大请求；未追平时本轮结束后立即
   进入下一轮（不 sleep 30s）。

接口（POST queryData, type=1029）：
    queryCriteria 支持 ts >= start 与 ts < end；TARGETS 为空时只按
    ts 过滤，一次拉全窗口内所有设备/测点；TARGETS 非空时每个完整
    tagName（如 FJMJ1_{设备}{测点后缀}）各发一次查询。
返回的 data.list 每条是 {pointValue, description, tagName, ts}，
可直接交给 ingest_raw()。
"""
import json
import os
import random
import time
from datetime import datetime, timedelta

import requests

from ingest_wind import ingest_raw

# ---------------- 接口与拉取参数（按需直接改这里） ----------------
API_URL = 'http://10.65.78.65:18082/queryData'
API_TOKEN = '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9'
STB = 'ods_ly_data_hub_fjmjsj_new'
PAGE_SIZE = 5000
TIMEOUT = 30
# 空 = 只按 ts 拉全部；若非空则需列出每个要拉的完整 tagName
TARGETS = []
OVERLAP_SECONDS = 300          # 每轮回退重叠，防迟到/钟漂
INITIAL_LOOKBACK_SECONDS = 300  # 首次运行（无水位线文件）回看多久
MAX_WINDOW_SECONDS = 3600      # 追历史时单窗上限，避免超大请求
MAX_RETRIES = 3
RETRY_INTERVAL = 5
MOCK = False                   # True = 不请求真实接口，用模拟数据

FETCH_INTERVAL_SECONDS = 30
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


def fetch_target(criteria, tag=None):
    """拉一个查询范围（tag 给定时加 tag_name = 等值条件），翻页取全。"""
    qc = list(criteria)
    if tag:
        qc.insert(0, {
            'columnType': 'String',
            'columnName': 'tag_name',
            'condition': '=',
            'parameter': tag,
        })
    base_map = {
        'stb': STB,
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


def fetch_window(start, end):
    """拉半开时间窗 [start, end) 内的全部原始消息（自动分页）。"""
    if MOCK:
        return mock_window(start, end)
    criteria = [
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '>=', 'parameter': start},
        {'columnType': 'String', 'columnName': 'ts',
         'condition': '<', 'parameter': end},
    ]
    all_items = []
    for tag in (TARGETS or [None]):
        items, total = fetch_target(criteria, tag)
        print(f'    {tag or "全部tag"}: total={total}, fetched={len(items)}')
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
    """执行一轮「算窗口 -> 分页拉全 -> 入库 -> 落水位线文件」。

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
    n = ingest_raw(raw)
    save_watermark(end_s)  # 入库成功后才推进
    return start_s, end_s, len(raw), n, end >= now


def main():
    mode = '模拟' if MOCK else '真实接口'
    scope = f'targets={len(TARGETS)} 个tag' if TARGETS else '只按ts拉全部'
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
