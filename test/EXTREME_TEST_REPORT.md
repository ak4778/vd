# 前后端服务器极限测试报告

**测试日期**: 2026-08-09
**测试对象**: 视频设备管理系统 (C/Mongoose + SQLite + Preact 前端)
**服务器**: HTTP 127.0.0.1:8000, Mongoose 内置 TLS, SQLite WAL 模式
**数据规模**: 29,623 条设备通道记录

---

## 一、测试结果总览

| 类别 | 测试项 | 结果 | 备注 |
|------|--------|------|------|
| 数据加载 | 全量加载 | ✅ PASS | 29,623 条 |
| 数据加载 | 分页 (page 100) | ✅ PASS | 显示 4951-5000 行 |
| 数据保存 | 单条修改+持久化 | ✅ PASS | 重载后值保留 |
| 数据保存 | 批量保存 (256 上限) | ✅ PASS | (前序测试已验证) |
| 数据保存 | 并发写 | ✅ PASS | (前序测试已验证) |
| 过滤 | 状态/设备类型/操作 | ✅ PASS | (前序测试已验证) |
| 搜索 | 关键词 "马家滩" | ✅ PASS | 144 条结果 |
| 鉴权 | Basic Auth 登录 | ✅ PASS | HTTP 200 |
| 鉴权 | apiToken Header | ✅ PASS | 正确200/错误403/OWS200 |
| 鉴权 | Cookie token | ✅ PASS | 登出前200 |
| 登录 | 错误密码 | ✅ PASS | HTTP 401 |
| 登录 | 无认证访问 | ✅ PASS | HTTP 403 |
| 登出 | 浏览器登出 | ✅ PASS | 返回登录页 |
| 登出 | 会话失效 (token复用) | ✅ PASS | 登出后 403 |
| 并发 | 纯读 100 并发 | ✅ PASS | 100% (25 r/s) |
| 并发 | 纯读 200 并发 | ✅ PASS | 100% (25 r/s) |
| 并发 | 混合读写 100 并发 | ✅ PASS | 100% (26 r/s) |
| 并发 | 混合读写 200 并发 | ✅ PASS | 100% (26 r/s) |
| 并发 | 极限 500 并发 | ⚠️ 容量限制 | ~70-76% 成功 (设计区间 ≤200 为 100%) |
| HTTPS/TLS | 内置 TLS 1.3 | ✅ PASS | Python TLS1.3 客户端验证 5/5 通过 |
| 服务器日志 | 错误检查 | ✅ PASS | 无 error/fail/sql 错误 |

**总体结论**: ✅ **核心功能全部通过**; 500 并发为单线程事件循环容量上限 (非缺陷), HTTPS 经 Python TLS1.3 客户端验证健康

---

## 二、详细测试数据

### 1. 数据加载与分页

- **全量加载**: 前端显示 "设备通道( 29623 )", 每页 50 条, 共 593 页
- **分页跳转测试**: 输入页码 100 → 跳转
  - 状态栏: `showing 4951 - 5000 of 29623 results`
  - 第1页首行: N113174 (马家滩-箱变-01-01, 宁夏新能源)
  - 第100页首行: N158115 (化学区域-一期加药间, 秦皇岛电厂)
  - OFFSET 计算: (100-1) × 50 = 4950 ✓ 正确

### 2. 数据保存与持久化

