#!/usr/bin/env python3
"""Concurrency stress test using Python ThreadPoolExecutor (bypasses curl/Schannel limits).
Tests both pure reads and mixed read+write workloads."""
import urllib.request, json, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = 'http://127.0.0.1:8000'
API_TOKEN = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'

def fetch_read(i):
    url = f'{BASE}/api/nodes/get?page={(i % 100)+1}&pageSize=10'
    try:
        req = urllib.request.Request(url, headers={'apiToken': API_TOKEN})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception:
        return 0

def fetch_mixed(i):
    if i % 3 == 0:
        # write
        bid = (i % 256) + 1
        body = json.dumps({"updates":[{"id":str(bid),"operation":str((i%4)+1),"customOperation":f"mix{i}"}]}).encode()
        url = f'{BASE}/api/nodes/batchset'
        try:
            req = urllib.request.Request(url, data=body, headers={'apiToken': API_TOKEN, 'Content-Type':'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except Exception:
            return 0
    else:
        return fetch_read(i)

def test_level(level, fn):
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=level) as pool:
        futures = [pool.submit(fn, i) for i in range(level)]
        results = [f.result() for f in as_completed(futures, timeout=30)]
    dur = time.time() - t0
    ok = sum(1 for r in results if r == 200)
    return ok, level, dur

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'read'
    levels = [int(x) for x in sys.argv[2:]] if len(sys.argv) > 2 else [50, 100, 200, 500]
    fn = fetch_mixed if mode == 'mixed' else fetch_read
    label = 'MIXED R/W' if mode == 'mixed' else 'READS'
    for level in levels:
        try:
            ok, total, dur = test_level(level, fn)
            rps = total / dur if dur > 0 else 0
            print(f'Python {label} {level:>4}: {ok}/{total} ok ({100*ok/total:.0f}%) in {dur:.2f}s = {rps:.0f} r/s')
        except Exception as e:
            print(f'Python {label} {level:>4}: ERROR {e}')
        time.sleep(1)
