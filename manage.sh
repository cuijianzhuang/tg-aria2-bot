#!/usr/bin/env bash
#
# tg-aria2-bot 交互式管理菜单
#
#   sudo ./manage.sh              # 打开菜单
#   sudo tg-aria2                 # 安装快捷命令后（菜单里选「安装快捷命令」，install.sh 也会自动装）
#
# 也可以直接带子命令运行，不进菜单（适合脚本/cron）：
#   status                 服务状态
#   logs [服务]            跟踪日志（bot/web/aria2/telegram-bot-api/ariang，默认 bot）
#   restart [服务|all]     重启（默认 all）
#   start | stop           启动 / 停止全部服务
#   config                 修改常用配置
#   info                   Web 后台访问信息
#   check | update         检查更新 / 升级（转给 update.sh）
#   backup | restore       立即备份 / 从备份恢复
#   rollback               回退到历史版本
#   install                运行 install.sh（首次安装或重新安装）

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$SCRIPT_PATH")"
cd "$REPO_DIR"

SHORTCUT=/usr/local/bin/tg-aria2

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""
fi

log()  { printf '%s[manage]%s %s\n' "$C_GREEN$C_BOLD" "$C_RESET" "$1"; }
warn() { printf '%s[warn]%s %s\n' "$C_YELLOW$C_BOLD" "$C_RESET" "$1"; }
err()  { printf '%s[error]%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$1" >&2; }
die()  { err "$1"; exit 1; }

pause() { [[ -t 0 ]] && read -rp "${C_DIM}按回车返回菜单...${C_RESET}" _ || true; }

confirm() {
  local ans
  read -rp "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

if [[ -f scripts/env_lib.sh ]]; then
  # shellcheck source=scripts/env_lib.sh
  source scripts/env_lib.sh
  # shellcheck source=scripts/net_lib.sh
  source scripts/net_lib.sh
else
  die "找不到 scripts/env_lib.sh，仓库不完整？"
fi

MODE=""
detect_mode() {
  case "$(env_get BOT_API_URL)" in
    http://telegram-bot-api:8081) MODE=docker ;;
    http://127.0.0.1:*) MODE=bare ;;
    *) MODE="" ;;
  esac
}

require_installed() {
  [[ -n "$MODE" ]] && return 0
  err "还没安装（找不到 .env 或无法识别部署模式），先选「安装」"
  return 1
}

require_root() {
  [[ "$EUID" -eq 0 ]] && return 0
  err "这个操作需要 root 权限：sudo $0"
  return 1
}

# ---------------------------------------------------------------- 服务抽象
# 逻辑服务名统一用 docker compose 的服务名；bare 模式映射到 systemd 单元或
# （混合模式下的）telegram-bot-api 独立容器

ALL_SERVICES=(bot web aria2 telegram-bot-api ariang)

unit_exists() { [[ -f "/etc/systemd/system/$1.service" || -f "/lib/systemd/system/$1.service" || -f "/etc/init.d/$1" ]]; }

# bare 模式：逻辑服务名 → "systemd:<unit>" / "docker:<container>" / ""（未安装）
bare_target() {
  case "$1" in
    bot) unit_exists tg-aria2-bot && echo systemd:tg-aria2-bot ;;
    web) unit_exists tg-aria2-web && echo systemd:tg-aria2-web ;;
    ariang) unit_exists tg-ariang && echo systemd:tg-ariang ;;
    aria2)
      local u
      u="$(env_get ARIA2_SERVICE_NAME)"
      if [[ -n "$u" ]] && unit_exists "$u"; then echo "systemd:$u"
      elif unit_exists aria2; then echo systemd:aria2
      fi ;;
    telegram-bot-api)
      if unit_exists telegram-bot-api; then echo systemd:telegram-bot-api
      elif command -v docker >/dev/null 2>&1 && docker inspect telegram-bot-api >/dev/null 2>&1; then
        echo docker:telegram-bot-api
      fi ;;
  esac
  return 0
}

