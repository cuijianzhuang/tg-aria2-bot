#!/usr/bin/env bash
# Bare-metal deployment:
#   - aria2 installed on the host via the official P3TERX/aria2.sh one-click script
#     (installs the "aria2.conf perfect config" + hook scripts + tracker updater)
#   - bot runs in a Python venv as a systemd service
#   - telegram-bot-api: building it from source (tdlib + gperf + cmake) takes 20-40 min
#     and a few GB of RAM. Default here is a lightweight *hybrid*: run only the
#     telegram-bot-api container via plain `docker run` (no compose, no other
#     containers), everything else stays bare metal. Pass --build-botapi-from-source
#     to compile it natively instead and skip Docker entirely.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

BUILD_FROM_SOURCE=0
WITH_RCLONE=0
NO_WEB=0
for arg in "$@"; do
  [[ "$arg" == "--build-botapi-from-source" ]] && BUILD_FROM_SOURCE=1
  [[ "$arg" == "--with-rclone" ]] && WITH_RCLONE=1
  [[ "$arg" == "--no-web" ]] && NO_WEB=1
done

log()  { printf '\033[1;32m[bare]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$1" >&2; exit 1; }

# ---------- 1. aria2 via P3TERX/aria2.sh ----------
# aria2.sh 是一个纯交互式数字菜单脚本（没有非交互 flag），"1" 对应菜单里的
# "安装 Aria2"。安装流程本身（装依赖、下载二进制、下载完美配置、注册 init.d 服务）
# 全程无需额外输入，但注意它会自动执行 Set_iptables/Add_iptables：往 iptables 插入
# 放行 RPC/BT/DHT 端口的规则并持久化（Debian 写 /etc/iptables.up.rules +
# if-pre-up.d 钩子；CentOS 用 service iptables save）。如果你用 ufw/firewalld/云
# 安全组管理防火墙，装完后检查一下是否有冲突或冗余规则。
if command -v aria2c >/dev/null 2>&1; then
  log "aria2c 已安装: $(aria2c --version | head -1)"
else
  log "通过 aria2.sh 安装 aria2 + 完美配置 (含 tracker 自动更新、下载完成钩子)"
  warn "该脚本会自动修改并持久化 iptables 规则以放行 RPC/BT/DHT 端口"
  # 优先用仓库里 vendor/aria2.sh/aria2.sh 这份逐字复刻的原版脚本（离线可用、可审计、
  # 不受上游改动影响）；只有 vendor 目录缺失时才回退到联网拉取最新版。
  if [[ -f "$SCRIPT_DIR/vendor/aria2.sh/aria2.sh" ]]; then
    cp "$SCRIPT_DIR/vendor/aria2.sh/aria2.sh" /tmp/aria2.sh
  else
    warn "vendor/aria2.sh/aria2.sh 缺失，回退到联网拉取"
    curl -fsSL https://raw.githubusercontent.com/P3TERX/aria2.sh/master/aria2.sh -o /tmp/aria2.sh
  fi
  chmod +x /tmp/aria2.sh
  printf '1\n' | bash /tmp/aria2.sh   # 选择菜单选项 "1. 安装 Aria2"
  command -v aria2c >/dev/null 2>&1 || die "aria2 安装失败，请查看上面的输出定位问题"
fi

ARIA2_CONF_DIR="/root/.aria2c"
ARIA2_RPC_SECRET_LINE="$(grep -oP '(?<=rpc-secret=).*' "$ARIA2_CONF_DIR/aria2.conf" 2>/dev/null || true)"
if [[ -n "$ARIA2_RPC_SECRET_LINE" ]]; then
  log "检测到 aria2.sh 已生成的 RPC secret，同步到 .env"
  sed -i "s#ARIA2_SECRET=.*#ARIA2_SECRET=${ARIA2_RPC_SECRET_LINE}#" .env
fi
log "move.sh / upload.sh 默认未接入 aria2 钩子（on-download-complete 只调用 clean.sh），不会自动生效，无需额外操作"
systemctl enable --now aria2 2>/dev/null || true

# ---------- 1b. rclone (可选，仅在 --with-rclone 时安装，逻辑与 docker 模式共用) ----------
if [[ "$WITH_RCLONE" -eq 1 ]]; then
  bash "$SCRIPT_DIR/scripts/install_rclone.sh"
  log "aria2.sh 安装时已自带下载 ${ARIA2_CONF_DIR}/rclone.env 模板，按需编辑"
fi

# ---------- 2. telegram-bot-api ----------
if [[ "$BUILD_FROM_SOURCE" -eq 1 ]]; then
  log "从源码编译 telegram-bot-api（需要 20-40 分钟，2GB+ 内存）"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y make git zlib1g-dev libssl-dev gperf cmake g++ clang-14 libc++-dev libc++abi-dev
  else
    warn "非 apt 系统，请参考 https://github.com/tdlib/telegram-bot-api 手动装编译依赖"
  fi
  BUILD_DIR="/opt/telegram-bot-api-src"
  if [[ ! -d "$BUILD_DIR" ]]; then
    git clone --recursive https://github.com/tdlib/telegram-bot-api.git "$BUILD_DIR"
  fi
  mkdir -p "$BUILD_DIR/build"
  (
    cd "$BUILD_DIR/build"
    CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 cmake -DCMAKE_BUILD_TYPE=Release ..
    cmake --build . --target install -j"$(nproc)"
  )
  install -m 755 "$BUILD_DIR/build/telegram-bot-api" /usr/local/bin/telegram-bot-api
  log "编译完成: $(telegram-bot-api --version 2>&1 | head -1 || echo installed)"

  install -m 644 systemd/telegram-bot-api.service /etc/systemd/system/telegram-bot-api.service
  # shellcheck disable=SC1091
  source .env
  sed -i "s#{{API_ID}}#${API_ID}#; s#{{API_HASH}}#${API_HASH}#" /etc/systemd/system/telegram-bot-api.service
  systemctl daemon-reload
  systemctl enable --now telegram-bot-api
else
  log "使用轻量混合模式：仅用 docker 跑 telegram-bot-api 容器（其余全部裸机）"
  if ! command -v docker >/dev/null 2>&1; then
    warn "未检测到 Docker，正在安装（仅用于 telegram-bot-api 一个容器）"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  fi
  # shellcheck disable=SC1091
  source .env
  docker rm -f telegram-bot-api >/dev/null 2>&1 || true
  docker run -d --name telegram-bot-api --restart unless-stopped \
    -p 127.0.0.1:8081:8081 \
    -e TELEGRAM_API_ID="${API_ID}" \
    -e TELEGRAM_API_HASH="${API_HASH}" \
    -e TELEGRAM_LOCAL=true \
    -v tg-botapi-data:/var/lib/telegram-bot-api \
    aiogram/telegram-bot-api:latest
  log "telegram-bot-api 容器已启动，监听 127.0.0.1:8081"
fi

# ---------- 3. bot: python venv + systemd ----------
log "创建 Python venv 并安装依赖"
if ! command -v python3 >/dev/null 2>&1; then
  apt-get update -y && apt-get install -y python3 python3-venv python3-pip
fi
python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip -q
"$SCRIPT_DIR/.venv/bin/pip" install -r requirements.txt -q

install -m 644 systemd/tg-aria2-bot.service /etc/systemd/system/tg-aria2-bot.service
sed -i "s#{{WORKDIR}}#${SCRIPT_DIR}#g" /etc/systemd/system/tg-aria2-bot.service
systemctl daemon-reload
systemctl enable --now tg-aria2-bot

log "机器人已作为 systemd 服务启动。"

# ---------- 4. web 管理后台 + AriaNg（可选，--no-web 时跳过） ----------
if [[ "$NO_WEB" -eq 0 ]]; then
  # shellcheck disable=SC1091
  source .env
  WEB_PORT_VALUE="${WEB_PORT:-8080}"

  log "注册 web 管理后台 systemd 服务 (监听 127.0.0.1:${WEB_PORT_VALUE})"
  install -m 644 systemd/tg-aria2-web.service /etc/systemd/system/tg-aria2-web.service
  sed -i "s#{{WORKDIR}}#${SCRIPT_DIR}#g; s#{{WEB_PORT}}#${WEB_PORT_VALUE}#g" /etc/systemd/system/tg-aria2-web.service
  systemctl daemon-reload
  systemctl enable --now tg-aria2-web

  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    warn "ADMIN_PASSWORD 为空，web 管理后台已启动但登录会被拒绝（返回 503），编辑 .env 设置密码后 systemctl restart tg-aria2-web"
  fi

  log "下载并部署 AriaNg 静态页面到 /opt/ariang（通过 python http.server 提供，监听 127.0.0.1:6880）"
  if [[ ! -f /opt/ariang/index.html ]]; then
    TAG=$(curl -fsSL https://api.github.com/repos/mayswind/AriaNg/releases/latest | grep -m1 '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    mkdir -p /opt/ariang
    curl -fsSL -o /tmp/ariang.zip "https://github.com/mayswind/AriaNg/releases/download/${TAG}/AriaNg-${TAG}-AllInOne.zip"
    if command -v unzip >/dev/null 2>&1; then
      unzip -oq /tmp/ariang.zip -d /opt/ariang
    else
      apt-get install -y unzip 2>/dev/null || yum install -y unzip
      unzip -oq /tmp/ariang.zip -d /opt/ariang
    fi
    rm -f /tmp/ariang.zip
  fi
  install -m 644 systemd/tg-ariang.service /etc/systemd/system/tg-ariang.service
  systemctl daemon-reload
  systemctl enable --now tg-ariang
fi

cat <<EOF

常用命令：
  systemctl status tg-aria2-bot        查看机器人状态
  journalctl -u tg-aria2-bot -f        查看机器人日志
  systemctl status aria2               查看 aria2 状态
  aria2p -p 6800 --secret \$(grep rpc-secret ${ARIA2_CONF_DIR}/aria2.conf | cut -d= -f2)  # 可选 CLI 查看任务
EOF

if [[ "$NO_WEB" -eq 0 ]]; then
  cat <<'EOF'

Web 管理后台: http://127.0.0.1:8080  (仅监听本机，远程访问需要 SSH 隧道或反向代理+TLS)
AriaNg:       http://127.0.0.1:6880  (首次打开需要手动填 RPC 地址/密钥，之后记在浏览器本地)
EOF
fi
