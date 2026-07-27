#!/bin/bash
# Daily trade pipeline — runs on Oracle VM (Indian IP, not blocked by CBOS on Sundays).
# Uses curl + GitHub API — no git required on the VM.
#
# Usage:  ./vm_daily_run.sh [true|false]   true = full FY history
# Cron:   Mon-Sat 14:00 UTC (7:30 PM IST)  — incremental
#         Sun     05:30 UTC (11:00 AM IST)  — full

set -e
export PATH=/usr/local/bin:/usr/bin:/bin:/home/opc/.local/bin:$PATH

IS_FULL=${1:-false}
REPO_DIR=/home/opc/app
REPO="jainrishank20/client-tracker-mofsl"
LOG=/home/opc/vm_daily_run.log

# Always send Telegram on exit (success or failure)
_tg_notify() {
  TG_TOKEN=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(c.get('telegram_token',''))" 2>/dev/null)
  TG_CHAT=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(c.get('allowed_chat_id',''))" 2>/dev/null)
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    LAST=$(tail -3 "$LOG" | grep "=== Done" | wc -l)
    if [ "$LAST" -gt 0 ]; then
      MSG="Pipeline done ($(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST')) — GHA triggered. Check dashboard."
    else
      ERRMSG=$(tail -5 "$LOG" | grep -i "error\|fail\|timed out" | tail -1)
      MSG="Pipeline FAILED at $(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST'). Error: ${ERRMSG:-unknown}. Check /vmlog."
    fi
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT}&text=${MSG}" > /dev/null
  fi
}
trap _tg_notify EXIT

{
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — IS_FULL=$IS_FULL ==="
cd "$REPO_DIR"

# Load GitHub token from bot_config.json
GITHUB_TOKEN=$(python3 -c "
import json, sys
try:
    c = json.load(open('bot_config.json', encoding='utf-8-sig'))
    t = c.get('github_token', '')
    if not t: raise ValueError('github_token is empty')
    print(t)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
")

# Pull latest Python scripts from GitHub API (always fresh, no CDN caching)
echo "Updating scripts from GitHub..."
for f in mo_downloader.py import_all.py symbol_map.py telegram_bot.py ticker_overrides.json tests/test_import_counts.py tests/qa_positions.py; do
    mkdir -p "$(dirname "$f")"
    curl -sf \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3.raw" \
        "https://api.github.com/repos/${REPO}/contents/${f}?ref=main" -o "${f}.new" \
        && mv "${f}.new" "${f}" \
        || { echo "  Warning: could not update $f"; rm -f "${f}.new"; }
done
echo "Scripts updated."

# Download CSVs + ledger from CBOS (hard timeout: 45 min for full, 20 min incremental)
if [ "$IS_FULL" = "true" ]; then
    echo "Running FULL download (FY25-26 + FY26-27)..."
    timeout 2700 python3 mo_downloader.py --full || { echo "ERROR: mo_downloader.py timed out or failed"; exit 1; }
else
    echo "Running incremental download (FY26-27)..."
    timeout 1200 python3 mo_downloader.py || { echo "ERROR: mo_downloader.py timed out or failed"; exit 1; }
fi

# Import CSVs → trades.json
python3 import_all.py

# Push trades.json + ledger.json to GitHub via API
python3 - << PYEOF
import json, base64, urllib.request, urllib.error, os, sys

TOKEN = "${GITHUB_TOKEN}"
REPO  = "${REPO}"
HEADERS = {
    'Authorization': f'token {TOKEN}',
    'Content-Type':  'application/json',
}

def gh_put(path, content_bytes, message):
    url = f'https://api.github.com/repos/{REPO}/contents/{path}'
    # Get existing SHA (needed for update; absent for new file)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req) as r:
            sha = json.load(r).get('sha', '')
    except urllib.error.HTTPError as e:
        sha = '' if e.code == 404 else (_ for _ in ()).throw(e)

    body = {
        'message': message,
        'content': base64.b64encode(content_bytes).decode(),
        'branch':  'main',
    }
    if sha:
        body['sha'] = sha

    req = urllib.request.Request(url,
        data=json.dumps(body).encode(),
        headers=HEADERS,
        method='PUT')
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.load(r)
            print(f'  Pushed {path} — commit {resp["commit"]["sha"][:7]}')
    except Exception as e:
        print(f'  ERROR pushing {path}: {e}', file=sys.stderr)
        sys.exit(1)

for fname in ('trades.json', 'ledger.json'):
    if os.path.exists(fname):
        gh_put(fname, open(fname, 'rb').read(), f'chore: update {fname} from VM [skip ci]')
    else:
        print(f'  Skipping {fname} (not found)')
PYEOF

# Trigger GHA: sync GSheet + send Telegram
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"main\",\"inputs\":{\"skip_download\":\"true\",\"full_history\":\"${IS_FULL}\"}}" \
  "https://api.github.com/repos/${REPO}/actions/workflows/daily_run.yml/dispatches")
echo "Triggered GHA daily_run.yml — HTTP $HTTP"
if [ "$HTTP" != "204" ]; then
  echo "ERROR: GHA workflow dispatch failed (HTTP $HTTP) — GSheet sync will not run."
  exit 1
fi

echo "=== Done $(date '+%Y-%m-%d %H:%M:%S') ==="
} >> "$LOG" 2>&1
