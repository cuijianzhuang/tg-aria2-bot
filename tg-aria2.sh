#!/usr/bin/env bash
#
# tg-aria2-bot 一体化脚本：安装 / 升级 / 管理 全在这里。
#
#   sudo ./tg-aria2.sh                 打开交互式管理菜单
#   sudo tg-aria2                      同上（安装时会注册这个快捷命令）
#
# 子命令（不进菜单，适合脚本 / cron / CI）：
#   install [选项]          安装或修复安装（可重复运行，已完成的步骤自动跳过）
#       --mode docker|bare     部署方式（不传则交互选择；重跑沿用 .env 里的）
#       --token T --api-id N --api-hash H --allowed-ids 1,2
#       --download-dir DIR     下载目录（默认 ./downloads）
#       --admin-password PW    Web 后台密码（不传则自动生成，只显示一次）
#       --web-port PORT        Web 后台端口（默认 8080，被占用自动顺延）
#       --no-web               不装 Web 后台和 AriaNg
#       --with-rclone          顺带在宿主机装 rclone（网盘上传）
#       --botapi-from-source   bare 模式：源码编译 telegram-bot-api（不用 docker）
#   web-install             只安装 / 修复 Web 管理后台（+ AriaNg）
#   update [选项]           升级到最新版本：备份 → 拉代码 → 应用 → 健康检查 → 失败自动回滚
#       -y                     不询问确认
#       --check                只看有没有新版本，不做改动（也可以用子命令 check）
#       --to REF               回退 / 切换到指定提交或标签
#       --force                没有新提交也重新应用一遍
#       --reset                本地分支和远端分叉时强制对齐远端
#       --branch NAME          跟踪的远端分支（默认当前分支）
#       --pull-images          docker 模式：顺带更新第三方镜像
#       --no-rollback          失败时不自动回滚（留现场排查）
#       --dir PATH             仓库目录（CI 从临时文件运行时用）
#   check                   检查更新
#   status                  服务状态
#   logs [服务]             跟踪日志（bot/web/aria2/telegram-bot-api/ariang，默认 bot）
#   restart [服务|all]      重启（默认 all）
#   start | stop            启动 / 停止全部服务
#   config                  修改常用配置
#   info                    Web 后台访问信息
#   backup | restore        立即备份 / 从备份恢复 .env 和数据库
#   rollback                回退到历史版本（交互选择）
#
# 环境变量：BOT_API_PORT（bare 模式 telegram-bot-api 端口，默认 8081）、
#          UPDATE_HEALTH_WAIT（升级后健康检查观察秒数，默认 20）

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$SCRIPT_PATH")"
SHORTCUT=/usr/local/bin/tg-aria2

# =============================================================== 输出

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""
fi
LOG_TAG="tg-aria2"

log()  { printf '%s[%s]%s %s\n' "$C_GREEN$C_BOLD" "$LOG_TAG" "$C_RESET" "$1"; }
warn() { printf '%s[warn]%s %s\n' "$C_YELLOW$C_BOLD" "$C_RESET" "$1"; }
err()  { printf '%s[error]%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$1" >&2; }
die()  { err "$1"; exit 1; }

pause() { [[ -t 0 ]] && read -rp "${C_DIM}按回车返回菜单...${C_RESET}" _ || true; }

confirm() {
  local ans
  read -rp "$1 [y/N] " ans || return 1
  [[ "$ans" =~ ^[Yy]$ ]]
}

require_root() {
  [[ "$EUID" -eq 0 ]] && return 0
  err "这个操作需要 root 权限：sudo $0 ${1:-}"
  return 1
}

rand_hex() { openssl rand -hex "$1" 2>/dev/null || head -c"$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# =============================================================== .env 读写
# 只改指定的键，其余行（注释、设置菜单/Web 后台写回的运行时配置）原样保留；
# 用 `cat tmp > file` 原地写回：docker 模式下 .env 是单文件 bind mount，rename
# 覆盖会让容器里看到的还是旧 inode，同时保留属主（容器内 UID 1000 要能写）

env_get() {
  local key="$1" file="${2:-.env}"
  [[ -f "$file" ]] || return 0
  grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2- || true
}

env_has() {
  local key="$1" file="${2:-.env}"
  [[ -f "$file" ]] && grep -q "^${key}=" "$file"
}

env_set() {
  local key="$1" value="$2" file="${3:-.env}" tmp
  tmp="$(mktemp)"
  if env_has "$key" "$file"; then
    KEY="$key" VAL="$value" awk '
      BEGIN { k = ENVIRON["KEY"] "="; v = ENVIRON["VAL"] }
      !done && index($0, k) == 1 { print k v; done = 1; next }
      { print }
    ' "$file" > "$tmp"
  else
    if [[ -s "$file" ]]; then
      cat "$file" > "$tmp"
      [[ -n "$(tail -c1 "$file")" ]] && echo >> "$tmp"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

# =============================================================== 端口探测

# 有没有程序在监听。用 bash 自带的 /dev/tcp，不依赖 ss/netstat（精简系统未必有）
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

# 尽量说出是谁占着端口（只用于提示）
port_owner() {
  local out=""
  if command -v ss >/dev/null 2>&1; then
    out="$(ss -Hltnp "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true)"
  elif command -v lsof >/dev/null 2>&1; then
    out="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}' || true)"
  elif command -v netstat >/dev/null 2>&1; then
    out="$(netstat -ltnp 2>/dev/null | awk -v p=":$1" '$4 ~ p"$" {print $7; exit}' || true)"
  fi
  echo "${out:-未知程序}"
}

# 是不是一个能用的 telegram-bot-api：拿无效 token 调 getMe，Bot API 一定回
# {"ok":false,"error_code":...}——按协议判断，不靠进程名
port_is_botapi() {
  local body
  body="$(curl -s -m 5 "http://127.0.0.1:$1/bot0:invalid/getMe" 2>/dev/null || true)"
  [[ "$body" == *'"ok":false'* && "$body" == *'"error_code"'* ]]
}

# 是不是本项目自带的 Web 后台（首页 <title> 固定，未登录也能拿到）
port_is_our_web() {
  local body
  body="$(curl -s -m 5 "http://127.0.0.1:$1/" 2>/dev/null || true)"
  [[ "$body" == *"tg-aria2-bot 管理后台"* ]]
}

# find_free_port <起始> <结束> [要避开的端口...]
find_free_port() {
  local start="$1" end="$2" p x skip
  shift 2
  for (( p = start; p <= end; p++ )); do
    skip=0
    for x in "$@"; do [[ "$p" == "$x" ]] && skip=1; done
    [[ "$skip" -eq 1 ]] && continue
    port_in_use "$p" || { echo "$p"; return 0; }
  done
  return 1
}

# =============================================================== 部署模式 / 服务抽象

MODE=""
detect_mode() {
  case "$(env_get BOT_API_URL)" in
    http://telegram-bot-api:8081) MODE=docker ;;
    http://127.0.0.1:*) MODE=bare ;;   # 端口可能因冲突换成了别的
    *) MODE="" ;;
  esac
}

require_installed() {
  [[ -n "$MODE" ]] && return 0
  err "还没安装（找不到 .env 或无法识别部署模式），先运行：sudo $0 install"
  return 1
}

compose() { docker compose "$@"; }

# 逻辑服务名统一用 docker compose 的服务名；bare 模式映射到 systemd 单元或
# （混合模式下的）telegram-bot-api 独立容器
ALL_SERVICES=(bot web aria2 telegram-bot-api ariang)

unit_exists() { [[ -f "/etc/systemd/system/$1.service" || -f "/lib/systemd/system/$1.service" || -f "/etc/init.d/$1" ]]; }

# bare：逻辑服务名 → "systemd:<unit>" / "docker:<container>" / ""（未安装）
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

installed_services() {
  local s
  if [[ "$MODE" == "docker" ]]; then
    local enabled
    enabled="$(compose config --services 2>/dev/null || true)"
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

# running / stopped / restarting / missing
svc_state() {
  local s="$1"
  if [[ "$MODE" == "docker" ]]; then
    grep -qx "$s" <<<"$(compose config --services 2>/dev/null || true)" || { echo missing; return 0; }
    case "$(compose ps -a --format '{{.State}}' "$s" 2>/dev/null | head -1)" in
      running) echo running ;;
      restarting) echo restarting ;;
      *) echo stopped ;;
    esac
    return 0
  fi
  local t
  t="$(bare_target "$s")"
  case "$t" in
    systemd:*)
      case "$(systemctl is-active "${t#systemd:}" 2>/dev/null || true)" in
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
    if [[ "$action" == "start" ]]; then compose up -d "$@"; else compose "$action" "$@"; fi
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
    compose logs -f --tail 100 "$s" || true
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

host_download_dir() {
  if [[ "$MODE" == "docker" ]]; then
    local d
    d="$(env_get HOST_DOWNLOAD_DIR)"
    echo "${d:-./downloads}"
  else
    env_get DOWNLOAD_DIR
  fi
}

host_db_path() {
  if [[ "$MODE" == "docker" ]]; then
    echo "$REPO_DIR/data/tasks.db"   # 容器内 /app/data 映射到仓库的 ./data
  else
    local p
    p="$(env_get DB_PATH)"
    echo "${p:-$REPO_DIR/data/tasks.db}"
  fi
}

# systemd 单元模板渲染（{{WORKDIR}} / {{WEB_PORT}}）
render_unit() {
  local tpl="$1" web_port
  web_port="$(env_get WEB_PORT)"
  sed "s#{{WORKDIR}}#${REPO_DIR}#g; s#{{WEB_PORT}}#${web_port:-8080}#g" "$tpl"
}

show_status() {
  require_installed || return 0
  if [[ "$MODE" == "docker" ]]; then
    compose ps -a
  else
    local s t
    for s in "${ALL_SERVICES[@]}"; do
      t="$(bare_target "$s")"
      printf '  %-18s %-28s %s\n' "$s" "${t:-—}" "$(state_label "$(svc_state "$s")")"
    done
  fi
  local dl
  dl="$(host_download_dir)"
  if [[ -n "$dl" && -d "$dl" ]]; then
    echo
    df -h "$dl"
  fi
  return 0
}

# =============================================================== 安装

I_MODE=""; I_TOKEN=""; I_API_ID=""; I_API_HASH=""; I_ALLOWED=""
I_DOWNLOAD_DIR="./downloads"; I_ADMIN_PASSWORD=""; I_WEB_PORT=""
I_NO_WEB=0; I_RCLONE=0; I_FROM_SOURCE=0
I_PASSWORD_GENERATED=0
CURRENT_STEP=""

step() {
  CURRENT_STEP="$1"
  printf '\n%s==> %s%s\n' "$C_BOLD$C_CYAN" "$1" "$C_RESET"
}

on_install_error() {
  local rc=$?
  trap - ERR
  echo
  err "安装在「${CURRENT_STEP:-准备}」这一步失败（退出码 ${rc}）。"
  err "按上面的报错处理后重新运行：sudo $0 install —— 已完成的步骤会自动跳过，.env 里的配置会沿用。"
  if [[ -n "$MODE" ]]; then
    echo
    echo "当前各服务状态："
    show_status || true
  fi
  exit "$rc"
}

pkg_install() {
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" || { apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; }
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y "$@"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y "$@"
  else
    return 1
  fi
}

parse_install_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode) I_MODE="$2"; shift 2 ;;
      --token) I_TOKEN="$2"; shift 2 ;;
      --api-id) I_API_ID="$2"; shift 2 ;;
      --api-hash) I_API_HASH="$2"; shift 2 ;;
      --allowed-ids) I_ALLOWED="$2"; shift 2 ;;
      --download-dir) I_DOWNLOAD_DIR="$2"; shift 2 ;;
      --admin-password) I_ADMIN_PASSWORD="$2"; shift 2 ;;
      --web-port) I_WEB_PORT="$2"; shift 2 ;;
      --no-web) I_NO_WEB=1; shift ;;
      --with-rclone) I_RCLONE=1; shift ;;
      --botapi-from-source|--build-botapi-from-source) I_FROM_SOURCE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "未知参数: $1（$0 --help 查看用法）" ;;
    esac
  done
  if [[ -n "$I_WEB_PORT" ]] && ! [[ "$I_WEB_PORT" =~ ^[0-9]+$ && "$I_WEB_PORT" -ge 1 && "$I_WEB_PORT" -le 65535 ]]; then
    die "--web-port 必须是 1-65535 之间的数字"
  fi
}

