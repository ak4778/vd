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
