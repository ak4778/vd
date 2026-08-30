"""Probe: find the connect-burst threshold that triggers WSAECONNREFUSED."""
import urllib.request, concurrent.futures as cf, collections, socket, time

BASE = 'http://127.0.0.1:7777'

def hit(i):
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + '/', headers={'Connection': 'close'}), timeout=30) as r:
            r.read()
            return ('ok', None)
    except Exception as e:
        reason = getattr(e, 'reason', None) or e
        return (type(e).__name__, f'{reason}'[:60])

def run(w, n):
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=w) as p:
        res = list(p.map(hit, range(n)))
    dur = time.time() - t0
    c = collections.Counter(res)
    oks = c[('ok', None)]
    errs = n - oks
    print(f'workers={w:>3} n={n}: ok={oks}/{n} ({100*oks/n:.0f}%) in {dur:.1f}s', flush=True)

if __name__ == '__main__':
    # ramp: connect burst size grows, find refusal threshold
    for w in (100, 150, 200, 300, 400, 500):
        run(w, 1500)
        time.sleep(3)
