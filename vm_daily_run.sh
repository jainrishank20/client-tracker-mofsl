#!/bin/bash
# Daily trade pipeline — download + import run on VM (Indian IP can reach CBOS).
# GHA runners cannot reach backoffice.motilaloswal.com or be SSH'd into from outside.
# So: VM downloads CSVs, imports to trades.json, pushes to git, then triggers
# GHA with skip_download=true for GSheet sync + Telegram notification.
#
# Cron:   Mon-Sat 14:00 UTC (7:30 PM IST)  — incremental
#         Sun     05:30 UTC (11:00 AM IST)  — full

set -e
export PATH=/usr/local/bin:/usr/bin:/bin:/home/opc/.local/bin:$PATH

IS_FULL=${1:-false}
REPO_DIR=/home/opc/app
REPO="jainrishank20/client-tracker-mofsl"
LOG=/home/opc/vm_daily_run.log

_tg_notify() {
  TG_TOKEN=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(c.get('telegram_token',''))" 2>/dev/null)
  TG_CHAT=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(str(c.get('allowed_chat_id','')).split(',')[0].strip())" 2>/dev/null)
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    LAST=$(tail -3 "$LOG" | grep "=== Done" | wc -l)
    if [ "$LAST" -gt 0 ]; then
      MSG="VM download done ($(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST')) — GHA running GSheet sync. Check /vmlog."
    else
      ERRMSG=$(tail -10 "$LOG" | grep -i "error\|fail\|traceback" | tail -1)
      MSG="Pipeline FAILED at $(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST'). ${ERRMSG:-Check /vmlog.}"
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

# ── Ensure playwright + chromium are installed ───────────────────────────────
if ! python3 -c "import playwright" 2>/dev/null; then
  echo "Installing playwright..."
  python3 -m pip install playwright --quiet || true
  python3 -m playwright install chromium || true
fi

# ── Install chromium system libs ─────────────────────────────────────────────
CHROME_BIN="/home/opc/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell"
LIBDIR=/home/opc/lib
mkdir -p "$LIBDIR"
export LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}"

