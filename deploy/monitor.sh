#!/usr/bin/env bash
# gddl_alarm 组件健康巡查脚本（Linux / systemd）。
#
# 功能：
#   1. 巡检 gddl_alarm 全家桶 4 个 systemd 服务的存活、重启次数、启动时间
#   2. 解析 data_config.json 中的 pgConnStr，检查 PG 可达性与 role 连接数
#   3. 从各 unit 的 journalctl 里抽取关键指标：
#      - gddl_wind_alarm  : tick / 活跃告警 / 超温累计 / 拉取错误 / 最近告警事件
#      - gddl_wind_fetch  : 水位线时间戳 / 最近拉取量 / 分钟聚合状态
#      - gddl_pv_fetch   : 当前时段 / 水位线 / 空拉取次数 / 回补进度
#      - gddl_alarm      : HTTP 端口 / 活动连接数
#   4. 支持单次巡检、持续 watch、tail 单程序日志、一键告警事件流
#
# 用法：
#   ./deploy/monitor.sh                单次巡检（默认）
#   ./deploy/monitor.sh --watch        持续监控（每 15 秒刷新一次）
#   ./deploy/monitor.sh --short       极简版（只给 OK/FAIL，适合 cron / 飞书推送）
#   ./deploy/monitor.sh --tail wind_alarm   实时跟踪某个 unit 的日志
#   ./deploy/monitor.sh --events      只盯 ALARM / RECOVER 事件流
#   ./deploy/monitor.sh --pg          只看 PG 连接与活动查询
#
# 退出码（--short 模式下）：
#   0  全部 OK
#   1  至少一个服务 down
#   2  PG 不可达
#   3  活跃告警数 > 0（业务告警，不是进程挂）
#   4  综合故障（进程 down + PG 不可达）
set -euo pipefail

# ======================= 颜色 =======================
if [ -t 1 ]; then
  C_RED=$'\033[91m'; C_GRN=$'\033[92m'; C_YLW=$'\033[93m'
  C_BLU=$'\033[94m'; C_CYN=$'\033[96m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YLW=''; C_BLU=''; C_CYN=''; C_BLD=''; C_RST=''
fi

# ======================= 参数 =======================
MODE="check"           # check | watch | short | tail | events | pg
TARGET=""              # --tail 时指定 unit 名
INTERVAL=15            # --watch 刷新秒数

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) sed -n '2,22p' "$0"; exit 0 ;;
    --watch)  MODE="watch" ;;
    --short)  MODE="short" ;;
    --tail)   MODE="tail"; TARGET="${2:-}"; shift ;;
    --events) MODE="events" ;;
    --pg)     MODE="pg" ;;
    -*) echo "未知选项: $1"; exit 1 ;;
    *)  TARGET="$1" ;;
  esac
  shift
done

# ======================= 定位安装目录 =======================
# 默认就地部署：install.sh 默认 APP_DIR=SRC_DIR，运行目录 = deploy/ 的父目录
# 后备：如果就地找不到，再试 /opt/gddl_alarm（拷贝部署）
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR_INPLACE="$(cd "$DEPLOY_DIR/.." && pwd)"
APP_DIR_COPY="/opt/gddl_alarm"

APP_DIR=""
if [ -f "$APP_DIR_INPLACE/data_config.json" ]; then
  APP_DIR="$APP_DIR_INPLACE"
  APP_MODE="就地部署"
elif [ -f "$APP_DIR_COPY/data_config.json" ]; then
  APP_DIR="$APP_DIR_COPY"
  APP_MODE="拷贝部署"
else
  echo "!! 找不到 data_config.json"
  echo "  已尝试就地: $APP_DIR_INPLACE"
  echo "  已尝试拷贝: $APP_DIR_COPY"
  echo "请确认部署目录存在，或手动指定: APP_DIR=/your/path $0"
  exit 1
fi
if [ -n "${APP_DIR_OVERRIDE:-}" ]; then
  APP_DIR="$APP_DIR_OVERRIDE"
  APP_MODE="手动指定"
fi

