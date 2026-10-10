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

# shellcheck source=scripts/env_lib.sh
source "$SCRIPT_DIR/scripts/env_lib.sh"

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
  env_set ARIA2_SECRET "$ARIA2_RPC_SECRET_LINE"
fi
log "move.sh / upload.sh 默认未接入 aria2 钩子（on-download-complete 只调用 clean.sh），不会自动生效，无需额外操作"
systemctl enable --now aria2 2>/dev/null || true

# ---------- 1b. rclone (可选，仅在 --with-rclone 时安装，逻辑与 docker 模式共用) ----------
if [[ "$WITH_RCLONE" -eq 1 ]]; then
  bash "$SCRIPT_DIR/scripts/install_rclone.sh"
  log "aria2.sh 安装时已自带下载 ${ARIA2_CONF_DIR}/rclone.env 模板，按需编辑"
fi

# ---------- 2. telegram-bot-api ----------
# 端口以 .env 里 BOT_API_URL 的端口为准（install.sh 默认写 8081，可用环境变量
# BOT_API_PORT 指定）。8081 是很常见的端口，机器上可能已经有别的程序占着——
# 以前这里直接 docker run，端口冲突就报 "address already in use" 整个安装中断。

# 端口上有没有程序在监听。用 bash 自带的 /dev/tcp 探测，不依赖 ss/netstat
# （精简系统上未必装了）
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

# 尽量说出是谁占着端口（只用于提示，拿不到就算了）
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

# 端口上已经是一个能用的 telegram-bot-api（之前源码编译装的原生服务、或者
# 别的名字的容器）时返回 0——直接复用，不再另起一个。按协议判断而不是按
# 进程名：拿一个无效 token 调 getMe，Bot API 服务一定回 {"ok":false,"error_code":...}
port_is_botapi() {
  local body
  body="$(curl -s -m 5 "http://127.0.0.1:$1/bot0:invalid/getMe" 2>/dev/null || true)"
  [[ "$body" == *'"ok":false'* && "$body" == *'"error_code"'* ]]
}

BOT_API_URL_CUR="$(env_get BOT_API_URL)"
BOT_API_PORT="${BOT_API_URL_CUR##*:}"
[[ "$BOT_API_PORT" =~ ^[0-9]+$ ]] || BOT_API_PORT=8081

# 先清掉我们自己上一次留下的容器（失败的 docker run 也会留下一个 Created
# 状态的容器），它占着端口的话这一步就释放了
if command -v docker >/dev/null 2>&1; then
  docker rm -f telegram-bot-api >/dev/null 2>&1 || true
fi

REUSE_BOTAPI=0
if port_in_use "$BOT_API_PORT"; then
  if port_is_botapi "$BOT_API_PORT"; then
    REUSE_BOTAPI=1
    log "127.0.0.1:${BOT_API_PORT} 上已经有一个 telegram-bot-api 在运行（$(port_owner "$BOT_API_PORT")），直接复用，不再另起容器"
  else
    warn "端口 ${BOT_API_PORT} 已被其它程序占用：$(port_owner "$BOT_API_PORT")"
    NEW_PORT=""
    for p in $(seq 8082 8099); do
      if ! port_in_use "$p"; then NEW_PORT="$p"; break; fi
    done
    [[ -n "$NEW_PORT" ]] || die "8081-8099 端口全被占用，无法启动 telegram-bot-api。请释放端口后重试，或用 BOT_API_PORT=<端口> 指定"
    BOT_API_PORT="$NEW_PORT"
    warn "telegram-bot-api 改用端口 ${BOT_API_PORT}（已写入 .env 的 BOT_API_URL，不影响原来占用 8081 的程序）"
  fi
fi
env_set BOT_API_URL "http://127.0.0.1:${BOT_API_PORT}"

if [[ "$REUSE_BOTAPI" -eq 1 ]]; then
  :
elif [[ "$BUILD_FROM_SOURCE" -eq 1 ]]; then
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
  sed -i "s#{{API_ID}}#$(env_get API_ID)#; s#{{API_HASH}}#$(env_get API_HASH)#; s#{{BOT_API_PORT}}#${BOT_API_PORT}#" \
    /etc/systemd/system/telegram-bot-api.service
  systemctl daemon-reload
  systemctl enable --now telegram-bot-api
else
  log "使用轻量混合模式：仅用 docker 跑 telegram-bot-api 容器（其余全部裸机）"
  if ! command -v docker >/dev/null 2>&1; then
    warn "未检测到 Docker，正在安装（仅用于 telegram-bot-api 一个容器）"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  fi
  docker run -d --name telegram-bot-api --restart unless-stopped \
    -p "127.0.0.1:${BOT_API_PORT}:8081" \
    -e TELEGRAM_API_ID="$(env_get API_ID)" \
    -e TELEGRAM_API_HASH="$(env_get API_HASH)" \
    -e TELEGRAM_LOCAL=true \
    -v tg-botapi-data:/var/lib/telegram-bot-api \
    aiogram/telegram-bot-api:latest >/dev/null \
    || die "telegram-bot-api 容器启动失败（见上方 docker 报错）。修好后重新运行 sudo ./install.sh 即可，已完成的步骤会自动跳过"
  log "telegram-bot-api 容器已启动，监听 127.0.0.1:${BOT_API_PORT}"
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
  sudo ./update.sh                     升级到最新版本（自动备份、失败自动回滚）
  sudo tg-aria2                        交互式管理菜单（状态/日志/重启/改配置/备份恢复）
EOF

if [[ "$NO_WEB" -eq 0 ]]; then
  cat <<'EOF'

Web 管理后台: http://127.0.0.1:8080  (仅监听本机，远程访问需要 SSH 隧道或反向代理+TLS)
AriaNg:       http://127.0.0.1:6880  (首次打开需要手动填 RPC 地址/密钥，之后记在浏览器本地)
EOF
fi
