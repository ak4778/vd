#!/usr/bin/env bash
# 一键把 gddl_alarm 组件装成 systemd 服务并拉起。
# 用法：
#   sudo ./deploy/install.sh                                 就地部署：编译 + 装全部 4 个服务
#   sudo ./deploy/install.sh alarm_service                   就地部署 + 只装 C 告警主服务
#   sudo ./deploy/install.sh wind_alarm wind_fetch pv_fetch  就地部署 + 装指定 Python 服务（可多选）
#   sudo ./deploy/install.sh /opt/gddl_alarm wind_alarm     指定安装目录（拷贝模式）+ 目标选择
#
# 可选目标（不区分顺序，可混合）：
#   alarm_service  - C 告警主服务 gddl_alarm（编译 gddl_alarm 二进制 + web_root + certs）
#   wind_alarm     - Python 风电场温度告警判定 (wind_alarm.py)
#   wind_fetch     - Python 风电场秒级采集 (wind_fetch_and_upsert.py)
#   pv_fetch       - Python 光伏支路秒级采集 (pv_fetch_and_upsert.py)
#
# 说明：脚本幂等，可重复执行；已存在的 data_config.json 不会被覆盖。
set -euo pipefail

# ======================= 参数解析 =======================
APP_DIR=""
SRC_DIR=""
TARGETS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      sed -n '2,16p' "$0"
      exit 0 ;;
    *)
      if [ -z "$APP_DIR" ] && [ -d "$1" ] && [ ! -f "$1/Makefile" ]; then
        # 显式指定了一个存在但不含 Makefile 的目录 -> 拷贝模式安装目录
        APP_DIR="$1"
      elif [ -z "$SRC_DIR" ] && [ -f "$1/Makefile" ]; then
        SRC_DIR="$1"
      else
        TARGETS+=("$1")
      fi
      shift ;;
  esac
done

# 默认装全部（向后兼容）
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(alarm_service wind_alarm wind_fetch pv_fetch)
fi

# ======================= 目标映射表 =======================
# 每个 target 对应一个或多个 (unit, 源文件, 类型)
# 类型：c / python
declare -A TARGET_MAP
TARGET_MAP[alarm_service]="c:gddl_alarm:gddl_alarm"
TARGET_MAP[wind_alarm]="py:gddl_wind_alarm:wind_alarm.py"
TARGET_MAP[wind_fetch]="py:gddl_wind_fetch:wind_fetch_and_upsert.py"
TARGET_MAP[pv_fetch]="py:gddl_pv_fetch:pv_fetch_and_upsert.py"

# 校验目标
VALID_TARGETS="alarm_service wind_alarm wind_fetch pv_fetch"
for t in "${TARGETS[@]}"; do
  if ! echo "$VALID_TARGETS" | grep -qw "$t"; then
    echo "!! 未知目标: $t"
    echo "有效目标: $VALID_TARGETS"
    exit 1
  fi
done

NEED_C=0
NEED_PY=0
NEED_WEB=0
for t in "${TARGETS[@]}"; do
  case "${TARGET_MAP[$t]%%:*}" in
    c) NEED_C=1; NEED_WEB=1 ;;
    py) NEED_PY=1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "请用 root 或 sudo 运行"; exit 1; }

# ======================= 定位源码目录 =======================
if [ -z "$SRC_DIR" ]; then
  for d in "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" "$PWD"; do
    if [ -f "$d/Makefile" ]; then SRC_DIR="$d"; break; fi
  done
fi
if [ -z "$SRC_DIR" ]; then
  echo "未找到源码目录（需包含 Makefile）。"
  echo "用法：sudo $0 [安装目录] [源码目录] [目标...]"
  exit 1
fi
echo "==> 源码目录：$SRC_DIR"

# ======================= 安装目录 =======================
# 默认就地部署：运行目录 = 源码目录，不拷贝任何文件
# 只有用户显式传了安装目录（APP_DIR 非空）才走拷贝模式
if [ -z "$APP_DIR" ]; then
  APP_DIR="$SRC_DIR"
  echo "==> 就地部署模式：运行目录 = 源码目录 = $APP_DIR"
else
  echo "==> 拷贝部署模式：运行目录 = $APP_DIR（源码 $SRC_DIR）"
fi

# ======================= C 告警主服务（可选） =======================
if [ "$NEED_C" -eq 1 ]; then
  # 编译
  BIN="$SRC_DIR/gddl_alarm"
  if [ ! -f "$BIN" ]; then
    command -v make >/dev/null || { echo "没有现成二进制，且缺少 make"; exit 1; }
    echo "==> 编译 gddl_alarm（PG 模式）"
    make -C "$SRC_DIR" ./gddl_alarm
    [ -f "$BIN" ] || { echo "编译失败：未生成 $BIN"; exit 1; }
  fi

  # 动态库自检
  if ldd "$BIN" 2>/dev/null | grep -q "not found"; then
    echo "!! 缺少运行时动态库，请安装 libpq5（Debian/Ubuntu: apt install libpq5；RHEL: yum install libpq）"
    ldd "$BIN" | grep "not found"
    exit 1
  fi

  # 部署
  if [ "$APP_DIR" != "$SRC_DIR" ]; then
    install -d -m 0755 "$APP_DIR"
    install -m 0755 "$BIN" "$APP_DIR/gddl_alarm"
    if [ "$NEED_WEB" -eq 1 ]; then
      install -d -m 0755 "$APP_DIR/web_root" "$APP_DIR/certs"
      cp -a "$SRC_DIR/web_root/." "$APP_DIR/web_root/"
      cp -a "$SRC_DIR/certs/." "$APP_DIR/certs/"
    fi
  fi
