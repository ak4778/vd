# -*- coding: utf-8 -*-
"""一次性回补脚本：按 1 分钟块重拉指定时间段的 PV 组串数据。

复用 pv_fetch_and_upsert 的分页拉取与 upsert 入库，只做补数据：
不改任何配置、不改水位线、不动回补游标，写入幂等（ON CONFLICT 更新）。

用法：
    python pv_backfill_once.py 2026-10-27 10:00 11:00 [--workers 3] [--block 60]
"""
import argparse
import time
from datetime import datetime, timedelta

import psycopg2

import pv_fetch_and_upsert as pv

TS_FMT = pv.TS_FMT


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('day', help='日期，例如 2026-10-27')
    ap.add_argument('start', help='开始时间，例如 10:00')
    ap.add_argument('end', help='结束时间（不含），例如 11:00')
    ap.add_argument('--workers', type=int, default=3, help='并发 pattern 数，默认 3')
    ap.add_argument('--block', type=int, default=60, help='单块秒数，默认 60')
    return ap.parse_args()


def main():
    a = parse_args()
    try:
        pv.setup_logging()
    except Exception:
        pass
    start = datetime.strptime(f'{a.day} {a.start}', '%Y-%m-%d %H:%M')
    end = datetime.strptime(f'{a.day} {a.end}', '%Y-%m-%d %H:%M')
    total = int((end - start).total_seconds() // a.block)
    print(f'回补开始 {start} ~ {end}，共 {total} 块，workers={a.workers}，block={a.block}s', flush=True)

    cum_f = cum_n = 0
    t_all = time.time()
    for i in range(total):
        s = start + timedelta(seconds=i * a.block)
        e = s + timedelta(seconds=a.block)
        ss, se = s.strftime(TS_FMT), e.strftime(TS_FMT)
        t0 = time.time()
        f = n = None
        for attempt in range(1, 6):
            try:
                f, n = pv.fetch_and_ingest_window(ss, se, workers=a.workers)
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as ex:
                wait = 15 * attempt
                print(f'块 {ss} 第{attempt}次连接失败：{str(ex).strip()[:160]}，{wait}s 后重试', flush=True)
                time.sleep(wait)
            except Exception as ex:
                wait = 10 * attempt
                print(f'块 {ss} 第{attempt}次失败：{type(ex).__name__}: {str(ex).strip()[:160]}，{wait}s 后重试', flush=True)
                time.sleep(wait)
        if f is None:
            print(f'块 {ss} 连续 5 次失败，跳过', flush=True)
            continue
        cum_f += f
        cum_n += n
        done = time.time() - t_all
        eta = int((total - i - 1) * (done / (i + 1)))
        print(f'块 {i + 1}/{total} {ss} fetched={f} ingested={n} 用时={time.time() - t0:.1f}s '
              f'累计入库={cum_n} ETA≈{eta // 60}min', flush=True)

    print(f'回补完成，用时 {int(time.time() - t_all)}s，拉到 {cum_f} 条，入库 {cum_n} 条', flush=True)


if __name__ == '__main__':
    main()
