#!/usr/bin/env python3
"""DETAILED EXTREME STRESS TEST - 更详细的极限压测
扩展维度：
  - 突破原 1000 并发上限（2000/3000）
  - 边界值测试（maxPageSize=200、batch=256 max）
  - 空结果集 / 通配符 / 五维组合过滤
  - 读写混合负载（80/20、50/50）
  - 持续 60s 稳定性测试
  - HTTPS 深度（混合读写、并发登录、持续 30s）
  - 深度翻页（最后页）
Uses ThreadPoolExecutor. Reports per-level OK/total, latency percentiles, RPS.
"""
import urllib.request, urllib.parse, json, time, sys, ssl, socket, statistics, random
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = 'http://127.0.0.1:7777'
SBASE = 'https://127.0.0.1:7443'
TOKEN = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
HDR = {'apiToken': TOKEN, 'Connection': 'close'}
HDR_JSON = {'apiToken': TOKEN, 'Content-Type': 'application/json', 'Connection': 'close'}
TIMEOUT = 30
COOL = 6

# ---------------------------------------------------------------- helpers
def pct(xs, p):
    if not xs: return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)

def fmt(label, ok, total, dur, lat_ms):
    rps = total / dur if dur > 0 else 0
    okp = 100 * ok / total if total else 0
    s = f'[{label}] {ok}/{total} ok ({okp:.0f}%) in {dur:.2f}s = {rps:.0f} r/s'
    if lat_ms:
        s += f' | lat(ms) min={min(lat_ms):.1f} avg={statistics.mean(lat_ms):.1f} p50={pct(lat_ms,50):.1f} p95={pct(lat_ms,95):.1f} p99={pct(lat_ms,99):.1f} max={max(lat_ms):.1f}'
    return s

def run_level(name, fn, n, workers):
    t0 = time.time()
    lat = []
    def wrap(i):
        t = time.time()
        try:
            rc = fn(i)
            lat.append((time.time() - t) * 1000)
            return rc
        except Exception:
            lat.append((time.time() - t) * 1000)
            return 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(wrap, i) for i in range(n)]
        codes = [f.result() for f in as_completed(futs, timeout=TIMEOUT + 60)]
    ok = sum(1 for c in codes if c == 200)
    return fmt(name, ok, n, time.time() - t0, lat)

def run_sustained(name, fn, duration_s, workers):
    """Run as many requests as possible within a fixed duration window."""
    t_end = time.time() + duration_s
    lat = []
    completed = [0]
    ok_count = [0]
    def wrap(i):
        while time.time() < t_end:
            t = time.time()
            try:
                rc = fn(i)
                lat.append((time.time() - t) * 1000)
                completed[0] += 1
                if rc == 200:
                    ok_count[0] += 1
                break  # one request per worker per round, then pool reuses
            except Exception:
                lat.append((time.time() - t) * 1000)
                completed[0] += 1
                break
    # Use a queue-based approach: submit tasks until time runs out
    t0 = time.time()
    counter = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = set()
        while time.time() < t_end or futs:
            # Top up futures if pool has capacity
            while len(futs) < workers and time.time() < t_end:
                futs.add(pool.submit(wrap, counter))
                counter += 1
            # Wait for at least one to complete
            if futs:
                done, futs = _wait_any(futs)
    dur = time.time() - t0
    rps = completed[0] / dur if dur > 0 else 0
    okp = 100 * ok_count[0] / completed[0] if completed[0] else 0
    s = f'[{name}] {ok_count[0]}/{completed[0]} ok ({okp:.0f}%) in {dur:.2f}s = {rps:.0f} r/s sustained'
    if lat:
        s += f' | lat(ms) min={min(lat):.1f} avg={statistics.mean(lat):.1f} p50={pct(lat,50):.1f} p95={pct(lat,95):.1f} p99={pct(lat,99):.1f} max={max(lat):.1f}'
    return s

