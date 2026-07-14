#!/usr/bin/env bash
#
# tg-aria2-bot one-click installer
# Supports two deployment modes:
#   --mode docker  : telegram-bot-api + aria2 + bot all containerized (docker compose)
#   --mode bare     : aria2 installed via P3TERX/aria2.sh on the host, bot runs as a
#                      systemd service in a venv. telegram-bot-api still needs a
#                      running instance; see scripts/install_bare.sh for the two options.
#
# Usage:
#   sudo ./install.sh --mode docker --token 123:ABC --api-id 12345 --api-hash xxxx --allowed-ids 111,222
#   sudo ./install.sh --mode bare   --token 123:ABC --api-id 12345 --api-hash xxxx --allowed-ids 111,222
#
# Extra flags:
#   --with-rclone         install rclone on the host (see README, off by default)
#   --admin-password PW   set the web admin password (auto-generated + printed once if omitted)
#   --no-web               skip the web admin entirely (AriaNg + custom backend both off)
#
# Any flag omitted is asked for interactively. Re-run any time to update .env in place.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE=""
BOT_TOKEN=""
API_ID=""
API_HASH=""
ALLOWED_IDS=""
DOWNLOAD_DIR="./downloads"
WITH_RCLONE=0
ADMIN_PASSWORD=""
NO_WEB=0

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --token) BOT_TOKEN="$2"; shift 2 ;;
    --api-id) API_ID="$2"; shift 2 ;;
    --api-hash) API_HASH="$2"; shift 2 ;;
    --allowed-ids) ALLOWED_IDS="$2"; shift 2 ;;
    --download-dir) DOWNLOAD_DIR="$2"; shift 2 ;;
    --with-rclone) WITH_RCLONE=1; shift ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --no-web) NO_WEB=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^#//'; exit 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  die "请用 root 权限运行 (sudo ./install.sh ...)"
fi

# ---- interactive fallback for anything not passed as a flag ----
if [[ -z "$MODE" ]]; then
  echo "选择部署方式:"
  echo "  1) docker  (推荐，三服务全容器化)"
  echo "  2) bare    (裸机，aria2 用 aria2.sh 装在宿主机)"
  read -rp "输入 1 或 2: " choice
  case "$choice" in
    1) MODE="docker" ;;
    2) MODE="bare" ;;
    *) die "无效选择" ;;
  esac
fi
[[ "$MODE" == "docker" || "$MODE" == "bare" ]] || die "--mode 必须是 docker 或 bare"

[[ -z "$BOT_TOKEN" ]]    && read -rp "Bot Token (来自 @BotFather): " BOT_TOKEN
[[ -z "$API_ID" ]]       && read -rp "API ID (来自 my.telegram.org): " API_ID
[[ -z "$API_HASH" ]]     && read -rp "API Hash (来自 my.telegram.org): " API_HASH
[[ -z "$ALLOWED_IDS" ]]  && read -rp "允许使用的用户 Telegram ID，逗号分隔: " ALLOWED_IDS

[[ -n "$BOT_TOKEN" ]] || die "Bot Token 不能为空"
[[ -n "$API_ID" ]]    || die "API ID 不能为空"
[[ -n "$API_HASH" ]]  || die "API Hash 不能为空"

# ---- idempotency guard: re-running this script must never silently rotate
# secrets or switch an existing deployment's mode out from under it. Both of
# those have actually happened (rotated ADMIN_PASSWORD/ARIA2_SECRET on rerun;
# switching --mode overwrote a live docker deployment's .env with bare-mode
# host paths) — so pull forward whatever already exists in .env first.
EXISTING_ARIA2_SECRET=""
EXISTING_ADMIN_PASSWORD=""
EXISTING_MODE=""
if [[ -f .env ]]; then
  EXISTING_ARIA2_SECRET="$(grep -m1 '^ARIA2_SECRET=' .env | cut -d= -f2- || true)"
  EXISTING_ADMIN_PASSWORD="$(grep -m1 '^ADMIN_PASSWORD=' .env | cut -d= -f2- || true)"
  # docker mode always sets BOT_API_URL to the compose service name; bare mode
  # rewrites it to 127.0.0.1. Use that as the fingerprint of the deployed mode.
  EXISTING_BOT_API_URL="$(grep -m1 '^BOT_API_URL=' .env | cut -d= -f2- || true)"
  [[ "$EXISTING_BOT_API_URL" == "http://telegram-bot-api:8081" ]] && EXISTING_MODE="docker"
  [[ "$EXISTING_BOT_API_URL" == "http://127.0.0.1:8081" ]] && EXISTING_MODE="bare"
