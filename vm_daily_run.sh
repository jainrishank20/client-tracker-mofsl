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

# ── Install chromium system libs (browse repo to find correct version) ────────
export LD_LIBRARY_PATH=/home/opc/lib:${LD_LIBRARY_PATH:-}
if ! ls /home/opc/lib/libatk-1.0* 2>/dev/null | grep -q .; then
  echo "Installing chromium system libs via AlmaLinux 8 repo..."
  python3 - <<'PYEOF'
import urllib.request, urllib.error, re, subprocess, os, shutil, sys, glob

LIBDIR = '/home/opc/lib'
os.makedirs(LIBDIR, exist_ok=True)

# Packages: (so_prefix, [repo_base_urls_to_search], pkg_name_prefix)
PKGS = [
    ('libatk-1.0',        ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'atk'),
    ('libatk-bridge-2.0', ['https://repo.almalinux.org/almalinux/8/AppStream/x86_64/os/Packages/'], 'at-spi2-atk'),
    ('libcups',           ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'cups-libs'),
    ('libdrm',            ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'libdrm'),
    ('libXcomposite',     ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'libXcomposite'),
    ('libXdamage',        ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'libXdamage'),
    ('libXfixes',         ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'libXfixes'),
    ('libXrandr',         ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'libXrandr'),
    ('libgbm',            ['https://repo.almalinux.org/almalinux/8/AppStream/x86_64/os/Packages/'], 'mesa-libgbm'),
    ('libpango-1.0',      ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'pango'),
    ('libasound',         ['https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages/'],  'alsa-lib'),
]

def find_rpm_url(bases, pkg_prefix):
    for base in bases:
        try:
            with urllib.request.urlopen(base, timeout=20) as r:
                html = r.read().decode(errors='replace')
            # Match e.g. atk-2.28.1-3.el8.x86_64.rpm (not debuginfo/devel/src)
            pat = rf'href="({re.escape(pkg_prefix)}-[\d][^"]*\.x86_64\.rpm)"'
            hits = [m for m in re.findall(pat, html)
                    if 'debug' not in m and 'devel' not in m and 'src' not in m]
            if hits:
                return base + hits[0]
        except Exception as e:
            print(f"  Browse {base}: {e}")
    return None

def install_rpm(url, so_prefix):
    f, ex = '/tmp/_cdep.rpm', '/tmp/_cdepex'
    try:
        urllib.request.urlretrieve(url, f)
        os.makedirs(ex, exist_ok=True)
        subprocess.run(f'cd {ex} && rpm2cpio {f} | cpio -idm --quiet',
                       shell=True, capture_output=True)
        for src in glob.glob(f'{ex}/**/{so_prefix}*.so*', recursive=True):
            dst = os.path.join(LIBDIR, os.path.basename(src))
            shutil.copy2(src, dst)
            print(f"  Copied {os.path.basename(src)}")
            # create unversioned symlink if needed
            base = os.path.basename(src).split('.so')[0] + '.so.' + os.path.basename(src).split('.so.')[1].split('.')[0] if '.so.' in os.path.basename(src) else None
        return True
    except Exception as e:
        print(f"  install_rpm error: {e}")
        return False
    finally:
        shutil.rmtree(ex, ignore_errors=True)
        try: os.remove(f)
        except: pass

errors = []
for so_prefix, bases, pkg in PKGS:
    if glob.glob(f'{LIBDIR}/{so_prefix}*'):
        continue
    print(f"Fetching {pkg} ({so_prefix})...")
    url = find_rpm_url(bases, pkg)
    if not url:
        errors.append(f"No RPM found for {pkg}")
        continue
    print(f"  URL: {url}")
    install_rpm(url, so_prefix)

# Create missing symlinks (e.g. libatk-1.0.so.0 -> libatk-1.0.so.0.28100.1)
for f in glob.glob(f'{LIBDIR}/*.so.*'):
    base = f
    parts = os.path.basename(f).split('.so.')
    if len(parts) == 2 and '.' in parts[1]:
        major = parts[1].split('.')[0]
        link = os.path.join(LIBDIR, parts[0] + '.so.' + major)
        if not os.path.exists(link):
            os.symlink(os.path.basename(f), link)
            print(f"  Symlinked {os.path.basename(link)}")

if errors:
    print('Errors:', errors)
else:
    print('All libs installed OK')
PYEOF
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
