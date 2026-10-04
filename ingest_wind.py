import json
import psycopg2
from psycopg2.extras import execute_values

# Merge upsert:
# - each fixed column is overwritten only when a new value arrives (COALESCE
#   keeps the old value when this message lacks it).
# - every other point is merged into other_points: new keys are appended,
#   existing keys are overwritten, keys absent from this message stay untouched.
MERGE_SQL = """
INSERT INTO wind_seconds_di
    (device_id, ts, gen_tem_driend, gen_tem_nonde, other_points)
VALUES (%s, %s, %s, %s, %s::jsonb)
ON CONFLICT (device_id, ts) DO UPDATE
SET gen_tem_driend = COALESCE(EXCLUDED.gen_tem_driend, wind_seconds_di.gen_tem_driend),
    gen_tem_nonde = COALESCE(EXCLUDED.gen_tem_nonde, wind_seconds_di.gen_tem_nonde),
    other_points = COALESCE(wind_seconds_di.other_points, '{}'::jsonb)
            || COALESCE(EXCLUDED.other_points, '{}'::jsonb)
"""

# 批量 upsert（execute_values 把多行注入 VALUES %s）。
# 同一 (device_id, ts) 的多条消息必须先在 Python 侧按到达顺序归并成
# 一行（见 merge_points）：PG 不允许单条 INSERT 的 ON CONFLICT DO
# UPDATE 命中同一行两次。归并后键唯一，与逐条 ingest_one 语义一致。
MERGE_SQL_BATCH = """
INSERT INTO wind_seconds_di
    (device_id, ts, gen_tem_driend, gen_tem_nonde, other_points)
VALUES %s
ON CONFLICT (device_id, ts) DO UPDATE
SET gen_tem_driend = COALESCE(EXCLUDED.gen_tem_driend, wind_seconds_di.gen_tem_driend),
    gen_tem_nonde = COALESCE(EXCLUDED.gen_tem_nonde, wind_seconds_di.gen_tem_nonde),
    other_points = COALESCE(wind_seconds_di.other_points, '{}'::jsonb)
            || COALESCE(EXCLUDED.other_points, '{}'::jsonb)
"""

# Points stored in fixed columns; every other point goes to other_points.
# To add another fixed column: add the column to the table and append its
# name here (and to the column list in MERGE_SQL).
FIXED_POINTS = ('gen_tem_driend', 'gen_tem_nonde')


def connect():
    with open('data_config.json', encoding='utf-8') as f:
        return psycopg2.connect(json.load(f)['pgConnStr'])


def ingest_one(cur, device_id, ts, points):
    """Ingest one collected message into the matching (device_id, ts) row.

    points accepts either:
      - a dict of points in one message, e.g.
        {'gen_tem_driend': 333, 'gen_tem_nonde': 320, 'rotate': 33}
      - a single (point_name, value) pair for backward compatibility, e.g.
        ('rotate', 33)
    Fixed points go to their columns; every other point goes to other_points.
    """
    if isinstance(points, dict):
        point_map = points
    else:
        point_name, value = points
        point_map = {point_name: value}
    fixed_vals = [point_map.get(fp) for fp in FIXED_POINTS]
    other_points = {k: v for k, v in point_map.items() if k not in FIXED_POINTS}
    cur.execute(MERGE_SQL,
                (device_id, ts, *fixed_vals,
                 json.dumps(other_points, ensure_ascii=False)))


def ingest_batch(messages):
    """messages: iterable of (device_id, ts, points).

    points is a dict (preferred) or a single (point_name, value) pair.
    All messages are applied in one transaction. ON CONFLICT serializes
    concurrent writers on the same (device_id, ts), so points arriving in
    separate messages merge into a single row safely.
    """
    conn = connect()
    try:
        cur = conn.cursor()
        for msg in messages:
            ingest_one(cur, *msg)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Raw tag message format -------------------------------------------
