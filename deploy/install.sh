#!/usr/bin/env bash
# 一键把 gddl_alarm C 服务装成 systemd 服务并拉起。
# 用法：
#   sudo ./deploy/install.sh                 装到 /opt/gddl_alarm（拷贝模式）
#   sudo ./deploy/install.sh --inplace       就地部署：直接用当前解压目录，不拷贝
#   sudo ./deploy/install.sh [安装目录] [源码目录]
# 说明：脚本幂等，可重复执行；已存在的 data_config.json 不会被覆盖。
set -euo pipefail

INPLACE=0
if [ "${1:-}" = "--inplace" ]; then
  INPLACE=1
  shift
fi
APP_DIR="${1:-}"
SERVICE="gddl_alarm"
SRC_DIR="${2:-}"

[ "$(id -u)" -eq 0 ] || { echo "请用 root 或 sudo 运行"; exit 1; }

# 0) 定位源码目录：脚本上一级 -> 当前目录
if [ -z "$SRC_DIR" ]; then
  for d in "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" "$PWD"; do
    if [ -f "$d/Makefile" ]; then SRC_DIR="$d"; break; fi
  done
fi
if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/Makefile" ]; then
  echo "未找到源码目录（需包含 Makefile）。"
  echo "用法：sudo $0 [安装目录] [源码目录]   例：sudo $0 /opt/gddl_alarm /root/gddl_alarm-src"
  exit 1
fi
echo "==> 源码目录：$SRC_DIR"

# 安装目录：--inplace 时直接就用解压目录，否则默认 /opt/gddl_alarm
if [ "$INPLACE" -eq 1 ]; then
  APP_DIR="$SRC_DIR"
elif [ -z "$APP_DIR" ]; then
  APP_DIR="/opt/gddl_alarm"
fi
echo "==> 运行目录：$APP_DIR"
if [ "$INPLACE" -eq 1 ]; then
  echo "==> 就地部署：不拷贝任何文件，直接使用解压目录"
fi

# 1) 取二进制：优先用已编译产物，否则现场编译（需 gcc + libpq-dev）
if [ ! -f "$SRC_DIR/gddl_alarm" ]; then
  command -v make >/dev/null || { echo "没有现成二进制，且缺少 make"; exit 1; }
  echo "==> 编译 $SRC_DIR（PG 模式）"
  # Linux 下 Makefile 的目标名是 ./gddl_alarm（PROG ?= ./gddl_alarm），不能直接 make gddl_alarm
  make -C "$SRC_DIR" ./gddl_alarm
  [ -f "$SRC_DIR/gddl_alarm" ] || { echo "编译失败：未生成 $SRC_DIR/gddl_alarm"; exit 1; }
fi
BIN="$SRC_DIR/gddl_alarm"

# 2) 部署文件：就地模式跳过拷贝（文件已经在运行目录里）
if [ "$APP_DIR" = "$SRC_DIR" ]; then
  echo "==> 就地模式：二进制/前端/配置均已在 $APP_DIR"
else
  install -d -m 0755 "$APP_DIR"
  install -m 0755 "$BIN" "$APP_DIR/gddl_alarm"
  install -d -m 0755 "$APP_DIR/web_root" "$APP_DIR/certs"
  cp -a "$SRC_DIR/web_root/." "$APP_DIR/web_root/"
  cp -a "$SRC_DIR/certs/." "$APP_DIR/certs/"
  # Python 常驻脚本（采集 / PV 采集 / 告警判定）
  for py_script in wind_fetch_and_upsert.py pv_fetch_and_upsert.py wind_alarm.py; do
    if [ -f "$SRC_DIR/$py_script" ]; then
      install -m 0644 "$SRC_DIR/$py_script" "$APP_DIR/$py_script"
    fi
  done
fi
if [ -f "$APP_DIR/data_config.json" ]; then
  echo "==> 保留已存在的 $APP_DIR/data_config.json"