# 当前部署里实际存在的服务
installed_services() {
  local s
  if [[ "$MODE" == "docker" ]]; then
    local enabled
    enabled="$(docker compose config --services 2>/dev/null || true)"
    for s in "${ALL_SERVICES[@]}"; do
      grep -qx "$s" <<<"$enabled" && echo "$s"
    done
  else
    for s in "${ALL_SERVICES[@]}"; do
      [[ -n "$(bare_target "$s")" ]] && echo "$s"
    done
  fi
  return 0
}

# 输出 running / stopped / restarting / missing
svc_state() {
  local s="$1"
  if [[ "$MODE" == "docker" ]]; then
    local st
    st="$(docker compose ps -a --format '{{.State}}' "$s" 2>/dev/null | head -1)"
    case "$st" in
      running) echo running ;;
      restarting) echo restarting ;;
      "") echo stopped ;;
      *) echo stopped ;;
    esac
    return 0
  fi
  local t
  t="$(bare_target "$s")"
  case "$t" in
    systemd:*)
      local a
      a="$(systemctl is-active "${t#systemd:}" 2>/dev/null || true)"
      case "$a" in
        active) echo running ;;
        activating) echo restarting ;;
        *) echo stopped ;;
      esac ;;
    docker:*)
      if [[ "$(docker inspect -f '{{.State.Running}}' "${t#docker:}" 2>/dev/null)" == "true" ]]; then
        echo running
      else
        echo stopped
      fi ;;
    *) echo missing ;;
  esac
}

state_label() {
  case "$1" in
    running) printf '%s● 运行中%s' "$C_GREEN" "$C_RESET" ;;
    restarting) printf '%s● 重启中%s' "$C_YELLOW" "$C_RESET" ;;
    stopped) printf '%s● 已停止%s' "$C_RED" "$C_RESET" ;;
    *) printf '%s○ 未安装%s' "$C_DIM" "$C_RESET" ;;
  esac
}

# svc_do <start|stop|restart> <服务...>
svc_do() {
  local action="$1"; shift
  [[ $# -gt 0 ]] || return 0
  if [[ "$MODE" == "docker" ]]; then
    if [[ "$action" == "start" ]]; then
      docker compose up -d "$@"
    else
      docker compose "$action" "$@"
    fi
    return
  fi
  local s t
  for s in "$@"; do
    t="$(bare_target "$s")"
    case "$t" in
      systemd:*) systemctl "$action" "${t#systemd:}" ;;
      docker:*) docker "$action" "${t#docker:}" >/dev/null ;;
      *) warn "$s 未安装，跳过" ;;
    esac
  done
}

svc_logs() {
  local s="${1:-bot}"
  log "跟踪 $s 的日志，Ctrl+C 退出"
  if [[ "$MODE" == "docker" ]]; then
    docker compose logs -f --tail 100 "$s" || true
    return 0
  fi
  local t
  t="$(bare_target "$s")"
  case "$t" in
    systemd:*) journalctl -u "${t#systemd:}" -f -n 100 --no-pager || true ;;
    docker:*) docker logs -f --tail 100 "${t#docker:}" || true ;;
    *) err "$s 未安装" ;;
  esac
}

