# -*- coding: utf-8 -*-
"""
PG 模式服务器端到端冒烟测试
=============================
流程：
  1. 用 Python pgserver 包启动内嵌 PostgreSQL 16.2（与 pump_test 验证同一环境）
  2. psycopg2 预建 nodes 表（与服务器 schema 同构）+ 插入 6 行种子数据
  3. 临时目录写 data_config.json（UTF-8 无 BOM），启动 PG 版 vvvv.exe
  4. 通过 HTTP 断言：mode/get、登录、过滤（IN/operation=0/keyword CJK/空值）、
     分页、batchset 更新（含 has 标志语义）+ psycopg2 直查库核对持久化
  5. 清理：停服务器、停 PG、删临时目录

前置条件：
  - 项目根目录已用 `mingw32-make PG_MODE=1 vvvv.exe` 构建出 PG 版 vvvv.exe
  - Python 依赖：psycopg2-binary、pgserver（pump_test 验证时已装）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg2
import pgserver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # c:\s\vd
EXE = os.path.join(ROOT, "vvvv.exe")
PORT = 18431
BASE = "http://127.0.0.1:%d" % PORT
API_TOKEN = "pg_smoke_test_token"

_results = []  # (name, ok, detail)


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + ("  " + detail if detail else ""))


def http_req(path, method="GET", body=None, headers=None, timeout=10):
    """返回 (status, headers, body_bytes)"""
    url = BASE + path
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def api_get(path):
    """带 apiToken 鉴权的 GET，返回解析后的 JSON"""
    status, _, body = http_req(path, headers={"apiToken": API_TOKEN})
    return status, json.loads(body.decode("utf-8"))


def seed_nodes(conn):
    """预建 nodes 表 + 种子数据（服务器 init 用 IF NOT EXISTS，不会冲突）"""
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS nodes ("
        "id TEXT PRIMARY KEY, name TEXT, channelCode TEXT, isOnline TEXT,"
        "cameraType TEXT, operation TEXT, customOperation TEXT,"
        "P1 TEXT, P3 TEXT, P4 TEXT)")
    rows = [
        # id,  name(含 CJK),  channelCode, online, cam, operation,  customOp, P1,     P3,  P4
        ("N1", "摄像头东侧",   "CH-001",    "1",    "1", "",          "",       "类型A", "x", "公司甲"),
        ("N2", "摄像头西侧",   "CH-002",    "1",    "2", "巡检",      "",       "类型A", "x", "公司甲"),
        ("N3", "球机北侧",     "CH-003",    "0",    "2", "维修",      "备注三", "类型B", "x", "公司乙"),
        ("N4", "枪机南侧",     "CH-004",    "0",    "3", None,        None,     "类型B", "x", "公司乙"),
        ("N5", "半球顶层",     "CH-005",    "1",    "3", None,        None,     "类型C", "x", "公司丙"),
        ("N6", "摄像头地下",   "CH-006",    "1",    "1", "巡检",      "",       "类型C", "x", "公司丙"),
    ]
    cur.executemany(
        "INSERT INTO nodes (id,name,channelCode,isOnline,cameraType,operation,"
        "customOperation,P1,P3,P4) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    conn.commit()
    cur.close()


def wait_ready(timeout=30):
    """轮询公开端点 /api/mode/get 直到服务器就绪"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _h, body = http_req("/api/mode/get", timeout=3)
            if status == 200:
                return json.loads(body.decode("utf-8"))
        except Exception:
            pass
        time.sleep(0.3)
    return None


def q(params):
    return "/api/nodes/get?" + urllib.parse.urlencode(params)


def exe_is_pg_build():
    """检查 vvvv.exe 是否为 PG 模式构建（PE/ELF 里是否含 libpq 导入名）。

    坑：SQLite 版/PG 版产物同名（都叫 vvvv.exe），构建模式切换靠 .mode_* 戳，
    若上次 `make` 是默认/SQLite 模式，直接跑测试会连到空的 device_dashboard.db
    且 mode/get 报 "SQLite"，断言全部误失败。
    """
    try:
        with open(EXE, "rb") as f:
            data = f.read().lower()
    except OSError:
        return False
    # MinGW PE 导入名存为大写 "LIBPQ.dll"，Linux DT_NEEDED 为 "libpq.so.5"
    return b"libpq.dll" in data or b"libpq.so" in data


def ensure_pg_build():
    """若当前二进制不是 PG 构建，自动执行 make PG_MODE=1 <prog> 重新编译"""
    if exe_is_pg_build():
        return True
    if os.name == "nt":
        candidates = [["mingw32-make", "PG_MODE=1", "vvvv.exe"],
                      ["make", "PG_MODE=1", "vvvv.exe"]]
    else:
        candidates = [["make", "PG_MODE=1", "./vvvv"]]
    print("== 当前 vvvv 不是 PG 构建，自动重新编译（make PG_MODE=1）==")
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=300)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and exe_is_pg_build():
            print("  PG 构建完成")
            return True
        print(r.stdout.decode("utf-8", errors="replace"))
    print("ERROR: 自动构建 PG 版失败，请手动执行：")
    print("  Windows: mingw32-make PG_MODE=1 vvvv.exe")
    print("  Linux:   make PG_MODE=1")
    return False