fi

if [[ -n "$EXISTING_MODE" && "$EXISTING_MODE" != "$MODE" ]]; then
  die "检测到当前 .env 是 ${EXISTING_MODE} 模式的部署，你这次选的是 ${MODE} 模式。
在同一台机器上切换模式会互相覆盖 .env 和端口/路径配置，导致已运行的服务崩掉。
如果你确实要推倒重来，先手动删除或备份 .env 再重新运行本脚本。"
fi

if [[ -n "$EXISTING_ARIA2_SECRET" ]]; then
  ARIA2_SECRET="$EXISTING_ARIA2_SECRET"
  log "沿用 .env 中已有的 ARIA2_SECRET（未重新生成）"
else
  ARIA2_SECRET="$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | xxd -p)"
fi

ADMIN_PASSWORD_GENERATED=0
if [[ "$NO_WEB" -eq 1 ]]; then
  ADMIN_PASSWORD=""
elif [[ -z "$ADMIN_PASSWORD" && -n "$EXISTING_ADMIN_PASSWORD" ]]; then
  ADMIN_PASSWORD="$EXISTING_ADMIN_PASSWORD"
  log "沿用 .env 中已有的 ADMIN_PASSWORD（未重新生成）"
elif [[ -z "$ADMIN_PASSWORD" ]]; then
  ADMIN_PASSWORD="$(openssl rand -hex 12 2>/dev/null || head -c12 /dev/urandom | xxd -p)"
  ADMIN_PASSWORD_GENERATED=1
fi

mkdir -p "$DOWNLOAD_DIR" data aria2-config

# ---- write .env (idempotent, always regenerated from current answers) ----
cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
API_ID=${API_ID}
API_HASH=${API_HASH}
BOT_API_URL=http://telegram-bot-api:8081

ARIA2_RPC=http://aria2:6800/jsonrpc
ARIA2_SECRET=${ARIA2_SECRET}

ALLOWED_USER_IDS=${ALLOWED_IDS}
DOWNLOAD_DIR=/downloads
MAX_FILE_SIZE=2147483648
MAX_CONCURRENT=3
PROXY_URL=
DB_PATH=/app/data/tasks.db

ADMIN_PASSWORD=${ADMIN_PASSWORD}
WEB_PORT=8080
EOF
log ".env 已生成 (aria2 密钥已自动生成)"

# docker 模式下 aria2-config/ 已预置真实的 P3TERX/aria2.conf 文件（离线可用），
# 只需要把生成的密钥写进占位符；bare 模式走 aria2.sh 自己的安装流程，不涉及这份预置文件。
if [[ "$MODE" == "docker" && -f aria2-config/aria2.conf ]]; then
  sed -i "s#__ARIA2_SECRET_PLACEHOLDER__#${ARIA2_SECRET}#" aria2-config/aria2.conf
  log "已将 aria2 RPC 密钥写入 aria2-config/aria2.conf"
fi

EXTRA_FLAGS=()
[[ "$WITH_RCLONE" -eq 1 ]] && EXTRA_FLAGS+=(--with-rclone)
[[ "$NO_WEB" -eq 1 ]]      && EXTRA_FLAGS+=(--no-web)

if [[ "$MODE" == "docker" ]]; then
  log "部署方式: docker"
  bash scripts/install_docker.sh "${EXTRA_FLAGS[@]}"
else
  log "部署方式: bare metal"
  # bare mode still needs http://127.0.0.1:8081 style URL (no compose network)
  sed -i 's#BOT_API_URL=.*#BOT_API_URL=http://127.0.0.1:8081#' .env
  sed -i 's#ARIA2_RPC=.*#ARIA2_RPC=http://127.0.0.1:6800/jsonrpc#' .env
  sed -i "s#DOWNLOAD_DIR=.*#DOWNLOAD_DIR=$(realpath "$DOWNLOAD_DIR")#" .env
  sed -i "s#DB_PATH=.*#DB_PATH=$(realpath data)/tasks.db#" .env
  bash scripts/install_bare.sh "${EXTRA_FLAGS[@]}"
fi

if [[ "$NO_WEB" -eq 1 ]]; then
  log "已跳过 Web 管理后台 (--no-web)"
elif [[ "$ADMIN_PASSWORD_GENERATED" -eq 1 ]]; then
  log "Web 管理后台密码已自动生成，只显示这一次，请立刻记下（也存在 .env 的 ADMIN_PASSWORD 里）："
  echo
  echo "    ${ADMIN_PASSWORD}"
  echo
fi

log "完成。"