# 选择一个服务；第一个参数为 1 时多给一个「全部」选项（输出 all）
pick_service() {
  local with_all="${1:-0}" i=1 choice
  local -a list
  mapfile -t list < <(installed_services)
  (( ${#list[@]} > 0 )) || { err "没有检测到已安装的服务"; return 1; }
  echo >&2
  for s in "${list[@]}"; do
    printf '  %s%2d.%s %-18s %s\n' "$C_CYAN" "$i" "$C_RESET" "$s" "$(state_label "$(svc_state "$s")")" >&2
    i=$((i + 1))
  done
  [[ "$with_all" -eq 1 ]] && printf '  %s%2d.%s 全部\n' "$C_CYAN" "$i" "$C_RESET" >&2
  printf '  %s%2d.%s 返回\n' "$C_CYAN" 0 "$C_RESET" >&2
  read -rp "请选择: " choice
  [[ "$choice" =~ ^[0-9]+$ ]] || return 1
  (( choice == 0 )) && return 1
  if [[ "$with_all" -eq 1 && "$choice" -eq "$i" ]]; then echo all; return 0; fi
  (( choice >= 1 && choice <= ${#list[@]} )) || return 1
  echo "${list[$((choice - 1))]}"
}

# ---------------------------------------------------------------- 状态

version_line() {
  if [[ -d .git ]]; then
    git log -1 --format="%h（%cd）" --date=format:'%Y-%m-%d' 2>/dev/null || echo "未知"
  else
    echo "非 git 部署"
  fi
}

host_download_dir() {
  if [[ "$MODE" == "docker" ]]; then
    local d
    d="$(env_get HOST_DOWNLOAD_DIR)"
    echo "${d:-./downloads}"
  else
    env_get DOWNLOAD_DIR
  fi
}

show_header() {
  clear 2>/dev/null || true
  printf '%s========== tg-aria2-bot 管理菜单 ==========%s\n' "$C_BOLD" "$C_RESET"
  if [[ -z "$MODE" ]]; then
    printf ' 状态: %s未安装%s\n' "$C_YELLOW" "$C_RESET"
    return
  fi
  printf ' 部署模式: %s%s%s    版本: %s\n' "$C_CYAN" "$MODE" "$C_RESET" "$(version_line)"
  local s line=""
  for s in $(installed_services); do
    line+="$(printf '%s %s   ' "$s" "$(state_label "$(svc_state "$s")")")"
  done
  printf ' %s\n' "${line:-（没有检测到服务）}"
  local dl
  dl="$(host_download_dir)"
  if [[ -n "$dl" && -d "$dl" ]]; then
    printf ' 下载目录: %s  剩余 %s\n' "$dl" "$(df -h --output=avail "$dl" 2>/dev/null | tail -1 | tr -d ' ')"
  fi
}

show_status() {
  require_installed || return 0
  if [[ "$MODE" == "docker" ]]; then
    docker compose ps -a
  else
    local s t
    for s in $(installed_services); do
      t="$(bare_target "$s")"
      printf '%-18s %-28s %s\n' "$s" "$t" "$(state_label "$(svc_state "$s")")"
    done
  fi
  echo
  local dl
  dl="$(host_download_dir)"
  [[ -n "$dl" && -d "$dl" ]] && df -h "$dl"
  return 0
}

# ---------------------------------------------------------------- 配置

mask() {
  local v="$1"
  if [[ -z "$v" ]]; then echo "${C_DIM}（未设置）${C_RESET}"
  else echo "${v:0:3}******"
  fi
}

# 改完配置后让它生效。docker 模式下 bot/web 直接读挂载的 .env，restart 即可；
# 端口监听地址这类 compose 层面的配置要 up -d 重建容器
apply_config() {
  local recreate="${1:-0}"
  if ! confirm "现在重启 bot/web 让配置生效？"; then
    warn "已保存但未生效，之后选「重启服务」即可"
    return 0
  fi
  local -a targets=()
  local s
  for s in bot web; do
    [[ "$(svc_state "$s")" != "missing" ]] && targets+=("$s")
  done
  if [[ "$MODE" == "docker" && "$recreate" -eq 1 ]]; then
    docker compose up -d
  else
    svc_do restart "${targets[@]}"
  fi
  log "已重启：${targets[*]}"
}

# 改 Web 后台端口：校验 → 写 .env →（bare）重新生成 systemd 单元。
# 生效（重启/重建容器）交给 config_menu 退出时的 apply_config
change_web_port() {
  local cur new bot_port
  cur="$(env_get WEB_PORT)"; cur="${cur:-8080}"
  new="$(read_validated "新的 Web 后台端口（当前 ${cur}）: " '^[0-9]{1,5}$' "请输入端口号")" || return 1
  if (( new < 1 || new > 65535 )); then err "端口范围是 1-65535"; return 1; fi
  [[ "$new" == "$cur" ]] && { log "端口没变"; return 1; }
  bot_port="$(env_get BOT_API_URL)"; bot_port="${bot_port##*:}"
  if [[ "$MODE" == "bare" && "$new" == "$bot_port" ]]; then
    err "端口 ${new} 是 telegram-bot-api 在用的，换一个"
    return 1
  fi
  if port_in_use "$new"; then
    err "端口 ${new} 已被占用：$(port_owner "$new")"
    return 1
  fi
  env_set WEB_PORT "$new"
  if [[ "$MODE" == "bare" && -f /etc/systemd/system/tg-aria2-web.service ]]; then
    sed "s#{{WORKDIR}}#${REPO_DIR}#g; s#{{WEB_PORT}}#${new}#g" systemd/tg-aria2-web.service \
      > /etc/systemd/system/tg-aria2-web.service
    systemctl daemon-reload
  fi
  log "Web 后台端口改为 ${new}，重启后生效；之后访问地址里的端口记得一起换"
}

read_validated() {
  # read_validated <提示> <正则> <错误提示> → 输出用户输入
  local prompt="$1" re="$2" msg="$3" v
  while true; do
    read -rp "$prompt" v || return 1
    if [[ "$v" =~ $re ]]; then echo "$v"; return 0; fi
    err "$msg"
  done
}

config_menu() {
  require_installed || return 0
  require_root || return 0
  local changed=0 recreate=0 choice v
  while true; do
    echo
    printf '%s---- 修改常用配置（.env）----%s\n' "$C_BOLD" "$C_RESET"
    printf '  1. 白名单用户 ALLOWED_USER_IDS     %s\n' "$(env_get ALLOWED_USER_IDS)"
    printf '  2. 管理员 ADMIN_USER_IDS            %s\n' "$(v="$(env_get ADMIN_USER_IDS)"; echo "${v:-${C_DIM}（同白名单）${C_RESET}}")"
    printf '  3. Web 后台密码 ADMIN_PASSWORD      %s\n' "$(mask "$(env_get ADMIN_PASSWORD)")"
    printf '  4. 同时下载数 MAX_CONCURRENT        %s\n' "$(env_get MAX_CONCURRENT)"
    printf '  5. 磁盘告警阈值(GB)                 %s\n' "$(env_get DISK_ALERT_THRESHOLD_GB)"
    printf '  6. 自动清理记录(天，0=关)           %s\n' "$(env_get AUTO_CLEANUP_DAYS)"
    printf '  7. 代理 PROXY_URL                   %s\n' "$(v="$(env_get PROXY_URL)"; echo "${v:-${C_DIM}（无）${C_RESET}}")"
    if [[ "$MODE" == "docker" ]]; then
      printf '  8. Web 端口监听地址 WEB_BIND        %s\n' "$(v="$(env_get WEB_BIND)"; echo "${v:-0.0.0.0}")"
    fi
    printf '  9. Web 后台端口 WEB_PORT            %s\n' "$(v="$(env_get WEB_PORT)"; echo "${v:-8080}")"
    printf ' 10. 用编辑器打开 .env（全部配置）\n'
    printf '  0. 返回%s\n' "$([[ "$changed" -eq 1 ]] && echo "（并选择是否重启生效）")"
    read -rp "请选择: " choice
    case "$choice" in
      1) v="$(read_validated "新的白名单（Telegram 用户 ID，逗号分隔，留空=对所有人开放）: " '^[0-9, ]*$' "只能是数字和逗号")"
         env_set ALLOWED_USER_IDS "${v// /}"; changed=1
         [[ -z "$v" ]] && warn "白名单为空：机器人对所有人开放，且管理功能会被锁定（除非设置了管理员）" ;;
      2) v="$(read_validated "管理员 ID（逗号分隔，留空=沿用白名单）: " '^[0-9, ]*$' "只能是数字和逗号")"
         env_set ADMIN_USER_IDS "${v// /}"; changed=1 ;;
      3) read -rsp "新密码（留空自动生成随机密码）: " v; echo
         if [[ -z "$v" ]]; then
           v="$(openssl rand -hex 12 2>/dev/null || head -c12 /dev/urandom | xxd -p)"
           printf '新密码：%s%s%s（请记下，只显示这一次）\n' "$C_BOLD" "$v" "$C_RESET"
         fi
         env_set ADMIN_PASSWORD "$v"
         # 删掉会话签名密钥，web 重启时重新生成——所有已登录的会话随之失效，
         # 跟在 Web 后台里改密码的效果一致
         rm -f data/web_session_secret "$(dirname "$(env_get DB_PATH)")/web_session_secret" 2>/dev/null || true
         changed=1 ;;
      4) v="$(read_validated "同时下载数（1-50）: " '^([1-9]|[1-4][0-9]|50)$' "请输入 1-50 的整数")"
         env_set MAX_CONCURRENT "$v"; changed=1 ;;
      5) v="$(read_validated "磁盘剩余低于多少 GB 时告警（0=关闭）: " '^[0-9]+$' "请输入整数")"
         env_set DISK_ALERT_THRESHOLD_GB "$v"; changed=1 ;;
      6) v="$(read_validated "已完成记录保留天数（0=不自动清理）: " '^[0-9]+$' "请输入整数")"
         env_set AUTO_CLEANUP_DAYS "$v"; changed=1 ;;
      7) v="$(read_validated "代理地址（如 http://127.0.0.1:7890 或 socks5://...，留空=不用代理）: " '^((https?|socks5h?)://[^ ]+)?$' "格式不对，需要 http:// https:// socks5:// 开头")"
         env_set PROXY_URL "$v"; changed=1 ;;
      8) [[ "$MODE" == "docker" ]] || continue
         echo "  1) 127.0.0.1 只监听本机（推荐，配合 SSH 隧道/反向代理）"
         echo "  2) 0.0.0.0   对公网开放（明文 HTTP）"
         read -rp "请选择: " v
         case "$v" in
           1) env_set WEB_BIND 127.0.0.1 ;;
           2) env_set WEB_BIND 0.0.0.0 ;;
           *) continue ;;
         esac
         changed=1; recreate=1 ;;
      9) change_web_port || continue
         changed=1; recreate=1 ;;
      10) "${EDITOR:-$(command -v nano || command -v vim || echo vi)}" .env
         changed=1; recreate=1 ;;
      0|"") break ;;
      *) err "无效选择" ;;
    esac
    [[ "$choice" =~ ^[1-8]$ ]] && log "已保存"
  done
  [[ "$changed" -eq 1 ]] && apply_config "$recreate"
  return 0
}

