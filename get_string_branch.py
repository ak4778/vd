"""批量排查：从 pv_station_invert 读全部逆变器 ID，逐个调 monitor_api
queryBranchCurrent 接口，筛 value == "/" 的异常支路，汇总 CSV（含站/区域维度）。

注意：同一接口对部分逆变器会混返回 mppt1..mpptN 等非支路项，
只保留 name 以 "支路" 开头的条目。
"""
import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import psycopg2
import requests
from openpyxl import Workbook
from psycopg2.extras import execute_values

# ---------- 数据库 ----------
TABLE_DEVICE = 'pv_station_invert'
TABLE_ABNORMAL = 'pv_inverter_branch_abnormal'   # 当前轮异常支路（每轮清空重写）
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
BRANCH_NAME_PREFIX = '支路'    # 只统计 name 以该前缀开头的项, 排除 mppt1..mpptN 等
TIMEOUT = 30
MAX_WORKERS = 8                # 单台请求较重, 并发过高易触发内网接口限流
MAX_RETRIES = 3                # 超时/连接错误/5xx 的重试次数
RETRY_BACKOFF = 2              # 重试退避基数 (秒), 第 n 次等待 RETRY_BACKOFF * n
PROGRESS_EVERY = 200
RUN_INTERVAL = 20 * 60         # 循环模式间隔 (秒), 默认 20 分钟

# ---------- 数据库连接配置 ----------
CONFIG_FILE = Path('data_config.json')
PG_CONN_ENV = 'PG_CONN_STR'    # 优先读环境变量

# ---------- 输出列 ----------
COL_META = ['region_company_name', 'station_id', 'station_name',
            'inverter_id', 'inverter_name']
COL_BAD = COL_META + ['branch_name']   # inverter_id + branch_name 即唯一键
COL_FAIL = COL_META + ['error']


def write_csv(path, columns, rows):
    """输出 CSV: UTF-8(BOM) 便于 Excel 直开, 单元格统一按字符串写入。"""
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(['' if r.get(c) is None else str(r.get(c)) for c in columns])


def write_xlsx(path, columns, rows):
    """全部单元格按字符串写入，避免 Excel 把 4-23-07 之类的编码识别成日期。"""
    wb = Workbook()
    ws = wb.active
    ws.append(columns)
    for r in rows:
        ws.append(['' if r.get(c) is None else str(r.get(c)) for c in columns])
    wb.save(path)


def load_pg_conn():
    """连接串来源: 优先环境变量 PG_CONN_STR, 其次 data_config.json 的 pgConnStr。"""
    conn_str = os.environ.get(PG_CONN_ENV, '').strip()
    if conn_str:
        return conn_str

    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open(encoding='utf-8') as f:
                conn_str = (json.load(f).get('pgConnStr') or '').strip()
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f'读取 {CONFIG_FILE} 失败: {e}') from e
        if conn_str:
            return conn_str

    raise RuntimeError(
        f'未找到 PostgreSQL 连接串: 请设置环境变量 {PG_CONN_ENV}, '
        f'或在 {CONFIG_FILE.resolve()} 中写入 '
        f'{{"pgConnStr": "host=... port=5432 dbname=... user=... password=..."}}'
    )


