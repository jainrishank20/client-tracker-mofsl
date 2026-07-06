"""
VM-side daily automation — runs entirely on Oracle VM via cron.
Cron entry (8:30 PM IST = 3:00 PM UTC, Mon-Fri):
    0 15 * * 1-5 cd /home/opc/client-tracker-mofsl && python3 run_daily_vm.py >> logs/daily.log 2>&1

No laptop required. Steps:
  1. Clean old current-FY CSVs from mo_csvs/
  2. Download fresh CSVs + scrape ledger from CBOS (Playwright headless)
  3. import_all.py  → rebuild trades.json
  4. vm_sync_gsheet.py → GSheet sync
  5. send_notify.py   → Telegram ledger summary
"""
import subprocess, os, glob, sys, time, json, urllib.request, urllib.parse

BASE    = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE, 'mo_csvs'), exist_ok=True)

FULL = "--full" in sys.argv

cfg      = json.load(open(os.path.join(BASE, 'bot_config.json')))
TOKEN    = cfg['telegram_token']
CHAT_IDS = [c.strip() for c in str(cfg['allowed_chat_id']).split(',')]


def tg_send(text):
    for chat_id in CHAT_IDS:
        try:
            data = urllib.parse.urlencode(
                {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
            ).encode()
            urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data
                ), timeout=10
            )
        except Exception:
            pass


def log(msg):
    print(f"\n{'='*55}\n{msg}\n{'='*55}", flush=True)


log(f"Daily run started: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    + (" [FULL MODE]" if FULL else ""))
tg_send(f"⏳ Daily run started: {time.strftime('%d %b %Y %I:%M %p')}")

# ── Step 1: Clean old current-FY CSVs ────────────────────────────────────────
log("[1/5] Cleaning old CSVs...")
pattern = os.path.join(BASE, 'mo_csvs', 'TradeDetailsAndSummary_RIMK*.csv')
old = glob.glob(pattern)
for f in old:
    os.remove(f)
    print(f"  Deleted: {os.path.basename(f)}")
if not old:
    print("  Nothing to clean.")

# ── Step 2: Download from CBOS ────────────────────────────────────────────────
log("[2/5] Downloading CSVs from CBOS (headless)...")
args = [sys.executable, os.path.join(BASE, 'mo_downloader.py')]
if FULL:
    args.append('--full')
r = subprocess.run(args, capture_output=True, text=True)
print(r.stdout)
if r.stderr:
    print(f"STDERR: {r.stderr}")
if r.returncode != 0:
    msg = "❌ Daily run FAILED at step 2 (CBOS download). Check logs."
    print(msg)
    tg_send(msg)
    sys.exit(1)

csvs = glob.glob(os.path.join(BASE, 'mo_csvs', 'TradeDetailsAndSummary_RIMK*.csv'))
print(f"  Downloaded {len(csvs)} CSV(s)")
if not csvs:
    msg = "❌ Daily run FAILED: no CSVs after download."
    print(msg)
    tg_send(msg)
    sys.exit(1)

# ── Step 3: Import ────────────────────────────────────────────────────────────
log("[3/5] Rebuilding trades...")
r = subprocess.run([sys.executable, os.path.join(BASE, 'import_all.py')],
                   capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    msg = "❌ Daily run FAILED at step 3 (import_all)."
    print(msg)
    tg_send(msg)
    sys.exit(1)

# ── Step 4: GSheet sync ───────────────────────────────────────────────────────
log("[4/5] Syncing Google Sheet...")
r = subprocess.run([sys.executable, os.path.join(BASE, 'vm_sync_gsheet.py')],
                   capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    print(f"GSheet sync error: {r.stderr}")

# ── Step 5: Telegram notify ───────────────────────────────────────────────────
log("[5/5] Sending Telegram summary...")
r = subprocess.run([sys.executable, os.path.join(BASE, 'send_notify.py')],
                   capture_output=True, text=True)
print(r.stdout)

log(f"Daily run completed: {time.strftime('%Y-%m-%d %H:%M:%S')}")
