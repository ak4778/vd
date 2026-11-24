#!/usr/bin/env bash
# 停止 gddl_alarm 组件的 systemd 服务（install.sh 安装的 unit）。
# 用法：
#   sudo ./deploy/stop.sh                              停止全部 4 个服务
#   sudo ./deploy/stop.sh alarm_service                只停 C 告警主服务
#   sudo ./deploy/stop.sh wind_alarm wind_fetch pv_fetch  停指定服务（可多选）
#
# 可选目标：
#   alarm_service  - gddl_alarm.service
#   wind_alarm     - gddl_wind_alarm.service
#   wind_fetch     - gddl_wind_fetch.service
#   pv_fetch       - gddl_pv_fetch.service
#
# 说明：只 stop，不改开机自启；需要取消自启请手动 systemctl disable。
set -euo pipefail

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      sed -n '2,14p' "$0"
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

for t in "${TARGETS[@]}"; do
  unit="${UNIT_MAP[$t]}"
  echo "==> 停止 $unit.service"
  systemctl stop "$unit" || echo "!! $unit 停止失败，查看：systemctl status $unit"
done

# ======================= 状态汇总 =======================
sleep 1
echo
echo "=========================================="
echo " 停止完成，目标: ${TARGETS[*]}"
echo "=========================================="
for t in "${TARGETS[@]}"; do
  unit="${UNIT_MAP[$t]}"
  printf '  %-18s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
done