def ensure_abnormal_table(cur):
    """建表（幂等）。每行 = 某台逆变器的一条异常支路，表每轮清空重写。"""
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_ABNORMAL} (
            region_company_name text,
            station_id          text,
            station_name        text,
            inverter_id         text        NOT NULL,
            inverter_name       text,
            branch_name         text        NOT NULL,   -- 如 支路12
            PRIMARY KEY (inverter_id, branch_name)
        )
    """)


def save_bad_to_pg(rows):
    """先清空表再写入本轮结果, 表内只保留最近一轮结果。
    写失败只打印告警, 不影响 CSV/XLSX 产出。"""
    values = [tuple(r.get(c) for c in COL_BAD) for r in rows]
    try:
        conn = psycopg2.connect(load_pg_conn(), connect_timeout=10)
    except Exception as e:
        print(f'PG 写入失败(连接): {type(e).__name__}: {e}')
        return 0

    try:
        with conn, conn.cursor() as cur:
            ensure_abnormal_table(cur)
            cur.execute(f'DELETE FROM {TABLE_ABNORMAL}')   # 覆盖上一轮结果
            if not values:
                return 0
            execute_values(cur, f"""
                INSERT INTO {TABLE_ABNORMAL}
                    (region_company_name, station_id, station_name,
                     inverter_id, inverter_name, branch_name)
                VALUES %s
                ON CONFLICT (inverter_id, branch_name) DO NOTHING
            """, values)
        return len(values)
    except Exception as e:
        print(f'PG 写入失败: {type(e).__name__}: {e}')
        return 0
    finally:
        conn.close()


def load_inverters():
    conn = psycopg2.connect(load_pg_conn(), connect_timeout=10)
    try:
        with conn.cursor() as cur:      # 退出即关闭游标
            cur.execute(SQL_LOAD)
            rows = cur.fetchall()
    finally:
        conn.close()                    # 读完立即释放连接, 后续处理不再占用
    return [dict(zip(COL_META, r)) for r in rows]


def query_one(meta):
    """调单台接口 -> (ok, bad_rows_or_error)。
    bad_rows: [{*meta, branch_name, state, value}, ...]
    超时/连接错误/5xx 自动重试; 业务 code 非 200 不重试 (重试也没用)。
    """
    iid = str(meta.get('inverter_id') or '').strip()
    if not iid:
        return False, 'inverter_id 为空'

    url = f'{API_BASE}?inverterId={iid}'
    raw = None
    last_err = ''

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
            break
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            last_err = f'HTTP {status}: {e}'
            if status is not None and status >= 500 and attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return False, last_err
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f'{type(e).__name__}: {e}'
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return False, last_err
        except ValueError as e:          # 返回非 JSON (如登录页 HTML)
            return False, f'响应非 JSON: {e}'
        except Exception as e:
            return False, f'{type(e).__name__}: {e}'
    else:
        return False, last_err or '未知错误'

    # ---- 响应体校验: 结构异常只影响单台, 不再让整个脚本崩溃 ----
    try:
        if not isinstance(raw, dict):
            return False, f'响应格式异常: 期望 JSON 对象, 实际 {type(raw).__name__}'

        code = raw.get('code')
        if code is not None and str(code) != '200':
            msg = raw.get('message') or ''
            hint = ''
            if str(code) in ('401', '403') or 'token' in msg.lower():
                hint = ' (疑似鉴权失效, 请更新 Authorization token)'
            return False, f'接口返回 code={code}: {msg}{hint}'

        if 'data' not in raw:
            return False, f'响应缺少 data 字段: {str(raw)[:200]}'

        payload = raw.get('data')
        if payload is None:
            items = []                    # code=200 且无数据: 视为该设备无支路上报
        elif isinstance(payload, list):
            items = [it for it in payload if isinstance(it, dict)]
        else:
            return False, f'data 字段格式异常: 期望数组, 实际 {type(payload).__name__}'

        bad = []
        for it in items:
            name = str(it.get('name') or '')
            # 只保留支路项: 部分逆变器同一接口会混返回 mppt1..mpptN
            if not name.startswith(BRANCH_NAME_PREFIX):
                continue
            if it.get('value') == '/':
                row = dict(meta)          # 拷贝维度信息
                row['branch_name'] = name
                bad.append(row)
        return True, bad
    except Exception as e:
        return False, f'解析响应失败: {type(e).__name__}: {e}'


def run_once():
    """执行一轮排查, 输出带本轮时间戳的 CSV/XLSX, 并覆盖写入 PG 表。"""
    run_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_bad = Path(f'branch_current_all_bad_{run_ts}.csv')
    out_fail = Path(f'branch_current_all_fail_{run_ts}.xlsx')

    inverters = load_inverters()
    total = len(inverters)
    print(f'[{run_ts}] 从 {TABLE_DEVICE} 读到 {total} 台逆变器，并发 {MAX_WORKERS} 开始排查...')

    bad_rows = []
    fails = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(query_one, inv): inv for inv in inverters}
        for fut in as_completed(futures):
            meta = futures[fut]
            try:
                ok, res = fut.result()
            except Exception as e:        # 兜底: 单台线程异常不影响整体
                ok, res = False, f'{type(e).__name__}: {e}'
            if ok:
                bad_rows.extend(res)
            else:
                row = dict(meta)
                row['error'] = res
                fails.append(row)
            done += 1
            if done % PROGRESS_EVERY == 0 or done == total:
                print(f'  进度 {done}/{total}  异常支路 {len(bad_rows)}  失败 {len(fails)}')

    # ---- 写异常汇总 (CSV, 文件名带时间戳) ----
    write_csv(out_bad, COL_BAD, bad_rows)

    # ---- 写失败清单 ----
    write_xlsx(out_fail, COL_FAIL, fails)

    # ---- 写 PG ----
    pg_rows = save_bad_to_pg(bad_rows)

    # 异常逆变器集合（用于快速统计）
    bad_inv_ids = {r['inverter_id'] for r in bad_rows}
    auth_fails = sum(1 for r in fails if '疑似鉴权失效' in (r.get('error') or ''))

    print(f'\n=== 完成 ===')
    print(f'总逆变器:      {total}')
    print(f'异常支路总数:  {len(bad_rows)}  -> {out_bad.resolve()}')
    print(f'异常逆变器数:  {len(bad_inv_ids)}')
    print(f'接口失败数:    {len(fails)}  -> {out_fail.resolve()}')
    print(f'PG 写入:       {pg_rows} 行 -> {TABLE_ABNORMAL}')
    if auth_fails:
        print(f'警告: {auth_fails} 台疑似鉴权失效, 异常支路统计可能不完整, 请更新 Authorization token 后重跑')


def run_forever(interval):
    """循环模式: 每轮结束等到下一个间隔点再跑, 单轮出错不退出。"""
    print(f'循环模式启动, 间隔 {interval} 秒 (约 {interval / 60:.1f} 分钟), Ctrl+C 退出')
    round_no = 0
    while True:
        round_no += 1
        started = time.monotonic()
        print(f'\n---------- 第 {round_no} 轮  {datetime.now():%Y-%m-%d %H:%M:%S} ----------')
        try:
            run_once()
        except KeyboardInterrupt:
            raise
        except Exception as e:        # 单轮失败 (如数据库连不上) 不中断循环
            print(f'本轮异常, 已跳过: {type(e).__name__}: {e}')

        elapsed = time.monotonic() - started
        wait = interval - elapsed
        if wait <= 0:
            print(f'本轮耗时 {elapsed:.0f}s 已超过间隔 {interval}s, 立即开始下一轮')
            continue
        print(f'本轮耗时 {elapsed:.0f}s, 等待 {wait:.0f}s 后开始下一轮')
        time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description='批量排查支路电流异常')
    parser.add_argument('--loop', action='store_true',
                        help='循环模式, 每轮完成后按 --interval 等待再跑')
    parser.add_argument('--interval', type=int, default=RUN_INTERVAL,
                        help=f'循环模式间隔秒数 (默认 {RUN_INTERVAL} = 20 分钟)')
    args = parser.parse_args()

    if args.loop:
        try:
            run_forever(args.interval)
        except KeyboardInterrupt:
            print('\n已手动停止')
    else:
        run_once()


if __name__ == '__main__':
    main()
