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

# ── Patch chromium: remove unneeded system lib deps ──────────────────────────
# Oracle Linux 8 minimal lacks several libs in chromium's DT_NEEDED list.
# Fix: pure-Python ELF patcher removes them from the binary's DT_NEEDED in-place.
# Fallback: stub .so files satisfy the dynamic linker for any remaining missing libs.

CHROME_BIN=$(find /home/opc/.cache/ms-playwright -name "chrome-headless-shell" -type f 2>/dev/null | head -1)
LIBDIR=/home/opc/lib
PATCH_MARKER=/home/opc/.chromium_elf_patched_v5
mkdir -p "$LIBDIR"
# Do NOT export LD_LIBRARY_PATH=/home/opc/lib — it would make stubs shadow real system libs.
# We rely on ldconfig (which puts /home/opc/lib AFTER system dirs) instead.

if [ -n "$CHROME_BIN" ] && [ -f "$CHROME_BIN" ] && [ ! -f "$PATCH_MARKER" ]; then
  echo "Patching chromium for Oracle Linux 8..."

  # Try playwright install-deps first (knows exactly what chromium needs)
  python3 -m playwright install-deps chromium 2>/dev/null || true
  # Also try dnf directly as backup
  sudo dnf install -y --quiet at-spi2-core at-spi2-atk atk libxkbcommon cups-libs \
    libXcomposite libXdamage libXfixes libXrandr libXtst libXss \
    mesa-libgbm libdrm alsa-lib pango gtk3 2>/dev/null || true

  # Pure-Python ELF patcher: removes missing libs from DT_NEEDED (no patchelf binary needed),
  # then creates stub .so files as a safety net for any remaining missing libs.
  # || true: never let this block kill the pipeline via set -e.
  python3 - <<'PYEOF' || true
import struct, os, glob, subprocess

LIBDIR   = '/home/opc/lib'
os.makedirs(LIBDIR, exist_ok=True)

# ── Find chromium binary ─────────────────────────────────────────────────────
ch = glob.glob('/home/opc/.cache/ms-playwright/**/chrome-headless-shell', recursive=True)
CHROME = ch[0] if ch else None
if not CHROME:
    print('ERROR: chrome-headless-shell not found'); raise SystemExit(1)
print(f'Chromium: {CHROME} ({os.path.getsize(CHROME)//1024//1024} MB)')