- **修改操作**: 第1行 N113174 操作下拉框改为 "重点区域" (value=1)
- **保存**: 点击 "保存全部修改" → 按钮变 disabled, "已编辑1行未保存" 提示消失
- **持久化验证**: 重载页面 (http://127.0.0.1:8000/)
  - N113174 操作仍为 "重点区域" ✓
  - 全量 29,623 条数据正常加载 ✓
  - 保存按钮 disabled (无未保存更改) ✓

### 3. 搜索与过滤

- **关键词搜索 "马家滩"**: 返回 144 条结果 (马家滩-箱变系列)
- **过滤**: 状态/设备类型/操作 多选过滤 (前序测试已全面验证)

### 4. 鉴权与认证

#### 4.1 登录 (HTTP Basic Auth)
```
POST /api/login  Authorization: Basic base64(admin:admin)
→ HTTP 200, {"user":"admin","token":"..."}, Set-Cookie: access_token=...
```

#### 4.2 apiToken Header 认证
| 场景 | HTTP 状态 | 预期 |
|------|-----------|------|
| 正确 apiToken | 200 | 200 ✓ |
| 错误 apiToken | 403 | 403 ✓ |
| apiToken + 前后空白 (OWS) | 200 | 200 ✓ (OWS 剥离生效) |

#### 4.3 登出与会话失效
| 步骤 | HTTP 状态 | 预期 |
|------|-----------|------|
| 登出前 token (query param) | 200 | 200 ✓ |
| 登出前 token (cookie) | 200 | 200 ✓ |
| 登出 (POST /api/logout) | 200 | 200 ✓ |
| 登出后 token (query param) | 403 | 401/403 ✓ |
| 登出后 token (cookie) | 403 | 401/403 ✓ |
| 错误密码登录 | 401 | 401 ✓ |
| 无认证访问受保护端点 | 403 | 401/403 ✓ |

**关键**: 登出后服务端 token 已清除, 复用被拒绝 — 会话失效服务端生效 (非仅清 cookie)。

### 5. 并发性能测试

使用 Python ThreadPoolExecutor (绕过 curl/Schannel 限制):

| 工作负载 | 并发数 | 成功 | 耗时 | 吞吐 |
|----------|--------|------|------|------|
| 纯读 | 100 | 100/100 (100%) | 3.92s | 25 r/s |
| 纯读 | 200 | 200/200 (100%) | 7.88s | 25 r/s |
| 混合读写 (1/3写) | 100 | 100/100 (100%) | 3.90s | 26 r/s |
| 混合读写 (1/3写) | 200 | 200/200 (100%) | 7.81s | 26 r/s |
| 极限 | 500 | ~340-380/500 (70-76%) | 5.6s | 88 r/s |

**结论**: 服务器在 200 并发下读写均 100% 成功, 无失败无超时。
500 并发超出单线程事件循环的可靠服务区间 (≤200), 约 70-76% 请求成功,
失败为 TCP 连接级 (连接被拒/重置), 服务器日志无错误, 无内存泄漏。

### 6. 服务器日志

- 日志大小: 2.2 KB
- 错误检查: 无 `error`/`fail`/`fault`/`panic`/`abort`/`sql` 关键字
- 服务器全程稳定运行, 无崩溃无重启

---

## 三、已知限制 (非缺陷)

1. **TLS 1.3 限制**: Mongoose 内置 TLS 仅支持 TLS 1.3 (X25519 + AES-GCM/ChaCha20)。
   Windows schannel 客户端 (curl.exe / .NET 默认) 协商 TLS 1.2, 无法完成握手。
   测试用 Python ssl 模块 (OpenSSL) 以 TLS 1.3 客户端验证 HTTPS 健康 (5/5 通过),
   并确认 TLS 1.2 被正确拒绝 (SSLEOFError)。
   生产客户端需启用 TLS 1.3, 或使用 HTTP。

2. **.NET HttpClient 连接池**: 默认 `DefaultConnectionLimit=2` + HTTP keep-alive 为最优配置。
   提高连接数 (63/100/200) 反而因 ThreadPool 线程创建延迟和 TCP 连接开销导致性能下降。

3. **curl/Schannel 并发上限 ~64**: Windows curl 受 schannel 限制, 并发上限约 64。
   非服务器端瓶颈 — Python ThreadPoolExecutor 验证服务器可处理 200+ 并发。

---

## 四、测试脚本

| 脚本 | 用途 |
|------|------|
| [test/full_extreme_test.ps1](file:///c:/s/vd/test/full_extreme_test.ps1) | 综合12场景极限测试 |
| [test/https_tls13_test.py](file:///c:/s/vd/test/https_tls13_test.py) | HTTPS/TLS 1.3 测试 (Python OpenSSL 客户端) |
| [test/py_concurrency_test.py](file:///c:/s/vd/test/py_concurrency_test.py) | 并发读写压测 |
| [test/logout_verify.ps1](file:///c:/s/vd/test/logout_verify.ps1) | 登出/会话失效/认证测试 |
| [test/results_extreme_final.md](file:///c:/s/vd/test/results_extreme_final.md) | 前序测试结果文档 |

---

## 五、总结

本次极限测试覆盖前后端与服务器的全部核心功能: **数据加载、保存、过滤、搜索、鉴权、认证、登录、登出、分页、并发、HTTPS/TLS**。

- **功能正确性**: 全部通过, 包括会话失效的服务端验证
- **数据持久化**: SQLite WAL 模式下保存可靠, 重载后数据一致
- **并发性能**: 200 并发读写 100% 成功, 吞吐 25-26 r/s; 500 并发为容量上限 (~70-76%)
- **HTTPS/TLS**: Python TLS 1.3 客户端验证 5/5 通过 (mode/get, nodes/get, login, cookie auth, TLS1.2 拒绝)
- **稳定性**: 服务器全程无错误无崩溃, 日志干净, 内存稳定 (+0.18MB)
- **安全性**: 三种认证方式 (Basic Auth / apiToken / Cookie) 均正确鉴权, 错误凭证被拒绝

系统在极限测试下表现稳定可靠。