# ======================= unit 清单 =======================
declare -A UNIT_SCRIPTS=(
  [gddl_alarm]="gddl_alarm"
  [gddl_wind_alarm]="wind_alarm.py"
  [gddl_wind_fetch]="wind_fetch_and_upsert.py"
  [gddl_pv_fetch]="pv_fetch_and_upsert.py"
)
PY_UNITS=(gddl_wind_alarm gddl_wind_fetch gddl_pv_fetch)
ALL_UNITS=(gddl_alarm gddl_wind_alarm gddl_wind_fetch gddl_pv_fetch)

# ======================= 从 data_config.json 解析 PG =======================
parse_pg() {
  local cfg="$APP_DIR/data_config.json"
  [ -f "$cfg" ] || return 1
  # pgConnStr 形如 "host=10.65.80.103 port=5432 dbname=gddl_yjyw user=gddl_yjyw"
  local conn host port db user password
  conn=$(grep -oP '"pgConnStr"\s*:\s*"\K[^"]+' "$cfg" 2>/dev/null || true)
  [ -z "$conn" ] && return 1
  host=$(echo "$conn" | grep -oP 'host=\K[^ ]+')
  port=$(echo "$conn" | grep -oP 'port=\K[^ ]+')
  db=$(echo "$conn"   | grep -oP 'dbname=\K[^ ]+')
  user=$(echo "$conn" | grep -oP 'user=\K[^ ]+')
  password=$(echo "$conn" | grep -oP 'password=\K[^ ]+' || true)
  PG_HOST="$host" PG_PORT="${port:-5432}" PG_DB="$db" PG_USER="$user" PG_PASSWORD="$password"
  return 0
}
parse_pg || true

# ======================= 工具函数 =======================
is_unit_active() { systemctl is-active "$1" 2>/dev/null; }
unit_pid()     { systemctl show "$1" -p MainPID --value 2>/dev/null; }
unit_since()   { systemctl show "$1" -p ActiveEnterTimestamp --value 2>/dev/null; }
unit_restarts(){ systemctl show "$1" -p NRestarts --value 2>/dev/null; }
unit_uptime() {
  local since=$1 now
  now=$(date +%s)
  local ts
  ts=$(date -d "$since" +%s 2>/dev/null || echo 0)
  [ "$ts" = "0" ] && echo "-" && return
  local sec=$(( now - ts ))
  if   [ "$sec" -ge 3600 ]; then printf '%dh%02dm' $((sec/3600)) $(((sec%3600)/60))
  elif [ "$sec" -ge 60 ];   then printf '%dm%02ds' $((sec/60))  $((sec%60))
  else                           printf '%ds'  "$sec"
  fi
}
pg_exec() {
  local sql="$1"
  # psql 不存在就彻底不行
  if ! command -v psql >/dev/null 2>&1; then
    return 99
  fi
  # bash 环境变量前缀：PGPASSWORD=xxx psql ...
  # 不能用 "${arr[@]}" psql 因为双引号内不重分词，会把 PGPASSWORD=xxx 当程序名
  if [ -n "${PG_PASSWORD:-}" ]; then
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
      -tAc "$sql" 2>/dev/null
  else
    psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
      -tAc "$sql" 2>/dev/null
  fi
}

# pg_exec 失败时打印详细诊断
pg_diagnose() {
  echo "  ${C_YLW}诊断步骤:${C_RST}"
  echo "    1) CLI 检查:  command -v psql → $(command -v psql >/dev/null 2>&1 && echo OK || echo MISSING)"
  if ! command -v psql >/dev/null 2>&1; then
    echo "       → 需安装: sudo dnf install postgresql  (或 apt install postgresql-client)"
  fi
  echo "    2) 密码检查:  PG_PASSWORD=${PG_PASSWORD:+已设置}${PG_PASSWORD:-空}"
  echo "    3) 端口可达:  timeout 2 bash -c '</dev/tcp/${PG_HOST}/${PG_PORT}' → $?"
  echo "       (0=TCP 通  其他=网络不通/防火墙)"
}