# 收集凭据、生成/更新 .env。重跑时一切沿用 .env 里已有的值：不会重新生成
# 密钥、不会切换部署模式，只改本函数管理的那几个键
install_prepare_env() {
  step "准备配置（.env）"
  local existing_mode="" existing_secret="" existing_password=""
  if [[ -f .env ]]; then
    existing_secret="$(env_get ARIA2_SECRET)"
    existing_password="$(env_get ADMIN_PASSWORD)"
    detect_mode; existing_mode="$MODE"
    [[ -z "$I_MODE" ]]     && I_MODE="$existing_mode"
    [[ -z "$I_TOKEN" ]]    && I_TOKEN="$(env_get BOT_TOKEN)"
    [[ -z "$I_API_ID" ]]   && I_API_ID="$(env_get API_ID)"
    [[ -z "$I_API_HASH" ]] && I_API_HASH="$(env_get API_HASH)"
    [[ -z "$I_ALLOWED" ]]  && I_ALLOWED="$(env_get ALLOWED_USER_IDS)"
  fi

  if [[ -z "$I_MODE" ]]; then
    echo "选择部署方式:"
    echo "  1) docker  (推荐，全部服务容器化)"
    echo "  2) bare    (裸机，aria2 用 aria2.sh 装在宿主机，bot 跑在 venv 里)"
    local choice
    read -rp "输入 1 或 2: " choice
    case "$choice" in
      1) I_MODE=docker ;;
      2) I_MODE=bare ;;
      *) die "无效选择" ;;
    esac
  fi
  [[ "$I_MODE" == "docker" || "$I_MODE" == "bare" ]] || die "--mode 必须是 docker 或 bare"
  if [[ -n "$existing_mode" && "$existing_mode" != "$I_MODE" ]]; then
    die "当前 .env 是 ${existing_mode} 模式的部署，这次选的是 ${I_MODE} 模式。
