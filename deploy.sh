#!/usr/bin/env bash
# Deploy the bot to the production server. Never touches the server's .env.
#
#   ./deploy.sh            # sync code, verify, restart bot service
#   ./deploy.sh --no-restart   # sync + verify only
set -euo pipefail

HOST="root@213.35.122.203"
KEY="$HOME/.ssh/tg_aria2_deploy"
REMOTE_DIR="/root/tg-aria2-bot"
SERVICE="tg-aria2-bot"

cd "$(dirname "$0")"

echo "==> syncing bot/ + requirements.txt to $HOST"
tar czf - bot requirements.txt | ssh -i "$KEY" "$HOST" "cd $REMOTE_DIR && tar xzf -"

echo "==> compile + import check (server venv)"
ssh -i "$KEY" "$HOST" "
  cd $REMOTE_DIR &&
  .venv/bin/python -m compileall -q bot &&
  .venv/bin/python -c 'import bot.main' &&
  echo VERIFY_OK
"

if [[ "${1:-}" == "--no-restart" ]]; then
  echo "==> skipping restart (--no-restart)"
  exit 0
fi

echo "==> restarting $SERVICE"
ssh -i "$KEY" "$HOST" "
  systemctl restart $SERVICE && sleep 2 &&
  systemctl is-active $SERVICE &&
  journalctl -u $SERVICE -n 5 --no-pager
"
echo "==> deploy complete"