# ======================= 子模块 =======================
check_unit_status() {
  # 打印一个 unit 的进程状态行
  local unit="$1"
  local active pid since uptime restarts
  active=$(is_unit_active "$unit")
  if [ "$active" = "active" ]; then
    pid=$(unit_pid "$unit")
    since=$(unit_since "$unit")
    uptime=$(unit_uptime "$since")
    restarts=$(unit_restarts "$unit")
    local restart_icon=""
    [ "${restarts:-0}" -gt 0 ] && restart_icon=" ${C_YLW}(restarted ${restarts}x)${C_RST}"
    echo "  ${C_GRN}●${C_RST} $unit  PID=${pid}  UP=${uptime}${restart_icon}"
    return 0
  else
    echo "  ${C_RED}✗${C_RST} $unit  状态=${active}  ${C_RED}!!!${C_RST}"
    return 1
  fi
}

check_units() {
  local failed=0
  echo "${C_BLD}【1】进程状态${C_RST}"
  for u in "${ALL_UNITS[@]}"; do
    check_unit_status "$u" || failed=1
  done
  return $failed
}

check_pg() {
  # 打印 PG 连接 + 活动查询
  echo
  echo "${C_BLD}【2】PG 数据库${C_RST}"
  if [ -z "${PG_HOST:-}" ]; then
    echo "  ${C_YLW}⚠ data_config.json 未解析到 pgConnStr${C_RST}"
    return 2
  fi

  # 连接可达性（只发 SET，不做事）
  if ! pg_exec "SELECT 1;" >/dev/null 2>&1; then
    echo "  ${C_RED}✗ PG 不可达  host=${PG_HOST}:${PG_PORT} user=${PG_USER} db=${PG_DB}${C_RST}"
    pg_diagnose
    return 2
  fi
  echo "  ${C_GRN}✓${C_RST} PG 可达  ${PG_HOST}:${PG_PORT}/${PG_DB} (role=${PG_USER})"

  # 当前角色的连接数
  local total
  total=$(pg_exec "SELECT count(*) FROM pg_stat_activity WHERE usename = '${PG_USER}';" || echo "?")
  echo "  连接数: ${C_CYN}${total}${C_RST}"

  # 各 unit 的 application_name（如果程序设置了的话）
  local per_app
  per_app=$(pg_exec "
    SELECT coalesce(application_name,'(none)') as app, count(*)
    FROM pg_stat_activity
    WHERE usename = '${PG_USER}'
    GROUP BY 1 ORDER BY 2 DESC;
  " 2>/dev/null || true)
  if [ -n "$per_app" ]; then
    echo "  分布:"
    echo "$per_app" | sed 's/^/    /' | sed 's/\t/ → /'
  fi

  # 最长活动查询（>5min）
  local slow
  slow=$(pg_exec "
    SELECT pid, client_addr, state, left(query, 60)
    FROM pg_stat_activity
    WHERE usename = '${PG_USER}' AND state = 'active'
      AND query_start < now() - interval '5 minutes'
    ORDER BY query_start LIMIT 3;
  " 2>/dev/null || true)
  if [ -n "$slow" ]; then
    echo "  ${C_YLW}⚠ 长查询(>5min):${C_RST}"
    echo "$slow" | sed 's/^/    /'
  fi

  return 0
}

check_wind_alarm() {
  echo
  echo "${C_BLD}【3】gddl_wind_alarm 核心指标${C_RST}"
  # unit 未运行时不要误判历史日志
  local active
  active=$(is_unit_active gddl_wind_alarm)
  if [ "$active" != "active" ]; then
    echo "  ${C_RED}✗ unit inactive（进程未运行）${C_RST}"
    return 1
  fi

  # 取 journal 最后一条 heartbeat
  local hb
  hb=$(journalctl -u gddl_wind_alarm --since "5 min ago" --no-pager 2>/dev/null \
    | grep 'heartbeat' | tail -1 || true)
  if [ -z "$hb" ]; then
    echo "  ${C_RED}近 5 分钟没有 heartbeat${C_RST}"
    return 1
  fi
  # 解析数值
  local tick dev pt err act exceed
  tick=$(echo "$hb"     | grep -oP 'tick=\K\d+'     || echo "?")
  dev=$(echo "$hb"      | grep -oP '设备=\K\d+'     || echo "?")
  pt=$(echo "$hb"       | grep -oP '测点=\K\d+'     || echo "?")
  err=$(echo "$hb"      | grep -oP '拉取错误=\K\d+' || echo "?")
  act=$(echo "$hb"      | grep -oP '活跃告警=\K\d+' || echo "?")
  exceed=$(echo "$hb"   | grep -oP '超温累计=\K\d+' || echo "?")

  # 拉取错误红标
  local err_icon="${C_GRN}✓${C_RST}"
  [ "${err:-0}" -gt 0 ] && err_icon="${C_RED}✗ err=${err}${C_RST}"
  # 活跃告警
  local act_icon="${C_GRN}·${C_RST}"
  [ "${act:-0}" -gt 0 ] && act_icon="${C_YLW}▲ ${act} 活跃${C_RST}"

  echo "  tick=${C_CYN}${tick}${C_RST}  设备=${dev}×${pt}  拉取错误=${err_icon}  活跃告警=${act_icon}  超温累计=${exceed}"

  # 最近 30 分钟告警事件
  local events
  events=$(journalctl -u gddl_wind_alarm --since "30 min ago" --no-pager 2>/dev/null \
    | grep -E 'ALARM|RECOVER' | tail -5 || true)
  if [ -n "$events" ]; then
    echo "  ${C_BLD}最近 30 分钟告警事件:${C_RST}"
    echo "$events" | sed 's/^/    /' | sed 's/\x1b\[[0-9;]*m//g'
  else
    echo "  ${C_GRN}最近 30 分钟无告警事件${C_RST}"
  fi

  [ "${err:-0}" -gt 0 ] || [ "${act:-0}" -gt 0 ] && return 3 || return 0
}

check_wind_fetch() {
  echo
  echo "${C_BLD}【4】gddl_wind_fetch 采集进度${C_RST}"
  local active
  active=$(is_unit_active gddl_wind_fetch)
  if [ "$active" != "active" ]; then
    echo "  ${C_RED}✗ unit inactive（进程未运行）${C_RST}"
    # 历史日志参考（unit inactive 时 journalctl 不返回）
    local log="$APP_DIR/wind_fetch.log"
    if [ -f "$log" ]; then
      local last
      last=$(tail -1 "$log" | grep -E '窗口|水位线' || true)
      [ -n "$last" ] && echo "  ${C_YLW}历史残留: $(echo "$last" | sed 's/\x1b\[[0-9;]*m//g')${C_RST}"
    fi
    return 1
  fi

  local line
  line=$(journalctl -u gddl_wind_fetch --since "2 min ago" --no-pager 2>/dev/null \
    | grep -E '窗口|水位线|分钟聚合' | tail -1 || true)
  if [ -z "$line" ]; then
    # 回退到日志文件
    local log="$APP_DIR/wind_fetch.log"
    [ -f "$log" ] && line=$(tail -3 "$log" | grep -E '窗口|水位线' | tail -1 || true)
  fi
  if [ -z "$line" ]; then
    echo "  ${C_YLW}近 2 分钟无采集日志${C_RST}"
    return 0
  fi
  # 只保留时间 + 条数
  local ts n
  ts=$(echo "$line" | grep -oP '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' | head -1 || true)
  n=$(echo "$line"  | grep -oP '拉取 \K\d+' || echo "?")
  echo "  最新窗口: ${C_CYN}${ts}${C_RST}  拉取 ${C_CYN}${n}${C_RST} 条"

  # 最近 5 分钟是否有 ERROR/WARNING
  local warn
  warn=$(journalctl -u gddl_wind_fetch --since "5 min ago" --no-pager -p warning 2>/dev/null \
    | tail -2 || true)
  [ -n "$warn" ] && echo "  ${C_YLW}⚠ 最近警告:${C_RST}" && echo "$warn" | sed 's/^/    /' | sed 's/\x1b\[[0-9;]*m//g'
  return 0
}

check_pv_fetch() {
  echo
  echo "${C_BLD}【5】gddl_pv_fetch 采集状态${C_RST}"
  local active
  active=$(is_unit_active gddl_pv_fetch)
  if [ "$active" != "active" ]; then
    echo "  ${C_RED}✗ unit inactive（进程未运行）${C_RST}"
    local log="$APP_DIR/pv_fetch.log"
    if [ -f "$log" ]; then
      local last
      last=$(tail -1 "$log" || true)
      [ -n "$last" ] && echo "  ${C_YLW}历史残留: $(echo "$last" | sed 's/\x1b\[[0-9;]*m//g')${C_RST}"
    fi
    return 1
  fi

  local last
  last=$(journalctl -u gddl_pv_fetch --since "2 min ago" --no-pager 2>/dev/null \
    | tail -1 || true)
  if [ -z "$last" ]; then
    local log="$APP_DIR/pv_fetch.log"
    [ -f "$log" ] && last=$(tail -1 "$log" || true)
  fi
  if [ -z "$last" ]; then
    echo "  ${C_YLW}无日志${C_RST}"; return 0
  fi
  # 判断状态：采集中 / idle
  local status
  echo "$last" | grep -q '非采集时段\|空闲等待' && status="${C_CYN}idle（等下一时段）${C_RST}" \
    || status="${C_GRN}采集中${C_RST}"
  echo "  状态: $status"
  echo "  $last" | sed 's/\x1b\[[0-9;]*m//g'

  # 空拉取连续次数
  local zero
  zero=$(journalctl -u gddl_pv_fetch --since "5 min ago" --no-pager 2>/dev/null \
    | grep -oP '已连续 \K\d+ (?=轮空拉取)' | tail -1 || echo "0")
  if [ "${zero:-0}" -ge 3 ]; then
    echo "  ${C_YLW}⚠ 连续 ${zero} 轮空拉取${C_RST}"
  fi
  return 0
}

check_alarm_c() {
  echo
  echo "${C_BLD}【6】gddl_alarm (C 主服务)${C_RST}"
  local active
  active=$(is_unit_active gddl_alarm)
  if [ "$active" != "active" ]; then
    echo "  ${C_RED}✗ unit inactive（进程未运行）${C_RST}"
    return 1
  fi
  local last
  last=$(journalctl -u gddl_alarm --since "2 min ago" --no-pager 2>/dev/null \
    | tail -1 || true)
  [ -n "$last" ] && echo "  $(echo "$last" | sed 's/\x1b\[[0-9;]*m//g')" || echo "  （无新日志）"

  # 如果 data_config.json 里有 httpPort，快速 curl
  local port
  port=$(grep -oP '"httpPort"\s*:\s*\K\d+' "$APP_DIR/data_config.json" 2>/dev/null || true)
  [ -n "$port" ] && {
    local curl_out
    curl_out=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/" 2>/dev/null || echo "ERR")
    [ "$curl_out" = "200" ] && echo "  ${C_GRN}✓ HTTP ${port} → 200${C_RST}" \
      || echo "  ${C_YLW}⚠ HTTP ${port} → ${curl_out}${C_RST}"
  }
  return 0
}

# ======================= 主入口 =======================
run_once() {
  local overall=0
  local units_down=0 pg_fail=0
  echo
  echo "${C_BLD}========================================${C_RST}"
  echo " $(date '+%Y-%m-%d %H:%M:%S')  gddl_alarm 健康巡检"
  echo " 部署模式: ${APP_MODE}  目录: $APP_DIR"
  echo "${C_BLD}========================================${C_RST}"

  check_units || units_down=1
  check_pg   || pg_fail=1

  check_wind_alarm  || true
  check_wind_fetch  || true
  check_pv_fetch    || true
  check_alarm_c     || true

  # 退出码：0=全绿  1=进程down  2=PG挂  3=进程+PG都挂（综合故障）
  if   [ "$units_down" -eq 1 ] && [ "$pg_fail" -eq 1 ]; then overall=3
  elif [ "$units_down" -eq 1 ]; then overall=1
  elif [ "$pg_fail" -eq 1 ];   then overall=2
  fi

  echo
  echo "${C_BLD}========================================${C_RST}"
  echo " 退出码: ${overall}"
  case $overall in
    0) echo "  ✓ 全绿：进程+PG 均正常" ;;
    1) echo "  ✗ 至少一个 systemd unit 未运行（进程 down）" ;;
    2) echo "  ✗ PG 数据库不可达" ;;
    3) echo "  ✗ 综合故障：进程 down + PG 挂" ;;
  esac
  echo "${C_BLD}========================================${C_RST}"
  return $overall
}

