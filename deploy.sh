#!/usr/bin/env bash
# 从本机直接把工作区代码推到服务器（开发调试用：未提交的改动也能马上上线验证）。
# 正式升级请在服务器上用 sudo ./tg-aria2.sh update（git 拉取 + 备份 + 健康检查 + 自动回滚），
# 或者推到 master 走 GitHub Actions。永远不会碰服务器上的 .env。
#
#   ./deploy.sh                # 同步代码 → 装依赖 → 校验 → 重启 bot/web
#   ./deploy.sh --no-restart   # 只同步 + 校验
#
# 目标服务器可用环境变量覆盖：DEPLOY_HOST / DEPLOY_KEY / DEPLOY_DIR
set -euo pipefail

HOST="${DEPLOY_HOST:-root@213.35.122.203}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/tg_aria2_deploy}"
REMOTE_DIR="${DEPLOY_DIR:-/root/tg-aria2-bot}"
SERVICE="tg-aria2-bot"
WEB_SERVICE="tg-aria2-web"

cd "$(dirname "$0")"

echo "==> syncing bot/ + requirements.txt to $HOST"
tar czf - bot requirements.txt | ssh -i "$KEY" "$HOST" "cd $REMOTE_DIR && tar xzf -"

echo "==> pip install + compile + import check (server venv)"
ssh -i "$KEY" "$HOST" "
  cd $REMOTE_DIR &&
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt &&
  .venv/bin/python -m compileall -q bot &&
  .venv/bin/python -c 'import bot.main, bot.web.app' &&
  echo VERIFY_OK
"

if [[ "${1:-}" == "--no-restart" ]]; then
  echo "==> skipping restart (--no-restart)"
  exit 0
fi

echo "==> restarting $SERVICE (+ $WEB_SERVICE if installed)"
ssh -i "$KEY" "$HOST" "
  systemctl restart $SERVICE &&
  systemctl try-restart $WEB_SERVICE &&
  sleep 3 &&
  systemctl is-active $SERVICE &&
  journalctl -u $SERVICE -n 5 --no-pager
"
echo "==> deploy complete"