fi

# ======================= Python 服务脚本（可选） =======================
if [ "$NEED_PY" -eq 1 ] && [ "$APP_DIR" != "$SRC_DIR" ]; then
  install -d -m 0755 "$APP_DIR"
  for t in "${TARGETS[@]}"; do
    if [ "${TARGET_MAP[$t]%%:*}" = "py" ]; then
      py_script="${TARGET_MAP[$t]##*:}"
      if [ -f "$SRC_DIR/$py_script" ]; then
        install -m 0644 "$SRC_DIR/$py_script" "$APP_DIR/$py_script"
        echo "==> 已拷贝 $py_script"
      else
        echo "!! 缺少源文件 $SRC_DIR/$py_script，跳过 $t"
      fi
    fi
  done
fi

# ======================= data_config.json =======================
if [ -f "$APP_DIR/data_config.json" ]; then
  echo "==> 保留已存在的 $APP_DIR/data_config.json"
elif [ "$APP_DIR" != "$SRC_DIR" ] && [ -f "$SRC_DIR/data_config.json" ]; then
  install -m 0644 "$SRC_DIR/data_config.json" "$APP_DIR/data_config.json"
elif [ "$APP_DIR" = "$SRC_DIR" ]; then
  echo "!! 就地模式缺少 $APP_DIR/data_config.json，请确认解压包内容完整"
fi

# ======================= 安装 systemd units =======================
# C 服务
if [ "$NEED_C" -eq 1 ]; then
  UNIT=/etc/systemd/system/gddl_alarm.service
  sed "s|/opt/gddl_alarm|$APP_DIR|g" "$SRC_DIR/deploy/gddl_alarm.service" > "$UNIT"
  chmod 0644 "$UNIT"
  systemctl daemon-reload
  systemctl enable gddl_alarm >/dev/null 2>&1 || true
  systemctl restart gddl_alarm || echo "!! gddl_alarm 启动失败，查看：journalctl -u gddl_alarm -n 50"
  echo "==> gddl_alarm 已安装并重启"
fi

# Python 服务
PYBIN=""
if [ "$NEED_PY" -eq 1 ]; then
  for cand in \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
    /usr/local/bin/python3 \
    /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3 \
    /bin/python3.13 /bin/python3.12 /bin/python3.11 /bin/python3 \
    python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if [ -x "$cand" ]; then PYBIN="$cand"; break; fi
    if p="$(type -P "$cand" 2>/dev/null)" && [ -x "$p" ]; then PYBIN="$p"; break; fi
  done
  if [ -z "$PYBIN" ]; then
    echo "!! 找不到 python3，跳过 Python 服务"
  else
    echo "==> Python 解释器：$PYBIN"
    "$PYBIN" -c 'import psycopg2, requests' 2>/dev/null || \
      echo "!! 缺少 Python 依赖，请先安装：$PYBIN -m pip install psycopg2-binary requests"

    PY_UNITS=""
    for t in "${TARGETS[@]}"; do
      [ "${TARGET_MAP[$t]%%:*}" = "py" ] || continue
      unit_name="${TARGET_MAP[$t]#*:}"
      unit_name="${unit_name%%:*}"
      py_script="${TARGET_MAP[$t]##*:}"
      if [ ! -f "$SRC_DIR/$py_script" ]; then continue; fi

      sed -e "s|/opt/gddl_alarm|$APP_DIR|g" \
          -e "s|/usr/bin/python3|$PYBIN|g" \
          "$SRC_DIR/deploy/$unit_name.service" \
        > "/etc/systemd/system/$unit_name.service"
      chmod 0644 "/etc/systemd/system/$unit_name.service"
      echo "==> 已安装 unit $unit_name.service ($py_script)"
      PY_UNITS="$PY_UNITS $unit_name"
    done

    systemctl daemon-reload
    for py_unit in $PY_UNITS; do
      systemctl enable "$py_unit" >/dev/null 2>&1 || true
      systemctl restart "$py_unit" || \
        echo "!! $py_unit 启动失败，查看：journalctl -u $py_unit -n 50"
    done
  fi
fi

# ======================= 状态汇总 =======================
sleep 1
echo
echo "=========================================="
echo " 部署完成，安装目标: ${TARGETS[*]}"
echo "=========================================="

if [ "$NEED_C" -eq 1 ]; then
  systemctl --no-pager --full status gddl_alarm || true
fi
for t in "${TARGETS[@]}"; do
  if [ "${TARGET_MAP[$t]%%:*}" = "py" ]; then
    unit_name="${TARGET_MAP[$t]#*:}"
    unit_name="${unit_name%%:*}"
    systemctl --no-pager --full status "$unit_name" 2>/dev/null || true
  fi
done

echo
echo "查看日志："
for t in "${TARGETS[@]}"; do
  if [ "${TARGET_MAP[$t]%%:*}" = "py" ]; then
    unit_name="${TARGET_MAP[$t]#*:}"
    unit_name="${unit_name%%:*}"
    echo "  $unit_name : journalctl -u $unit_name -f"
  fi
done
if [ "$NEED_C" -eq 1 ]; then
  echo "  gddl_alarm : journalctl -u gddl_alarm -f"
  echo "  端口       : $(grep -o '"httpPort"[^,]*' "$APP_DIR/data_config.json" || true) / $(grep -o '"httpsPort"[^,]*' "$APP_DIR/data_config.json" || true)"
fi