show_info() {
  require_installed || return 0
  local port bind pw
  port="$(env_get WEB_PORT)"; port="${port:-8080}"
  pw="$(env_get ADMIN_PASSWORD)"
  echo
  if [[ "$(svc_state web)" == "missing" ]]; then
    warn "Web 管理后台未安装/未启用"
  else
    if [[ "$MODE" == "docker" ]]; then
      bind="$(env_get WEB_BIND)"; bind="${bind:-0.0.0.0}"
    else
      bind=127.0.0.1
    fi
    if [[ "$bind" == "0.0.0.0" ]]; then
      local ip
      ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
      echo "Web 管理后台: http://${ip:-<服务器IP>}:${port}"
      echo "AriaNg:       http://${ip:-<服务器IP>}:6880"
      warn "端口对公网开放且是明文 HTTP，建议在「修改常用配置」里改成只监听本机"
    else
      echo "Web 管理后台: http://127.0.0.1:${port}（仅本机）"
      echo "AriaNg:       http://127.0.0.1:6880（仅本机）"
      echo "远程访问：ssh -L ${port}:localhost:${port} -L 6880:localhost:6880 root@<服务器IP>，然后浏览器打开上面的地址"
    fi
    echo "AriaNg 需要填的 RPC 密钥 = .env 里的 ARIA2_SECRET"
    if [[ -z "$pw" ]]; then
      warn "ADMIN_PASSWORD 为空，Web 后台无法登录——在「修改常用配置」里设置"
    elif confirm "显示 Web 后台密码？"; then
      echo "密码：$pw"
    fi
  fi
  return 0
}

