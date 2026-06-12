#!/bin/bash
set -e
sudo dnf install -y python3 python3-pip git screen
git clone https://github.com/jainrishank20/raghava-tracker-bot.git ~/client-tracker-mofsl || (cd ~/client-tracker-mofsl && git pull)
cd ~/client-tracker-mofsl
pip3 install -r requirements.txt
echo "Setup complete. Now create .env with TELEGRAM_TOKEN and GROQ_API_KEY"