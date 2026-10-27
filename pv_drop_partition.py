#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""删除 pv_string_branch 指定日期的分区，并把空间立即归还给操作系统。

分区按 ts 日切分，命名固定为 pv_string_branch_YYYYMMDD（由 pv_fetch_and_upsert.py
的 ensure_partitions 创建）。DROP TABLE 分区会在提交时删除该分区对应的物理文件
（含 TOAST 与索引段文件），因此磁盘空间当场归还给操作系统，不需要
VACUUM / VACUUM FULL。

用法（默认只预览，不删除）：
    python pv_drop_partition.py --list                        # 列出全部分区及大小
    python pv_drop_partition.py --day 2026-09-20              # 预览单日
    python pv_drop_partition.py --from 2026-09-01 --to 2026-09-20   # 预览区间
    python pv_drop_partition.py --from 2026-09-01 --to 2026-09-20 --execute
    python pv_drop_partition.py --day 2026-10-20 --execute --force --yes

安全约束：
  * 今天及未来日期一律拒绝（--force 也不放开未来日期）
  * 落在 RETENTION_DAYS 保留期内的分区默认拒绝，需显式 --force
  * 删除前用 pg_inherits 校验目标确实是 pv_string_branch 的子分区
  * --execute 未带 --yes 时需要交互确认
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'data_config.json')

TABLE_DATA = 'pv_string_branch'
PARTITION_PREFIX = TABLE_DATA + '_'
RETENTION_DAYS = 7                # 与 pv_fetch_and_upsert.py 保持一致
TZ = timezone(timedelta(hours=8))
PARTITION_RE = re.compile(r'^' + re.escape(PARTITION_PREFIX) + r'(\d{8})$')


def connect():
    with open(CONFIG_FILE, encoding='utf-8') as f:
        return psycopg2.connect(json.load(f)['pgConnStr'])


def human(n_bytes):
    """字节数转可读字符串。"""
    size = float(n_bytes or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.1f} {unit}'
        size /= 1024


def list_partitions(cur):
    """返回 [{name, day, bytes, est_rows}]，按日期升序。"""
    cur.execute("""
        SELECT c.oid, c.relname, c.reltuples
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        WHERE i.inhparent = to_regclass(%s)
    """, (TABLE_DATA,))
    rows = []
    for oid, name, reltuples in cur.fetchall():
        m = PARTITION_RE.match(name)
        if not m:
            continue
        cur.execute('SELECT pg_total_relation_size(%s)', (oid,))
        rows.append({
            'oid': oid,
            'name': name,
            'day': datetime.strptime(m.group(1), '%Y%m%d').date(),
            'bytes': cur.fetchone()[0],
            'est_rows': int(reltuples or 0),
        })
    rows.sort(key=lambda r: r['day'])
    return rows


def is_child_partition(cur, name):
    """校验 name 确实是 TABLE_DATA 的子分区（防手工造名误删）。"""
    cur.execute("""
        SELECT 1 FROM pg_inherits
        WHERE inhrelid = to_regclass(%s) AND inhparent = to_regclass(%s)
    """, (name, TABLE_DATA))
    return cur.fetchone() is not None


