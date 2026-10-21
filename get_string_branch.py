"""批量排查：从 pv_station_invert 读全部逆变器 ID，逐个调 monitor_api
queryBranchCurrent 接口，筛 value == "/" 的异常支路，汇总 CSV（含站/区域维度）。
"""
import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg2
import requests

# ---------- 数据库 ----------
TABLE_DEVICE = 'pv_station_invert'
SQL_LOAD = f"""
    SELECT DISTINCT region_company_name, station_id, station_name,
                    inverter_id, inverter_name
    FROM {TABLE_DEVICE}
    WHERE inverter_id IS NOT NULL AND inverter_id <> ''
"""

# ---------- 接口 ----------
API_BASE = 'http://10.170.243.41/monitor_api/baseLightvoltCentralizedInverter/queryBranchCurrent'
HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': 'base eyJhbGciOiJSUzI1NiJ9.ZXlKaGJHY2lPaUpTVXpJMU5pSjkuZXlKemRXSWlPaUl4TWpBME5qQTFPQ0lzSW5KbFpuSmxjMmhFWVhSbElqb3hOemt4T0RVMk1ESTVNekV4TENKcGMzTWlPaUpVUkVZZ1FrRlRSU0lzSW1WNGNDSTZNVGM1TVRnMU5ESTRPU3dpYVdGMElqb3hOemt4T0RVME1qSTVMQ0poZFhSb2IzSnBkR2xsY3lJNklsSnZiR1ZKWkZWd1pHRjBaVWQxYjBScFlXNHNVbTlzWlVsa1RHOXZhMGQxYjBScFlXNHNOREU1WVRKa01qTXdZelUwTkdVek9UZzVaV0k1TVRRMllqSmpNemhpT1dZc09ERmxOalppWTJSbVl6RXlORFZtT1dJellqZG1NelZtT0RnM01ERm1PRFFzTURVd09EQmlNRE5qWlRNMU5HUXlabUk0TmpFNU1tRXdNVE15TnpBME9EUXNZMk16TnpkbE1XSXpNbVUzTkdVM01UazFNMlJrWTJRMU9UVmtOVFE1T0dJaWZRLlhDRGlzRGZqSFhkeXlQdGFKckxoaEdhLWxOZ3MzaDZHZ0Z4bWlCMTFJVUloWjZfMXhqR1YybDJKM1p3TkJJTWdEeUtpUDZaN2pjM1RBME00U1FVWnhoRGhsOUVoZllmcExpQTR5YWhmRWlZNERwVWJ2N2FHX2tOS0FZNVVJcVB3dVNBOGV1b0QwdEdRWkdXdGVwektwNm16eFVhT25PYjRIeWt3SWsyTnlvX1pYY2RGZTY1WndMVGJoUGVvMGFTelZkUDNoeGU2WFpsTFdQTnNZTkRkNlNYTzRoTFloU3ZJSlVuRk9JT2NTb250eV9KVFpUZnNtN2IzRElhbGxTOUNfcGR4Rkp4YjBJb1IzV3F0SGhpX3N0RWdweHZZWkROUFRMOEFDNXlUSE1tcVRndlZidjllSjhPelhCMXNpOGttdERlVDduN3MxOG0wdXhDMEM4a09EUQ.Zunmx912-huC-_1BHe1AgdS_q_WYQ0vt8h_5njrLBAvU-H9m2817QQ05TiOvzNs4IfLL7o6a2DJCFkJcDQRnCainZJqewXAxwtTbhP5ohlsN8gnvDUV45DwFodHAw0bWsu-p4CiYqiKkfIzBg7E03QBvCsjX6nbjcUrYSpJw3a126z3PCCxlO3UpHClG_wMvGrtHo4L16UarJZusTwMbWqp4rS9oWBUFIlHwd6Ga9Ddet0s9lGJ5ahX10VFqi5R1qu5q5TIo6X29-_yRvKkgk6AZg6FwsTm40c3sgq7fpT_QLU8l5wbmawFhV5Pl_jRf19gUk-xUVHTWlaljhfepGg'
}
TIMEOUT = 30
MAX_WORKERS = 20
PROGRESS_EVERY = 200

# ---------- 输出列 ----------
COL_META = ['region_company_name', 'station_id', 'station_name',
            'inverter_id', 'inverter_name']
COL_BAD = COL_META + ['branch_name', 'state', 'value']
COL_FAIL = COL_META + ['error']

OUT_BAD = Path('branch_current_all_bad.csv')
OUT_FAIL = Path('branch_current_all_fail.csv')


def load_inverters():
    with open('data_config.json', encoding='utf-8') as f:
        conn = psycopg2.connect(json.load(f)['pgConnStr'])
    try:
        cur = conn.cursor()
        cur.execute(SQL_LOAD)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    return [dict(zip(COL_META, r)) for r in rows]


def query_one(meta):
    """调单台接口 -> (ok, bad_rows_or_error)。
    bad_rows: [{*meta, branch_name, state, value}, ...]
    """
    iid = meta['inverter_id'].strip()
    url = f'{API_BASE}?inverterId={iid}'
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return False, str(e)

    items = data.get('data', []) or []
    bad = []
    for it in items:
        if it.get('value') == '/':
            row = dict(meta)           # 拷贝维度信息
            row['branch_name'] = it.get('name')
            row['state'] = it.get('state')
            row['value'] = it.get('value')
            bad.append(row)
    return True, bad


def main():
    inverters = load_inverters()
    total = len(inverters)
    print(f'从 {TABLE_DEVICE} 读到 {total} 台逆变器，并发 {MAX_WORKERS} 开始排查...')

    bad_rows = []
    fails = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(query_one, inv): inv for inv in inverters}
        for fut in as_completed(futures):
            meta = futures[fut]
            ok, res = fut.result()
            if ok:
                bad_rows.extend(res)
            else:
                row = dict(meta)
                row['error'] = res
                fails.append(row)
            done += 1
            if done % PROGRESS_EVERY == 0 or done == total:
                print(f'  进度 {done}/{total}  异常支路 {len(bad_rows)}  失败 {len(fails)}')

    # ---- 写异常汇总 ----
    with OUT_BAD.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COL_BAD)
        w.writeheader()
        for r in bad_rows:
            w.writerow(r)

    # ---- 写失败清单 ----
    with OUT_FAIL.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COL_FAIL)
        w.writeheader()
        for r in fails:
            w.writerow(r)

    # 异常逆变器集合（用于快速统计）
    bad_inv_ids = {r['inverter_id'] for r in bad_rows}

    print(f'\n=== 完成 ===')
    print(f'总逆变器:      {total}')
    print(f'异常支路总数:  {len(bad_rows)}  -> {OUT_BAD.resolve()}')
    print(f'异常逆变器数:  {len(bad_inv_ids)}')
    print(f'接口失败数:    {len(fails)}  -> {OUT_FAIL.resolve()}')


if __name__ == '__main__':
    main()
