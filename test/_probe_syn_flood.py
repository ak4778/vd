"""Probe: raw SYN flood (connect-only, no HTTP) to isolate kernel backlog behavior."""
import socket, concurrent.futures as cf, collections, time, sys

HOST, PORT = '127.0.0.1', 7777

def raw_connect(i):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect((HOST, PORT))
        return 'ok'
    except OSError as e:
        return f'{type(e).__name__}: {e.errno}'
    finally:
        s.close()

def run(w, n, label):
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=w) as p:
        res = list(p.map(raw_connect, range(n)))
    dur = time.time() - t0
    c = collections.Counter(res)
    oks = c['ok']
    print(f'[{label}] workers={w:>3} n={n}: ok={oks}/{n} ({100*oks/n:.0f}%) in {dur:.1f}s  errs={ {k: v for k, v in c.items() if k != "ok"} }', flush=True)

if __name__ == '__main__':
    # connect-only flood, HOLD the connections open for 2s to force queue buildup
    def hold_connect(i):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        try:
            s.connect((HOST, PORT))
            time.sleep(2)          # keep it pending in accept queue (server won't accept instantly? it will accept!)
            return 'ok'
        except OSError as e:
            return f'{type(e).__name__}: {e.errno}'
        finally:
            s.close()

    for w in (300, 400, 500):
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=w) as p:
            res = list(p.map(hold_connect, range(w)))
        dur = time.time() - t0
        c = collections.Counter(res)
        print(f'[HOLD] workers={w:>3}: ok={c["ok"]}/{w} ({100*c["ok"]/w:.0f}%) in {dur:.1f}s  errs={ {k: v for k, v in c.items() if k != "ok"} }', flush=True)
        time.sleep(3)