# ── ldd: detect ALL missing libs for this binary ────────────────────────────
ALWAYS = [
    # AT accessibility stack
    'libatk-1.0.so.0','libatk-bridge-2.0.so.0','libatspi.so.0',
    # printing / audio
    'libcups.so.2','libasound.so.2',
    # X11 extensions
    'libXcomposite.so.1','libXdamage.so.1','libXfixes.so.3','libXrandr.so.2',
    'libXss.so.1','libXtst.so.6','libX11-xcb.so.1',
    # graphics
    'libgbm.so.1','libdrm.so.2','libEGL.so.1','libGL.so.1','libGLdispatch.so.0',
    # font / text
    'libpango-1.0.so.0','libpangocairo-1.0.so.0','libpangoft2-1.0.so.0',
    'libfontconfig.so.1','libfreetype.so.6',
    # input / keyboard
    'libxkbcommon.so.0','libxkbcommon-x11.so.0',
    # GTK (needed by some chromium internal libs even in headless)
    'libgtk-3.so.0','libgdk-3.so.0',
]
missing = []
try:
    r = subprocess.Popen(['ldd', CHROME], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = r.communicate(timeout=20)
    for line in out.decode(errors='replace').splitlines():
        if '=>' in line and 'not found' in line:
            lib = line.strip().split()[0]
            if '.so' in lib:
                missing.append(lib)
    print(f'ldd missing: {missing}')
except Exception as e:
    print(f'ldd failed ({e}), using fixed list')
    missing = list(ALWAYS)

if not missing:
    print('ldd found no missing libs — will still create ALWAYS stubs for transitive deps.')

# ── Step 1: ELF binary patch — remove DT_NEEDED for missing libs ─────────────
# This is patchelf --remove-needed implemented in pure Python (no external tools).
# It shifts DT_NEEDED entries up to fill gaps, null-terminates the section.
def elf_remove_needed(path, remove_set):
    with open(path, 'r+b') as f:
        data = bytearray(f.read())
    assert data[:4] == b'\x7fELF', 'Not ELF'
    assert data[4] == 2 and data[5] == 1, 'Expected 64-bit LE ELF'
    E = '<'
    e_phoff     = struct.unpack_from(E+'Q', data, 32)[0]
    e_phentsize = struct.unpack_from(E+'H', data, 54)[0]
    e_phnum     = struct.unpack_from(E+'H', data, 56)[0]

    dyn_off = dyn_sz = None
    loads = []
    for i in range(e_phnum):
        ph = e_phoff + i * e_phentsize
        ptype  = struct.unpack_from(E+'I', data, ph)[0]
        poff   = struct.unpack_from(E+'Q', data, ph+8)[0]
        pvaddr = struct.unpack_from(E+'Q', data, ph+16)[0]
        pfsz   = struct.unpack_from(E+'Q', data, ph+32)[0]
        if ptype == 2:   dyn_off, dyn_sz = poff, pfsz   # PT_DYNAMIC
        elif ptype == 1: loads.append((pvaddr, poff, pfsz))  # PT_LOAD

    if dyn_off is None: raise ValueError('No PT_DYNAMIC')

    def va2off(va):
        for v,o,sz in loads:
            if v <= va < v+sz: return o+(va-v)
        raise ValueError(f'VA 0x{va:x} unmapped')

    DT_NULL=0; DT_NEEDED=1; DT_STRTAB=5; ESIZ=16
    strtab_vaddr = None
    entries = []
    for i in range(dyn_sz // ESIZ):
        base = dyn_off + i*ESIZ
        tag  = struct.unpack_from(E+'q', data, base)[0]
        val  = struct.unpack_from(E+'Q', data, base+8)[0]
        entries.append((base, tag, val))
        if tag == DT_NULL: break
        if tag == DT_STRTAB: strtab_vaddr = val

    if strtab_vaddr is None: raise ValueError('No DT_STRTAB')
    strtab_off = va2off(strtab_vaddr)

    def rstr(off):
        end = data.index(b'\x00', strtab_off+off)
        return data[strtab_off+off:end].decode()

    # Partition entries: keep vs remove
    removed = []
    keep = []
    for base, tag, val in entries:
        if tag == DT_NEEDED and rstr(val) in remove_set:
            removed.append(rstr(val))
        else:
            keep.append((base, tag, val))

    if not removed: return []

    # Write kept entries at original positions (shifts removed entries out)
    bases = [e[0] for e in entries]
    for i, (_, tag, val) in enumerate(keep):
        struct.pack_into(E+'q', data, bases[i], tag)
        struct.pack_into(E+'Q', data, bases[i]+8, val)
    # Null out trailing positions
    for i in range(len(keep), len(entries)):
        struct.pack_into(E+'Q', data, bases[i], 0)
        struct.pack_into(E+'Q', data, bases[i]+8, 0)

    with open(path, 'r+b') as f:
        f.write(data)
    return removed

try:
    removed = elf_remove_needed(CHROME, set(missing))
    print(f'Removed from DT_NEEDED: {removed}')
except Exception as e:
    print(f'ELF patch failed ({e}) — will rely on stub .so files')

# ── Step 2: Stub .so files for any remaining missing libs ────────────────────
# Stubs are a safety net: if ELF patch worked, these never get loaded.
# If ELF patch failed, stubs satisfy the dynamic linker at load time.
# They contain no symbols — safe only if the missing functions are never called.
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

ldcfg = ''
for cmd in ['ldconfig','/sbin/ldconfig','/usr/sbin/ldconfig']:
    try:
        r = subprocess.Popen([cmd,'-p'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        ldcfg = r.communicate(timeout=5)[0].decode(errors='replace'); break
    except: pass

stub_list = list(set(missing + ALWAYS))

# First: DELETE old stubs that now shadow real system libs.
# Stubs have no symbols; if a real lib was installed (by dnf/playwright) but an old stub
# still exists in LIBDIR, anything finding the stub first would crash on symbol lookup.
for lib in stub_list:
    if '.so' not in lib: continue
    p = os.path.join(LIBDIR, lib)
    if not os.path.exists(p): continue
    is_real = lib in ldcfg or any(os.path.exists(os.path.join(d,lib)) for d in ['/lib64','/usr/lib64','/lib','/usr/lib'])
    if is_real:
        try: os.remove(p); print(f'Removed shadowing stub: {lib}')
        except Exception as e: print(f'Could not remove stub {lib}: {e}')

# Then: create stubs only for libs still not available on the system.
for lib in stub_list:
    if '.so' not in lib: continue
    if lib in ldcfg: continue
    if any(os.path.exists(os.path.join(d,lib)) for d in ['/lib64','/usr/lib64','/lib','/usr/lib']): continue
    p = os.path.join(LIBDIR, lib)
    make_stub(p); print(f'Stub: {lib}')

# Register /home/opc/lib with the system linker cache so stubs are found
# by ALL processes (including chromium subprocesses) without LD_LIBRARY_PATH.
try:
    subprocess.run(
        ['sudo', 'bash', '-c', 'echo /home/opc/lib > /etc/ld.so.conf.d/vm-stubs.conf && /sbin/ldconfig'],
        timeout=15, check=False)
    print('ldconfig updated — stubs registered system-wide.')
except Exception as e:
    print(f'ldconfig registration failed ({e}) — relying on LD_LIBRARY_PATH.')

print('Patch complete.')
PYEOF

  touch "$PATCH_MARKER"
  echo "Chromium patched. Stubs in $LIBDIR: $(ls $LIBDIR/*.so* 2>/dev/null | wc -l)"

elif [ -z "$CHROME_BIN" ]; then
  echo "WARN: chromium binary not found"
else
  echo "Chromium already patched (marker exists)."
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
