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

- apiToken header 是大小写不敏感的（mongoose的 mg_http_get_header 特性）
- OWS（Optional Whitespace） 前导/尾随空格被HTTP层正确剥离，token仍匹配 — 符合 RFC 7230
- token内部空格 正确拒绝（memcmp 精确匹配）
- 多认证方式共存 ：apiToken header 优先于 cookie/Basic；Bearer token 和 ?access_token= query param 也能用 apiToken 值认证