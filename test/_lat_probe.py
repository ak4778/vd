# Client-side latency probe: sequential + concurrent requests to a quiet server.
# Usage: python _lat_probe.py [out_file]
import ctypes, sys, time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Report whether THIS client process is inside any Windows Job Object
k32 = ctypes.windll.kernel32
b = ctypes.c_bool()
k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(b))
lines = [f"in_any_job={bool(b.value)}"]

URL = "http://127.0.0.1:7777/api/mode/get"

def one(_):
    t0 = time.perf_counter()
    try:
        urllib.request.urlopen(URL, timeout=15).read()
        return (time.perf_counter() - t0) * 1000
    except Exception:
        return -1.0

for conc in (1, 10, 50):
    n = conc * 4
    with ThreadPoolExecutor(max_workers=conc) as ex:
        lats = sorted(ex.map(one, range(n)))
    ok = [x for x in lats if x >= 0]
    if ok:
        p50 = ok[len(ok) // 2]
        lines.append(f"conc={conc:>3} ok={len(ok)}/{n} p50={p50:7.1f}ms max={ok[-1]:7.1f}ms")
    else:
        lines.append(f"conc={conc:>3} all failed")

out = "\n".join(lines)
print(out)
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as f:
        f.write(out + "\n")
