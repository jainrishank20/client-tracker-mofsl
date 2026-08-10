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

# ── Patch chromium: remove unneeded system lib deps ──────────────────────────
# On minimal Oracle Linux 8, several libs chromium lists as DT_NEEDED are absent
# but are never actually called in --headless --no-sandbox --disable-gpu mode.
# Two-pronged approach (no network, no sudo, no RPMs):
#   1) patchelf --remove-needed  → strips libs from the binary's DT_NEEDED list
#   2) Python stub .so files     → minimal valid ELF shared libs (pure stdlib)

CHROME_BIN=$(find /home/opc/.cache/ms-playwright -name "chrome-headless-shell" -type f 2>/dev/null | head -1)
LIBDIR=/home/opc/lib
PATCH_MARKER=/home/opc/.chromium_patched
mkdir -p "$LIBDIR"
export LD_LIBRARY_PATH="$LIBDIR:${LD_LIBRARY_PATH:-}"

# Libs safe to remove/stub in headless mode (accessibility, printing, audio, GPU)
STRIP_LIBS="libatk-1.0.so.0 libatk-bridge-2.0.so.0 libcups.so.2 \
  libXcomposite.so.1 libXdamage.so.1 libXfixes.so.3 libXrandr.so.2 \
  libgbm.so.1 libasound.so.2"

if [ -n "$CHROME_BIN" ] && [ -f "$CHROME_BIN" ] && [ ! -f "$PATCH_MARKER" ]; then
  echo "Patching chromium for Oracle Linux 8 (first-time setup)..."

  # ── Method 1: patchelf --remove-needed ─────────────────────────────────────
  pip3 install patchelf --quiet 2>/dev/null || true
  PELF=$(command -v patchelf 2>/dev/null || echo "")
  if [ -z "$PELF" ]; then
    # pip may put it in ~/.local/bin which is in PATH, but try explicit path too
    PELF=$(python3 -c "import sys,os; b=os.path.join(os.path.dirname(sys.executable),'patchelf'); print(b) if os.path.isfile(b) else None" 2>/dev/null || echo "")
  fi
  if [ -n "$PELF" ] && [ -x "$PELF" ]; then
    echo "  patchelf found: $PELF — removing unneeded deps..."
    for lib in $STRIP_LIBS; do
      "$PELF" --remove-needed "$lib" "$CHROME_BIN" 2>/dev/null \
        && echo "    Removed: $lib" || true
    done
    echo "  patchelf step complete."
  else
    echo "  patchelf not available (will rely on stub .so files)."
  fi

  # ── Method 2: stub .so files (pure Python, always runs as safety net) ──────
  echo "  Creating stub .so files in $LIBDIR..."
  python3 - <<'PYEOF'
import struct, os, subprocess

LIBDIR = '/home/opc/lib'
STUBS  = [
    'libatk-1.0.so.0', 'libatk-bridge-2.0.so.0', 'libcups.so.2',
    'libXcomposite.so.1', 'libXdamage.so.1', 'libXfixes.so.3',
    'libXrandr.so.2', 'libgbm.so.1', 'libasound.so.2',
]

def make_stub(path):
    """Minimal valid ELF64 shared library (192 bytes).
    DT_NULL-only .dynamic section satisfies the dynamic linker.
    No exported symbols — safe for libs whose functions are never called."""
    EH, PH, DE = 64, 56, 16
    dyn_off = EH + 2 * PH   # 176
    total   = dyn_off + DE  # 192
    ehdr = struct.pack('<4sBBBBBxxxxxxxHHIQQQIHHHHHH',
        b'\x7fELF', 2, 1, 1, 0, 0,  # ELF magic, ELFCLASS64, LE, v1
        3, 0x3e, 1,                   # ET_DYN, x86-64, version=1
        0, EH, 0,                     # entry=0, phoff=64, shoff=0
        0, EH, PH, 2,                 # flags=0, ehsize, phentsize, phnum=2
        64, 0, 0)                     # shentsize, shnum, shstrndx
    phdr_load = struct.pack('<IIQQQQQQ', 1, 5, 0, 0, 0, total, total, 0x1000)
    phdr_dyn  = struct.pack('<IIQQQQQQ', 2, 6, dyn_off, dyn_off, dyn_off, DE, DE, 8)
    dynamic   = struct.pack('<QQ', 0, 0)  # DT_NULL
    data = ehdr + phdr_load + phdr_dyn + dynamic
    assert len(data) == total, f'ELF size bug: {len(data)} != {total}'
    with open(path, 'wb') as f:
        f.write(data)
    os.chmod(path, 0o755)

ldcfg = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True).stdout
for name in STUBS:
    if name in ldcfg:
        print(f'    {name}: found in ldconfig, skipping stub')
        continue
    p = os.path.join(LIBDIR, name)
    if os.path.exists(p) and os.path.getsize(p) >= 192:
        print(f'    {name}: stub already present')
        continue
    make_stub(p)
    print(f'    Created stub: {name} (192 bytes)')

print('  Stub .so step complete.')
PYEOF

  touch "$PATCH_MARKER"
  echo "Chromium patch done. Stubs in $LIBDIR: $(ls $LIBDIR/*.so* 2>/dev/null | wc -l) files."
elif [ -z "$CHROME_BIN" ]; then
  echo "WARN: chromium binary not found — playwright install may have failed"
else
  echo "Chromium already patched (marker exists), skipping lib setup."
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