run_short() {
  # 极简版：只给 OK/FAIL + 退出码
  local failed=0 alarm_flag=0 pg_fail=0
  for u in "${ALL_UNITS[@]}"; do
    if [ "$(is_unit_active "$u")" != "active" ]; then
      echo "DOWN $u"; failed=1
    fi
  done
  if [ -n "${PG_HOST:-}" ]; then
    if ! pg_exec "SELECT 1;" >/dev/null 2>&1; then
      echo "PG_DOWN ${PG_HOST}"; pg_fail=1
    else
      echo "PG_OK ${PG_HOST} $(pg_exec "SELECT count(*) FROM pg_stat_activity WHERE usename='${PG_USER}';" 2>/dev/null || echo "?")"
    fi
  fi
  # 活跃告警数
  local act
  act=$(journalctl -u gddl_wind_alarm --since "2 min ago" --no-pager 2>/dev/null \
    | grep 'heartbeat' | grep -oP '活跃告警=\K\d+' | tail -1 || echo "0")
  [ "${act:-0}" -gt 0 ] && { echo "ALERT ${act} 活跃告警"; alarm_flag=3; } \
    || echo "ALERT_OK 0"

  local ec=0
  if [ "$failed" -eq 1 ] && [ "$pg_fail" -eq 1 ]; then ec=3
  elif [ "$failed" -eq 1 ]; then ec=1
  elif [ "$pg_fail" -eq 1 ]; then ec=2
  fi
  exit $ec
}

