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
if ! ldconfig -p 2>/dev/null | grep -q libatk-1.0; then
  echo "libatk-1.0 missing — trying dnf (chromium already cached, low mem pressure)..."
  sudo dnf install -y --quiet --setopt=install_weak_deps=False \
    atk at-spi2-atk cups-libs libdrm libXcomposite libXdamage libXfixes \
    libXrandr mesa-libgbm pango alsa-lib 2>&1 | tail -5 || true

  # If dnf failed, diagnose why urllib can't reach Oracle repo, then try rpm2cpio
  if ! ldconfig -p 2>/dev/null | grep -q libatk-1.0; then
    echo "dnf did not install libatk — testing network access..."
    python3 - <<'PYEOF'
import urllib.request, sys
test_url = "https://yum.oracle.com/repo/OracleLinux/OL8/baseos/latest/x86_64/getPackage/atk-2.28.1-1.0.1.el8.x86_64.rpm"
try:
    with urllib.request.urlopen(test_url, timeout=20) as r:
        data = r.read(1024)
        with open('/tmp/_atk.rpm', 'wb') as f:
            f.write(data)
            import shutil
            shutil.copyfileobj(r, f)
        print(f"Oracle repo OK, downloaded {r.length or '?'} bytes")
        sys.exit(0)
except Exception as e:
    print(f"Oracle repo FAILED: {e}")

# Try AlmaLinux
alt_url = "https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/atk-2.28.1-1.el8.x86_64.rpm"
try:
    urllib.request.urlretrieve(alt_url, '/tmp/_atk.rpm')
    print(f"AlmaLinux repo OK")
except Exception as e2:
    print(f"AlmaLinux repo FAILED: {e2}")
    sys.exit(1)
PYEOF
    if [ -f /tmp/_atk.rpm ]; then
      mkdir -p /tmp/_atkex /home/opc/lib
      ( cd /tmp/_atkex && rpm2cpio /tmp/_atk.rpm | cpio -idm --quiet 2>/dev/null )
      find /tmp/_atkex -name "libatk*.so*" -exec cp -f {} /home/opc/lib/ \;
      rm -rf /tmp/_atkex /tmp/_atk.rpm
      echo "Extracted libatk via rpm2cpio"
    fi
  fi
fi
export LD_LIBRARY_PATH=/home/opc/lib:${LD_LIBRARY_PATH:-}

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