def _wait_any(futs, timeout=1):
    """Wait for any future to complete, return (done, still_pending)."""
    from concurrent.futures import wait, FIRST_COMPLETED
    done, pending = wait(futs, return_when=FIRST_COMPLETED, timeout=timeout)
    for d in done:
        try:
            d.result()
        except Exception:
            pass
    return done, pending

# ----------------------------------------------------------- workloads
def w_load(i):
    url = f'{BASE}/api/nodes/get?page=1&pageSize=50'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_load_maxpage(i):
    """maxPageSize=200 boundary"""
    url = f'{BASE}/api/nodes/get?page={(i%59)+1}&pageSize=200'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_save_max_batch(i):
    """256 条批次上限"""
    updates = []
    for j in range(256):
        bid = (i * 256 + j) % 29623 + 1
        updates.append({'id': f'N{bid:06d}', 'operation': str((i % 4) + 1),
                        'customOperation': f'maxbatch_{i}_{j}'})
    body = json.dumps({'updates': updates}).encode()
    with urllib.request.urlopen(urllib.request.Request(
            f'{BASE}/api/nodes/batchset', data=body, headers=HDR_JSON, method='POST'),
            timeout=TIMEOUT) as r:
        return r.status

def w_save_single(i):
    bid = (i % 500) + 1
    body = json.dumps({'updates':[
        {'id':str(bid), 'operation':str((i%4)+1), 'customOperation':f'det{i:06d}'}
    ]}).encode()
    with urllib.request.urlopen(urllib.request.Request(
            f'{BASE}/api/nodes/batchset', data=body, headers=HDR_JSON, method='POST'),
            timeout=TIMEOUT) as r:
        return r.status

def w_query_no_result(i):
    """keyword 永不命中"""
    url = f'{BASE}/api/nodes/get?keyword=ZZZNEVERMATCH_{i:06d}&page=1&pageSize=10'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_query_wildcard(i):
    """keyword=% 应匹配全部（需服务端转义后字面匹配 %）"""
    url = f'{BASE}/api/nodes/get?keyword=%25&page=1&pageSize=10'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_filter_5dim(i):
    """五维组合过滤：状态+设备+操作+场站+公司"""
    filters = [
        'isOnline=0,1&cameraType=1,2,3&operation=0,1,2,3,4',
        'isOnline=1&cameraType=1&operation=1,2',
        'isOnline=0&cameraType=2&operation=3,4',
        'isOnline=1&cameraType=3&operation=0',
        'isOnline=0,1&operation=1,2,3',
    ]
    f = filters[i % len(filters)]
    url = f'{BASE}/api/nodes/get?{f}&page=1&pageSize=20'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_search_cjk_long(i):
    """长中文关键字"""
    kws = ['宁夏新能源马家滩光伏场站', '箱变逆变器汇流箱', '光伏组件汇流',
           '光伏逆变器箱变数据', '宁夏马家滩新能源']
    kw = kws[i % len(kws)]
    url = f'{BASE}/api/nodes/get?keyword={urllib.parse.quote(kw)}&page=1&pageSize=10'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_search_cjk_nomatch(i):
    """中文关键字无匹配"""
    kws = ['不存在的场站', '永不匹配中文', '虚构关键字测试']
    kw = kws[i % len(kws)]
    url = f'{BASE}/api/nodes/get?keyword={urllib.parse.quote(kw)}&page=1&pageSize=10'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_pagination_deep(i):
    """深度翻页：最后几页"""
    # total ~29623, pageSize=50 -> 593 pages
    page = 593 - (i % 10)  # 最后 10 页
    url = f'{BASE}/api/nodes/get?page={page}&pageSize=50'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_pagination_full(i):
    page = (i % 593) + 1
    url = f'{BASE}/api/nodes/get?page={page}&pageSize=50'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_frontend(i):
    assets = ['/', '/index.html', '/main.js', '/bundle.js', '/tailwind.css']
    a = assets[i % len(assets)]
    with urllib.request.urlopen(urllib.request.Request(BASE + a, headers={'Connection':'close'}), timeout=TIMEOUT) as r:
        return r.status

