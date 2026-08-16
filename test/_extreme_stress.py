#!/usr/bin/env python3
"""EXTREME STRESS TEST covering load/save/query/filter/search/pagination/frontend/HTTPS.
Uses ThreadPoolExecutor. Reports per-level OK/total, latency percentiles, RPS."""
import urllib.request, json, time, sys, ssl, socket, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = 'http://127.0.0.1:7777'
SBASE = 'https://127.0.0.1:7443'
TOKEN = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
HDR = {'apiToken': TOKEN, 'Connection': 'close'}
HDR_JSON = {'apiToken': TOKEN, 'Content-Type': 'application/json', 'Connection': 'close'}
TIMEOUT = 30
COOL = 6  # seconds between levels to avoid TIME_WAIT buildup

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
    lat_sorted = sorted(lat_ms)
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
        codes = [f.result() for f in as_completed(futs, timeout=TIMEOUT + 30)]
    ok = sum(1 for c in codes if c == 200)
    return fmt(name, ok, n, time.time() - t0, lat)

# ----------------------------------------------------------- workloads
def w_load(i):
    url = f'{BASE}/api/nodes/get?page=1&pageSize=50'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_load_bigpage(i):
    # Load 500 rows at a time (max pageSize)
    url = f'{BASE}/api/nodes/get?page={(i%59)+1}&pageSize=500'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_save(i):
    bid = (i % 500) + 1
    body = json.dumps({'updates':[
        {'id':str(bid), 'operation':str((i%4)+1), 'customOperation':f'ext{i:06d}'}
    ]}).encode()
    with urllib.request.urlopen(urllib.request.Request(
            f'{BASE}/api/nodes/batchset', data=body, headers=HDR_JSON, method='POST'),
            timeout=TIMEOUT) as r:
        return r.status

def w_query_single(i):
    nid = f'N{(i%29623)+1:06d}'
    url = f'{BASE}/api/nodes/get?keyword={nid}&page=1&pageSize=10'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_filter_combo(i):
    # Vary filter combinations
    filters = [
        'isOnline=1&cameraType=1',
        'isOnline=0&operation=1',
        'cameraType=2&operation=2',
        'isOnline=1&cameraType=1&operation=3',
        'cameraType=0&isOnline=0,1',
    ]
    f = filters[i % len(filters)]
    url = f'{BASE}/api/nodes/get?{f}&page=1&pageSize=20'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_search_cjk(i):
    kws = ['马家滩', '新能源场站', '宁夏新能源', '箱变', '逆变器', '光伏']
    kw = kws[i % len(kws)]
    import urllib.parse
    url = f'{BASE}/api/nodes/get?keyword={urllib.parse.quote(kw)}&page=1&pageSize=10'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_pagination(i):
    # Spread pages across full range (593 pages of 50 rows)
    page = (i % 593) + 1
    url = f'{BASE}/api/nodes/get?page={page}&pageSize=50'
    with urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=TIMEOUT) as r:
        return r.status

def w_frontend(i):
    assets = ['/', '/index.html', '/main.js', '/bundle.js', '/tailwind.css', '/components.js']
    a = assets[i % len(assets)]
    with urllib.request.urlopen(urllib.request.Request(BASE + a, headers={'Connection':'close'}), timeout=TIMEOUT) as r:
        return r.status

def w_https_read(i):
    # HTTPS TLS 1.3: use raw socket
    ctx = ssl._create_unverified_context()
    host = '127.0.0.1'
    port = 7443
    page = (i % 50) + 1
    with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            req = f'GET /api/nodes/get?page={page}&pageSize=10&access_token={TOKEN} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n'
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

# ---------------------------------------------------------------------- plan
PLAN = [
    # (label, fn, requests, workers)
    ('LOAD page=1/pageSize=50 100x', w_load, 100, 100),
    ('LOAD page=1/pageSize=50 500x', w_load, 500, 500),
    ('LOAD page=1/pageSize=50 1000x', w_load, 1000, 500),
    ('LOAD big pageSize=500 300x', w_load_bigpage, 300, 200),
    ('LOAD big pageSize=500 1000x', w_load_bigpage, 1000, 500),
    ('SAVE batch x50 50x', w_save, 50, 50),
    ('SAVE batch x200 200x', w_save, 200, 200),
    ('SAVE batch x500 500x', w_save, 500, 300),
    ('QUERY by id-keyword 200x', w_query_single, 200, 200),
    ('QUERY by id-keyword 800x', w_query_single, 800, 500),
    ('FILTER combo 200x', w_filter_combo, 200, 200),
    ('FILTER combo 1000x', w_filter_combo, 1000, 500),
    ('SEARCH CJK 200x', w_search_cjk, 200, 200),
    ('SEARCH CJK 1000x', w_search_cjk, 1000, 500),
    ('PAGINATE full-range 593x', w_pagination, 593, 300),
    ('PAGINATE full-range x2 1186x', w_pagination, 1186, 500),
    ('FRONTEND static assets 100x', w_frontend, 100, 100),
    ('FRONTEND static assets 500x', w_frontend, 500, 500),
    ('FRONTEND static assets 1500x', w_frontend, 1500, 500),
    ('HTTPS TLS1.3 read 50x', w_https_read, 50, 50),
    ('HTTPS TLS1.3 read 200x', w_https_read, 200, 200),
    ('HTTPS TLS1.3 read 500x', w_https_read, 500, 300),
]

if __name__ == '__main__':
    print('=' * 78)
    print('EXTREME STRESS TEST - load/save/query/filter/search/paginate/frontend/HTTPS')
    print(f'Server: {BASE} (HTTP)  {SBASE} (HTTPS TLS1.3)   nodes=29623')
    print('=' * 78)
    total_reqs = sum(p[2] for p in PLAN)
    print(f'Total planned requests: {total_reqs}    levels: {len(PLAN)}')
    t_all = time.time()
    results = []
    for idx, (label, fn, n, w) in enumerate(PLAN, 1):
        print(f'\n--- [{idx}/{len(PLAN)}] {label} (n={n}, workers={w}) ---')
        try:
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
    print(f'Total planned requests: {total_reqs}')
