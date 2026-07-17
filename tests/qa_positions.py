"""
QA checks that run after import_all.py:

  1. Stale MTF positions — MTF open > 60 days = almost certainly a data error
     (brokers auto-square MTF; real positions never sit open this long)

  2. Open position delta — compares today's open set against the previous
     snapshot (pnl_snapshot.json). New positions that appear for a client
     that already has a trade history are flagged for manual review.

Exits non-zero on any ERROR so the pipeline health alert fires.
Warnings are printed but do not block the run.
"""
import json, os, sys
from datetime import date, timedelta
from collections import defaultdict

BASE         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_PATH  = os.path.join(BASE, 'trades.json')
SNAP_PATH    = os.path.join(BASE, 'pnl_snapshot.json')
POS_SNAP     = os.path.join(BASE, 'open_positions_snapshot.json')

ERRORS   = []
WARNINGS = []

def err(msg):  ERRORS.append(msg)
def warn(msg): WARNINGS.append(msg)

today = date.today()

# ── Load trades ───────────────────────────────────────────────────────────────
if not os.path.exists(TRADES_PATH):
    print("FATAL: trades.json not found"); sys.exit(1)

trades = json.load(open(TRADES_PATH))
open_trades  = [t for t in trades if not t.get('exit_date')]
by_client    = defaultdict(list)
for t in open_trades:
    by_client[t['client']].append(t)

# ── CHECK 1: stale MTF positions ──────────────────────────────────────────────
MTF_PRODUCTS  = {'MTF', 'MTFX', 'MARGIN', 'MTF DELIVERY'}
MTF_THRESHOLD = timedelta(days=60)

stale = []
for t in open_trades:
    prod = str(t.get('product', '')).upper().strip()
    if prod not in MTF_PRODUCTS:
        continue
    try:
        entry = date.fromisoformat(t['entry_date'])
    except Exception:
        continue
    age = today - entry
    if age > MTF_THRESHOLD:
        stale.append((t['client'], t['script'], t['buy_qty'], t['entry_date'], age.days))

if stale:
    err(f"{len(stale)} stale MTF position(s) open > 60 days (likely data error — sell CSV missing):")
    for client, script, qty, entry, days in sorted(stale):
        err(f"  {client} | {script} | qty={qty} | entry={entry} | {days}d old")

# ── CHECK 2: open position delta ──────────────────────────────────────────────
# Build today's open set: {client: set of script names}
today_open = {c: set(t['script'] for t in ts) for c, ts in by_client.items()}

# Load previous snapshot
prev_open = {}
if os.path.exists(POS_SNAP):
    try:
        snap = json.load(open(POS_SNAP))
        prev_date = snap.get('date', '')
        prev_open = {c: set(scripts) for c, scripts in snap.get('open', {}).items()}
        print(f"Comparing vs open positions snapshot from {prev_date}")
    except Exception as e:
        print(f"Could not load open positions snapshot: {e}")

# Flag positions that newly appeared today vs yesterday
established_clients = set(prev_open.keys())  # clients we have prior data for
newly_opened = {}
for client in established_clients:
    prev = prev_open.get(client, set())
    curr = today_open.get(client, set())
    appeared = curr - prev
    if appeared:
        newly_opened[client] = appeared

if newly_opened:
    warn("New open positions appeared since last run — verify these are real buys:")
    for client, scripts in sorted(newly_opened.items()):
        warn(f"  {client}: {sorted(scripts)}")

# Save today's snapshot (always overwrite so next run has fresh baseline)
try:
    snap_data = {
        'date': today.isoformat(),
        'open': {c: sorted(scripts) for c, scripts in today_open.items()},
    }
    json.dump(snap_data, open(POS_SNAP, 'w'), indent=2)
    print(f"Saved open positions snapshot ({sum(len(v) for v in today_open.values())} positions across {len(today_open)} clients)")
except Exception as e:
    print(f"Could not save open positions snapshot: {e}")

# ── Report ────────────────────────────────────────────────────────────────────
print()
print(f"Open positions: {len(open_trades)} across {len(by_client)} clients")
print()

if WARNINGS:
    print("=== WARNINGS ===")
    for w in WARNINGS:
        print(f"  ⚠  {w}")
    print()

if ERRORS:
    print("=== ERRORS (pipeline will flag for review) ===")
    for e in ERRORS:
        print(f"  ✗  {e}")
    print()
    sys.exit(1)

print("✅ Position QA passed")
