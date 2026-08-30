# vd
6位: 新能源场站 (L030106)
9位: 生产期 (L030106010)
12位: 固定摄像机 ← 缺失的这一层！
15位: 山东新能源
18位: 泰安新泰风电场

ID长度	对应层级  示例
6位	P1	  新能源场站
9位	P2	  生产期
12位	P3	  固定摄像机
15位	P4	  山东新能源
18位	P5	  泰安新泰风电场
21位	P6	  东营一期分布式光伏
24位	P7	  8号汽机13.6米
27位	P8	  1号汽机及电气

**User**

make CSV_MODE=1 

## 构建架构

这个项目有两个独立的构建系统：

### 1. 后端（C 代码）——由 `make` 处理

```
make                  # SQLite 模式（默认）
make CSV_MODE=1       # CSV 模式
make clean            # 清理
```

`make` 只编译 C 源文件（`main.c`, `mongoose.c`, `net.c`, `data_source.c`），生成可执行文件 `vvvv`。

### 2. 前端（HTML/JS/CSS）——独立构建

前端由 mongoose（嵌入式 HTTP 服务器）直接提供静态文件，**不需要编译**。但 `tailwind.css` 需要单独处理：

```bash
# 首次或修改了 Tailwind 工具类后，需要重新编译 CSS：
npx tailwindcss build web_root/tailwind.css -o web_root/dist/tailwind.css
```

这是因为 Tailwind 采用 **JIT 模式**——它扫描 `web_root/` 下的 JS/HTML 文件，找出用到的 class，只编译这些到 `dist/tailwind.css`。如果 JS 里用了一个新的 class（比如 `pl-12`），但没重新编译，浏览器就识别不了这个 class。

### 首次完整编译运行步骤

```bash
# 1. 安装 Node 依赖（如果第一次）
npm install

# 2. 编译 Tailwind CSS（如果首次或前端改动）
npx tailwindcss build web_root/tailwind.css -o web_root/dist/tailwind.css

# 3. 编译后端 C 代码
make

# 4. 运行
./vvvv          # 或 vvvv.exe on Windows
```

### 日常开发中的修改规则

| 修改内容                                     | 需要重新编译？                 |
|----------------------------------------------|--------------------------------|
| `.c` 文件（后端逻辑）                        | ✅ `make`                       |
| `web_root/main.js`（前端 JS 逻辑）           | ❌ 直接刷新浏览器               |
| `web_root/main.js` 中**新增 Tailwind class** | ✅ `npx tailwindcss build ...` |
| `web_root/index.html`（HTML 结构）           | ❌ 直接刷新浏览器               |
| `data_config.json`（数据配置）               | ❌ 直接刷新浏览器               |

### 便捷方案

