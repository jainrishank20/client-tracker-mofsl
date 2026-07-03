"""
Daily automation — runs via Windows Task Scheduler at 8:30 PM (Mon-Fri)
or automatically on laptop login if the day's run was missed.

Flow:
  1. Clean old current-FY CSVs from local Downloads folder
  2. Download fresh current-FY CSVs from CBOS (Playwright on laptop)
  3. SCP CSVs to VM (VM already has last-FY CSVs from initial setup)
  4. VM: import_all.py  → rebuild trades.json from ALL CSVs (both FYs)
  5. VM: vm_sync_gsheet.py → push to Google Sheet
  6. VM: send_notify.py → Telegram summary

Run manually anytime by double-clicking "Run Daily Update" on Desktop.
For a full rebuild (both FYs): python run_daily.py --full
"""
import subprocess, os, glob, sys, time

SSH_KEY  = r"C:\Users\jainr\Downloads\ssh-key-2026-06-11.key"
VM_USER  = "opc"
VM_HOST  = "152.67.164.204"
VM_DIR   = "/home/opc/client-tracker-mofsl"
CSV_DIR  = r"C:\Users\jainr\Downloads\MO_Trades"
BASE     = os.path.dirname(os.path.abspath(__file__))
FULL     = "--full" in sys.argv

SSH_OPTS = ["-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30"]


def ssh(cmd, check=False):
    r = subprocess.run(["ssh"] + SSH_OPTS + [f"{VM_USER}@{VM_HOST}", cmd],
                       capture_output=True, text=True)
    print(r.stdout.strip())
    if r.stderr.strip():
        print(f"  stderr: {r.stderr.strip()}")
    if check and r.returncode != 0:
        raise RuntimeError(f"SSH command failed: {cmd}")
    return r.returncode == 0


def scp(local_files, remote_dir):
    r = subprocess.run(
        ["scp"] + SSH_OPTS + local_files + [f"{VM_USER}@{VM_HOST}:{remote_dir}/"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"  SCP error: {r.stderr.strip()}")
    return r.returncode == 0


def log(msg):
    print(f"\n{'='*55}\n{msg}\n{'='*55}")


log(f"Daily run started: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    + (" [FULL MODE]" if FULL else ""))

# ── Step 1: Clean old current-FY CSVs ────────────────────────────────────────
log("[1/5] Cleaning old CSVs from Downloads...")
pattern = os.path.join(CSV_DIR, "TradeDetailsAndSummary_RIMK*.csv")
old = glob.glob(pattern)
for f in old:
    os.remove(f)
    print(f"  Deleted: {os.path.basename(f)}")
if not old:
    print("  Nothing to clean.")

# ── Step 2: Download from CBOS ────────────────────────────────────────────────
log("[2/5] Downloading CSVs from CBOS...")
args = [sys.executable, os.path.join(BASE, "mo_downloader.py")]
if FULL:
    args.append("--full")
r = subprocess.run(args)
if r.returncode != 0:
    print("ERROR: Download failed. Aborting.")
    sys.exit(1)

# ── Step 3: SCP to VM ────────────────────────────────────────────────────────
# (ledger.json is scraped inside mo_downloader.py in the same CBOS session)
log("[3/5] Uploading CSVs + ledger to VM...")
csvs = glob.glob(os.path.join(CSV_DIR, "TradeDetailsAndSummary_RIMK*.csv"))
if not csvs:
    print("No CSV files found after download. Aborting.")
    sys.exit(1)
print(f"  Uploading {len(csvs)} CSV(s)...")
ssh(f"mkdir -p {VM_DIR}/mo_csvs")
if not scp(csvs, f"{VM_DIR}/mo_csvs"):
    print("ERROR: Upload failed. Aborting.")
    sys.exit(1)
ledger_path = os.path.join(BASE, "ledger.json")
if os.path.exists(ledger_path):
    scp([ledger_path], VM_DIR)
    print("  Uploaded ledger.json")

# ── Step 4: Import on VM ─────────────────────────────────────────────────────
log("[4/5] Rebuilding trades on VM...")
ssh(f"cd {VM_DIR} && python3 import_all.py 2>&1", check=True)

# ── Step 5: GSheet + Telegram ────────────────────────────────────────────────
log("[5/5] Syncing Google Sheet and sending Telegram...")
ssh(f"cd {VM_DIR} && python3 vm_sync_gsheet.py 2>&1")
ssh(f"cd {VM_DIR} && python3 send_notify.py 2>&1")

log(f"Daily run completed: {time.strftime('%Y-%m-%d %H:%M:%S')}")
