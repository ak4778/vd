#!/usr/bin/env python3
"""FD_SETSIZE=64 越界测试（Windows 专项）

策略：
  1. 阶梯式并发建立 N 个 TCP 连接（每个连接都保持 alive，不关闭）
  2. 每个连接发送一个完整 HTTP GET 请求，但**不 recv、不 close**，
     使得服务器端 accept 出来的 socket fd 一直递增
  3. 每个阶梯结束后用独立探针请求检查服务器是否还能响应 200
     （如果 fd_set 越界写导致栈损坏，服务器 select 主循环会崩或卡住）
  4. 若任意一级探针失败 / 超时 → 判定触发 bug，记录崩溃点

用法：
  python .\test\test_fd_setsize.py --host 127.0.0.1 --port 7777
"""
import socket, time, argparse, threading, sys, urllib.request, os

HOST = '127.0.0.1'
PORT = 7777
API_TOKEN = 'm4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
PROBE_TIMEOUT = 5     # 探针超时
HOLD_TIME = 8         # 每批连接保持秒数（留足时间触发 select 越界写）

STEPS = [
    ('STEP 30 conns',    30),
    ('STEP 50 conns',    50),
    ('STEP 60 conns',    60),
    ('STEP 64 conns',    64),   # 理论临界值
    ('STEP 70 conns',    70),   # 刚越线
    ('STEP 80 conns',    80),
    ('STEP 100 conns',  100),
    ('STEP 120 conns',  120),   # 远超 FD_SETSIZE=64
    ('STEP 150 conns',  150),
    ('STEP 200 conns',  200),
    ('STEP 300 conns',  300),
]

# 将已建立的 socket 放全局列表保活，防止 GC 关连接
KEPT_SOCKETS = []
KEPT_LOCK = threading.Lock()


def probe_ok():
    """独立连接检查服务器是否还在响应。返回 True=活着, False=崩了"""
    try:
        req = urllib.request.Request(
            f'http://{HOST}:{PORT}/api/mode/get',
            headers={'apiToken': API_TOKEN, 'Connection': 'close'},
        )
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return r.status == 200
    except Exception as e:
        print(f'      [PROBE FAIL] {type(e).__name__}: {e}')
        return False


def hold_one_conn(_i):
    """建立一个 TCP 连接，发送 HTTP 请求头，然后一直 HOLD_TIME 秒占着不 recv/close"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((HOST, PORT))
        # 发一个合法 HTTP 请求（半请求也行，发个不完整请求服务器不会关）
        s.sendall(
            f'GET /api/nodes/get?page=1&pageSize=50&access_token={API_TOKEN} HTTP/1.1\r\n'
            f'Host: {HOST}:{PORT}\r\n'
            f'Connection: keep-alive\r\n'
            f'\r\n'.encode()
        )
        # 加入全局保活池
        with KEPT_LOCK:
            KEPT_SOCKETS.append(s)
        time.sleep(HOLD_TIME)
    except Exception:
        pass
    # 时间到也不关，留在 KEPT_SOCKETS 直到脚本结束
    return True


def run_step(label, n):
    t0 = time.time()
    # 并发起 n 个线程各占一个连接
    threads = []
    for i in range(n):
        t = threading.Thread(target=hold_one_conn, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        # 稍微错峰，避免 connect 全部瞬间砸进来导致 backlog 满
        if i % 20 == 0:
            time.sleep(0.02)

    # 等所有连接都建好并发出请求
    waited = 0
    while True:
        with KEPT_LOCK:
            n_kept = len(KEPT_SOCKETS)
        if n_kept >= n or waited > 15:
            break
        time.sleep(0.2)
        waited += 0.2

    # 给服务器 1 秒跑完一个完整主循环（含 select/FD_SET 越界写）
    time.sleep(1.5)
    build_ms = int((time.time() - t0) * 1000)

    # 探测：服务器还活着吗？连续 3 次，有任意一次成功算活着
    ok = any(probe_ok() for _ in range(3))
    print(f'  [{label}] conns_target={n}  conns_active={n_kept}  build={build_ms}ms  probe={"OK" if ok else "DEAD/CRASHED"}')
    return ok


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=HOST)
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--token', default=API_TOKEN)
    ap.add_argument('--hold', type=int, default=HOLD_TIME)
    args = ap.parse_args()
    HOST, PORT, API_TOKEN, HOLD_TIME = args.host, args.port, args.token, args.hold

    print('=' * 70)
    print(f'FD_SETSIZE Windows 越界测试  server=http://{HOST}:{PORT}')
    print(f'每批保持 {HOLD_TIME}s，共 {sum(n for _, n in STEPS)} 个累计连接目标')
    print(f'（每步结束后所有已建连接仍保持，总 fd 数会逐步累积）')
    print('=' * 70)

    # 热身：确认服务器在线
    if not probe_ok():
        print('FATAL: 服务器未启动，退出')
        sys.exit(2)
    print('热身探针：OK\n')

    results = []
    crashed_at = None
    for label, n in STEPS:
        ok = run_step(label, n)
        results.append((label, n, ok))
        if not ok:
            crashed_at = (label, n)
            print('\n  *** 服务器已崩溃/卡住，终止后续阶梯 ***')
            break
        print(f'     当前累计连接：{len(KEPT_SOCKETS)}，休息 2s 进入下一级…\n')
        time.sleep(2)

    print()
    print('=' * 70)
    print('FINAL SUMMARY')
    print('=' * 70)
    for label, n, ok in results:
        tag = 'OK  ' if ok else 'FAIL'
        print(f'  [{tag}] {label:15s}  target={n:4d}')

    print()
    if crashed_at:
        label, n = crashed_at
        print(f'🔴 崩溃/卡死发生在：{label} (target={n})，累计连接约 {len(KEPT_SOCKETS)}')
        print('   （注意：因为每个 step 之间不关闭连接，真正触发越界的 fd 编号')
        print('    可能早于该 step 的 target 数，取决于服务器其他 fd 的占用）')
        # 输出累计数作为参考
        ok_steps = [r for r in results if r[2]]
        if ok_steps:
            print(f'   最后稳定级别：{ok_steps[-1][0]} ({ok_steps[-1][1]})')
        sys.exit(1)
    else:
        print(f'🟢 全级别通过，最大累计连接 {len(KEPT_SOCKETS)}，服务器未崩')
        print('   可能编译已显式使用 -DFD_SETSIZE=XXXX，或当前平台不限制。')
        sys.exit(0)