def parse_day(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        raise argparse.ArgumentTypeError(f'日期格式应为 YYYY-MM-DD，收到 {s!r}')


def build_args():
    p = argparse.ArgumentParser(
        description='删除 pv_string_branch 指定日期分区并立即释放磁盘空间')
    p.add_argument('--day', type=parse_day, help='单个日期，如 2026-09-20')
    p.add_argument('--from', dest='date_from', type=parse_day, help='起始日期（含）')
    p.add_argument('--to', dest='date_to', type=parse_day, help='结束日期（含）')
    p.add_argument('--list', action='store_true', help='只列出全部分区，不做任何删除')
    p.add_argument('--execute', action='store_true', help='真正执行 DROP（默认仅预览）')
    p.add_argument('--yes', action='store_true', help='跳过交互确认')
    p.add_argument('--force', action='store_true',
                   help='允许删除保留期(RETENTION_DAYS)内的分区；今天及未来仍拒绝')
    p.add_argument('--lock-timeout', default='5s',
                   help='DROP 等锁超时，默认 5s，避免卡住采集/回补（默认: %(default)s）')
    args = p.parse_args()
    if not args.list and not args.day and not (args.date_from and args.date_to):
        p.error('需要 --list，或 --day，或同时给出 --from 与 --to')
    if args.day and (args.date_from or args.date_to):
        p.error('--day 与 --from/--to 不能同时使用')
    if args.date_from and args.date_to and args.date_from > args.date_to:
        p.error('--from 不能晚于 --to')
    return args


def main():
    args = build_args()
    today = datetime.now(TZ).date()
    keep_from = today - timedelta(days=RETENTION_DAYS - 1)

    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    db_size_before = cur.fetchone()[0]

    partitions = list_partitions(cur)

    if args.list:
        print(f'=== {TABLE_DATA} 分区清单（今天 {today}，保留自 {keep_from}）===')
        total = 0
        for r in partitions:
            total += r['bytes']
            print(f"{r['name']}  {human(r['bytes']):>10}  约 {r['est_rows']:>12} 行")
        print(f'—— 共 {len(partitions)} 个分区，合计 {human(total)}')
        print(f'当前数据库大小: {db_size_before}')
        cur.close()
        conn.close()
        return

    # 计算目标日期集合
    if args.day:
        targets = [args.day]
    else:
        targets = []
        d = args.date_from
        while d <= args.date_to:
            targets.append(d)
            d += timedelta(days=1)

    by_day = {r['day']: r for r in partitions}
    selected, skipped, blocked = [], [], []

    for day in targets:
        if day >= today:
            blocked.append((day, '今天或未来日期，拒绝删除'))
            continue
        if day >= keep_from and not args.force:
            blocked.append((day, f'在保留期({RETENTION_DAYS}天)内，需 --force 才删'))
            continue
        row = by_day.get(day)
        if row is None:
            skipped.append((day, '分区不存在'))
            continue
        if not is_child_partition(cur, row['name']):
            blocked.append((day, f'{row["name"]} 不是 {TABLE_DATA} 的子分区'))
            continue
        selected.append(row)

    print(f'=== 目标区间 {targets[0]} ~ {targets[-1]}，今天 {today}，保留自 {keep_from} ===')
    if selected:
        print('--- 将删除 ---')
        total_bytes = 0
        for r in selected:
            total_bytes += r['bytes']
            print(f"  {r['name']}  {human(r['bytes']):>10}  约 {r['est_rows']} 行")
        print(f'  合计 {len(selected)} 个分区，可释放 {human(total_bytes)}')
    if skipped:
        print('--- 跳过 ---')
        for day, why in skipped:
            print(f'  {day}  {why}')
    if blocked:
        print('--- 拒绝 ---')
        for day, why in blocked:
            print(f'  {day}  {why}')

    if not selected:
        print('没有可删除的分区，退出。')
        cur.close()
        conn.close()
        return

    if not args.execute:
        print('\n当前为预览模式，未删除任何数据。加 --execute 才会真正 DROP。')
        cur.close()
        conn.close()
        return

    if not args.yes:
        print(f'\n即将永久删除 {len(selected)} 个分区（数据不可恢复）。')
        print("确认请输入大写 YES：", end='', flush=True)
        try:
            answer = input().strip()
        except EOFError:
            answer = ''
        if answer != 'YES':
            print('已取消，未删除任何数据。')
            cur.close()
            conn.close()
            return

    dropped_bytes = 0
    failed = []
    for r in selected:
        name = r['name']
        try:
            cur.execute(f"SET lock_timeout = '{args.lock_timeout}'")
            cur.execute(f'DROP TABLE IF EXISTS {name}')
            dropped_bytes += r['bytes']
            print(f"DROP {name} ... 完成，释放 {human(r['bytes'])}")
        except Exception as exc:                      # noqa: BLE001 单表失败不中断整体
            failed.append((name, str(exc)))
            print(f'DROP {name} ... 失败: {exc}')

    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    db_size_after = cur.fetchone()[0]

    print()
    print(f'已删除 {len(selected) - len(failed)} 个分区，释放 {human(dropped_bytes)}')
    if failed:
        print('失败清单:')
        for name, err in failed:
            print(f'  {name}: {err}')
    print(f'数据库大小: {db_size_before} -> {db_size_after}')
    print('分区物理文件已在 DROP 提交时删除，空间已归还操作系统，无需额外 VACUUM。')
    print('提示：pv_fetch_and_upsert.py 下次巡检会自动补建未来 7 天分区，不影响采集。')

    cur.close()
    conn.close()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
