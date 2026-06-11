#!/bin/bash
# One-command setup for Oracle Cloud Free Tier (Ubuntu 22.04)
# Run as: bash setup.sh

set -e
echo "=== Raghava Tracker Bot — Oracle VM Setup ==="

# 1. System packages
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip git screen nano

# 2. Clone / pull repo
if [ -d "raghava-tracker-bot" ]; then
  echo "Repo exists, pulling latest..."
  cd raghava-tracker-bot && git pull
else
  git clone https://github.com/jainrishank20/raghava-tracker-bot.git
  cd raghava-tracker-bot
fi

# 3. Install Python deps
pip3 install -r requirements.txt

# 4. Create .env if missing
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo ">>> EDIT .env NOW: nano .env"
  echo "    Add your TELEGRAM_TOKEN and GROQ_API_KEY, then save."
  echo "    Press Enter when done..."
  read -r
fi

# 5. Load env and start bot in a screen session
set -a; source .env; set +a

screen -dmS bot bash -c '
  cd ~/raghava-tracker-bot
  while true; do
    echo "[$(date)] Starting bot..."
    set -a; source .env; set +a
    python3 telegram_bot.py
    echo "[$(date)] Bot crashed, restarting in 5s..."
    sleep 5
  done
'

echo ""
echo "=== Bot is running in screen session 'bot' ==="
echo "  View logs:   screen -r bot"
echo "  Detach:      Ctrl+A then D"
echo "  Stop bot:    screen -X -S bot quit"
echo ""
echo "To update the bot later, run:  bash update.sh"