同一台机器上切换模式会互相覆盖 .env 和端口/路径配置，导致已运行的服务崩掉。
确实要推倒重来的话，先备份并删除 .env 再重新安装。"
  fi

  [[ -n "$I_TOKEN" ]]    || read -rp "Bot Token (来自 @BotFather): " I_TOKEN
  [[ -n "$I_API_ID" ]]   || read -rp "API ID (来自 my.telegram.org): " I_API_ID
  [[ -n "$I_API_HASH" ]] || read -rp "API Hash (来自 my.telegram.org): " I_API_HASH
  [[ -n "$I_ALLOWED" ]]  || read -rp "允许使用的用户 Telegram ID，逗号分隔: " I_ALLOWED
  [[ -n "$I_TOKEN" ]]    || die "Bot Token 不能为空"
  [[ -n "$I_API_ID" ]]   || die "API ID 不能为空"
  [[ -n "$I_API_HASH" ]] || die "API Hash 不能为空"

  local secret="${existing_secret:-$(rand_hex 16)}"
  if [[ "$I_NO_WEB" -eq 1 ]]; then
    I_ADMIN_PASSWORD=""
  elif [[ -z "$I_ADMIN_PASSWORD" && -n "$existing_password" ]]; then
    I_ADMIN_PASSWORD="$existing_password"
  elif [[ -z "$I_ADMIN_PASSWORD" ]]; then
    I_ADMIN_PASSWORD="$(rand_hex 12)"
    I_PASSWORD_GENERATED=1
  fi

  mkdir -p "$I_DOWNLOAD_DIR" data aria2-config
  local fresh=0
  if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    fresh=1
  fi
  env_set BOT_TOKEN "$I_TOKEN"
  env_set API_ID "$I_API_ID"
  env_set API_HASH "$I_API_HASH"
  env_set ARIA2_SECRET "$secret"
  env_set ALLOWED_USER_IDS "$I_ALLOWED"
  env_set ADMIN_PASSWORD "$I_ADMIN_PASSWORD"
  [[ -n "$I_WEB_PORT" ]] && env_set WEB_PORT "$I_WEB_PORT"

  if [[ "$I_MODE" == "docker" ]]; then
    env_set BOT_API_URL "http://telegram-bot-api:8081"
    env_set ARIA2_RPC "http://aria2:6800/jsonrpc"
    env_set DOWNLOAD_DIR "/downloads"
    env_set DB_PATH "/app/data/tasks.db"
    # 下面两个只给 docker compose 读（变量插值 / 默认启用的 profile）
    env_set HOST_DOWNLOAD_DIR "$I_DOWNLOAD_DIR"
    if [[ "$I_NO_WEB" -eq 1 ]]; then env_set COMPOSE_PROFILES ""; else env_set COMPOSE_PROFILES "web"; fi
  else
    # telegram-bot-api 端口：BOT_API_PORT 环境变量 > .env 已有的 > 8081；
    # 被占用时 bare_step_botapi 会再自动换
    local port="${BOT_API_PORT:-}"
    if [[ -z "$port" && "$(env_get BOT_API_URL)" == http://127.0.0.1:* ]]; then
      port="$(env_get BOT_API_URL)"; port="${port##*:}"
    fi
    env_set BOT_API_URL "http://127.0.0.1:${port:-8081}"
    env_set ARIA2_RPC "http://127.0.0.1:6800/jsonrpc"
    env_set DOWNLOAD_DIR "$(realpath "$I_DOWNLOAD_DIR")"
    env_set DB_PATH "$(realpath data)/tasks.db"
    if [[ "$fresh" -eq 1 ]]; then
      # .env.example 里这几项是 docker 的默认值；bare 下 aria2.sh 装在
      # /root/.aria2c、服务名 aria2。只在首次生成时写，不覆盖用户改过的
      env_set ARIA2_SERVICE_NAME "aria2"
      env_set ARIA2_CONFIG_DIR "/root/.aria2c"
      env_set ARIA2_CLEAN_HOOK "/root/.aria2c/clean.sh"
      env_set ARIA2_UPLOAD_HOOK "/root/.aria2c/upload.sh"
    fi
  fi
  MODE="$I_MODE"
  if [[ "$fresh" -eq 1 ]]; then
    log ".env 已生成（aria2 密钥已自动生成）"
  else
    log ".env 已更新（只改了凭据/路径相关的键，其它配置保持不变）"
  fi
}

install_rclone() {
  step "安装 rclone"
  if command -v rclone >/dev/null 2>&1; then
    log "rclone 已安装: $(rclone version | head -1)"
  else
    curl -fsSL https://rclone.org/install.sh | bash
    command -v rclone >/dev/null 2>&1 || die "rclone 安装失败"
  fi
  warn "配置网盘 remote 需要交互式 OAuth 授权，请之后手动执行 rclone config"
}

# ---------------------------------------------------------------- docker 模式

docker_install_engine() {
  step "检查 Docker"
  if ! command -v docker >/dev/null 2>&1; then
    warn "未检测到 Docker，将用官方 get.docker.com 脚本安装"
    if [[ -t 0 ]]; then confirm "确认安装 Docker？" || die "已取消，请手动安装 Docker 后重试"; fi
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  else
    log "Docker 已安装: $(docker --version)"
  fi
  if ! docker compose version >/dev/null 2>&1; then
    warn "未检测到 docker compose 插件，尝试安装"
    pkg_install docker-compose-plugin || die "无法自动安装 docker compose 插件，请手动安装后重试"
  fi
}

install_docker_mode() {
  # aria2-config/ 预置了 P3TERX 配置，只需要把密钥写进占位符
  if [[ -f aria2-config/aria2.conf ]]; then
    sed -i "s#__ARIA2_SECRET_PLACEHOLDER__#$(env_get ARIA2_SECRET)#" aria2-config/aria2.conf
  fi
  # bot/web 容器以 UID/GID 1000 非 root 运行，bind mount 的属主要对齐
  chown -R 1000:1000 "$I_DOWNLOAD_DIR" data aria2-config .env

  docker_install_engine

  if [[ "$I_RCLONE" -eq 1 ]]; then
    install_rclone
    # p3terx/aria2-pro 不带 rclone：宿主机的静态二进制只读挂进容器
    cat > docker-compose.override.yml <<EOF
services:
  aria2:
    volumes:
      - $(command -v rclone):/usr/bin/rclone:ro
EOF
    log "已生成 docker-compose.override.yml；配置网盘：docker compose exec -it aria2 rclone config"
  fi

  if [[ "$I_NO_WEB" -eq 0 ]] && [[ -z "$(compose ps -q web 2>/dev/null)" ]]; then
    step "检查 Web 后台端口"
    local port
    port="$(env_get WEB_PORT)"; port="${port:-8080}"
    if port_in_use "$port"; then
      warn "端口 ${port} 已被其它程序占用：$(port_owner "$port")"
      port="$(find_free_port 8080 8099 "$port")" || die "8080-8099 端口全被占用，请在 .env 里设 WEB_PORT 后重试"
      env_set WEB_PORT "$port"
      warn "Web 后台改用端口 ${port}（已写入 .env）"
    else
      log "端口 ${port} 可用"
    fi
  fi

  step "启动容器（docker compose up -d --build）"
  compose up -d --build
}

# ---------------------------------------------------------------- bare 模式

bare_step_aria2() {
  step "安装 aria2（P3TERX aria2.sh）"
  if command -v aria2c >/dev/null 2>&1; then
    log "aria2c 已安装: $(aria2c --version | head -1)"
  else
    # aria2.sh 是纯交互菜单脚本，"1" = 安装 Aria2。它会自动改 iptables 放行
    # RPC/BT/DHT 端口并持久化；用 ufw/firewalld/云安全组的装完检查一下
    warn "aria2.sh 会自动修改并持久化 iptables 规则以放行 RPC/BT/DHT 端口"
    if [[ -f vendor/aria2.sh/aria2.sh ]]; then
      cp vendor/aria2.sh/aria2.sh /tmp/aria2.sh
    else
      curl -fsSL https://raw.githubusercontent.com/P3TERX/aria2.sh/master/aria2.sh -o /tmp/aria2.sh
    fi
    printf '1\n' | bash /tmp/aria2.sh
    command -v aria2c >/dev/null 2>&1 || die "aria2 安装失败，请查看上面的输出"
  fi
  local secret
  secret="$(grep -oP '(?<=rpc-secret=).*' /root/.aria2c/aria2.conf 2>/dev/null || true)"
  if [[ -n "$secret" ]]; then
    env_set ARIA2_SECRET "$secret"
    log "已同步 aria2.sh 生成的 RPC 密钥到 .env"
  fi
  systemctl enable --now aria2 2>/dev/null || true
}

bare_step_botapi() {
  step "启动 telegram-bot-api"
  local port url web_port="" reuse=0 move=0
  url="$(env_get BOT_API_URL)"; port="${url##*:}"
  [[ "$port" =~ ^[0-9]+$ ]] || port=8081
  if [[ "$I_NO_WEB" -eq 0 ]]; then
    web_port="$(env_get WEB_PORT)"; web_port="${web_port:-8080}"
  fi

  # 清掉我们自己上次留下的容器（失败的 docker run 也会留一个 Created 状态的）
  command -v docker >/dev/null 2>&1 && { docker rm -f telegram-bot-api >/dev/null 2>&1 || true; }

  if [[ "$port" == "$web_port" ]]; then
    warn "telegram-bot-api 的端口 ${port} 跟 Web 后台（WEB_PORT）相同"
    move=1
  elif port_in_use "$port"; then
    if port_is_botapi "$port"; then
      reuse=1
      log "127.0.0.1:${port} 上已经有 telegram-bot-api 在运行（$(port_owner "$port")），直接复用"
    elif port_is_our_web "$port"; then
      warn "端口 ${port} 被本项目的 Web 后台占用"; move=1
    else
      warn "端口 ${port} 已被其它程序占用：$(port_owner "$port")"; move=1
    fi
  fi
  if [[ "$move" -eq 1 ]]; then
    # telegram-bot-api 只给本机 bot 用，换端口无感；Web 后台端口是用户记着的，冲突时总让它让路
    local old="$port"
    port="$(find_free_port 8081 8099 "$old" "$web_port")" \
      || die "8081-8099 全被占用，无法启动 telegram-bot-api；释放端口或用 BOT_API_PORT=<端口> 指定"
    warn "telegram-bot-api 改用端口 ${port}（已写入 .env；占用 ${old} 的程序不受影响）"
  fi
  env_set BOT_API_URL "http://127.0.0.1:${port}"
  [[ "$reuse" -eq 1 ]] && return 0

  if [[ "$I_FROM_SOURCE" -eq 1 ]]; then
    log "从源码编译 telegram-bot-api（需要 20-40 分钟，2GB+ 内存）"
    pkg_install make git zlib1g-dev libssl-dev gperf cmake g++ clang-14 libc++-dev libc++abi-dev \
      || warn "编译依赖没装全，参考 https://github.com/tdlib/telegram-bot-api 手动安装"
    local build=/opt/telegram-bot-api-src
    [[ -d "$build" ]] || git clone --recursive https://github.com/tdlib/telegram-bot-api.git "$build"
    mkdir -p "$build/build"
    (cd "$build/build" && CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 cmake -DCMAKE_BUILD_TYPE=Release .. \
      && cmake --build . --target install -j"$(nproc)")
    install -m 755 "$build/build/telegram-bot-api" /usr/local/bin/telegram-bot-api
    sed "s#{{API_ID}}#$(env_get API_ID)#; s#{{API_HASH}}#$(env_get API_HASH)#; s#{{BOT_API_PORT}}#${port}#" \
      systemd/telegram-bot-api.service > /etc/systemd/system/telegram-bot-api.service
    systemctl daemon-reload
    systemctl enable --now telegram-bot-api
  else
    if ! command -v docker >/dev/null 2>&1; then
      warn "未检测到 Docker，正在安装（只用来跑 telegram-bot-api 一个容器）"
      curl -fsSL https://get.docker.com | sh
      systemctl enable --now docker
    fi
    docker run -d --name telegram-bot-api --restart unless-stopped \
      -p "127.0.0.1:${port}:8081" \
      -e TELEGRAM_API_ID="$(env_get API_ID)" \
      -e TELEGRAM_API_HASH="$(env_get API_HASH)" \
      -e TELEGRAM_LOCAL=true \
      -v tg-botapi-data:/var/lib/telegram-bot-api \
      aiogram/telegram-bot-api:latest >/dev/null
    log "telegram-bot-api 容器已启动，监听 127.0.0.1:${port}"
  fi
}

# 找一个 ≥3.11 的 Python（bot 代码用了 3.11 的特性）；没有就尝试装 python3.11
find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

ensure_venv() {
  local venv="$REPO_DIR/.venv" py
  if [[ -x "$venv/bin/python" && -x "$venv/bin/pip" ]] \
     && "$venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    log "沿用已有的 .venv（$("$venv/bin/python" --version)）"
    return 0
  fi
  if ! py="$(find_python)"; then
    log "系统 Python 低于 3.11，尝试安装 python3.11"
    pkg_install python3.11 python3.11-venv || pkg_install python3.11 || true
    py="$(find_python)" || die "需要 Python 3.11 及以上，当前系统装不上（$(python3 --version 2>&1 || echo 没有 python3)）。
请先手动安装 Python 3.11+，或改用 docker 模式部署（--mode docker）。"
  fi
  log "使用 $("$py" --version)（$py）"
  rm -rf "$venv"
  if ! "$py" -m venv "$venv" >/dev/null 2>&1; then
    # Debian/Ubuntu 的 python3 默认不带 venv/ensurepip，要单独装 pythonX.Y-venv
    local ver
    ver="$("$py" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    log "缺少 venv 模块，安装 python${ver}-venv"
    rm -rf "$venv"
    pkg_install "python${ver}-venv" || pkg_install python3-venv || true
    "$py" -m venv "$venv" || die "创建 Python 虚拟环境失败，请手动安装 python${ver}-venv 后重试"
  fi
}

bare_step_bot() {
  step "安装机器人（Python 虚拟环境 + systemd 服务）"
  ensure_venv
  "$REPO_DIR/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
  "$REPO_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r requirements.txt
  "$REPO_DIR/.venv/bin/python" -c 'import bot.main' \
    || die "机器人代码导入失败（见上方报错），通常是 .env 配置有误或依赖没装全"
  render_unit systemd/tg-aria2-bot.service > /etc/systemd/system/tg-aria2-bot.service
  systemctl daemon-reload
  systemctl enable tg-aria2-bot >/dev/null 2>&1
  systemctl restart tg-aria2-bot
  log "机器人服务已启动（tg-aria2-bot）"
}

bare_step_web() {
  step "安装 Web 管理后台"
  [[ -x "$REPO_DIR/.venv/bin/python" ]] || die "Python 虚拟环境还没建好，先完成机器人那一步（sudo $0 install）"
  local port bot_port
  port="$(env_get WEB_PORT)"; port="${port:-8080}"
  bot_port="$(env_get BOT_API_URL)"; bot_port="${bot_port##*:}"
  # 自己的旧实例先停掉，这样探测到的占用一定是别的程序
  systemctl stop tg-aria2-web 2>/dev/null || true
  if [[ "$port" == "$bot_port" ]] || port_in_use "$port"; then
    if [[ "$port" == "$bot_port" ]]; then
      warn "Web 后台端口 ${port} 跟 telegram-bot-api 相同"
    else
      warn "Web 后台端口 ${port} 已被其它程序占用：$(port_owner "$port")"
    fi
    local old="$port"
    port="$(find_free_port 8080 8099 "$old" "$bot_port")" \
      || die "8080-8099 全被占用，无法启动 Web 后台；释放端口或在 .env 里设 WEB_PORT"
    env_set WEB_PORT "$port"
    warn "Web 后台改用端口 ${port}（已写入 .env）"
  fi
  if [[ -z "$(env_get ADMIN_PASSWORD)" ]]; then
    local pw
    pw="$(rand_hex 12)"
    env_set ADMIN_PASSWORD "$pw"
    I_PASSWORD_GENERATED=1; I_ADMIN_PASSWORD="$pw"
  fi
  render_unit systemd/tg-aria2-web.service > /etc/systemd/system/tg-aria2-web.service
  systemctl daemon-reload
  systemctl enable tg-aria2-web >/dev/null 2>&1
  systemctl restart tg-aria2-web
  log "Web 后台已启动，监听 127.0.0.1:${port}"
}

# AriaNg 失败不影响主体功能（经常是访问不了 GitHub），只警告不中断安装
bare_step_ariang() {
  step "安装 AriaNg 面板（可选）"
  if [[ ! -f /opt/ariang/index.html ]]; then
    local tag
    tag="$(curl -fsSL -m 20 https://api.github.com/repos/mayswind/AriaNg/releases/latest \
      | grep -m1 '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')" || return 1
    [[ -n "$tag" ]] || return 1
    command -v unzip >/dev/null 2>&1 || pkg_install unzip || return 1
    mkdir -p /opt/ariang
    curl -fsSL -m 120 -o /tmp/ariang.zip \
      "https://github.com/mayswind/AriaNg/releases/download/${tag}/AriaNg-${tag}-AllInOne.zip" || return 1
    unzip -oq /tmp/ariang.zip -d /opt/ariang || return 1
    rm -f /tmp/ariang.zip
  fi
  install -m 644 systemd/tg-ariang.service /etc/systemd/system/tg-ariang.service || return 1
  systemctl daemon-reload || return 1
  systemctl enable --now tg-ariang >/dev/null 2>&1 || return 1
  log "AriaNg 已启动，监听 127.0.0.1:6880"
}

install_bare_mode() {
  bare_step_aria2
  [[ "$I_RCLONE" -eq 1 ]] && install_rclone
  bare_step_botapi
  bare_step_bot
  if [[ "$I_NO_WEB" -eq 0 ]]; then
    bare_step_web
    bare_step_ariang || warn "AriaNg 安装失败（多半是访问不了 GitHub），不影响机器人和 Web 后台；之后可以重试：sudo $0 web-install"
  fi
}

install_summary() {
  echo
  printf '%s========== 安装完成 ==========%s\n' "$C_BOLD$C_GREEN" "$C_RESET"
  show_status
  if [[ "$I_NO_WEB" -eq 0 ]]; then
    echo
    show_info_brief
    if [[ "$I_PASSWORD_GENERATED" -eq 1 ]]; then
      echo
      log "Web 后台密码已自动生成，只显示这一次，请立刻记下（也存在 .env 的 ADMIN_PASSWORD 里）："
      echo
      echo "    ${I_ADMIN_PASSWORD}"
    fi
  fi
  echo
  log "日常管理（状态/日志/重启/改配置/升级/备份）：sudo tg-aria2"
}

cmd_install() {
  LOG_TAG="install"
  parse_install_args "$@"
  require_root install || exit 1
  set -o errtrace
  trap on_install_error ERR
  install_prepare_env
  if [[ "$MODE" == "docker" ]]; then
    install_docker_mode
  else
    install_bare_mode
  fi
  trap - ERR
  install_shortcut quiet
  install_summary
}

# 只装 / 修复 Web 后台（之前装的时候中断了、或者当初用了 --no-web）
cmd_web_install() {
  LOG_TAG="install"
  require_root web-install || exit 1
  require_installed || exit 1
  set -o errtrace
  trap on_install_error ERR
  I_NO_WEB=0
  if [[ "$MODE" == "docker" ]]; then
    env_set COMPOSE_PROFILES web
    if [[ -z "$(env_get ADMIN_PASSWORD)" ]]; then
      I_ADMIN_PASSWORD="$(rand_hex 12)"; I_PASSWORD_GENERATED=1
      env_set ADMIN_PASSWORD "$I_ADMIN_PASSWORD"
    fi
    step "启动 Web 后台容器"
    compose up -d --build web ariang
  else
    bare_step_web
    bare_step_ariang || warn "AriaNg 安装失败（多半是访问不了 GitHub），Web 后台不受影响，之后可以再试一次"
  fi
  trap - ERR
  echo
  show_info_brief
  if [[ "$I_PASSWORD_GENERATED" -eq 1 ]]; then
    log "Web 后台密码已自动生成，只显示这一次：${I_ADMIN_PASSWORD}"
  fi
}

# =============================================================== 升级

U_YES=0; U_CHECK=0; U_BRANCH=""; U_RESET=0; U_FORCE=0; U_PULL_IMAGES=0
U_ROLLBACK=1; U_TARGET=""; U_BACKUP_ONLY=0
APPLY_FROM=""; BACKUP_DIR=""; IS_ROLLBACK=0; UPDATE_READY=0
BACKUP_KEEP=10
HEALTH_WAIT_SECONDS="${UPDATE_HEALTH_WAIT:-20}"

parse_update_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes) U_YES=1; shift ;;
      --check) U_CHECK=1; shift ;;
      --branch) U_BRANCH="$2"; shift 2 ;;
      --reset) U_RESET=1; shift ;;
      --force) U_FORCE=1; shift ;;
      --pull-images) U_PULL_IMAGES=1; shift ;;
      --no-rollback) U_ROLLBACK=0; shift ;;
      --to) U_TARGET="$2"; shift 2 ;;
      --backup-only) U_BACKUP_ONLY=1; shift ;;
      --dir) REPO_DIR="$(cd "$2" && pwd)"; shift 2 ;;
      # 内部用：第二阶段 / 回滚
      --_apply) APPLY_FROM="$2"; BACKUP_DIR="$3"; shift 3 ;;
      --_rollback) IS_ROLLBACK=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "未知参数: $1（$0 --help 查看用法）" ;;
    esac
  done
}

