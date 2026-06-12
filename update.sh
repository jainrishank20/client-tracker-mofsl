#!/bin/bash
# Pull latest code and restart bot — run this after every GitHub push
set -e

cd ~/client-tracker-mofsl
git pull

# Restart the screen session
screen -X -S bot quit 2>/dev/null || true
sleep 1

set -a; source .env; set +a
screen -dmS bot bash -c '
  cd ~/client-tracker-mofsl
  while true; do
    echo "[$(date)] Starting bot..."
    set -a; source .env; set +a
    python3 telegram_bot.py
    echo "[$(date)] Bot crashed, restarting in 5s..."
    sleep 5
  done
'
echo "Bot restarted. View with: screen -r bot"
