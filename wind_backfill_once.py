"""一次性回补：把风电上游历史数据补进 wind_seconds_di，并重算对应分钟聚合。

复用 wind_fetch_and_upsert 的 fetch_window / ingest_raw，不修改主程序；
入库是 MERGE 幂等 upsert，可重复执行。用法：
    python wind_backfill_once.py [起始时间] [结束时间]
不传参数时使用 DEFAULT_START / DEFAULT_END。
"""
import logging
import sys
from datetime import datetime, timedelta

import psycopg2

import wind_fetch_and_upsert as wf

DEFAULT_START = '2026-11-01 00:00:00'
DEFAULT_END = '2026-11-02 02:20:00'
WINDOW_MINUTES = 10      # 每轮拉取的时间跨度（分钟）
ROLLUP_CHUNK_MINUTES = 10   # 聚合按 10 分钟分段，避免单条 SQL 扫描过大/连接被服务端中断

TS_FMT = '%Y-%m-%d %H:%M:%S'


def parse(s):
    return datetime.strptime(s, TS_FMT)


def backfill(start, end):
    """按固定窗口拉取并入库，返回 (拉取条数, 入库条数)。"""
    total_fetched = total_ingested = 0
    rounds = int((end - start).total_seconds() // (WINDOW_MINUTES * 60)) + 1
    idx = 0
    cur = start
    while cur < end:
        idx += 1
        nxt = min(cur + timedelta(minutes=WINDOW_MINUTES), end)
        s, e = cur.strftime(TS_FMT), nxt.strftime(TS_FMT)
        items = wf.fetch_window(s, e)
        ingested = wf.ingest_raw(items)
        total_fetched += len(items)
        total_ingested += ingested
        print(f'[回补 {idx}/{rounds}] {s} ~ {e} 拉取 {len(items)} 条，入库 {ingested} 条；'
              f'累计拉取 {total_fetched}，累计入库 {total_ingested}', flush=True)
        cur = nxt
    return total_fetched, total_ingested


def rollup(start, end):
    """把聚合 SQL 的“最近3分钟”条件替换成指定区间，按 10 分钟分段执行。"""
    with open(wf.ROLLUP_SQL_FILE, encoding='utf-8') as f:
        sql = f.read()
    insert_sql = sql[sql.index('INSERT INTO'):]
    insert_sql = insert_sql[:insert_sql.index(';') + 1]
    old = ("WHERE ts >= date_trunc('minute', now()) - interval '3 min'\n"
           "  AND ts <  date_trunc('minute', now())")
    if insert_sql.count(old) != 1:
        raise RuntimeError('聚合 SQL 的 WHERE 结构不符合预期，已中止聚合')
    cur = start
    chunk = timedelta(minutes=ROLLUP_CHUNK_MINUTES)
    idx = 0
    while cur < end:
        idx += 1
        nxt = min(cur + chunk, end)
        s, e = cur.strftime(TS_FMT), nxt.strftime(TS_FMT)
        q = insert_sql.replace(old, f"WHERE ts >= '{s}'\n  AND ts <  '{e}'")
        for attempt in range(1, 3):
            conn = wf.connect()
            try:
                with conn.cursor() as cur_:
                    cur_.execute(q)
                conn.commit()
                break
            except psycopg2.OperationalError as ex:
                if not conn.closed:
                    conn.rollback()
                if attempt == 2:
                    raise
                print(f'[聚合] {s} ~ {e} 连接异常（{ex}），重试一次', flush=True)
            except Exception:
                if not conn.closed:
                    conn.rollback()
                raise
            finally:
                conn.close()
        print(f'[聚合 {idx}] {s} ~ {e} 完成', flush=True)
        cur = nxt


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s')
    start_s = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end_s = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END
    start, end = parse(start_s), parse(end_s)
    if start >= end:
        raise SystemExit('起始时间必须早于结束时间')
    print(f'回补区间 {start_s} ~ {end_s}，窗口 {WINDOW_MINUTES} 分钟', flush=True)
    fetched, ingested = backfill(start, end)
    print(f'秒级回补完成：拉取 {fetched} 条，入库 {ingested} 条', flush=True)
    rollup(start, end)
    print('分钟聚合完成，回补结束', flush=True)


if __name__ == '__main__':
    main()