run_watch() {
  # 持续刷新：用 ANSI 清屏 + 光标回到原点
  trap 'exit 0' INT TERM
  while true; do
    printf '\033[2J\033[H'   # 清屏 + 回到 (1,1)
    run_once || true
    sleep "$INTERVAL"
  done
}

run_tail() {
  local unit="$TARGET"
  if [ -z "$unit" ]; then
    echo "用法: $0 --tail <unit>"
    echo "  unit 可选: gddl_alarm | gddl_wind_alarm | gddl_wind_fetch | gddl_pv_fetch"
    exit 1
  fi
  if ! systemctl list-unit-files | grep -q "^${unit}\.service"; then
    echo "!! unit ${unit}.service 未安装"
    exit 1
  fi
  echo "${C_BLD}-- tail -f journalctl -u ${unit} (Ctrl+C 退出) --${C_RST}"
  exec journalctl -u "$unit" -f
}

run_events() {
  # 只盯 ALARM / RECOVER 事件流
  echo "${C_BLD}-- 实时告警事件流 (journalctl 合并 4 个 unit, Ctrl+C 退出) --${C_RST}"
  journalctl -u gddl_alarm -u gddl_wind_alarm -u gddl_wind_fetch -u gddl_pv_fetch -f \
    | grep --line-buffered -E 'ALARM|RECOVER|ERROR|FATAL|too many connections'
}

run_pg() {
  if [ -z "${PG_HOST:-}" ]; then
    echo "!! 未解析到 PG 连接（data_config.json pgConnStr）"
    exit 1
  fi
  echo "${C_BLD}PG 连接池  ${PG_HOST}:${PG_PORT}  user=${PG_USER}  db=${PG_DB}${C_RST}"
  pg_exec "
    SELECT pid, usename, coalesce(application_name,'(none)') app,
           client_addr, state,
           age(now(), query_start) AS age,
           left(query, 80)
    FROM pg_stat_activity
    WHERE usename = '${PG_USER}'
    ORDER BY query_start;
  " | column -t -s '|' || true
  echo
  echo "总连接数: $(pg_exec "SELECT count(*) FROM pg_stat_activity WHERE usename='${PG_USER}';" 2>/dev/null || echo "?")"
}

# ======================= 分发 =======================
case "$MODE" in
  watch)  run_watch ;;
  short)  run_short ;;
  tail)   run_tail ;;
  events) run_events ;;
  pg)     run_pg ;;
  *)      run_once ;;
esac