# Check if chromium already patched with our RPATH (skip if already done)
if [ -f "$CHROME_BIN" ] && ! python3 -c "
import subprocess, sys
r = subprocess.run(['strings', sys.argv[1]], capture_output=True, text=True)
sys.exit(0 if '/home/opc/lib' in r.stdout else 1)
" "$CHROME_BIN" 2>/dev/null; then

  # Step A: Get libs via dnf download (no sudo, non-privileged)
  echo "Getting chromium libs via dnf download..."
  mkdir -p /tmp/cdeps /tmp/cdepex
  dnf download --destdir=/tmp/cdeps \
    atk at-spi2-atk cups-libs libXcomposite libXdamage libXfixes libXrandr \
    mesa-libgbm pango alsa-lib 2>&1 | grep -v "^$" | tail -8 || true
  N=$(ls /tmp/cdeps/*.rpm 2>/dev/null | wc -l)
  echo "dnf download: $N RPMs downloaded"
  for rpm in /tmp/cdeps/*.rpm; do
    [ -f "$rpm" ] || continue
    ( cd /tmp/cdepex && rpm2cpio "$rpm" | cpio -idm --quiet 2>/dev/null )
  done
  find /tmp/cdepex -name "*.so*" ! -name "*debug*" ! -name "*.py" \
    -exec cp -f {} "$LIBDIR/" \; 2>/dev/null || true
  rm -rf /tmp/cdeps /tmp/cdepex

  # Step B: Bake LIBDIR into chromium RPATH (patchelf — no root needed)
  pip3 install patchelf --quiet 2>/dev/null || true
  PELF=$(command -v patchelf 2>/dev/null || python3 -c "
import subprocess, sys
r=subprocess.run(['pip3','show','-f','patchelf'],capture_output=True,text=True)
for l in r.stdout.splitlines():
    if 'patchelf' in l and not l.startswith('Name'):
        import os,site
        for d in site.getsitepackages()+[site.getusersitepackages()]:
            p=os.path.join(os.path.dirname(d),'bin','patchelf')
            if os.path.isfile(p): print(p); sys.exit()
" 2>/dev/null)
  if [ -n "$PELF" ] && [ -x "$PELF" ]; then
    "$PELF" --add-rpath "$LIBDIR" "$CHROME_BIN" 2>/dev/null \
      && echo "Patched chromium RPATH → $LIBDIR" \
      || echo "patchelf --add-rpath failed (non-fatal)"
  else
    echo "patchelf not found — relying on LD_LIBRARY_PATH only"
  fi

  echo "Libs in $LIBDIR: $(ls $LIBDIR/ | wc -l) files"
  ls "$LIBDIR"/libatk* 2>/dev/null && echo "libatk: OK" || echo "WARN: libatk not in $LIBDIR (will use LD_LIBRARY_PATH)"
fi

# ── Step 1: Download CSVs from CBOS ─────────────────────────────────────────
# VM has Indian IP — can reach backoffice.motilaloswal.com
if [ "$IS_FULL" = "true" ]; then
  echo "Running FULL history download..."
  python3 mo_downloader.py --full --downloads-only
else
  echo "Running incremental download..."
  python3 mo_downloader.py --downloads-only
fi

# ── Step 2: Import CSVs → trades.json ───────────────────────────────────────
echo "Importing CSVs..."
python3 import_all.py

# ── Step 3: Push trades.json + ledger.json to repo via GitHub API ───────────
# No git binary needed — uses Python + urllib (stdlib only)
echo "Pushing data files to repo..."
python3 - <<'PYEOF'
import json, base64, urllib.request, os, sys

import json as _json
TOKEN = os.environ.get('GITHUB_TOKEN') or _json.load(open('/home/opc/app/bot_config.json', encoding='utf-8-sig')).get('github_token', '')
REPO  = 'jainrishank20/client-tracker-mofsl'
API   = f'https://api.github.com/repos/{REPO}/contents'
FILES = ['trades.json', 'ledger.json', 'open_positions_snapshot.json', 'ticker_overrides.json']
DIR   = '/home/opc/app'

pushed = 0
for fname in FILES:
    fpath = os.path.join(DIR, fname)
    if not os.path.exists(fpath):
        continue
    with open(fpath, 'rb') as f:
        raw = f.read()
    content_b64 = base64.b64encode(raw).decode()

    # Get current SHA (needed for update)
    url = f'{API}/{fname}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'token {TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'vm-runner'
    })
    try:
        resp = urllib.request.urlopen(req)
        sha = json.loads(resp.read().decode()).get('sha')
    except Exception:
        sha = None

    body = {'message': f'chore: update {fname} from VM download [skip ci]',
            'content': content_b64, 'branch': 'main'}
    if sha:
        body['sha'] = sha

    req = urllib.request.Request(url,
        data=json.dumps(body).encode(),
        headers={'Authorization': f'token {TOKEN}',
                 'Content-Type': 'application/json',
                 'Accept': 'application/vnd.github.v3+json',
                 'User-Agent': 'vm-runner'},
        method='PUT')
    try:
        urllib.request.urlopen(req)
        print(f'  Pushed {fname}')
        pushed += 1
    except Exception as e:
        print(f'  WARN: failed to push {fname}: {e}', file=sys.stderr)

print(f'Done — pushed {pushed}/{len(FILES)} files to repo.')
PYEOF

# ── Step 4: Trigger GHA with skip_download=true ─────────────────────────────
# GHA handles: GSheet sync (needs gsheet_key secret) + Telegram notification
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"main\",\"inputs\":{\"skip_download\":\"true\",\"full_history\":\"${IS_FULL}\"}}" \
  "https://api.github.com/repos/${REPO}/actions/workflows/daily_run.yml/dispatches")
echo "Triggered GHA (skip_download=true) — HTTP $HTTP"
if [ "$HTTP" != "204" ]; then
  echo "ERROR: GHA workflow dispatch failed (HTTP $HTTP)"
  exit 1
fi

echo "=== Done $(date '+%Y-%m-%d %H:%M:%S') ==="
} >> "$LOG" 2>&1