def main():
    if not os.path.exists(EXE):
        print("ERROR: 未找到 %s，请先运行 mingw32-make PG_MODE=1 vvvv.exe" % EXE)
        return 1
    if not ensure_pg_build():
        return 1

    tmpdir = tempfile.mkdtemp(prefix="vd_pg_smoke_")
    pgdata = os.path.join(tmpdir, "pgdata")
    proc = None
    conn = None
    try:
        # ---------- 1. 启动内嵌 PostgreSQL ----------
        print("== 启动内嵌 PostgreSQL ==")
        srv = pgserver.get_server(pgdata)
        uri = srv.get_uri()
        print(uri)
        print("  pg uri ready")

        # ---------- 2. 预建表 + 种子数据 ----------
        conn = psycopg2.connect(uri)
        seed_nodes(conn)
        print("  nodes 表已建 + 6 行种子数据")

        # ---------- 3. 写配置、启动服务器 ----------
        cfg = {
            "pgConnStr": uri,
            "apiToken": API_TOKEN,
            "httpPort": PORT,
            "httpsPort": PORT + 1,
            "users": [{"name": "tester", "pass": "pw12345"}],
            "defaultPageSize": 50,
            "maxPageSize": 200,
            "logLevel": "error",
            "logToFile": False,
            "logFile": "server.log",
            # nodes/get 的行字段由 fields 数组决定，缺省则输出空行
            "fields": [
                {"key": "id", "label": "通道ID"},
                {"key": "name", "label": "通道名称"},
                {"key": "channelCode", "label": "通道编号"},
                {"key": "isOnline", "label": "状态"},
                {"key": "cameraType", "label": "设备类型"},
                {"key": "P1", "label": "场站类型"},
                {"key": "P4", "label": "公司名称"},
                {"key": "operation", "label": "操作"},
                {"key": "customOperation", "label": "自定义"},
            ],
        }
        cfg_path = os.path.join(tmpdir, "data_config.json")
        # UTF-8 无 BOM（带 BOM 会被 mg_json 解析破坏）
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        env = dict(os.environ)
        # 双保险：exe 目录已有 libpq.dll（make 已拷），这里再把 PG bin 插到 PATH
        pg_bin = os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin")
        env["PATH"] = pg_bin + os.pathsep + env.get("PATH", "")

        out_log = open(os.path.join(tmpdir, "stdout.log"), "wb")
        err_log = open(os.path.join(tmpdir, "stderr.log"), "wb")
        proc = subprocess.Popen([EXE], cwd=tmpdir, stdout=out_log, stderr=err_log, env=env)

        mode_info = wait_ready()
        check("服务器就绪", mode_info is not None,
              "" if mode_info else "30s 内未就绪，stderr 见下")
        if mode_info is None:
            try:
                with open(os.path.join(tmpdir, "stderr.log"), "rb") as f:
                    print("---- vvvv.exe stderr ----")
                    print(f.read().decode("utf-8", errors="replace"))
            except Exception:
                pass
            return 1
        check("mode/get 报告 PostgreSQL 后端", mode_info.get("mode") == "PostgreSQL", str(mode_info))
        check("mode/get available=1", mode_info.get("available") == 1, str(mode_info))

        # ---------- 4. HTTP 断言 ----------
        print("== API 断言 ==")

        # 登录（config 明文 pass 遗留字段），期望 200 + Set-Cookie
        status, headers, body = http_req(
            "/api/login", method="POST",
            body=json.dumps({"user": "tester", "password": "pw12345"}),
            headers={"Content-Type": "application/json"})
        has_cookie = any(k.lower() == "set-cookie" for k in headers)
        check("登录成功（200 + Set-Cookie）", status == 200 and has_cookie,
              "status=%s cookie=%s" % (status, has_cookie))

        # 无过滤分页：total=6，且 operation=0 映射涵盖 '' 与 NULL
        status, r = api_get(q({"page": 1, "pageSize": 10}))
        check("无过滤 total==6", status == 200 and r["data"]["total"] == 6,
              "total=%s" % r.get("data", {}).get("total"))

        # isOnline 过滤
        _, r = api_get(q({"isOnline": "1", "page": 1, "pageSize": 10}))
        check("isOnline=1 -> 4 条", r["data"]["total"] == 4, "total=%s" % r["data"]["total"])
        _, r = api_get(q({"isOnline": "0", "page": 1, "pageSize": 10}))
        check("isOnline=0 -> 2 条", r["data"]["total"] == 2, "total=%s" % r["data"]["total"])

        # 多值 IN 过滤（N2/N3=cameraType 2，N4/N5=cameraType 3）
        _, r = api_get(q({"cameraType": "2,3", "page": 1, "pageSize": 10}))
        check("cameraType=2,3 (IN) -> 4 条", r["data"]["total"] == 4,
              "total=%s" % r["data"]["total"])

        # 组合过滤
        _, r = api_get(q({"isOnline": "1", "cameraType": "1", "page": 1, "pageSize": 10}))
        check("isOnline=1&cameraType=1 -> 2 条", r["data"]["total"] == 2,
              "total=%s" % r["data"]["total"])

        # operation=0 -> IS NULL OR ''（N1=''、N4/N5=NULL 共 3 条）
        _, r = api_get(q({"operation": "0", "page": 1, "pageSize": 10}))
        check("operation=0 -> 3 条（''+NULL 双匹配）", r["data"]["total"] == 3,
              "total=%s" % r["data"]["total"])

        # operation 单值（参数化）
        _, r = api_get(q({"operation": "巡检", "page": 1, "pageSize": 10}))
        check("operation=巡检 -> 2 条", r["data"]["total"] == 2,
              "total=%s" % r["data"]["total"])

        # CJK keyword LIKE（命中 name）
        _, r = api_get(q({"keyword": "东侧", "page": 1, "pageSize": 10}))
        check("keyword=东侧 -> 1 条", r["data"]["total"] == 1,
              "total=%s" % r["data"]["total"])
        # CJK keyword LIKE（命中 P4）
        _, r = api_get(q({"keyword": "公司乙", "page": 1, "pageSize": 10}))
        check("keyword=公司乙 -> 2 条", r["data"]["total"] == 2,
              "total=%s" % r["data"]["total"])

        # 空值参数 -> 1=0 -> 0 条
        _, r = api_get(q({"isOnline": "", "page": 1, "pageSize": 10}))
        check("isOnline= (空值) -> 0 条", r["data"]["total"] == 0,
              "total=%s" % r["data"]["total"])

        # 分页：ORDER BY id，第 2 页 pageSize=2 -> N3,N4
        _, r = api_get(q({"page": 2, "pageSize": 2}))
        ids = [n["id"] for n in r["data"]["nodes"]]
        check("分页 page=2/pageSize=2 -> [N3,N4]", ids == ["N3", "N4"], "ids=%s" % ids)

        # 未认证请求应被拒
        status, _h, _body = http_req(q({"page": 1, "pageSize": 1}))
        check("无凭证访问被拒 403", status == 403, "status=%s" % status)

        # ---------- 5. batchset 更新 + 持久化核对 ----------
        print("== batchset 更新 ==")
        upd = {"updates": [
            {"id": "N1", "operation": "已标记", "customOperation": "自定义甲"},
            {"id": "N2", "operation": "新巡检"},  # 只带 operation：customOperation 不得被覆盖
        ]}
        status, _, _ = http_req("/api/nodes/batchset", method="POST",
                                body=json.dumps(upd, ensure_ascii=False),
                                headers={"Content-Type": "application/json",
                                         "apiToken": API_TOKEN})
        check("batchset 返回 200", status == 200, "status=%s" % status)

        # psycopg2 直查库核对持久化（数据源是 PG 而非缓存）
        cur = conn.cursor()
        cur.execute("SELECT operation, customOperation FROM nodes WHERE id IN ('N1','N2')"
                    " ORDER BY id")
        rows = cur.fetchall()
        cur.close()
        check("直查 PG 确认 N1 两字段均更新、N2 仅 operation 更新",
              rows == [("已标记", "自定义甲"), ("新巡检", "")],
              "rows=%s" % (rows,))
        # API 侧核对（响应行含 operation/customOperation 字段）
        _, r = api_get(q({"keyword": "东侧", "page": 1, "pageSize": 10}))
        n1 = r["data"]["nodes"][0]
        check("API 确认 N1 operation/customOperation 已更新",
              n1["operation"] == "已标记" and n1["customOperation"] == "自定义甲",
              "op=%s cop=%s" % (n1["operation"], n1["customOperation"]))
        _, r = api_get(q({"keyword": "西侧", "page": 1, "pageSize": 10}))
        n2 = r["data"]["nodes"][0]
        check("API 确认 N2 仅 operation 更新",
              n2["operation"] == "新巡检" and n2["customOperation"] == "",
              "op=%s cop=%s" % (n2["operation"], n2["customOperation"]))

        # ---------- 汇总 ----------
        fails = [x for x in _results if not x[1]]
        print("\n===== %d/%d PASS =====" % (len(_results) - len(fails), len(_results)))
        return 0 if not fails else 1

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if conn is not None:
            conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
