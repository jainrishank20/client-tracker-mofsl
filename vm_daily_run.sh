#!/bin/bash
# Daily trade pipeline — download + import run on VM (Indian IP can reach CBOS).
# GHA runners cannot reach backoffice.motilaloswal.com or be SSH'd into from outside.
# So: VM downloads CSVs, imports to trades.json, pushes to git, then triggers
# GHA with skip_download=true for GSheet sync + Telegram notification.
#
# Cron:   Mon-Sat 14:00 UTC (7:30 PM IST)  — incremental
#         Sun     05:30 UTC (11:00 AM IST)  — full

set -e
export PATH=/usr/local/bin:/usr/bin:/bin:/home/opc/.local/bin:/sbin:/usr/sbin:$PATH

IS_FULL=${1:-false}
REPO_DIR=/home/opc/app
REPO="jainrishank20/client-tracker-mofsl"
LOG=/home/opc/vm_daily_run.log
LOG_START=0  # line number where current run starts — set inside the block below

_tg_notify() {
  TG_TOKEN=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(c.get('telegram_token',''))" 2>/dev/null)
  TG_CHAT=$(python3 -c "import json; c=json.load(open('${REPO_DIR}/bot_config.json',encoding='utf-8-sig')); print(str(c.get('allowed_chat_id','')).split(',')[0].strip())" 2>/dev/null)
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    # Read only THIS run's log output (not stale entries from previous runs)
    THIS_RUN=$(tail -n "+${LOG_START}" "$LOG" 2>/dev/null || tail -20 "$LOG" 2>/dev/null)
    LAST=$(echo "$THIS_RUN" | grep "=== Done" | wc -l)
    if [ "$LAST" -gt 0 ]; then
      MSG="VM download done ($(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST')) — GHA running GSheet sync. Check /vmlog."
    else
      ERRMSG=$(echo "$THIS_RUN" | grep -i "error\|fail\|traceback\|exception" | grep -v "continue.on.error\|non.fatal\|skipping" | tail -1)
      MSG="Pipeline FAILED at $(TZ='Asia/Kolkata' date '+%d %b %I:%M %p IST'). ${ERRMSG:-Check /vmlog.}"
    fi
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d "chat_id=${TG_CHAT}&text=${MSG}" > /dev/null
  fi
}
trap _tg_notify EXIT

