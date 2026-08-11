#!/bin/bash
# vm_restore.sh — rebuild /home/opc/app from scratch on a fresh VM.
# Run as: bash vm_restore.sh <GITHUB_PAT> <BOT_CONFIG_JSON> <GSHEET_KEY_JSON>
#
# After running: crontab is installed, bot is live, all data restored.

set -e

GITHUB_PAT="${1:?Pass GitHub PAT as arg 1}"
BOT_CONFIG_JSON="${2:?Pass BOT_CONFIG JSON as arg 2}"
GSHEET_KEY_JSON="${3:?Pass GSHEET_KEY JSON as arg 3}"

REPO="jainrishank20/client-tracker-mofsl"
APP_DIR="/home/opc/app"

echo "=== VM Restore: client-tracker-mofsl ==="
echo "Target: $APP_DIR"

# ── 1. Install system deps ────────────────────────────────────────────────────
echo "Installing system packages..."
sudo dnf install -y python3 python3-pip curl unzip git 2>/dev/null || \
  sudo yum install -y python3 python3-pip curl unzip git 2>/dev/null || true

# ── 2. Download all repo files ────────────────────────────────────────────────
echo "Downloading repo files from GitHub..."
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Code files
CODE_FILES=(
  mo_downloader.py import_all.py send_notify.py telegram_bot.py
  vm_daily_run.sh vm_setup.sh vm_sync_gsheet.py symbol_map.py
  requirements.txt
)

# Data files — state that the pipeline maintains and pushes back to repo
DATA_FILES=(
  trades.json ledger.json ticker_overrides.json cmp_cache.json
  open_positions_snapshot.json pnl_snapshot.json
)

for f in "${CODE_FILES[@]}" "${DATA_FILES[@]}"; do
  curl -fsSL \
    -H "Authorization: token $GITHUB_PAT" \
    -H "Accept: application/vnd.github.v3.raw" \
    "https://api.github.com/repos/$REPO/contents/$f" \
    -o "$APP_DIR/$f" \
    && echo "  Downloaded: $f" \
    || echo "  WARN: $f not found in repo (skipping)"
done

chmod +x "$APP_DIR/vm_daily_run.sh" "$APP_DIR/vm_setup.sh"

# ── 3. Write secrets ──────────────────────────────────────────────────────────
echo "Writing secrets..."
echo "$BOT_CONFIG_JSON" > "$APP_DIR/bot_config.json"
echo "$GSHEET_KEY_JSON" > "$APP_DIR/gsheet_key.json"
chmod 600 "$APP_DIR/bot_config.json" "$APP_DIR/gsheet_key.json"

# ── 4. Install Python deps ────────────────────────────────────────────────────
echo "Installing Python packages..."
pip3 install -r "$APP_DIR/requirements.txt" --quiet

# ── 5. Install Playwright + Chromium ─────────────────────────────────────────
echo "Installing Playwright..."
pip3 install playwright --quiet
python3 -m playwright install chromium

# ── 6. Create mo_csvs dir ────────────────────────────────────────────────────
mkdir -p "$APP_DIR/mo_csvs"

# ── 7. Install crontab ───────────────────────────────────────────────────────
echo "Installing crontab..."
(crontab -l 2>/dev/null | grep -vE vm_daily_run; \
 echo '30 15 * * 1-6 /home/opc/app/vm_daily_run.sh false >> /home/opc/vm_daily_run.log 2>&1') \
 | crontab -
echo "Crontab installed:"
crontab -l

# ── 8. Start Telegram bot ────────────────────────────────────────────────────
echo "Starting Telegram bot..."
pkill -f telegram_bot.py 2>/dev/null || true
nohup python3 "$APP_DIR/telegram_bot.py" >> /home/opc/telegram_bot.log 2>&1 &
sleep 2
pgrep -f telegram_bot.py && echo "Bot running" || echo "WARN: bot did not start"

echo ""
echo "=== Restore complete ==="
echo "All code + data restored from repo. Secrets written from GitHub Secrets."
echo "Test: send /summary to your Telegram bot"
echo "Cron: Mon-Sat 9 PM IST auto-runs the full pipeline"
