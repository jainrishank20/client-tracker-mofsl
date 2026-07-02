"""
Daily automation — runs on Windows laptop via Task Scheduler at 8:30 PM.
1. Downloads CSVs from CBOS (Playwright on laptop)
2. SCPs CSVs to VM
3. SSHs into VM: import_all → gsheet sync → telegram
"""
import subprocess, os, glob, sys, time

SSH_KEY  = r"C:\Users\jainr\Downloads\ssh-key-2026-06-11.key"
VM_USER  = "opc"
VM_HOST  = "152.67.164.204"
VM_DIR   = "/home/opc/client-tracker-mofsl"
CSV_DIR  = r"C:\Users\jainr\Downloads\MO_Trades"
BASE     = os.path.dirname(os.path.abspath(__file__))

SSH_OPTS = ["-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30"]

def ssh(cmd):
    result = subprocess.run(
        ["ssh"] + SSH_OPTS + [f"{VM_USER}@{VM_HOST}", cmd],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"STDERR: {result.stderr}")
    return result.returncode == 0

def scp(local_files, remote_dir):
    result = subprocess.run(
        ["scp"] + SSH_OPTS + local_files + [f"{VM_USER}@{VM_HOST}:{remote_dir}/"],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"SCP error: {result.stderr}")
    return result.returncode == 0


print("=" * 50)
print(f"Daily run started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 50)

# Step 1: Download CSVs
print("\n[1/4] Downloading CSVs from CBOS...")
r = subprocess.run([sys.executable, os.path.join(BASE, "mo_downloader.py")])
if r.returncode != 0:
    print("ERROR: Download failed. Aborting.")
    sys.exit(1)

# Step 2: SCP CSVs to VM
print("\n[2/4] Uploading CSVs to VM...")
csvs = glob.glob(os.path.join(CSV_DIR, "TradeDetailsAndSummary_RIMK*.csv"))
if not csvs:
    print("No CSV files found. Aborting.")
    sys.exit(1)
print(f"  Uploading {len(csvs)} files...")
ssh(f"mkdir -p {VM_DIR}/mo_csvs")
if not scp(csvs, f"{VM_DIR}/mo_csvs"):
    print("ERROR: SCP failed. Aborting.")
    sys.exit(1)
print(f"  Uploaded {len(csvs)} CSVs to VM.")

# Step 3: Import on VM
print("\n[3/4] Running import on VM...")
ok = ssh(f"cd {VM_DIR} && python3 import_all.py 2>&1")
if not ok:
    print("WARNING: Import may have failed.")

# Step 4: GSheet sync + Telegram on VM
print("\n[4/4] Syncing GSheet and sending Telegram...")
ssh(f"cd {VM_DIR} && python3 vm_sync_gsheet.py 2>&1 && python3 send_notify.py 2>&1")

print("\n" + "=" * 50)
print(f"Daily run completed: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 50)