{
LOG_START=$(wc -l < "$LOG" 2>/dev/null || echo 0)
LOG_START=$((LOG_START + 1))
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

# ── Ensure chromium + system libs are set up for Oracle Linux 8 ──────────────
# The ELF-patcher approach (removing DT_NEEDED) caused SIGSEGV because chromium
# has weak PLT refs to those libs — removing them makes weak refs resolve to NULL.
# Correct fix: install REAL system libs via playwright install-deps + dnf.
# Reinstall fresh chromium if the binary was previously ELF-patched.

LIBDIR=/home/opc/lib
SETUP_MARKER=/home/opc/.chromium_setup_v1
mkdir -p "$LIBDIR"

if [ ! -f "$SETUP_MARKER" ]; then
  echo "Setting up chromium for Oracle Linux 8..."

  # Delete any ELF-patched chromium binary — it has broken weak PLT refs → SIGSEGV
  rm -rf /home/opc/.cache/ms-playwright
  echo "  Deleted old (possibly ELF-patched) chromium cache."

  # Reinstall fresh playwright + chromium
  python3 -m pip install playwright --quiet 2>/dev/null || true
  python3 -m playwright install chromium 2>/dev/null || true
  echo "  Playwright + chromium reinstalled."

  # Install system deps — playwright install-deps knows the exact list for this chromium
  echo "  Running playwright install-deps chromium..."
  python3 -m playwright install-deps chromium 2>/dev/null || true

  # Belt-and-suspenders: also try dnf directly
  echo "  Running dnf install for chromium system deps..."
  sudo dnf install -y at-spi2-core at-spi2-atk atk libxkbcommon cups-libs \
    libXcomposite libXdamage libXfixes libXrandr libXtst \
    mesa-libgbm libdrm alsa-lib pango gtk3 \
    nss nspr 2>/dev/null || true

  # Clean up any old stubs — if real libs installed, stubs would shadow them
  rm -f "$LIBDIR"/*.so* 2>/dev/null || true
  echo "  Cleared old stubs."

  # Check what's still missing after all install attempts, create minimal stubs
  python3 - <<'PYEOF' || true
import os, glob, subprocess, struct

LIBDIR = '/home/opc/lib'
os.makedirs(LIBDIR, exist_ok=True)

ch = glob.glob('/home/opc/.cache/ms-playwright/**/chrome-headless-shell', recursive=True)
CHROME = ch[0] if ch else None
if not CHROME:
    print('chrome-headless-shell not found — playwright install may have failed')
    raise SystemExit(0)

# Refresh ldconfig cache after installs
for cmd in ['sudo /sbin/ldconfig', 'sudo ldconfig']:
    try: subprocess.run(cmd, shell=True, timeout=10); break
    except: pass

# Check ldd for still-missing libs
missing = []
try:
    r = subprocess.Popen(['ldd', CHROME], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = r.communicate(timeout=20)
    for line in out.decode(errors='replace').splitlines():
        if '=>' in line and 'not found' in line:
            lib = line.strip().split()[0]
            if '.so' in lib:
                missing.append(lib)
    print(f'Still missing after installs: {missing}')
except Exception as e:
    print(f'ldd failed: {e}')

if not missing:
    print('All libs found — no stubs needed. Chromium should work.')
    raise SystemExit(0)

# Warn: stubs for missing libs have no symbols — only safe if those
# libs are dlopen'd by chromium (with NULL-check before calling functions).
def make_stub(path):
    EH,PH,DE = 64,56,16
    dyn_off = EH+2*PH; total = dyn_off+DE
    ehdr = struct.pack('<4sBBBBBxxxxxxxHHIQQQIHHHHHH',
        b'\x7fELF',2,1,1,0,0, 3,0x3e,1, 0,EH,0, 0,EH,PH,2, 64,0,0)
    phdr_load = struct.pack('<IIQQQQQQ',1,5,0,0,0,total,total,0x1000)
    phdr_dyn  = struct.pack('<IIQQQQQQ',2,6,dyn_off,dyn_off,dyn_off,DE,DE,8)
    dynamic   = struct.pack('<QQ',0,0)
    with open(path,'wb') as f: f.write(ehdr+phdr_load+phdr_dyn+dynamic)
    os.chmod(path,0o755)

for lib in missing:
    p = os.path.join(LIBDIR, lib)
    make_stub(p)
    print(f'Stub (last resort): {lib}')

# Register stubs with ldconfig (AFTER system dirs, so real libs take priority)
subprocess.run(
    ['sudo', 'bash', '-c', 'echo /home/opc/lib > /etc/ld.so.conf.d/vm-stubs.conf && /sbin/ldconfig'],
    timeout=15, check=False)
print('Done.')
PYEOF

  touch "$SETUP_MARKER"
  echo "Chromium setup complete."
else
  echo "Chromium setup already done (marker exists)."
fi

# Belt-and-suspenders: set LD_LIBRARY_PATH so linker finds stubs even if ldconfig failed.
# Stubs only exist for libs NOT in system paths, so no shadowing of real libs.
if ls /home/opc/lib/*.so* 2>/dev/null | grep -q .; then
  export LD_LIBRARY_PATH="/home/opc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  echo "Stubs present → LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
fi

CHROME_BIN=$(find /home/opc/.cache/ms-playwright -name "chrome-headless-shell" -type f 2>/dev/null | head -1)
[ -z "$CHROME_BIN" ] && echo "WARN: chromium binary not found after setup"

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

TOKEN = os.environ.get('GITHUB_TOKEN') or json.load(open('/home/opc/app/bot_config.json', encoding='utf-8-sig')).get('github_token', '')
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