# ---------------------------------------------------------------- 备份 / 恢复 / 回退

# 列出备份，选中的目录输出到 stdout
pick_backup() {
  local -a dirs
  mapfile -t dirs < <(find backups -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r)
  (( ${#dirs[@]} > 0 )) || { err "还没有备份（backups/ 为空）"; return 1; }
  echo >&2
  local i=1 d note commit
  for d in "${dirs[@]}"; do
    note="$(cat "$d/NOTE" 2>/dev/null || echo "升级前自动备份")"
    commit="$(cut -c1-7 "$d/COMMIT" 2>/dev/null || echo "?")"
    printf '  %s%2d.%s %s  版本 %s  %s%s%s\n' "$C_CYAN" "$i" "$C_RESET" "${d#backups/}" "$commit" "$C_DIM" "$note" "$C_RESET" >&2
    i=$((i + 1))
  done
  printf '  %s%2d.%s 返回\n' "$C_CYAN" 0 "$C_RESET" >&2
  local choice
  read -rp "请选择: " choice
  [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#dirs[@]} )) || return 1
  echo "${dirs[$((choice - 1))]}"
}

host_db_path() {
  if [[ "$MODE" == "docker" ]]; then
    echo "$REPO_DIR/data/tasks.db"
  else
    local p
    p="$(env_get DB_PATH)"
    echo "${p:-$REPO_DIR/data/tasks.db}"
  fi
}

do_restore() {
  require_installed || return 0
  require_root || return 0
  local b
  b="$(pick_backup)" || return 0
  local has_env=0 has_db=0
  [[ -f "$b/.env" ]] && has_env=1
  [[ -f "$b/tasks.db" ]] && has_db=1
  echo
  echo "将从 ${b#backups/} 恢复：$([[ $has_env -eq 1 ]] && echo ".env ")$([[ $has_db -eq 1 ]] && echo "数据库")"
  warn "当前的 .env 和数据库会先自动备份一份，然后被覆盖；bot/web 会短暂停止"
  confirm "确认恢复？" || return 0

  bash ./update.sh --backup-only
  local -a running=()
  local s
  for s in bot web; do
    [[ "$(svc_state "$s")" == "running" ]] && running+=("$s")
  done
  svc_do stop "${running[@]}"

  if [[ $has_env -eq 1 ]]; then
    cat "$b/.env" > .env   # 原地写回，保留属主和 bind mount 的 inode
    log "已恢复 .env"
  fi
  if [[ $has_db -eq 1 ]]; then
    local db
    db="$(host_db_path)"
    rm -f "${db}-wal" "${db}-shm"
    cp "$b/tasks.db" "$db"
    [[ "$MODE" == "docker" ]] && chown 1000:1000 "$db"
    log "已恢复数据库"
  fi
  svc_do start "${running[@]}"
  log "恢复完成"
}

do_rollback() {
  require_installed || return 0
  require_root || return 0
  [[ -d .git ]] || { err "非 git 部署，无法回退版本"; return 0; }
  echo
  echo "最近的版本（当前版本标 *）："
  local -a commits
  mapfile -t commits < <(git log -15 --format='%H')
  local i=1 c
  for c in "${commits[@]}"; do
    printf '  %s%2d.%s %s %s\n' "$C_CYAN" "$i" "$C_RESET" "$([[ $i -eq 1 ]] && echo '*' || echo ' ')" \
      "$(git log -1 --format='%h %s %C(dim)(%cd)%Creset' --date=format:'%Y-%m-%d' --color=always "$c")"
    i=$((i + 1))
  done
  echo "  也可以直接输入版本号（提交 hash / 标签），0 返回"
  local choice ref
  read -rp "回退到: " choice
  [[ -n "$choice" && "$choice" != "0" ]] || return 0
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#commits[@]} )); then
    ref="${commits[$((choice - 1))]}"
  else
    ref="$choice"
  fi
  # update.sh --to 会列出要撤销的提交并要求确认，同样有备份/健康检查/自动回滚
  bash ./update.sh --to "$ref" || true
  warn "注意：之后再执行「升级」会重新升级到最新版本"
}

install_shortcut() {
  require_root || return 0
  ln -sf "$SCRIPT_PATH" "$SHORTCUT"
  log "已安装快捷命令：以后在任何目录运行 ${C_BOLD}sudo tg-aria2${C_RESET} 即可打开本菜单"
}

# ---------------------------------------------------------------- 菜单

restart_menu() {
  require_installed || return 0
  require_root || return 0
  local s
  s="$(pick_service 1)" || return 0
  if [[ "$s" == "all" ]]; then
    mapfile -t list < <(installed_services)
    svc_do restart "${list[@]}"
    log "已重启全部服务"
  else
    svc_do restart "$s"
    log "已重启 $s"
  fi
}

logs_menu() {
  require_installed || return 0
  local s
  s="$(pick_service 0)" || return 0
  svc_logs "$s"
}

start_stop_menu() {
  require_installed || return 0
  require_root || return 0
  echo "  1) 启动全部服务"
  echo "  2) 停止全部服务"
  local c
  read -rp "请选择: " c
  local -a list
  mapfile -t list < <(installed_services)
  case "$c" in
    1) if [[ "$MODE" == "docker" ]]; then docker compose up -d; else svc_do start "${list[@]}"; fi
       log "已启动" ;;
    2) confirm "确认停止全部服务？下载会中断（aria2 会话会保存，启动后继续）" || return 0
       svc_do stop "${list[@]}"
       log "已停止" ;;
  esac
}

main_menu() {
  while true; do
    detect_mode
    show_header
    cat <<EOF

 ${C_DIM}—— 运行 ——${C_RESET}
  ${C_CYAN} 1.${C_RESET} 服务状态
  ${C_CYAN} 2.${C_RESET} 查看日志
  ${C_CYAN} 3.${C_RESET} 重启服务
  ${C_CYAN} 4.${C_RESET} 启动 / 停止全部服务
 ${C_DIM}—— 配置 ——${C_RESET}
  ${C_CYAN} 5.${C_RESET} 修改常用配置（白名单/管理员/密码/并发/代理…）
  ${C_CYAN} 6.${C_RESET} Web 后台访问信息
 ${C_DIM}—— 升级与备份 ——${C_RESET}
  ${C_CYAN} 7.${C_RESET} 检查更新
  ${C_CYAN} 8.${C_RESET} 升级到最新版本
  ${C_CYAN} 9.${C_RESET} 立即备份
  ${C_CYAN}10.${C_RESET} 从备份恢复（.env / 数据库）
  ${C_CYAN}11.${C_RESET} 回退到历史版本
 ${C_DIM}—— 其它 ——${C_RESET}
  ${C_CYAN}12.${C_RESET} 安装 / 重新安装
  ${C_CYAN}13.${C_RESET} 安装快捷命令 tg-aria2
  ${C_CYAN} 0.${C_RESET} 退出
EOF
    local choice
    read -rp "请输入数字: " choice || exit 0
    echo
    case "$choice" in
      1) show_status; pause ;;
      2) logs_menu ;;
      3) restart_menu; pause ;;
      4) start_stop_menu; pause ;;
      5) config_menu; pause ;;
      6) show_info; pause ;;
      7) require_installed && bash ./update.sh --check || true; pause ;;
      8) require_installed && require_root && bash ./update.sh || true; pause ;;
      9) require_installed && require_root && bash ./update.sh --backup-only || true; pause ;;
      10) do_restore; pause ;;
      11) do_rollback; pause ;;
      12) require_root && bash ./install.sh || true; pause ;;
      13) install_shortcut; pause ;;
      0|q|exit) exit 0 ;;
      *) err "无效选择"; sleep 1 ;;
    esac
  done
}