# Message example:
# {"pointValue": 46.6, "description": "风机秒级数据",
#  "tagName": "FJMJ1_B524F6D0B8FF4B2DBC0C102FC4B032B9WGEN.TEMGENDRIEND",
#  "ts": "2026-09-30 13:57:20"}
#
# tagName layout (1-based positions):
#   chars 1-6              -> source prefix, discarded (e.g. FJMJ1_)
#   char 7 .. key start    -> device_id
#   4 chars before '.' .. end -> key (e.g. WGEN.TEMGENDRIEND)

TAG_PREFIX_LEN = 6  # characters skipped at the start of tagName

# Parsed tagName key -> fixed column mapping.
#   - key maps to a column name -> value goes into that fixed column
#   - key maps to None          -> value goes into other_points
#   - key not in the map        -> the message is ignored (not inserted)
# To promote another point to a fixed column later: add the column to the
# table, FIXED_POINTS and MERGE_SQL, then set its mapping here.
TAG_MAP = {
    'WGEN.TEMGENDRIEND': 'gen_tem_driend',
    'WGEN.TEMGENNONDRIEND': 'gen_tem_nonde',  # non-drive end temp; verify real tag name
    'WGEN.SPEED': None,
}


def parse_tag(tag_name):
    """Split a raw tagName into (device_id, key)."""
    dot = tag_name.find('.')
    if dot == -1 or dot - 4 <= TAG_PREFIX_LEN:
        raise ValueError(f'bad tagName: {tag_name!r}')
    key = tag_name[dot - 4:]
    device_id = tag_name[TAG_PREFIX_LEN:dot - 4]
    return device_id, key


def parse_message(msg):
    """Convert one raw message dict into (device_id, ts, points dict).

    Returns None when the key is not in TAG_MAP (message must be dropped).
    """
    device_id, key = parse_tag(msg['tagName'])
    if key not in TAG_MAP:
        return None
    column = TAG_MAP[key]
    if column is None:
        return device_id, msg['ts'], {key: msg['pointValue']}
    return device_id, msg['ts'], {column: msg['pointValue']}


def merge_points(messages):
    """把 (device_id, ts, points) 序列按到达顺序归并到 (device_id, ts) 上。

    语义与逐条 ingest_one 完全一致：
    - 固定列：后到的非 None 值覆盖；后到 None 不覆盖已有值；
    - other_points：键值对按到达顺序合并，后到覆盖先到。
    返回 [(device_id, ts, point_map)]，键唯一。
    """
    merged = {}
    for dev, ts, points in messages:
        point_map = points if isinstance(points, dict) else dict([points])
        dst = merged.setdefault((dev, ts), {})
        for k, v in point_map.items():
            if k in FIXED_POINTS:
                if v is not None or k not in dst:
                    dst[k] = v
            else:
                dst[k] = v
    return [(dev, ts, merged[(dev, ts)]) for dev, ts in merged]