def w_mixed_8020(i):
    """80% 读 + 20% 写"""
    if i % 5 == 0:
        return w_save_single(i)
    return w_load(i)

def w_mixed_5050(i):
    """50% 读 + 50% 写"""
    if i % 2 == 0:
        return w_save_single(i)
    return w_load(i)

def w_mixed_filter_search(i):
    """混合过滤/搜索/翻页（模拟真实业务多用户行为）"""
    mode = i % 3
    if mode == 0:
        return w_filter_5dim(i)
    elif mode == 1:
        return w_search_cjk_long(i)
    else:
        return w_pagination_full(i)

def w_https_mixed(i):
    """HTTPS 读写混合"""
    ctx = ssl._create_unverified_context()
    host = '127.0.0.1'
    port = 7443
    if i % 3 == 0:
        # 写
        bid = (i % 500) + 1
        body = json.dumps({'updates':[
            {'id':str(bid), 'operation':str((i%4)+1), 'customOperation':f'https_{i:06d}'}
        ]}).encode()
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                req = (f'POST /api/nodes/batchset?access_token={TOKEN} HTTP/1.1\r\n'
                       f'Host: {host}:{port}\r\nContent-Type: application/json\r\n'
                       f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
                s.sendall(req)
                resp = b''
                while True:
                    chunk = s.recv(4096)
                    if not chunk: break
                    resp += chunk
                    if b'\r\n\r\n' in resp:
                        first = resp.split(b'\r\n', 1)[0]
                        return 200 if b'200' in first else 0
                return 0
    else:
        page = (i % 50) + 1
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                req = (f'GET /api/nodes/get?page={page}&pageSize=10&access_token={TOKEN} HTTP/1.1\r\n'
                       f'Host: {host}:{port}\r\nConnection: close\r\n\r\n')
                s.sendall(req.encode())
                resp = b''
                while True:
                    chunk = s.recv(4096)
                    if not chunk: break
                    resp += chunk
                    if b'\r\n\r\n' in resp and b'HTTP/1.1 200' in resp[:resp.find(b'\r\n')]:
                        return 200
                first = resp.split(b'\r\n', 1)[0] if resp else b''
                return 200 if b'200' in first else 0

def w_https_login(i):
    """HTTPS 并发登录"""
    ctx = ssl._create_unverified_context()
    host = '127.0.0.1'
    port = 7443
    body = json.dumps({'user': 'Gddl-bq', 'password': 'Gddl!#%2026!@'}).encode()
    with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            req = (f'POST /api/login HTTP/1.1\r\nHost: {host}:{port}\r\n'
                   f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
                   f'Connection: close\r\n\r\n').encode() + body
            s.sendall(req)
            resp = b''
            while True:
                chunk = s.recv(4096)
                if not chunk: break
                resp += chunk
                if b'\r\n\r\n' in resp:
                    first = resp.split(b'\r\n', 1)[0]
                    return 200 if b'200' in first else 0
            return 0

# ---------------------------------------------------------------------- plan
PLAN = [
    # Phase 1: 极限并发（突破 1000 上限）
    ('LOAD 2000x workers=500',       w_load,            2000, 500),
    ('LOAD 3000x workers=500',       w_load,            3000, 500),
    ('SAVE single 1000x',           w_save_single,     1000, 500),
    ('PAGINATE 2000x full-range',   w_pagination_full, 2000, 500),

    # Phase 2: 边界值（maxPageSize=200、batch=256 max）
    ('LOAD pageSize=200 500x',      w_load_maxpage,     500, 300),
    ('LOAD pageSize=200 2000x',     w_load_maxpage,    2000, 500),
    ('SAVE batch=256 max 100x',     w_save_max_batch,   100, 100),
    ('SAVE batch=256 max 300x',     w_save_max_batch,   300, 200),

    # Phase 3: 查询/过滤/搜索详细
    ('QUERY no-result 500x',        w_query_no_result,  500, 300),
    ('QUERY no-result 2000x',       w_query_no_result, 2000, 500),
    ('QUERY wildcard % 200x',        w_query_wildcard,   200, 200),
    ('FILTER 5-dim combo 500x',    w_filter_5dim,       500, 300),
    ('FILTER 5-dim combo 2000x',    w_filter_5dim,      2000, 500),
    ('SEARCH CJK long 500x',        w_search_cjk_long,   500, 300),
    ('SEARCH CJK no-match 500x',    w_search_cjk_nomatch,500, 300),
    ('PAGINATE deep last 500x',    w_pagination_deep,   500, 300),

    # Phase 4: 混合工作负载（模拟真实业务）
    ('MIXED 80R/20W 2000x',         w_mixed_8020,       2000, 500),
    ('MIXED 50R/50W 2000x',         w_mixed_5050,       2000, 500),
    ('MIXED filter+search+page 2000x', w_mixed_filter_search, 2000, 500),
    ('FRONTEND 2000x',              w_frontend,        2000, 500),

    # Phase 5: 持续时间稳定性测试（关注稳定性而非吞吐）
    ('SUSTAINED 60s load',          w_load,            (60, 300), None),
    ('SUSTAINED 60s mixed 80/20',  w_mixed_8020,       (60, 300), None),

    # Phase 6: HTTPS 深度
    ('HTTPS MIXED read/write 200x', w_https_mixed,       200, 100),
    ('HTTPS login concurrent 100x', w_https_login,       100, 100),
]

SUSTAINED_PLAN = [
    ('SUSTAINED 30s HTTPS load',   (w_https_mixed,      30, 100)),
]

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--http-port', type=int, default=7777)
    ap.add_argument('--https-port', type=int, default=7443)
    ap.add_argument('--token', default=TOKEN)
    args = ap.parse_args()
    if args.host != '127.0.0.1' or args.http_port != 7777:
        BASE = f'http://{args.host}:{args.http_port}'
    if args.https_port != 7443:
        SBASE = f'https://{args.host}:{args.https_port}'
    if args.token != TOKEN:
        TOKEN = args.token
        HDR = {'apiToken': TOKEN, 'Connection': 'close'}
        HDR_JSON = {'apiToken': TOKEN, 'Content-Type': 'application/json', 'Connection': 'close'}

    print('=' * 78)
    print('DETAILED EXTREME STRESS TEST - 更详细的极限压测')
    print(f'Server: {BASE} (HTTP)  {SBASE} (HTTPS TLS1.3)   nodes=29623')
    print(f'Dimensions: 极限并发 / 边界值 / 详细查询 / 混合负载 / 持续稳定性 / HTTPS深度')
    print('=' * 78)
    total_reqs = sum(p[2] if not isinstance(p[2], tuple) else 0 for p in PLAN)
    total_sustained = sum(p[2][0] for p in PLAN if isinstance(p[2], tuple))
    print(f'Fixed requests: {total_reqs}    Sustained seconds: {total_sustained}s    levels: {len(PLAN)}')
    t_all = time.time()
    results = []
    for idx, (label, fn, n, w) in enumerate(PLAN, 1):
        print(f'\n--- [{idx}/{len(PLAN)}] {label} ---')
        try:
            if isinstance(n, tuple):
                # Sustained mode: n = (duration_s, workers_actual)
                dur_s, workers_actual = n
                line = run_sustained(label, fn, dur_s, workers_actual)
            else:
                line = run_level(label, fn, n, w)
            print(line)
            results.append((label, line, True))
        except Exception as e:
            print(f'ERROR: {e}')
            results.append((label, str(e), False))
        if idx < len(PLAN):
            time.sleep(COOL)
    print()
    print('=' * 78)
    print('FINAL SUMMARY')
    print('=' * 78)
    for label, line, ok in results:
        tag = 'OK  ' if ok else 'FAIL'
        print(f'  [{tag}] {line}')
    all_ok = sum(1 for _, _, ok in results if ok)
    print(f'\n{all_ok}/{len(results)} levels completed successfully in {time.time()-t_all:.1f}s')
    print(f'Fixed planned requests: {total_reqs}    Sustained seconds: {total_sustained}s')
