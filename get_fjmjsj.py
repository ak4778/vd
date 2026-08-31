import requests
import json
import csv
import time
import os

# 网络请求重试参数
MAX_RETRIES = 3
RETRY_INTERVAL = 5  # 秒


def request_with_retry(url, headers, payload, max_retries=MAX_RETRIES, interval=RETRY_INTERVAL):
    """带重试的 POST 请求，仅对网络错误重试，HTTP 业务错误不重试"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers=headers, data=payload, timeout=30)
            rj = r.json()
            return rj
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt < max_retries:
                print(f"    [retry {attempt}/{max_retries}] 网络错误: {e.__class__.__name__}, 等待 {interval}s 后重试...")
                time.sleep(interval)
            else:
                raise
        except Exception as e:
            # 非网络错误（如 JSON 解析失败），不重试直接抛出
            raise

PAGE_SIZE = 5000  # 分页拉取时的每页条数
START_TIME = "2026-08-22 00:00:00"  # 数据查询起始时间
END_TIME = "2026-08-31 00:00:00"    # 数据查询结束时间
OUTPUT_DIR = r"e:\data\d2"  # CSV 输出目录


def fetch_all_pages(url, headers, parameter, page_size=PAGE_SIZE, start_time=None, end_time=None):
    """先以 pageSize=1 探测 total，再分页拉取全部数据，返回 (所有记录列表, total)"""
    query_criteria = [
        {
            "columnType": "String",
            "columnName": "tag_name",
            "condition": "=",
            "parameter": parameter
        }
    ]
    if start_time:
        query_criteria.append({
            "columnType": "String",
            "columnName": "ts",
            "condition": ">=",
            "parameter": start_time
        })
    if end_time:
        query_criteria.append({
            "columnType": "String",
            "columnName": "ts",
            "condition": "<",
            "parameter": end_time
        })
    base_map = {
        "stb": "ods_ly_data_hub_fjmjsj_new",
        "queryCriteria": query_criteria,
        "sortType": "desc",
        "sortField": "ts",
    }
    # 第一步：pageSize=1 探测 total
    probe_payload = json.dumps({
        "type": 1029,
        "map": {**base_map, "pageNum": 1, "pageSize": 1}
    })
    rj = request_with_retry(url, headers, probe_payload)
    total = rj.get('data', {}).get('total', 0) or 0
    if total == 0:
        return [], 0

    # 第二步：pageSize=5000 分页拉取，直到 pageNum * pageSize > total
    all_items = []
    page_num = 1
    while True:
        page_payload = json.dumps({
            "type": 1029,
            "map": {**base_map, "pageNum": page_num, "pageSize": page_size}
        })
        rj = request_with_retry(url, headers, page_payload)
        items = rj.get('data', {}).get('list', []) or []
        all_items.extend(items)
        if page_num * page_size >= total:
            break
        page_num += 1
    return all_items, total

# 震动类型后缀（可在此处增删）
#   WVIB.VIBRATIONLFIL = 侧向震动, WVIB.VIBRATIONVFIL = 轴向震动, WNAC.WINDSPEED = 风速
suffixes = [
#    ('WVIB.VIBRATIONLFIL', 'fjmjsj_vibration_lateral.csv'),
#    ('WVIB.VIBRATIONVFIL', 'fjmjsj_vibration_vertical.csv'),
##    ('WNAC.WINDSPEED', 'fjmjsj_windspeed.csv'),
#    ('WCNV.CURCONL1', 'fjmjsj_cur1.csv'),
#    ('WCNV.CURCONL2', 'fjmjsj_cur2.csv'),
#    ('WCNV.CURCONL3', 'fjmjsj_cur3.csv'),
##    ('WGEN.GENSPD', 'fjmjsj_genspeed.csv'),
#   ('WNAC.WINDDIRECTION', 'fjmjsj_winddirection.csv'),
#    ('WTRM.TEMGEAOIL', 'fjmjsj_temgeaoil.csv'),
#    ('WTRM.TEMGEAMSDE', 'fjmjsj_temgeamsde.csv'),
#    ('WTRM.TEMGEAMSND', 'fjmjsj_temgeamsnd.csv'),
#    ('WGEN.TEMGENDRIEND', 'fjmjsj_temgende.csv'),
#    ('WGEN.TEMGENNONDE', 'fjmjsj_temgennode.csv'),
    ('WTRM.TEMMAINBEARING2', 'fjmjsj_temmainbearing2.csv'),
]

# pointValue 阈值过滤：只输出 pointValue > 阈值的记录（未列出的 suffix 不过滤）
#   侧向/轴向震动 > 0.04，发电机转速 > 0（即过滤掉 0 值）
thresholds = {
    'WVIB.VIBRATIONLFIL': 0.04,
    'WVIB.VIBRATIONVFIL': 0.04,
#    'WGEN.GENSPD': 200.0,
    'WTRM.TEMGEAOIL': 75.0,
    'WGEN.TEMGENDRIEND': 80.0,
    'WGEN.TEMGENNONDE': 80.0,
    'WTRM.TEMMAINBEARING2': 65.0,
}

url = "http://10.65.78.65:18082/queryData"

payload = json.dumps({
  "type": 1022,
  "map": {
    "stb": "gddl_ods_ly.ods_ly_ads_gd_wind_device_df1016",
    "sortType": "desc",
    "sortField": "",
    "pageNum": 1,
    "pageSize": 5521
  }
})
headers = {
  'zb-token': '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9',
  'Content-Type': 'application/json'
}

response = requests.request("POST", url, headers=headers, data=payload)
data = response.json()
lst = data.get('data', {}).get('list', [])
print(f"total: {len(lst)}")

# regionCompanyName 黑名单：命中就直接忽略
exclude_regions = {
    "宁波风电", "广东新能源", "广西风电", "云南新能源",
    "江西新能源", "湖南新能源", "宁夏新能源", "甘肃新能源",
    "陕西新能源", "国电建投"
}
# stationName 黑名单：命中直接忽略
exclude_stations = {
    "宿松风电场", "太湖风电场", "瓜州风电场", "岷县一期风电场", "通渭风电场",
    "东源蝉子顶风电场", "东里风电场", "高帮山风电场", "弄好岭风电场", "鱼塘风电场",
    "舸川风电场", "浩宏风电场", "风雨殿风电场", "贤良风电场", "天华山风电场",
    "茶山风电场", "穿山风电场", "大武口光伏电站", "海子井光伏电站", "麻黄山风电场",
    "宣和马场湖光伏电站", "灵武马家滩光伏", "牛首山风电场", "平罗光伏电站", "青山风电场",
    "石板泉风电场", "香山风电场", "定边光伏电站", "皇赵光伏电站", "圣熙风电场",
    "烁光光伏电站", "希恒光伏电站", "旭阳光伏电站", "智亮光伏电站", "大风丫口风电场",
    "凤代光伏电站", "金铜盆风电场", "朗山风电场", "柳树冲光伏电站", "磨刀石光伏电站",
    "西泽风电场", "卓干山风电场", "宝山风电场", "青龙风电场", "郊区风电场", "万发风电场",
}
# 过滤：排除 regionCompanyName 黑名单 + stationName 黑名单 + stationName 为空
hits_raw = [(i, x) for i, x in enumerate(lst) if x.get('regionCompanyName') not in exclude_regions]
hits_no_station = [(i, x) for i, x in hits_raw if x.get('stationName') not in exclude_stations]
hits = [(i, x) for i, x in hits_no_station if x.get('stationName')]
excluded_by_region = len(lst) - len(hits_raw)
excluded_by_station = len(hits_raw) - len(hits_no_station)
invalid_no_station = len(hits_no_station) - len(hits)
print(f"  excluded_by_region({len(exclude_regions)} 家): {excluded_by_region},  excluded_by_station({len(exclude_stations)} 个): {excluded_by_station},  invalid_no_station: {invalid_no_station},  final hits: {len(hits)}")

# 按 stationName 分组统计（保持原始出现顺序）
from collections import OrderedDict
station_groups = OrderedDict()
for _, x in hits:
    sn = x.get('stationName', '(未知)')
    station_groups.setdefault(sn, []).append(x)
station_names = list(station_groups.keys())
print(f"  station count: {len(station_names)}")
for sn in station_names:
    devs = station_groups[sn]
    regions = sorted({d.get('regionCompanyName', '(空)') for d in devs})
    print(f"    - {sn}: {len(devs)} 台  (regions: {', '.join(regions)})")
# 后续循环继续用 targets 变量名，保持代码兼容
targets = station_names
print()

# 拼接 parameter: FJMJ1_{大写windDeviceId}WVIB.{suffix}
query_url = "http://10.65.78.65:18082/queryData"
query_headers = {
  'zb-token': '775823708c6a807f3fe2eb801287cf71ff31ba00976045788497045348eddcf9',
  'Content-Type': 'application/json'
}

for suffix, out_path in suffixes:
    print(f"########## suffix={suffix} -> {out_path} ##########")
    out_f = open(os.path.join(OUTPUT_DIR, out_path), 'w', encoding='utf-8-sig', newline='')
    writer = csv.writer(out_f)
    writer.writerow(['windDeviceId', 'windDeviceName', 'typeName', 'phaseName', 'pointValue', 'tagName', 'ts'])

    total_rows = 0
    no_data_list = []  # 收集无数据风机
    for name in targets:
        rows = [x for _, x in hits if x.get('stationName') == name]
        print(f"=== {name}  {len(rows)} 台 ===")
        for idx, x in enumerate(rows, 1):
            wid_raw = x.get('windDeviceId') or ''
            wid = wid_raw.upper()
            phase = x.get('phaseName') or ''
            windDeviceName = x.get('windDeviceName') or ''
            typeName = x.get('typeName') or ''
            parameter = f"FJMJ1_{wid}{suffix}"
            try:
                items, total = fetch_all_pages(query_url, query_headers, parameter,
                                               start_time=START_TIME,
                                               end_time=END_TIME)
                if not items:
                    no_data_list.append((wid_raw, phase, parameter))
                    print(f"  [{idx}/{len(rows)}] {parameter}  (no data)")
                    continue
                filtered = 0
                for it in items:
                    pv = it.get('pointValue', '')
                    # 阈值过滤：只输出 pointValue > 阈值
                    if suffix in thresholds:
                        try:
                            pv_float = float(pv)
                        except (TypeError, ValueError):
                            pv_float = 0.0
                        if pv_float <= thresholds[suffix]:
                            filtered += 1
                            continue
                    writer.writerow([
                        wid_raw,
                        windDeviceName,
                        typeName,
                        phase,
                        pv,
                        it.get('tagName', ''),
                        it.get('ts', '')
                    ])
                    total_rows += 1
                print(f"  [{idx}/{len(rows)}] {parameter}  -> total={total}, fetched={len(items)} rows (filtered out {filtered})")
            except Exception as e:
                no_data_list.append((wid_raw, phase, parameter, str(e)))
                print(f"  [{idx}/{len(rows)}] {parameter}  ERROR: {e}")
        print()

    out_f.close()
    print(f"done. csv={os.path.join(OUTPUT_DIR, out_path)}, total data rows={total_rows}")

    # 统计无数据风机
    if no_data_list:
        print(f"  >>> 无数据风机 {len(no_data_list)} 台:")
        for item in no_data_list:
            if len(item) == 4:  # ERROR
                wid, phase, parameter, err = item
                print(f"    wid={wid}  phase={phase}  ERROR={err}")
            else:
                wid, phase, parameter = item
                print(f"    wid={wid}  phase={phase}  parameter={parameter}")
    else:
        print(f"  >>> 全部风机均有数据")
    print()