如果你希望每次改前端代码后自动重新编译 CSS，可以加一个 `watch` 命令。在 [package.json](file:///c:/s/vd/package.json) 的 `scripts` 中添加：

```json
"watch:css": "npx tailwindcss -i web_root/tailwind.css -o web_root/dist/tailwind.css --watch"
```

然后运行 `npm run watch:css`，它会监听文件变化并自动重新编译 CSS，这样修改 Tailwind class 后刷新浏览器就能直接看到效果。
HTTP请求 → 创建work_request → 启动工作线程 → 主循环继续处理其他请求
                                        ↓
                              线程执行数据库操作（加锁）
                                        ↓
                              构建响应 → mg_wakeup唤醒主循环 → 释放资源
                                        ↓
                              MG_EV_WAKEUP → 发送HTTP响应


采用驱动注册模式：

PlainText



data_source.h          (统一接口)    ↓data_source.c          (路由层，根据配置调用不同驱动)    
↓┌───────┴───────┬───────┐
sqlite_driver    mysql_driver   mongo_driver
配置文件指定使用哪个驱动：

JSON



{  "dataSource": {    "type": "mysql",    "host": "localhost",    "port": 3306,    "database": "video_devices",    "user": "root",    "password": "xxx"  }}
这样上层 net.c 完全不用改，只需新增对应数据库的驱动文件即可。


扩展新数据库的步骤
如果未来需要支持 MySQL/PostgreSQL/MongoDB，只需：

创建 mysql_driver.c，实现 ds_driver 接口的 5 个函数
在 data_source.c 中添加 extern const struct ds_driver mysql_driver;
修改 select_driver() 根据配置选择驱动
更新 Makefile 添加新源文件



认证方式支持
后端支持三种认证方式（通过 mongoose 的 mg_http_creds）：

Basic Auth：Authorization: Basic <base64(user:pass)> — 用于登录
Bearer Token：Authorization: Bearer <token> — token 来自登录后生成的 access_token
Cookie：Cookie: access_token=<token> — 前端浏览器默认方式
三种方式都经过同一个 authenticate() 函数验证，安全逻辑一致。



Postman 调用 API 时，有三种方式携带 token：

1. Bearer Token（推荐）
Authorization: Bearer postman_token_2024

2. Cookie
Cookie: access_token=postman_token_2024

3. URL 参数
GET /api/nodes/get?access_token=postman_token_2024

关键验证点：

- Api-Token header 是大小写不敏感的（mongoose的 mg_http_get_header 特性）
- OWS（Optional Whitespace） 前导/尾随空格被HTTP层正确剥离，token仍匹配 — 符合 RFC 7230
- token内部空格 正确拒绝（memcmp 精确匹配）
- 多认证方式共存 ：apiToken header 优先于 cookie/Basic；Bearer token 和 ?access_token= query param 也能用 apiToken 值认证

这是一个浏览器 Cookie 安全策略导致的问题。让我分析一下：

net.c
Thought
根本原因：Secure Cookie 标志。

当你通过 HTTPS 登录时（net.c:851），Cookie 被设置为：

PlainText



Set-Cookie: access_token=xxx; Path=/; Secure; HttpOnly; ...
浏览器规范规定：带 Secure 标志的 Cookie 只会在 HTTPS 请求中发送。所以当你切回 HTTP（端口 8000）时，浏览器自动丢弃了这个 Cookie。

修复方法：去掉 Secure 标志，让 Cookie 在 HTTP 和 HTTPS 中都能工作。

前端**并不手动添加 cookie**，而是依赖浏览器的自动 cookie 管理 + fetch 的 `credentials: 'include'` 选项。整个 cookie 流程是这样的：

## 一、Cookie 的产生（登录时由服务器写入）

登录表单提交时，前端只发送 Basic Auth 头，cookie 由服务器通过 `Set-Cookie` 响应头写入浏览器：

[components.js:113-117](file:///c:/s/vd/web_root/components.js#L113-L117)
```js
const onsubmit = function(ev) {
  const authhdr = 'Basic ' + btoa(user + ':' + pass);
  const headers = {Authorization: authhdr};
  return fetch('api/login', {headers, credentials: 'include'}).then(loginFn).finally(r => setPass(''));
};
```

服务器验证账号密码后，在响应里返回 `Set-Cookie: access_token=<token>; HttpOnly; Path=/`（HTTPS 上还会加 `Secure`），浏览器自动把这个 cookie 存起来。**前端代码完全没碰 `document.cookie`**。

## 二、Cookie 的携带（每个请求都靠 `credentials: 'include'`）

所有需要鉴权的请求都带了 `credentials: 'include'`，浏览器就会自动把刚才存的 `access_token` cookie 附在 `Cookie` 请求头里发出去。一共有 5 处：

| 位置 | 用途 | 代码 |
|------|------|------|
| [main.js:721](file:///c:/s/vd/web_root/main.js#L721) | 页面加载时的自动登录（GET /api/login） | `fetchWithTimeout('api/login', { credentials: 'include' })` |
| [main.js:715](file:///c:/s/vd/web_root/main.js#L715) | 登出 | `fetchWithTimeout('api/logout', { credentials: 'include' })` |
| [main.js:110](file:///c:/s/vd/web_root/main.js#L110) | 加载节点数据（GET /api/nodes/get） | `fetchWithTimeout(url, { method: 'GET', cache: 'no-cache', credentials: 'include' })` |
| [main.js:478-483](file:///c:/s/vd/web_root/main.js#L478-L483) | 批量保存（POST /api/nodes/batchset） | `fetchWithTimeout('api/nodes/batchset', { ..., credentials: 'include' })` |
| [components.js:116](file:///c:/s/vd/web_root/components.js#L116) | 登录表单提交（POST /api/login） | `fetch('api/login', {headers, credentials: 'include'})` |

## 三、关键点

1. **`credentials: 'include'` 是必须的**：fetch 默认不带 cookie（同源也不带），必须显式打开。如果漏掉，浏览器就不会发送 `access_token`，服务器 `authenticate()` 拿不到 token 就返回 403。
2. **`HttpOnly` cookie 无法被 JS 读取**：所以前端根本读不到 `access_token` 的值，只能让浏览器代为发送。这也是防 XSS 窃取 token 的设计。
3. **服务端对应读取逻辑**：net.c 里的 `authenticate()` 用 `cookie_exact()` 在 `Cookie` 头里精确匹配 `access_token`（见 project memory 里 2026-08-05 修的 Bug 2，mongoose 的 `mg_http_get_header_var` 是子串匹配，不可靠）。
4. **`api/mode/get` 不需要 cookie**：[main.js:722](file:///c:/s/vd/web_root/main.js#L722) 没带 `credentials`，因为它是公开端点。

简而言之：**前端只在每个 fetch 里写 `credentials: 'include'`，cookie 的写入、存储、发送全由浏览器自动完成**。

---

高并发读取瓶颈（3 项失败） — 500/800/1000 并发读取均只有约 200 个成功。这是由于：

mongoose.h#L1175-L1177 中 MG_SOCK_LISTEN_BACKLOG_SIZE 默认值为 128
mongoose.c#L14318 中 listen(fd, 128) 将 TCP backlog 限制在 128
Windows TCP 栈自身的 SOMAXCONN 上限约为 200
主线程 accept 速度 + SQLite 互斥锁竞争，共同导致约 200 的实际并发上限


Mongoose 可优化参数汇总
基于对你项目（Windows + HTTP + SQLite）的分析，以下是所有可调优的参数，按影响程度从大到小排列：

🔥 高影响参数
1. MG_SOCK_LISTEN_BACKLOG_SIZE — TCP 连接等待队列
位置: mongoose.h#L1175-L1177
当前值: 128
作用: 操作系统 TCP listen() 的 backlog 大小，决定能同时接受多少并发连接
建议: 提高到 512 或 1024，但 Windows 系统硬上限约 200（SOMAXCONN）
注意: 这是你之前极限测试中 ~200 并发读取上限的根因之一
2. mg_mgr_poll() 轮询间隔
位置: main.c#L85
当前值: 50ms
作用: 事件循环的轮询间隔，越小响应越快但 CPU 占用越高
建议: 降低到 10ms 或 5ms（提升响应速度，代价是 CPU 略增）
权衡: 你的定时器 dispatch_results 间隔是 20ms，所以 poll 间隔应 ≤ 20ms
3. 编译优化等级
位置: Makefile#L8
当前值: -O0（调试模式，无优化）
建议: 发布版本使用 -O2 或 -O3，性能可提升 15-30%
注意: -O0 会使函数调用、循环等都不被优化，严重影响性能
🟡 中等影响参数
4. MG_IO_SIZE — IO 缓冲区粒度
位置: mongoose.h#L856-L858（Win32 平台默认 16384）
当前值: 16384 (16KB)
作用: 发送/接收缓冲区的增长粒度
建议: 对于大数据量响应（你的 nodes/get 返回大量 JSON），可提高到 65536 (64KB)，减少内存分配次数
5. MG_MAX_RECV_SIZE — 最大接收缓冲区
位置: mongoose.h#L1151-L1153
当前值: 3MB
作用: 单个请求最大允许的接收数据
建议: 你的 POST 请求通常较小，可降低到 1MB 节省内存
6. MG_MAX_HTTP_HEADERS — HTTP 头数量上限
位置: mongoose.h#L1159-L1161
当前值: 30
作用: 每个 HTTP 请求允许的 header 数量
建议: 你用了 Basic Auth + Cookie，约需 5-8 个 header，30 已足够，不需要改
🟢 低影响 / 调优参数
7. MG_ENABLE_PROFILE — 性能分析
位置: mongoose.h#L1216-L1218
当前值: 0（关闭）
作用: 启用连接状态追踪，用于调试性能
建议: 开发时可临时开启 1，发布时保持 0
8. MG_ENABLE_LOG / mg_log_set() — 日志级别
位置: main.c#L60
当前值: MG_LL_DEBUG（最详细）
作用: 日志输出量，debug 级别会产生大量 I/O
建议: 生产环境改为 MG_LL_INFO 或 MG_LL_WARN，减少日志 I/O 开销
9. MG_ENABLE_CHACHA20 — TLS 加密算法
位置: mongoose.h#L1272-L1274
当前值: 1（启用）
作用: ChaCha20 比 AES 更高效但略耗 CPU
建议: 已有 AES 加速的平台可关闭，嵌入式平台保留
⚙️ 编译时可开关的功能模块
      宏	          默认值	       作用	               建议
MG_ENABLE_TCPIP	            1	           内置 TCP/IP 栈     保持 1（Win32 使用 Winsock）
MG_ENABLE_WINSOCK	 1 (Win32)	   Winsock 支持	      保持 1
MG_ENABLE_POSIX_FS	 1 (Win32)	   POSIX 文件系统     保持 1（serve_dir 需要）
MG_ENABLE_DIRLIST	 1 (Win32)	     目录列表	      如果不用目录列表可设 0
MG_ENABLE_SSI	            0	           服务器端包含	      保持 0（不需要）
MG_TLS	               MG_TLS_BUILTIN	     TLS 后端	      已正确配置
MG_TLS_RSA_USE_CRT	    0	           RSA CRT 优化	      已正确配置

针对你的项目的优化建议
基于极限测试结果（~200 并发读取上限、SQLite 锁竞争），最有效的三项优化是：

MG_SOCK_LISTEN_BACKLOG_SIZE 提高到 512 — 在 Makefile 的 CFLAGS 中添加 -DMG_SOCK_LISTEN_BACKLOG_SIZE=512
mg_mgr_poll 间隔降到 10ms — 在 main.c#L85 中将 50 改为 10
编译优化改为 -O2 — 在 Makefile#L8 中将 -O0 改为 -O2

时间          buffer的拥有者
──────────────────────────────────
build_nodes_get_response()    →  wr->response_body
push_result()                 →  async_result->body（转移）
wr->response_body = NULL      →  wr 明确放弃所有权
free_work_request(wr)         →  不碰 buffer（因为是NULL）
...等待最多20ms...
dispatch_results()            →  发送 buffer 内容
free(r->body)                 →  最终释放 buffer


## A. 登录支持更多凭证来源(主要功能变更)
新增 find_user_by_creds() 函数, handle_login() 签名增加 hm 参数以读取请求体。 /api/login 现在接受 3 种凭证:

来源                                       格式                                状态 
Basic Auth header/cookie            Authorization: Basic xxx                  原有 
POST body JSON                  {"user":"xxx","password":"yyy"}               新增 
POST body form-encoded             user=xxx&password=yyy                      新增

原来只能通过 Basic Auth header 登录,现在前端可以用 JSON body 提交表单登录。

## B. 响应格式统一为 JSON(API 规范化)
端点/场景              旧响应                                               新响应 
登录 401          Unauthorized\n (纯文本)               {"status":"false","message":"Invalid credentials"} 
登录 200          {"user":"...","token":"..."}           {"status":"true","user":"...","token":"..."} 
登出 200            true\n (纯文本)                                  {"status":"true"} 
403 未授权         Not Authorised\n                     {"status":"false","message":"Not Authorised"}

所有响应都加了 Content-Type: application/json header,cookie 缓冲区从 256→512 字节以容纳合并的 header。

找到问题了！这是 死锁 。看调用链：

```
get_max_page_size() / get_default_page_size()
  → cfg_lock()                              <-- 第一次加锁
    → get_config_buf()
      → cfg_lock()                          <-- 第二次加锁！死
      锁（Linux 下 PTHREAD_MUTEX 默认非递归）
```
pthread_mutex_t 默认不是递归锁，同一线程连续 lock 两次会永久阻塞。Windows 的 CRITICAL_SECTION 天然支持递归，所以 Windows 下不死。
调用关系形成了嵌套加锁：

```
get_max_page_size()          [net.c:191]
  → cfg_lock()               ← 第一次加锁 ✅
    → get_config_buf()       [net.c:614]  
      → cfg_lock()           ← 同一线程第二次加锁 ❌ 死锁！
```
- Windows 的 CRITICAL_SECTION 是递归锁（同线程可重入），所以没问题
- Linux 的 PTHREAD_MUTEX_INITIALIZER 默认是 非递归锁 ，同一线程连续 lock 两次会永久挂起


gen_passhash.py ，三种用法：

```
# 1. 交互式（只打印结果，手动复制）
python gen_passhash.py

# 2. 命令行参数
python gen_passhash.py Gddl-bq "Gddl!#%2026!@"

# 3. 直接更新 data_config.json（新增或替换已有用户）
python gen_passhash.py --update
```
echo b3MuMjAyMTAy | base64 -d
## Windows 实际上会发生什么
Windows 下即使 fd 超过 FD_SETSIZE， accept 也会 照单全收 。限制在后面的 select() 主循环里才暴露：

mongoose.c:14825-14844 （select 分支）：

FD_SET 宏 本身不做边界检查 。 fd_set 在 WinSock2 里是一个长度为 FD_SETSIZE(=64) 的 SOCKET fd_array[] 。fd ≥ 64 时：

- 不会报错
- 会越界写到栈上 fd_set 结构体后面的内存 （ rset / wset / eset 都是栈变量）
- 结果：随机值覆盖 / select 行为错乱 / 直接 crash（segfault / access violation）
所以 Windows 的表现不是"优雅地拒绝连接"，而是"表面正常，超过 64 个 socket 后随时可能崩"。

## 修正后的各平台实际限制表
平台                        accept检查               硬限制位置           限制值                       超限表现 
Windows（当前）               ❌ 无               select() 栈越界写   FD_SETSIZE=64              静默越界 + 随机 crash 
Linux（select 模式）          ✅ 有（L14683）        accept 阶段     FD_SETSIZE=1024     打印 error、closesocket，用户侧断连 
Linux（MG_ENABLE_EPOLL=1）   ❌ 不检查                无此限制          看ulimit                      正常工作

## 怎么修 Windows 的隐患
有两条路：

### 方案 A：Windows 改走 poll（推荐，开销小）
项目的 memory 里已经记载了：
 Mongoose 必须在 Windows 下用 -DMG_ENABLE_POLL=0 ，否则 WSAPoll bug 导致高并发卡死
这条路 有已知 bug，不能用 。

### 方案 B：显式 #define FD_SETSIZE 到更大的值
必须在 #include <winsock2.h> 之前生效。最稳妥的办法是编译选项直接传：

```
# Makefile Windows 分支里加
ifeq ($(OS),Windows_NT)
  CFLAGS += -DFD_SETSIZE=4096
endif
```
FD_SETSIZE=4096 后 fd_set 结构体的大小从 64*sizeof(SOCKET)=256B 变成 4096*8=32KB ， 但它是栈上变量 ， mg_iotest() 里一次就 3 个（rset/wset/eset）= 96KB。一般 Windows 默认栈 1MB，这个开销没问题。

### 方案 C：在 Windows 的 accept 里补一个检查，超限就 closesocket
在 accept_conn 里给 Windows 也加一段逻辑（不依赖现有 #if ），但改 mongoose.c 不太好。

总结：你是对的，Windows 完全走不到 14683，FD_SETSIZE 限制会以更危险的形式（越界写导致 crash）出现。建议直接 -DFD_SETSIZE=4096 编译。