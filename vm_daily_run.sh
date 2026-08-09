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

# ── Install chromium system libs (no yum/apt — uses rpm2cpio, no sudo) ───────
# CentOS 8 vault = RHEL 8 / Oracle Linux 8 compatible
_install_so() {
  local so_name="$1"; shift
  ldconfig -p 2>/dev/null | grep -q "$so_name" && return 0
  ls /home/opc/lib/"${so_name}"* 2>/dev/null | grep -q . && return 0
  echo "  Installing $so_name..."
  local f="/tmp/_chromedep.rpm" ex="/tmp/_chromedep_ex" ok=false
  for url in "$@"; do
    curl -sfL --max-time 30 "$url" -o "$f" 2>/dev/null && ok=true && break
  done
  if ! $ok; then echo "  WARN: download failed for $so_name"; return 1; fi
  mkdir -p "$ex" /home/opc/lib
  ( cd "$ex" && rpm2cpio "$f" | cpio -idm --quiet 2>/dev/null )
  find "$ex" -name "${so_name}*" -exec cp -f {} /home/opc/lib/ \;
  rm -rf "$ex" "$f"
}
export LD_LIBRARY_PATH=/home/opc/lib:${LD_LIBRARY_PATH:-}
mkdir -p /home/opc/lib
# Rocky Linux 8 = RHEL 8 / Oracle Linux 8 compatible; packages in alphabetical subdirs
RL8="https://dl.rockylinux.org/pub/rocky/8/BaseOS/x86_64/os/Packages"
RL8A="https://dl.rockylinux.org/pub/rocky/8/AppStream/x86_64/os/Packages"
_install_so libatk-1.0.so.0        "$RL8/a/atk-2.28.1-1.el8.x86_64.rpm"
_install_so libatk-bridge-2.0.so.0 "$RL8A/a/at-spi2-atk-2.26.2-1.el8.x86_64.rpm"
_install_so libcups.so.2            "$RL8/c/cups-libs-2.2.6-38.el8.x86_64.rpm"
_install_so libdrm.so.2             "$RL8/l/libdrm-2.4.103-1.el8.x86_64.rpm"
_install_so libXcomposite.so.1      "$RL8/l/libXcomposite-0.4.4-14.el8.x86_64.rpm"
_install_so libXdamage.so.1         "$RL8/l/libXdamage-1.1.4-14.el8.x86_64.rpm"
_install_so libXfixes.so.3          "$RL8/l/libXfixes-5.0.3-7.el8.x86_64.rpm"
_install_so libXrandr.so.2          "$RL8/l/libXrandr-1.5.2-1.el8.x86_64.rpm"
_install_so libgbm.so.1             "$RL8A/m/mesa-libgbm-20.3.3-2.el8.x86_64.rpm"
_install_so libpango-1.0.so.0       "$RL8/p/pango-1.42.4-6.el8.x86_64.rpm"
_install_so libasound.so.2          "$RL8/a/alsa-lib-1.2.1.2-4.el8.x86_64.rpm"

# ── Step 1: Download CSVs from CBOS ─────────────────────────────────────────
# VM has Indian IP — can reach backoffice.motilaloswal.com
# LD_LIBRARY_PATH carries the manually installed .so files into the chromium process
export LD_LIBRARY_PATH=/home/opc/lib:${LD_LIBRARY_PATH:-}
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
