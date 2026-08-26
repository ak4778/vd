import http.server
import socketserver
import base64
import os
import sys
import functools

# ===== 配置区 =====
USERNAME = "Gddl-bq"
PASSWORD = "Gddl!#%1352026!@"
PORT = 7778 
DIRECTORY = r"c:\s\vd\data"   # 改成你要共享的目录
# ==================

class AuthHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        if not self._check_auth():
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._check_auth():
            return
        super().do_HEAD()

    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return self._reject()
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, pwd = decoded.split(":", 1)
        except Exception:
            return self._reject()
        if user == USERNAME and pwd == PASSWORD:
            return True
        return self._reject()

    def _reject(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Protected"')
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("401 Unauthorized\n".encode("utf-8"))
        return False

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), AuthHandler) as httpd:
        print(f"Serving {DIRECTORY}")
        print(f"http://localhost:{PORT}/")
        print(f"  user: {USERNAME}")
        print(f"  pass: {PASSWORD}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
