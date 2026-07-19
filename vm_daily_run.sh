#!/bin/bash
# Daily trade pipeline — runs on Oracle VM (Indian IP, not blocked by CBOS).
# GHA runner (Azure US) is blocked by CBOS on Sundays; VM is not.
#
# Usage: ./vm_daily_run.sh [true|false]   — true = full FY history (both FY25-26 + FY26-27)
# Cron (set by setup_vm_downloader.yml):
#   Mon-Sat 7:30 PM IST  (14:00 UTC): incremental
#   Sunday  11:00 AM IST (05:30 UTC): full

set -e
IS_FULL=${1:-false}
REPO_DIR=/home/opc/client-tracker-mofsl
LOG=/home/opc/vm_daily_run.log

{
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — IS_FULL=$IS_FULL ==="

cd "$REPO_DIR"

# Pull latest code (git-ignored files — bot_config.json, mo_csvs/, ledger.json — are preserved)
git fetch origin main
git reset --hard origin/main

# Load GitHub token from bot_config.json
GITHUB_TOKEN=$(python3 - << 'PYEOF'
import json, sys
try:
    c = json.load(open('bot_config.json', encoding='utf-8-sig'))
    t = c.get('github_token', '')
    if not t:
        raise ValueError('github_token is empty')
    print(t)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
PYEOF
)
REPO="jainrishank20/client-tracker-mofsl"

# Download CSVs + ledger from CBOS
if [ "$IS_FULL" = "true" ]; then
    echo "Running FULL download (FY25-26 + FY26-27)..."
    python3 mo_downloader.py --full
else
    echo "Running incremental download (FY26-27)..."
    python3 mo_downloader.py
fi

# Import CSVs → trades.json
python3 import_all.py

# Push trades.json + ledger.json to GitHub
git config user.email "vm-runner@oraclecloud"
git config user.name "Oracle VM Runner"
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git"
git add trades.json ledger.json 2>/dev/null || true
if ! git diff --cached --quiet; then
    git commit -m "chore: update trades [skip ci]"
    git pull --rebase origin main || true
    git push origin main
    echo "Pushed trades.json + ledger.json to GitHub"
else
    echo "No data changes to commit"
fi

# Trigger GHA: sync GSheet + send Telegram (no re-download needed)
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"main\",\"inputs\":{\"skip_download\":\"true\",\"full_history\":\"${IS_FULL}\"}}" \
  "https://api.github.com/repos/${REPO}/actions/workflows/daily_run.yml/dispatches")
echo "Triggered GHA daily_run.yml — HTTP $HTTP_STATUS (skip_download=true, full_history=${IS_FULL})"

echo "=== Completed $(date '+%Y-%m-%d %H:%M:%S') ==="
} >> "$LOG" 2>&1
