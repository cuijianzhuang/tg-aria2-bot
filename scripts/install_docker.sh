#!/usr/bin/env bash
# Docker deployment: installs Docker Engine + compose plugin if missing, then brings
# up telegram-bot-api + aria2 + bot via docker-compose.yml.
set -euo pipefail

WITH_RCLONE=0
NO_WEB=0
for arg in "$@"; do
  [[ "$arg" == "--with-rclone" ]] && WITH_RCLONE=1
  [[ "$arg" == "--no-web" ]] && NO_WEB=1
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=scripts/env_lib.sh
source "$SCRIPT_DIR/scripts/env_lib.sh"
# shellcheck source=scripts/net_lib.sh
source "$SCRIPT_DIR/scripts/net_lib.sh"

log()  { printf '\033[1;32m[docker]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }

if ! command -v docker >/dev/null 2>&1; then
  warn "未检测到 Docker，准备安装 (使用官方 get.docker.com 脚本)"
  read -rp "确认安装 Docker？[y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "已取消，请手动安装 Docker 后重试。"; exit 1; }
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  log "Docker 已安装: $(docker --version)"
fi

if ! docker compose version >/dev/null 2>&1; then
  warn "未检测到 docker compose 插件，尝试安装 docker-compose-plugin"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y docker-compose-plugin
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker-compose-plugin
  else
    echo "无法自动安装 docker compose 插件，请手动安装后重试。" && exit 1
  fi
fi

# p3terx/aria2-pro 本身不带 rclone（已核实其 Dockerfile/rootfs 里没有 rclone）。
# 不重新 build 镜像：直接在宿主机装官方 rclone 静态二进制（已验证纯静态链接、无 glibc
# 依赖），再只读挂载进 aria2 容器同一路径即可，容器内的 upload.sh 就能直接调用它。
# 好处：升级 rclone 只需要更新宿主机这一份，不用重新 build 镜像。
if [[ "$WITH_RCLONE" -eq 1 ]]; then
  bash "$SCRIPT_DIR/scripts/install_rclone.sh"
  RCLONE_BIN="$(command -v rclone)"
  log "启用 --with-rclone：生成 docker-compose.override.yml，只读挂载宿主机 ${RCLONE_BIN} 进 aria2 容器"
  cat > docker-compose.override.yml <<EOF
services:
  aria2:
    volumes:
      - ${RCLONE_BIN}:/usr/bin/rclone:ro
EOF
  log "还需要手动配置网盘 remote（OAuth 授权，无法自动化）："
  log "  docker compose exec -it aria2 rclone config"
  log "  无桌面浏览器的服务器上用 rclone config 里的 headless 授权流程（打印 URL，本机浏览器登录后粘贴 token）"
else
  rm -f docker-compose.override.yml
fi

# web/ariang 两个服务标了 profiles: ["web"]。是否启用由 install.sh 写进 .env 的
# COMPOSE_PROFILES 决定（docker compose 会自动读取），这样之后手敲的
# docker compose up/restart/logs 也会带上它们，不用每次记得加 --profile web。
# Web 后台端口（宿主机侧）被别的程序占着时 compose up 会直接失败，先换一个。
# 我们自己的 web 容器正在跑时它占着端口是正常的（up 会原地重建），跳过检查
if [[ "$NO_WEB" -eq 0 ]] && [[ -z "$(docker compose ps -q web 2>/dev/null)" ]]; then
  WEB_PORT_VALUE="$(env_get WEB_PORT)"
  WEB_PORT_VALUE="${WEB_PORT_VALUE:-8080}"
  if port_in_use "$WEB_PORT_VALUE"; then
    warn "Web 管理后台的端口 ${WEB_PORT_VALUE} 已被其它程序占用：$(port_owner "$WEB_PORT_VALUE")"
    NEW_PORT="$(find_free_port 8080 8099 "$WEB_PORT_VALUE")" \
      || { echo "8080-8099 端口全被占用，请在 .env 里设 WEB_PORT 后重试"; exit 1; }
    env_set WEB_PORT "$NEW_PORT"
    warn "Web 管理后台改用端口 ${NEW_PORT}（已写入 .env 的 WEB_PORT）"
  fi
fi

log "启动服务 (docker compose up -d --build)"
docker compose up -d --build

log "等待容器就绪..."
sleep 5
docker compose ps

cat <<EOF

部署完成。常用命令：
  docker compose logs -f bot            查看机器人日志
  docker compose logs -f aria2          查看 aria2 / 钩子脚本日志
  docker compose restart bot            重启机器人
  docker compose down                   停止全部服务
  sudo ./update.sh                      升级到最新版本（自动备份、失败自动回滚）
  sudo tg-aria2                         交互式管理菜单（状态/日志/重启/改配置/备份恢复）

move.sh / upload.sh 默认未接入任何 aria2 钩子（on-download-complete 只调用 clean.sh），
不会自动移动或上传文件，无需额外操作；如需启用见 aria2-config/script.conf 顶部说明。
EOF

if [[ "$NO_WEB" -eq 0 ]]; then
  WEB_BIND_VALUE="$(grep -m1 '^WEB_BIND=' .env 2>/dev/null | cut -d= -f2- || true)"
  WEB_BIND_VALUE="${WEB_BIND_VALUE:-0.0.0.0}"
  echo
  WEB_PORT_VALUE="$(env_get WEB_PORT)"
  echo "Web 管理后台: http://<服务器IP>:${WEB_PORT_VALUE:-8080}   AriaNg: http://<服务器IP>:6880"
  if [[ "$WEB_BIND_VALUE" == "0.0.0.0" ]]; then
    warn "这两个端口当前对公网开放且是明文 HTTP。建议在 .env 里设 WEB_BIND=127.0.0.1 后"
    warn "docker compose up -d，再通过 SSH 隧道或带 TLS 的反向代理访问。"
  else
    echo "（仅监听 ${WEB_BIND_VALUE}，远程访问需要 SSH 隧道或反向代理+TLS）"
  fi
fi