backup_db() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] || return 0
  # 运行中 + WAL 模式直接 cp 可能不一致，优先用 SQLite 在线备份
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$src" "$dst" && return 0
  fi
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$dst'" && return 0
  fi
  warn "宿主机没有 python3/sqlite3，数据库按文件直接复制（运行中可能不完全一致）"
  cp -p "$src" "$dst"
  [[ -f "${src}-wal" ]] && cp -p "${src}-wal" "${dst}-wal"
  return 0
}

prune_backups() {
  local dirs
  mapfile -t dirs < <(find "$REPO_DIR/backups" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
  local n=${#dirs[@]} i
  if (( n > BACKUP_KEEP )); then
    for (( i = 0; i < n - BACKUP_KEEP; i++ )); do rm -rf "${dirs[$i]}"; done
  fi
}

# make_backup <备注> —— 备份 .env / 数据库 / 当前版本号，目录写进 BACKUP_DIR
make_backup() {
  BACKUP_DIR="$REPO_DIR/backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$REPO_DIR/backups" "$BACKUP_DIR"
  [[ -f .env ]] && cp -p .env "$BACKUP_DIR/.env"
  backup_db "$(host_db_path)" "$BACKUP_DIR/tasks.db"
  git rev-parse HEAD > "$BACKUP_DIR/COMMIT" 2>/dev/null || true
  echo "$1" > "$BACKUP_DIR/NOTE"
  log "已备份 .env / 数据库 / 当前版本号到 ${BACKUP_DIR#"$REPO_DIR"/}"
}

restore_conf() {
  [[ -d "$BACKUP_DIR/conf" ]] || return 0
  (cd "$BACKUP_DIR/conf" && find . -type f -print0) | while IFS= read -r -d '' f; do
    cat "$BACKUP_DIR/conf/$f" > "$REPO_DIR/$f"   # 原地写回，保留属主/inode
  done
}

# 第一阶段：拉代码（备份 + 保留 aria2-config/ 的本地改动）
phase_fetch() {
  [[ -d .git ]] || die "$REPO_DIR 不是 git 仓库，升级依赖 git clone 方式的部署"
  U_BRANCH="${U_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
  [[ "$U_BRANCH" != "HEAD" ]] || die "当前处于 detached HEAD，请用 --branch 指定要跟踪的分支"

  local old new
  old="$(git rev-parse HEAD)"
  if [[ -n "$U_TARGET" ]]; then
    git rev-parse --quiet --verify "${U_TARGET}^{commit}" >/dev/null \
      || git fetch --quiet origin "$U_BRANCH" --tags \
      || die "git fetch 失败，检查网络或远端地址：$(git remote get-url origin)"
    new="$(git rev-parse --quiet --verify "${U_TARGET}^{commit}")" || die "找不到版本 ${U_TARGET}"
    U_RESET=1   # 回退不是快进，只能 reset
  else
    log "检查更新（origin/${U_BRANCH}）..."
    git fetch --quiet origin "$U_BRANCH" || die "git fetch 失败，检查网络或远端地址：$(git remote get-url origin)"
    new="$(git rev-parse "origin/${U_BRANCH}")"
  fi

  if [[ "$old" == "$new" ]]; then
    log "已经是最新版本（$(git log -1 --format='%h %s' HEAD)）"
    if [[ "$U_FORCE" -eq 0 || "$U_CHECK" -eq 1 ]]; then return 0; fi
    log "--force：没有新提交，照样重新应用一遍"
  elif [[ -n "$U_TARGET" ]] && git merge-base --is-ancestor "$new" "$old"; then
    echo
    warn "将回退到 $(git log -1 --format='%h %s (%cr)' "$new")，撤销以下 $(git rev-list --count "${new}..${old}") 个提交："
    git --no-pager log --format='  %C(yellow)%h%Creset %s %C(dim)(%cr)%Creset' "${new}..${old}" | head -30
    echo
  else
    if [[ -z "$U_TARGET" ]] && ! git merge-base --is-ancestor "$old" "$new"; then
      [[ "$U_RESET" -eq 1 ]] || die "本地分支和 origin/${U_BRANCH} 已经分叉（本地有远端没有的提交），无法快进。
确认本地提交可以丢弃的话加 --reset 强制对齐远端。"
      warn "本地分支和远端分叉，--reset 将丢弃本地提交"
    fi
    echo
    log "发现 $(git rev-list --count "${old}..${new}") 个新提交："
    git --no-pager log --format='  %C(yellow)%h%Creset %s %C(dim)(%cr)%Creset' "${old}..${new}" | head -30
    echo
    git diff --quiet "$old" "$new" -- requirements.txt || log "· 依赖有变化（requirements.txt），会自动重新安装"
    git diff --quiet "$old" "$new" -- .env.example || log "· .env.example 有新配置项，按需对照添加（git diff ${old:0:7} ${new:0:7} -- .env.example）"
    git diff --quiet "$old" "$new" -- aria2-config || warn "· aria2-config/ 模板有变化——本地配置会保留，需要的话手动对照：git diff ${old:0:7} ${new:0:7} -- aria2-config"
  fi

  if [[ "$U_CHECK" -eq 1 ]]; then
    log "（只检查，未做任何改动；升级请运行 sudo $0 update）"
    return 0
  fi
  if [[ "$U_YES" -eq 0 ]]; then
    confirm "确认执行？" || { log "已取消"; return 0; }
  fi

  make_backup "升级前自动备份（${old:0:7} → ${new:0:7}）"

  # aria2-config/ 是 git 跟踪的模板，但运行时会被改写（rpc-secret、rclone 钩子……），
  # 这些改动会让 git 拒绝更新。先存一份，更新完原样放回——用户配置优先，
  # 上游模板的改动只提示不自动合并
  local dirty_conf f
  mapfile -t dirty_conf < <(git diff --name-only HEAD -- aria2-config)
  if (( ${#dirty_conf[@]} > 0 )); then
    for f in "${dirty_conf[@]}"; do
      mkdir -p "$BACKUP_DIR/conf/$(dirname "$f")"
      cp -p "$f" "$BACKUP_DIR/conf/$f"
    done
    git checkout --quiet -- aria2-config
  fi

  if [[ "$U_RESET" -eq 1 ]]; then
    git reset --quiet --hard "$new"
  else
    if ! git diff --quiet HEAD; then
      restore_conf
      die "仓库里有未提交的本地改动（aria2-config/ 之外），为避免覆盖已中止：
$(git diff --name-only HEAD | sed 's/^/    /')
处理方式：git stash 暂存，或确认不要后加 --reset 重新运行。"
    fi
    git merge --quiet --ff-only "origin/${U_BRANCH}"
  fi
  restore_conf
  log "代码已更新到 $(git log -1 --format='%h %s' HEAD)"
  APPLY_FROM="$old"
  UPDATE_READY=1
}

# 把第二阶段交给仓库里（刚拉下来的那个版本的）脚本执行，这样新版本新增的
# 升级步骤第一次升级就能生效。老版本只有 update.sh 时交给它；都没有返回 1，
# 由调用方在当前进程里继续
handoff() {
  if [[ -f "$REPO_DIR/tg-aria2.sh" ]]; then
    exec bash "$REPO_DIR/tg-aria2.sh" update "$@"
  fi
  if [[ -f "$REPO_DIR/update.sh" ]] && grep -q -- '--_rollback' "$REPO_DIR/update.sh"; then
    exec bash "$REPO_DIR/update.sh" "$@"
  fi
  return 1
}

migrate_env() {
  if [[ "$MODE" == "docker" ]] && ! env_has COMPOSE_PROFILES; then
    if [[ -n "$(compose --profile web ps -a -q web 2>/dev/null)" ]]; then
      env_set COMPOSE_PROFILES web
    else
      env_set COMPOSE_PROFILES ""
    fi
    log ".env 补充 COMPOSE_PROFILES=$(env_get COMPOSE_PROFILES)"
  fi
}

apply_docker() {
  command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
  local dl
  dl="$(env_get HOST_DOWNLOAD_DIR)"
  chown -R 1000:1000 data .env aria2-config 2>/dev/null || true
  chown 1000:1000 "${dl:-./downloads}" 2>/dev/null || true
  if [[ "$U_PULL_IMAGES" -eq 1 ]]; then
    log "拉取第三方镜像新版本（telegram-bot-api / aria2）"
    compose pull --ignore-buildable --quiet || warn "部分镜像拉取失败，继续使用本地已有版本"
  fi
  log "重建并启动容器（docker compose up -d --build）"
  if [[ "$U_PULL_IMAGES" -eq 1 ]]; then
    compose build --pull --quiet || return 1
  else
    compose build --quiet || return 1
  fi
  compose up -d --remove-orphans || return 1
}

bare_units() {
  local u
  for u in tg-aria2-bot tg-aria2-web; do
    [[ -f "/etc/systemd/system/${u}.service" ]] && echo "$u"
  done
  return 0
}

apply_bare() {
  [[ -x .venv/bin/python ]] || die "找不到 .venv/bin/python，安装没完成？先运行 sudo $0 install"
  if [[ -z "$APPLY_FROM" ]] || ! git diff --quiet "$APPLY_FROM" HEAD -- requirements.txt || [[ "$U_FORCE" -eq 1 ]]; then
    log "安装 Python 依赖"
    .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt || return 1
  else
    log "requirements.txt 无变化，跳过依赖安装"
  fi
  # 重启前先校验新代码能 import，过不了直接回滚，服务一秒都不中断
  log "校验新代码（编译 + 导入）"
  .venv/bin/python -m compileall -q bot || return 1
  .venv/bin/python -c 'import bot.main, bot.web.app' || return 1

  # 只在这次升级里 systemd/ 模板确实变了才同步，且先备份——不能拿模板去比对
  # 已安装的文件，那样会覆盖用户手改过的单元
  local u changed=0
  if [[ -n "$APPLY_FROM" ]]; then
    for u in $(bare_units); do
      # 按单元分别判断：只有这个单元自己的模板变了才动它
      git diff --quiet "$APPLY_FROM" HEAD -- "systemd/${u}.service" && continue
      if ! render_unit "systemd/${u}.service" | cmp -s - "/etc/systemd/system/${u}.service"; then
        if [[ -n "$BACKUP_DIR" ]]; then
          mkdir -p "$BACKUP_DIR/units"
          cp -p "/etc/systemd/system/${u}.service" "$BACKUP_DIR/units/"
        fi
        render_unit "systemd/${u}.service" > "/etc/systemd/system/${u}.service" || return 1
        log "已更新 /etc/systemd/system/${u}.service（原文件备份在 ${BACKUP_DIR#"$REPO_DIR"/}/units/）"
        changed=1
      fi
    done
  fi
  if [[ "$changed" -eq 1 ]]; then systemctl daemon-reload || return 1; fi
  for u in $(bare_units); do
    log "重启 $u"
    systemctl restart "$u" || return 1
  done
}

health_docker() {
  local svc cid running restarts
  local -A before=()
  local services=(bot)
  [[ -n "$(compose ps -q web 2>/dev/null)" ]] && services+=(web)
  for svc in "${services[@]}"; do
    cid="$(compose ps -q "$svc")"
    [[ -n "$cid" ]] || { warn "容器 $svc 没有运行"; return 1; }
    before[$svc]="$(docker inspect -f '{{.RestartCount}}' "$cid")"
  done
  log "健康检查：观察 ${HEALTH_WAIT_SECONDS}s 内容器是否崩溃重启..."
  sleep "$HEALTH_WAIT_SECONDS"
  for svc in "${services[@]}"; do
    cid="$(compose ps -q "$svc")"
    running="$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || echo false)"
    restarts="$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 999)"
    if [[ "$running" != "true" || "$restarts" != "${before[$svc]}" ]]; then
      warn "容器 $svc 不健康（running=$running，重启次数 ${before[$svc]} → $restarts），最近日志："
      compose logs --tail 30 "$svc" || true
      return 1
    fi
  done
}

health_bare() {
  local u now
  local -A before=()
  for u in $(bare_units); do
    before[$u]="$(systemctl show -p NRestarts --value "$u")"
  done
  log "健康检查：观察 ${HEALTH_WAIT_SECONDS}s 内服务是否崩溃重启..."
  sleep "$HEALTH_WAIT_SECONDS"
  for u in $(bare_units); do
    now="$(systemctl show -p NRestarts --value "$u")"
    if ! systemctl is-active --quiet "$u" || [[ "$now" != "${before[$u]}" ]]; then
      warn "服务 $u 不健康（重启次数 ${before[$u]} → $now），最近日志："
      journalctl -u "$u" -n 30 --no-pager || true
      return 1
    fi
  done
}

rollback() {
  local reason="$1"
  [[ "$IS_ROLLBACK" -eq 0 ]] || die "回滚到旧版本后依然失败（$reason），需要人工处理。备份在 ${BACKUP_DIR}"
  if [[ "$U_ROLLBACK" -eq 0 || -z "$APPLY_FROM" ]]; then
    die "升级失败：$reason（未自动回滚，备份在 ${BACKUP_DIR:-无}）"
  fi
  warn "升级失败：$reason —— 自动回滚到 ${APPLY_FROM:0:7}"
  # 只回滚代码：数据库迁移只增不删，旧代码能直接用新 schema；.env 只会被
  # 补键，旧代码会忽略不认识的键
  [[ -d "${BACKUP_DIR:-}/conf" ]] && git checkout --quiet -- aria2-config
  git reset --quiet --hard "$APPLY_FROM"
  [[ -n "${BACKUP_DIR:-}" ]] && restore_conf
  if [[ -d "${BACKUP_DIR:-}/units" ]]; then
    cp -p "$BACKUP_DIR"/units/*.service /etc/systemd/system/
    systemctl daemon-reload
    log "已恢复升级前的 systemd 单元文件"
  fi
  # 用旧版本自己的脚本重新应用；旧版本连 update.sh 都没有时在当前进程里继续
  handoff --dir "$REPO_DIR" --_apply "" "${BACKUP_DIR:-}" --_rollback --force -y || true
  IS_ROLLBACK=1; APPLY_FROM=""; U_FORCE=1
  phase_apply
  exit 1
}

phase_apply() {
  migrate_env
  if [[ "$MODE" == "docker" ]]; then
    apply_docker || rollback "容器构建/启动失败"
    health_docker || rollback "容器启动后崩溃"
  else
    apply_bare || rollback "依赖安装或代码校验失败"
    health_bare || rollback "服务启动后崩溃"
  fi
  if [[ "$IS_ROLLBACK" -eq 1 ]]; then
    warn "已回滚到 $(git log -1 --format='%h %s' HEAD) 并恢复运行。失败原因见上方日志，备份在 ${BACKUP_DIR:-无}"
    exit 1
  fi
  prune_backups
  # 以前的快捷命令指向 manage.sh，合并成单脚本后改指向本脚本
  [[ -L "$SHORTCUT" ]] && ln -sf "$REPO_DIR/tg-aria2.sh" "$SHORTCUT"
  log "完成，当前版本：$(git log -1 --format='%h %s (%cr)' HEAD)"
}

cmd_update() {
  LOG_TAG="update"
  parse_update_args "$@"
  cd "$REPO_DIR"
  [[ "$EUID" -eq 0 || "$U_CHECK" -eq 1 ]] || die "请用 root 权限运行 (sudo $0 update)"
  [[ -f .env ]] || die "$REPO_DIR 下找不到 .env —— 还没安装过（sudo $0 install），或者 --dir 指错了"
  detect_mode
  [[ -n "$MODE" ]] || die "无法从 .env 的 BOT_API_URL 识别部署模式（docker/bare）"

  if [[ "$U_BACKUP_ONLY" -eq 1 ]]; then
    make_backup "手动备份"
    prune_backups
    return 0
  fi

  if [[ -z "$APPLY_FROM" && "$IS_ROLLBACK" -eq 0 ]]; then
    log "部署模式：$MODE，仓库：$REPO_DIR"
    phase_fetch
    [[ "$UPDATE_READY" -eq 1 ]] || return 0
    local args=(--dir "$REPO_DIR" --_apply "$APPLY_FROM" "$BACKUP_DIR" -y)
    [[ "$U_PULL_IMAGES" -eq 1 ]] && args+=(--pull-images)
    [[ "$U_ROLLBACK" -eq 0 ]] && args+=(--no-rollback)
    [[ "$U_FORCE" -eq 1 ]] && args+=(--force)
    handoff "${args[@]}" || true
  fi
  phase_apply
}

# =============================================================== 管理

mask() {
  if [[ -z "$1" ]]; then echo "${C_DIM}（未设置）${C_RESET}"; else echo "${1:0:3}******"; fi
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

# 改完配置后让它生效。docker 下 bot/web 直接读挂载的 .env，restart 即可；
# 端口这类 compose 层面的配置要 up -d 重建容器
apply_config() {
  local recreate="${1:-0}" s
  if ! confirm "现在重启 bot/web 让配置生效？"; then
    warn "已保存但未生效，之后选「重启服务」即可"
    return 0
  fi
  local -a targets=()
  for s in bot web; do
    [[ "$(svc_state "$s")" != "missing" ]] && targets+=("$s")
  done
  if [[ "$MODE" == "docker" && "$recreate" -eq 1 ]]; then
    compose up -d
  else
    svc_do restart "${targets[@]}"
  fi
  log "已重启：${targets[*]}"
}

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
    render_unit systemd/tg-aria2-web.service > /etc/systemd/system/tg-aria2-web.service
    systemctl daemon-reload
  fi
  log "Web 后台端口改为 ${new}，重启后生效；访问地址里的端口记得一起换"
}

config_menu() {
  require_installed || return 0
  require_root config || return 0
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
    read -rp "请选择: " choice || break
    case "$choice" in
      1) v="$(read_validated "新的白名单（Telegram 用户 ID，逗号分隔，留空=对所有人开放）: " '^[0-9, ]*$' "只能是数字和逗号")" || continue
         env_set ALLOWED_USER_IDS "${v// /}"; changed=1
         [[ -z "$v" ]] && warn "白名单为空：机器人对所有人开放，且管理功能会被锁定（除非设置了管理员）" ;;
      2) v="$(read_validated "管理员 ID（逗号分隔，留空=沿用白名单）: " '^[0-9, ]*$' "只能是数字和逗号")" || continue
         env_set ADMIN_USER_IDS "${v// /}"; changed=1 ;;
      3) read -rsp "新密码（留空自动生成随机密码）: " v || continue; echo
         if [[ -z "$v" ]]; then
           v="$(rand_hex 12)"
           printf '新密码：%s%s%s（请记下，只显示这一次）\n' "$C_BOLD" "$v" "$C_RESET"
         fi
         env_set ADMIN_PASSWORD "$v"
         # 删掉会话签名密钥，web 重启时重新生成——旧的登录会话全部失效
         rm -f data/web_session_secret "$(dirname "$(env_get DB_PATH)")/web_session_secret" 2>/dev/null || true
         changed=1 ;;
      4) v="$(read_validated "同时下载数（1-50）: " '^([1-9]|[1-4][0-9]|50)$' "请输入 1-50 的整数")" || continue
         env_set MAX_CONCURRENT "$v"; changed=1 ;;
      5) v="$(read_validated "磁盘剩余低于多少 GB 时告警（0=关闭）: " '^[0-9]+$' "请输入整数")" || continue
         env_set DISK_ALERT_THRESHOLD_GB "$v"; changed=1 ;;
      6) v="$(read_validated "已完成记录保留天数（0=不自动清理）: " '^[0-9]+$' "请输入整数")" || continue
         env_set AUTO_CLEANUP_DAYS "$v"; changed=1 ;;
      7) v="$(read_validated "代理地址（如 http://127.0.0.1:7890 或 socks5://...，留空=不用代理）: " '^((https?|socks5h?)://[^ ]+)?$' "格式不对，需要 http:// https:// socks5:// 开头")" || continue
         env_set PROXY_URL "$v"; changed=1 ;;
      8) [[ "$MODE" == "docker" ]] || continue
         echo "  1) 127.0.0.1 只监听本机（推荐，配合 SSH 隧道/反向代理）"
         echo "  2) 0.0.0.0   对公网开放（明文 HTTP）"
         read -rp "请选择: " v || continue
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

# 访问地址（不问密码），安装总结和 info 共用
show_info_brief() {
  local port bind ip
  port="$(env_get WEB_PORT)"; port="${port:-8080}"
  if [[ "$(svc_state web)" == "missing" ]]; then
    warn "Web 管理后台未安装。安装 / 修复：sudo $0 web-install"
    return 0
  fi
  if [[ "$MODE" == "docker" ]]; then
    bind="$(env_get WEB_BIND)"; bind="${bind:-0.0.0.0}"
  else
    bind=127.0.0.1
  fi
  if [[ "$bind" == "0.0.0.0" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "Web 管理后台: http://${ip:-<服务器IP>}:${port}"
    [[ "$(svc_state ariang)" == missing ]] || echo "AriaNg:       http://${ip:-<服务器IP>}:6880"
    warn "端口对公网开放且是明文 HTTP，建议在「修改常用配置」里改成只监听本机"
  else
    echo "Web 管理后台: http://127.0.0.1:${port}（仅本机）"
    [[ "$(svc_state ariang)" == missing ]] || echo "AriaNg:       http://127.0.0.1:6880（仅本机）"
    echo "远程访问：ssh -L ${port}:localhost:${port} -L 6880:localhost:6880 root@<服务器IP>，然后浏览器打开上面的地址"
  fi
  [[ "$(svc_state ariang)" == missing ]] || echo "AriaNg 需要填的 RPC 密钥 = .env 里的 ARIA2_SECRET"
}

show_info() {
  require_installed || return 0
  echo
  show_info_brief
  [[ "$(svc_state web)" == "missing" ]] && return 0
  if [[ -z "$(env_get ADMIN_PASSWORD)" ]]; then
    warn "ADMIN_PASSWORD 为空，Web 后台无法登录——在「修改常用配置」里设置"
  elif [[ -t 0 ]] && confirm "显示 Web 后台密码？"; then
    echo "密码：$(env_get ADMIN_PASSWORD)"
  fi
  return 0
}

pick_backup() {
  local -a dirs
  mapfile -t dirs < <(find backups -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort -r)
  (( ${#dirs[@]} > 0 )) || { err "还没有备份（backups/ 为空）"; return 1; }
  echo >&2
  local i=1 d note commit choice
  for d in "${dirs[@]}"; do
    note="$(cat "$d/NOTE" 2>/dev/null || echo "升级前自动备份")"
    commit="$(cut -c1-7 "$d/COMMIT" 2>/dev/null || echo "?")"
    printf '  %s%2d.%s %s  版本 %s  %s%s%s\n' "$C_CYAN" "$i" "$C_RESET" "${d#backups/}" "$commit" "$C_DIM" "$note" "$C_RESET" >&2
    i=$((i + 1))
  done
  printf '  %s%2d.%s 返回\n' "$C_CYAN" 0 "$C_RESET" >&2
  read -rp "请选择: " choice || return 1
  [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#dirs[@]} )) || return 1
  echo "${dirs[$((choice - 1))]}"
}

do_backup() {
  require_installed || return 0
  require_root backup || return 0
  make_backup "手动备份"
  prune_backups
}

do_restore() {
  require_installed || return 0
  require_root restore || return 0
  local b has_env=0 has_db=0 s db
  b="$(pick_backup)" || return 0
  [[ -f "$b/.env" ]] && has_env=1
  [[ -f "$b/tasks.db" ]] && has_db=1
  echo
  echo "将从 ${b#backups/} 恢复：$([[ $has_env -eq 1 ]] && echo ".env ")$([[ $has_db -eq 1 ]] && echo "数据库")"
  warn "当前的 .env 和数据库会先自动备份一份，然后被覆盖；bot/web 会短暂停止"
  confirm "确认恢复？" || return 0

  make_backup "恢复前自动备份"
  local -a running=()
  for s in bot web; do
    [[ "$(svc_state "$s")" == "running" ]] && running+=("$s")
  done
  svc_do stop "${running[@]}"
  if [[ $has_env -eq 1 ]]; then
    cat "$b/.env" > .env   # 原地写回，保留属主和 bind mount 的 inode
    log "已恢复 .env"
  fi
  if [[ $has_db -eq 1 ]]; then
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
  require_root rollback || return 0
  [[ -d .git ]] || { err "非 git 部署，无法回退版本"; return 0; }
  echo
  echo "最近的版本（当前版本标 *）："
  local -a commits
  mapfile -t commits < <(git log -15 --format='%H')
  local i=1 c choice ref
  for c in "${commits[@]}"; do
    printf '  %s%2d.%s %s %s\n' "$C_CYAN" "$i" "$C_RESET" "$([[ $i -eq 1 ]] && echo '*' || echo ' ')" \
      "$(git log -1 --format='%h %s %C(dim)(%cd)%Creset' --date=format:'%Y-%m-%d' --color=always "$c")"
    i=$((i + 1))
  done
  echo "  也可以直接输入版本号（提交 hash / 标签），0 返回"
  read -rp "回退到: " choice || return 0
  [[ -n "$choice" && "$choice" != "0" ]] || return 0
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#commits[@]} )); then
    ref="${commits[$((choice - 1))]}"
  else
    ref="$choice"
  fi
  # 子进程跑：升级流程里会 exec / exit，不能把菜单进程带走
  bash "$SCRIPT_PATH" update --to "$ref" || true
  warn "注意：之后再执行「升级」会重新升级到最新版本"
}

install_shortcut() {
  require_root || return 0
  ln -sf "$REPO_DIR/tg-aria2.sh" "$SHORTCUT"
  [[ "${1:-}" == quiet ]] || log "已安装快捷命令：以后在任何目录运行 ${C_BOLD}sudo tg-aria2${C_RESET} 即可打开本菜单"
}

pick_service() {
  local with_all="${1:-0}" i=1 choice s
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
  read -rp "请选择: " choice || return 1
  [[ "$choice" =~ ^[0-9]+$ ]] || return 1
  (( choice == 0 )) && return 1
  if [[ "$with_all" -eq 1 && "$choice" -eq "$i" ]]; then echo all; return 0; fi
  (( choice >= 1 && choice <= ${#list[@]} )) || return 1
  echo "${list[$((choice - 1))]}"
}

restart_menu() {
  require_installed || return 0
  require_root restart || return 0
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

start_stop_menu() {
  require_installed || return 0
  require_root || return 0
  echo "  1) 启动全部服务"
  echo "  2) 停止全部服务"
  local c
  read -rp "请选择: " c || return 0
  local -a list
  mapfile -t list < <(installed_services)
  case "$c" in
    1) if [[ "$MODE" == "docker" ]]; then compose up -d; else svc_do start "${list[@]}"; fi
       log "已启动" ;;
    2) confirm "确认停止全部服务？下载会中断（aria2 会话会保存，启动后继续）" || return 0
       svc_do stop "${list[@]}"
       log "已停止" ;;
  esac
}

version_line() {
  if [[ -d .git ]]; then
    git log -1 --format="%h（%cd）" --date=format:'%Y-%m-%d' 2>/dev/null || echo "未知"
  else
    echo "非 git 部署"
  fi
}

show_header() {
  clear 2>/dev/null || true
  printf '%s========== tg-aria2-bot 管理菜单 ==========%s\n' "$C_BOLD" "$C_RESET"
  if [[ -z "$MODE" ]]; then
    printf ' 状态: %s未安装%s —— 选 12 开始安装\n' "$C_YELLOW" "$C_RESET"
    return
  fi
  printf ' 部署模式: %s%s%s    版本: %s\n' "$C_CYAN" "$MODE" "$C_RESET" "$(version_line)"
  local s line="" missing=0
  for s in "${ALL_SERVICES[@]}"; do
    local st
    st="$(svc_state "$s")"
    [[ "$st" == missing && ( "$s" == bot || "$s" == aria2 || "$s" == telegram-bot-api ) ]] && missing=1
    line+="$(printf '%s %s   ' "$s" "$(state_label "$st")")"
  done
  printf ' %s\n' "$line"
  [[ "$missing" -eq 1 ]] && printf ' %s有核心服务未安装，可能上次安装中途失败了 —— 选 12 修复安装%s\n' "$C_YELLOW" "$C_RESET"
  local dl
  dl="$(host_download_dir)"
  if [[ -n "$dl" && -d "$dl" ]]; then
    printf ' 下载目录: %s  剩余 %s\n' "$dl" "$(df -h --output=avail "$dl" 2>/dev/null | tail -1 | tr -d ' ')"
  fi
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
  ${C_CYAN} 5.${C_RESET} 修改常用配置（白名单/管理员/密码/并发/代理/端口…）
  ${C_CYAN} 6.${C_RESET} Web 后台访问信息
 ${C_DIM}—— 升级与备份 ——${C_RESET}
  ${C_CYAN} 7.${C_RESET} 检查更新
  ${C_CYAN} 8.${C_RESET} 升级到最新版本
  ${C_CYAN} 9.${C_RESET} 立即备份
  ${C_CYAN}10.${C_RESET} 从备份恢复（.env / 数据库）
  ${C_CYAN}11.${C_RESET} 回退到历史版本
 ${C_DIM}—— 安装 ——${C_RESET}
  ${C_CYAN}12.${C_RESET} 安装 / 修复安装
  ${C_CYAN}13.${C_RESET} 安装 / 修复 Web 管理后台
  ${C_CYAN}14.${C_RESET} 安装快捷命令 tg-aria2
  ${C_CYAN} 0.${C_RESET} 退出
EOF
    local choice
    read -rp "请输入数字: " choice || exit 0
    echo
    # 安装/升级这些会 exit 或 exec 的操作放子进程里跑，跑完回到菜单
    case "$choice" in
      1) show_status; pause ;;
      2) require_installed && pick_svc_logs; true ;;
      3) restart_menu; pause ;;
      4) start_stop_menu; pause ;;
      5) config_menu; pause ;;
      6) show_info; pause ;;
      7) require_installed && { bash "$SCRIPT_PATH" check || true; }; pause ;;
      8) require_installed && require_root update && { bash "$SCRIPT_PATH" update || true; }; pause ;;
      9) do_backup; pause ;;
      10) do_restore; pause ;;
      11) do_rollback; pause ;;
      12) require_root install && { bash "$SCRIPT_PATH" install || true; }; pause ;;
      13) require_root web-install && { bash "$SCRIPT_PATH" web-install || true; }; pause ;;
      14) install_shortcut; pause ;;
      0|q|exit) exit 0 ;;
      *) err "无效选择"; sleep 1 ;;
    esac
  done
}

pick_svc_logs() {
  local s
  s="$(pick_service 0)" || return 0
  svc_logs "$s"
}

usage() {
  sed -n '2,/^$/p' "$SCRIPT_PATH" | grep '^#' | sed 's/^# \{0,1\}//'
}

# =============================================================== main

main() {
  local cmd="${1:-menu}"
  [[ $# -gt 0 ]] && shift
  # update 可能带 --dir（CI 从临时文件运行），由 cmd_update 自己 cd
  [[ "$cmd" == update || "$cmd" == upgrade ]] || cd "$REPO_DIR"
  [[ "$cmd" == update || "$cmd" == upgrade ]] || detect_mode
  case "$cmd" in
    menu)
      [[ -t 0 ]] || die "菜单需要交互式终端；非交互场景请用子命令（$0 --help）"
      main_menu ;;
    install) cmd_install "$@" ;;
    web-install) cmd_web_install ;;
    update|upgrade) cmd_update "$@" ;;
    check) cmd_update --check "$@" ;;
    status) show_status ;;
    logs) require_installed || exit 1; svc_logs "${1:-bot}" ;;
    restart)
      require_installed && require_root restart || exit 1
      if [[ "${1:-all}" == "all" ]]; then
        mapfile -t list < <(installed_services); svc_do restart "${list[@]}"
      else
        svc_do restart "$@"
      fi ;;
    start)
      require_installed && require_root start || exit 1
      if [[ "$MODE" == "docker" ]]; then compose up -d
      else mapfile -t list < <(installed_services); svc_do start "${list[@]}"; fi ;;
    stop)
      require_installed && require_root stop || exit 1
      mapfile -t list < <(installed_services); svc_do stop "${list[@]}" ;;
    config) config_menu ;;
    info) show_info ;;
    backup) do_backup ;;
    restore) do_restore ;;
    rollback) do_rollback ;;
    -h|--help|help) usage ;;
    *) die "未知命令: $cmd（$0 --help 查看用法）" ;;
  esac
}

# 整个脚本包在函数里、最后一行才调用：bash 边读边执行脚本文件，升级过程中
# git 会替换掉本文件，先完整解析进内存才安全
main "$@"