def ingest_raw(messages):
    """messages: iterable of raw dicts with tagName/pointValue/ts.

    全部解析后按 (device_id, ts) 归并，execute_values 批量 upsert
    （同一事务）；TAG_MAP 之外的 key 丢弃。返回实际入库的消息条数
    （丢弃的不计）。
    """
    parsed = [p for p in (parse_message(m) for m in messages) if p is not None]
    if not parsed:
        return 0
    rows = []
    for dev, ts, point_map in merge_points(parsed):
        other = {k: v for k, v in point_map.items() if k not in FIXED_POINTS}
        rows.append((dev, ts,
                     *(point_map.get(fp) for fp in FIXED_POINTS),
                     json.dumps(other, ensure_ascii=False)))
    conn = connect()
    try:
        cur = conn.cursor()
        execute_values(cur, MERGE_SQL_BATCH, rows,
                       template='(%s, %s, %s, %s, %s::jsonb)',
                       page_size=1000)
        conn.commit()
        return len(parsed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    # Demo: three point messages for the same device and timestamp arrive
    # one after another and must accumulate into one row.
    dev, ts = 'WT-DEMO', '2026-10-01 12:13:44'
    conn = connect()
    cur = conn.cursor()
    cur.execute('DELETE FROM wind_seconds_di WHERE device_id=%s AND ts=%s', (dev, ts))
    conn.commit()
    cur.close()
    conn.close()

    # One message carrying multiple points (fixed + other points).
    messages = [
        (dev, ts, {'power_kw': 1200}),
        (dev, ts, {'gen_tem_driend': 62.5}),
        (dev, ts, {'gen_tem_nonde': 60.1}),
        (dev, ts, {'wind_speed': 9.4}),
        (dev, ts, {'power_kw': 1350}),  # retransmission: value updated only
    ]
    for m in messages:
        ingest_batch([m])
        conn = connect()
        cur = conn.cursor()
        cur.execute(
            'SELECT gen_tem_driend, gen_tem_nonde, other_points '
            'FROM wind_seconds_di '
            'WHERE device_id=%s AND ts=%s', (dev, ts))
        row = cur.fetchone()
        print(f'after {m[2]}: de={row[0]} nde={row[1]} other_points={row[2]}')
        cur.execute(
            'SELECT count(*) FROM wind_seconds_di WHERE device_id=%s AND ts=%s',
            (dev, ts))
        print('  rows for this key:', cur.fetchone()[0])
        cur.close()
        conn.close()

    # Multi-point dict in a single message.
    ingest_batch([(dev, ts, {
        'gen_tem_driend': 70.0, 'gen_tem_nonde': 68.0,
        'rotate': 33, 'status': 'running'})])
    conn = connect(); cur = conn.cursor()
    cur.execute(
        'SELECT gen_tem_driend, gen_tem_nonde, other_points '
        'FROM wind_seconds_di '
        'WHERE device_id=%s AND ts=%s', (dev, ts))
    print('after one multi-point message:', cur.fetchone())
    cur.close(); conn.close()

    # Raw tag-format messages (real collector format), one per rule:
    # 1. TEMGENDRIEND -> fixed column gen_tem_driend
    # 2. SPEED -> None -> other_points
    # 3. NOTMAPPED -> not in TAG_MAP -> message dropped
    raw_messages = [
        {"pointValue": 46.6, "description": "风机秒级数据",
         "tagName": "FJMJ1_B524F6D0B8FF4B2DBC0C102FC4B032B9WGEN.TEMGENDRIEND",
         "ts": "2026-09-30 13:57:20"},
        {"pointValue": 8.2, "description": "风机秒级数据",
         "tagName": "FJMJ1_B524F6D0B8FF4B2DBC0C102FC4B032B9WGEN.SPEED",
         "ts": "2026-09-30 13:57:20"},
        {"pointValue": 1.0, "description": "风机秒级数据",
         "tagName": "FJMJ1_B524F6D0B8FF4B2DBC0C102FC4B032B9WGEN.NOTMAPPED",
         "ts": "2026-09-30 13:57:20"},
    ]
    raw_dev, raw_ts, points = parse_message(raw_messages[0])
    print('parsed:', raw_dev, raw_ts, points)
    for raw in raw_messages:
        ingest_raw([raw])
        conn = connect(); cur = conn.cursor()
        cur.execute(
            'SELECT device_id, ts, gen_tem_driend, gen_tem_nonde, '
            'other_points FROM wind_seconds_di '
            'WHERE device_id=%s AND ts=%s',
            (raw_dev, raw_ts))
        print(f'after ...{raw["tagName"][-15:]}:', cur.fetchone())
        cur.close(); conn.close()

    # Clean up demo rows
    conn = connect()
    cur = conn.cursor()
    cur.execute('DELETE FROM wind_seconds_di WHERE device_id=%s AND ts=%s', (dev, ts))
    cur.execute('DELETE FROM wind_seconds_di WHERE device_id=%s AND ts=%s', (raw_dev, raw_ts))
    conn.commit()
    cur.close()
    conn.close()
    print('demo row removed')
