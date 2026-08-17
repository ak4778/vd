# Changelog

本项目所有重要改动均记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### Security
- **net.c `authenticate()` — 常量时间 token 比较**
  - 全局 API token (`s_global_api_token`) 比较由 `strcmp` 改为 `ct_memcmp`，先 `strlen` 校验长度，再做常量时间比较。
  - 用户会话 token (`u->access_token`) 比较同样改用 `ct_memcmp`，循环内对每个用户的 token 先校验长度再常量时间比较。
  - 消除针对 64 字符长生命周期凭据的时序侧信道风险。
  - 位置: `net.c:1000-1019`
- **net.c `authenticate()` — 栈帧凭据清零**
  - 函数所有返回路径（含 `global_token_user` 早返回与默认末尾返回）前均加入 `memset(user, 0, sizeof(user))` 和 `memset(pass, 0, sizeof(pass))`。
  - 防止明文密码与 token 残留在栈帧上被后续调用覆盖前读取。
  - 位置: `net.c:1009-1010, 1020-1022`
- **net.c `handle_login()` — 登录凭据清零**
  - 局部变量 `nm[64]` 与 `pw[128]` 从 `if` 块内提升到函数作用域，使所有返回路径都能访问并清零。
  - 401 拒绝路径与 200 成功响应后均执行 `memset(nm/pw, 0, sizeof(...))`。
  - 位置: `net.c:1033-1077`

### Verification — 全量测试通过
本次改动后重新编译前后端并执行完整测试套件，结果如下：

| 测试套件 | 结果 | 耗时 |
|----------|------|------|
| 后端编译 `mingw32-make` | 通过（仅 sqlite3.c / mongoose.c 既有第三方警告，无 net.c 警告） | ~3s |
| 前端编译 `npm run build:css` | 通过 | 417ms |
| 单元测试 `test_verify_password.exe` | **49/49 通过** | <1s |
| 功能测试 `functional_test.ps1` | **40/40 通过** | 5.49s |
| 安全测试 `security_test.ps1` | **59/59 通过**（1 项为已知设计选择） | 20s |
| 边界测试 `edge_case_test.ps1` | **83/83 通过** | 89.8s |
| HTTPS TLS1.3 测试 `https_tls13_test.py` | **5/5 通过** | ~5s |
| 极限压力测试 `_extreme_stress.py` | **22/22 级别完成** | 211.2s |

#### 极限压力测试明细（共 11679 请求 / 22 级别，服务器全程稳定无崩溃）

| 类别 | 级别 | 结果 | 峰值吞吐 |
|------|------|------|----------|
| 数据加载 | `pageSize=50` 100/500/1000 并发 | 全部 100% | 469 r/s |
| 数据加载 | `pageSize=500` 300/1000 并发 | 全部 100% | 446 r/s |
| 数据保存 | batch ×50/200/500 并发 | 全部 100% | 541 r/s |
| 查询 (id-keyword) | 200/800 并发 | 200: 100% / 800: 44%（客户端超时） | 22-50 r/s |
| 过滤 (combo) | 200/1000 并发 | 200: 100% / 1000: 77% | 108-141 r/s |
| 搜索 (CJK) | 200/1000 并发 | 200: 100% / 1000: 54% | 46-90 r/s |
| 翻页 (full-range) | 593 / 1186 并发 | 全部 100% | 468 r/s |
| 前端静态资源 | 100/500/1500 并发 | 100: 100% / 500: 100% / 1500: 85% | 260-314 r/s |
| HTTPS TLS1.3 | 50/200/500 并发 | 服务器 100% 稳定 | 34-51 r/s |

#### 回归验证要点
- `verify_password` 调用参数顺序未变，49 个单元测试全通过。
- `apiToken` 头部认证（含 OWS 空白剥离）行为不变。
- 登录/登出、cookie/query token 三种凭据来源行为不变。
- 11679 请求后服务器仍正常响应 200，内存稳定 ~11MB，日志无 crash/assert/FATAL。
