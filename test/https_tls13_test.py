#!/usr/bin/env python3
"""HTTPS/TLS test helper for the extreme test suite.

Mongoose's built-in TLS only supports TLS 1.3 (X25519 + AES-GCM/ChaCha20).
Windows curl.exe uses schannel, which defaults to TLS 1.2 and cannot connect.
This script uses Python's ssl module (OpenSSL) to exercise the HTTPS listener
with a real TLS 1.3 client, and verifies that TLS 1.2 is rejected.

Output: one line per check, pipe-delimited for PowerShell parsing:
    HTTPS_RESULT|<name>|<http_code>|<PASS|FAIL>|<detail>

Exit code: 0 if all checks pass, 1 otherwise.
"""
import ssl
import socket
import http.client
import base64
import argparse
import sys


def make_ctx(min_ver, max_ver=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = min_ver
    if max_ver is not None:
        ctx.maximum_version = max_ver
    return ctx


def https_request(ctx, host, port, path, headers=None, method='GET', timeout=10):
    """Perform an HTTPS request. Returns (status, body, set_cookie)."""
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        # getheader() joins multiple Set-Cookie values with ', '
        set_cookie = resp.getheader('Set-Cookie')
        status = resp.status
    finally:
        conn.close()
    return status, body, set_cookie


def extract_token(set_cookie):
    """Extract access_token value from a Set-Cookie header."""
    if not set_cookie:
        return None
    for part in set_cookie.split(';'):
        part = part.strip()
        if part.startswith('access_token='):
            return part[len('access_token='):]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8443)
    ap.add_argument('--token', required=True,
                    help='apiToken value from data_config.json')
    ap.add_argument('--user', default='admin', help='login username')
    ap.add_argument('--pass', dest='password', default='admin', help='login password')
    args = ap.parse_args()

    host, port, token = args.host, args.port, args.token
    results = []  # (name, code, ok, detail)

    # Verify this OpenSSL build supports TLS 1.3
    try:
        ctx13 = make_ctx(ssl.TLSVersion.TLSv1_3)
    except (ValueError, AttributeError) as e:
        print(f'HTTPS_RESULT|TLS1.3 support|0|FAIL|OpenSSL lacks TLS 1.3: {e}')
        return 1

    # 1. mode/get via apiToken header (TLS 1.3)
    try:
        status, body, _ = https_request(ctx13, host, port, '/api/mode/get',
                                        {'apiToken': token})
        ok = (status == 200)
        results.append(('HTTPS TLS1.3 mode/get', status, ok, f'code={status}'))
    except Exception as e:
        results.append(('HTTPS TLS1.3 mode/get', 0, False,
                        f'{type(e).__name__}: {e}'))

    # 2. nodes/get via apiToken header (TLS 1.3)
    try:
        status, body, _ = https_request(ctx13, host, port,
                                        '/api/nodes/get?page=1&pageSize=1',
                                        {'apiToken': token})
        ok = (status == 200 and b'"total"' in body)
        results.append(('HTTPS TLS1.3 nodes/get', status, ok, f'code={status}'))
    except Exception as e:
        results.append(('HTTPS TLS1.3 nodes/get', 0, False,
                        f'{type(e).__name__}: {e}'))

    # 3. login via Basic Auth (TLS 1.3) — capture access_token cookie
    tok = None
    try:
        auth = base64.b64encode(f'{args.user}:{args.password}'.encode()).decode()
        status, body, set_cookie = https_request(
            ctx13, host, port, '/api/login',
            {'Authorization': f'Basic {auth}'}, method='POST')
        tok = extract_token(set_cookie)
        ok = (status == 200 and tok is not None)
        results.append(('HTTPS login', status, ok,
                        f'code={status} token={"yes" if tok else "no"}'))
    except Exception as e:
        results.append(('HTTPS login', 0, False, f'{type(e).__name__}: {e}'))

    # 4. cookie auth with the login token (TLS 1.3)
    if tok:
        try:
            status, body, _ = https_request(
                ctx13, host, port, '/api/nodes/get?page=1&pageSize=1',
                {'Cookie': f'access_token={tok}'})
            ok = (status == 200)
            results.append(('HTTPS cookie auth', status, ok, f'code={status}'))
        except Exception as e:
            results.append(('HTTPS cookie auth', 0, False,
                            f'{type(e).__name__}: {e}'))
    else:
        results.append(('HTTPS cookie auth', 0, False, 'skipped: no login token'))

    # 5. TLS 1.2 must be REJECTED (mongoose builtin = TLS 1.3 only)
    try:
        ctx12 = make_ctx(ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_2)
        status, body, _ = https_request(ctx12, host, port, '/api/mode/get',
                                        {'apiToken': token}, timeout=8)
        # Reached here = TLS 1.2 handshake succeeded (unexpected)
        ok = (status != 200)
        results.append(('TLS1.2 rejected', status, ok,
                        f'code={status} (expected handshake failure)'))
    except (ssl.SSLError, OSError, socket.timeout) as e:
        # Connection/handshake failed = TLS 1.2 rejected as expected
        results.append(('TLS1.2 rejected', 0, True,
                        f'rejected: {type(e).__name__}'))
    except Exception as e:
        results.append(('TLS1.2 rejected', 0, True,
                        f'rejected: {type(e).__name__}'))

    # Emit results
    all_ok = True
    for name, code, ok, detail in results:
        if not ok:
            all_ok = False
        print(f'HTTPS_RESULT|{name}|{code}|{"PASS" if ok else "FAIL"}|{detail}')

    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
