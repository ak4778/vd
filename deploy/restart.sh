#!/usr/bin/env bash
# 重启 gddl_alarm 组件的 systemd 服务（install.sh 安装的 unit）。
# 用法：
#   sudo ./deploy/restart.sh                              重启全部 4 个服务
#   sudo ./deploy/restart.sh alarm_service                只重启 C 告警主服务
#   sudo ./deploy/restart.sh wind_alarm wind_fetch pv_fetch  重启指定服务（可多选）
#
# 可选目标：
#   alarm_service  - gddl_alarm.service
#   wind_alarm     - gddl_wind_alarm.service
#   wind_fetch     - gddl_wind_fetch.service
#   pv_fetch       - gddl_pv_fetch.service
#
# 说明：只重启服务，不重新编译、不改 unit、不改开机自启。
#       改过源码或 unit 文件请用 sudo ./deploy/install.sh 重新部署。
set -euo pipefail

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      sed -n '2,16p' "$0"
      exit 0 ;;
    *) break ;;
  esac
done

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(alarm_service wind_alarm wind_fetch pv_fetch)
fi

declare -A UNIT_MAP
UNIT_MAP[alarm_service]="gddl_alarm"
UNIT_MAP[wind_alarm]="gddl_wind_alarm"
UNIT_MAP[wind_fetch]="gddl_wind_fetch"
UNIT_MAP[pv_fetch]="gddl_pv_fetch"

VALID_TARGETS="alarm_service wind_alarm wind_fetch pv_fetch"
for t in "${TARGETS[@]}"; do
  if ! echo "$VALID_TARGETS" | grep -qw "$t"; then
    echo "!! 未知目标: $t"
    echo "有效目标: $VALID_TARGETS"
    exit 1
  fi
done

[ "$(id -u)" -eq 0 ] || { echo "请用 root 或 sudo 运行"; exit 1; }

# 先确认 unit 已安装，避免对不存在的 unit 报一堆错
for t in "${TARGETS[@]}"; do
  unit="${UNIT_MAP[$t]}"
  if ! systemctl cat "$unit" >/dev/null 2>&1; then
    echo "!! 未找到 $unit.service，请先运行 sudo ./deploy/install.sh"
    exit 1
  fi
done

for t in "${TARGETS[@]}"; do
  unit="${UNIT_MAP[$t]}"
  echo "==> 重启 $unit.service"
  systemctl restart "$unit" || echo "!! $unit 重启失败，查看：journalctl -u $unit -n 50"
done

# ======================= 状态汇总 =======================
sleep 2
echo
echo "=========================================="
echo " 重启完成，目标: ${TARGETS[*]}"
echo "=========================================="
for t in "${TARGETS[@]}"; do
  unit="${UNIT_MAP[$t]}"
  printf '  %-18s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done

if echo "${TARGETS[*]}" | grep -qw alarm_service; then
  echo
  echo "提示：gddl_alarm 已重启，浏览器会话失效，需要重新登录。"
  echo "日志：journalctl -u gddl_alarm -f"
fi