main() {
  detect_mode
  local cmd="${1:-menu}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    menu) [[ -t 0 ]] || die "菜单需要交互式终端；非交互场景请用子命令（$0 --help）"
          main_menu ;;
    status) show_status ;;
    logs) require_installed || exit 1; svc_logs "${1:-bot}" ;;
    restart)
      require_installed && require_root || exit 1
      if [[ "${1:-all}" == "all" ]]; then
        mapfile -t list < <(installed_services); svc_do restart "${list[@]}"
      else
        svc_do restart "$@"
      fi ;;
    start)
      require_installed && require_root || exit 1
      if [[ "$MODE" == "docker" ]]; then docker compose up -d
      else mapfile -t list < <(installed_services); svc_do start "${list[@]}"; fi ;;
    stop)
      require_installed && require_root || exit 1
      mapfile -t list < <(installed_services); svc_do stop "${list[@]}" ;;
    config) config_menu ;;
    info) show_info ;;
    check) exec bash ./update.sh --check "$@" ;;
    update|upgrade) exec bash ./update.sh "$@" ;;
    backup) exec bash ./update.sh --backup-only ;;
    restore) do_restore ;;
    rollback) do_rollback ;;
    install) exec bash ./install.sh "$@" ;;
    -h|--help|help) sed -n '2,/^$/p' "$SCRIPT_PATH" | grep '^#' | sed 's/^# \{0,1\}//' ;;
    *) die "未知命令: $cmd（$0 --help 查看用法）" ;;
  esac
}

main "$@"