elif [ "$APP_DIR" = "$SRC_DIR" ]; then
  echo "!! 就地模式缺少 $APP_DIR/data_config.json，请确认解压包内容完整"
else
  install -m 0644 "$SRC_DIR/data_config.json" "$APP_DIR/data_config.json"
fi

# 3) 动态库自检（libpq 缺失时给出明确提示，而不是等 systemd 里报 127）
if ldd "$APP_DIR/gddl_alarm" 2>/dev/null | grep -q "not found"; then
  echo "!! 缺少运行时动态库，请安装 libpq5（Debian/Ubuntu: apt install libpq5；RHEL: yum install libpq）"
  ldd "$APP_DIR/gddl_alarm" | grep "not found"
  exit 1
fi

# 4) 安装 unit（自定义安装目录时同步改写路径）
UNIT=/etc/systemd/system/$SERVICE.service
sed "s|/opt/gddl_alarm|$APP_DIR|g" "$SRC_DIR/deploy/gddl_alarm.service" > "$UNIT"
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

# 5) Python 常驻服务：脚本存在就安装对应 unit
#    格式 "服务名:脚本名"
PY_SERVICES="gddl_wind_alarm:wind_alarm.py
gddl_wind_fetch:wind_fetch_and_upsert.py
gddl_pv_fetch:pv_fetch_and_upsert.py"

PY_UNITS=""
# 解析 Python 解释器绝对路径：
#   - 先检查常见绝对路径（sudo 环境 PATH 可能不含 /usr/local/bin）
#   - 再用 type -P 在 PATH 中搜索（type -P 跳过 alias/function，避免返回 "alias python3='...'"）
PYBIN=""
for cand in \
  /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
  /usr/local/bin/python3 \
  /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3 \
  /bin/python3.13 /bin/python3.12 /bin/python3.11 /bin/python3 \
  python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if [ -x "$cand" ]; then
    PYBIN="$cand"; break
  fi
  if p="$(type -P "$cand" 2>/dev/null)" && [ -x "$p" ]; then
    PYBIN="$p"; break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "!! 找不到 python3，跳过全部 Python 服务"
else
  echo "==> Python 解释器：$PYBIN"
  "$PYBIN" -c 'import psycopg2, requests' 2>/dev/null || \
    echo "!! 缺少 Python 依赖，请先安装：$PYBIN -m pip install psycopg2-binary requests"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    py_unit="${line%%:*}"
    py_script="${line##*:}"
    if [ ! -f "$SRC_DIR/$py_script" ]; then
      continue
    fi
    # 替换安装目录，并把模板里硬编码的 /usr/bin/python3 替换为实际解释器路径
    sed -e "s|/opt/gddl_alarm|$APP_DIR|g" \
        -e "s|/usr/bin/python3|$PYBIN|g" \
        "$SRC_DIR/deploy/$py_unit.service" \
      > "/etc/systemd/system/$py_unit.service"
    chmod 0644 "/etc/systemd/system/$py_unit.service"
    echo "==> 已安装 $py_unit（$py_script，Python=$PYBIN）"
    PY_UNITS="$PY_UNITS $py_unit"
  done <<EOF
$PY_SERVICES
EOF
  systemctl daemon-reload
  for py_unit in $PY_UNITS; do
    systemctl enable "$py_unit" >/dev/null 2>&1 || true
    systemctl restart "$py_unit" || \
      echo "!! $py_unit 启动失败，查看：journalctl -u $py_unit -n 50"
  done
fi

sleep 1
systemctl --no-pager --full status "$SERVICE" || true
echo
echo "查看日志：journalctl -u $SERVICE -f"
for py_unit in $PY_UNITS; do
  echo "      $py_unit：journalctl -u $py_unit -f"
done
echo "端口：$(grep -o '"httpPort"[^,]*' "$APP_DIR/data_config.json" || true) / $(grep -o '"httpsPort"[^,]*' "$APP_DIR/data_config.json" || true)"
