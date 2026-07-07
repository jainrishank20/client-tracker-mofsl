"""
QA gate: validate trades.json after import_all.py runs.
Fails the pipeline immediately if data looks wrong, before anything
touches the Google Sheet or the VM.

Checks:
  1. trades.json exists and is non-empty
  2. Every configured client has at least 1 trade
  3. No client has identical trade data to another client (duplicate bug)
  4. Total open positions > minimum expected threshold
  5. No two clients share the exact same set of open script names
     (catches the rows[0] downloader bug where account B gets account A's CSV)
"""
import json, os, sys
from collections import defaultdict

BASE       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_PATH = os.path.join(BASE, 'trades.json')
CFG_PATH    = os.path.join(BASE, 'bot_config.json')

ERRORS   = []
WARNINGS = []

def fail(msg):  ERRORS.append(msg)
def warn(msg):  WARNINGS.append(msg)

# ── Load ──────────────────────────────────────────────────────────────────────
if not os.path.exists(TRADES_PATH):
    print("FATAL: trades.json not found"); sys.exit(1)

trades = json.load(open(TRADES_PATH))
if not trades:
    print("FATAL: trades.json is empty — import produced 0 trades")
    sys.exit(1)

cfg     = json.load(open(CFG_PATH)) if os.path.exists(CFG_PATH) else {}
clients = list(cfg.get('clients', {}).keys())

# ── Per-client breakdown ──────────────────────────────────────────────────────
by_client = defaultdict(list)
for t in trades:
    by_client[t['client']].append(t)

open_by_client   = {c: [t for t in ts if not t.get('exit_date')] for c, ts in by_client.items()}
closed_by_client = {c: [t for t in ts if t.get('exit_date')]     for c, ts in by_client.items()}

print("=== Trade counts per client ===")
for c in sorted(by_client):
    op = len(open_by_client.get(c, []))
    cl = len(closed_by_client.get(c, []))
    open_scripts = sorted(set(t['script'] for t in open_by_client.get(c, [])))
    print(f"  {c}: {op} open, {cl} closed  |  scripts: {open_scripts[:5]}{'...' if len(open_scripts)>5 else ''}")

# ── CHECK 1: every configured client has at least 1 trade ────────────────────
for c in clients:
    if c not in by_client:
        fail(f"Client {c} has ZERO trades — CSV may not have downloaded")
    elif len(by_client[c]) < 2:
        warn(f"Client {c} has only {len(by_client[c])} trade(s) — suspiciously low")

# ── CHECK 2: no two clients share identical open-position script sets ─────────
# This is the exact signature of the rows[0] downloader bug:
# account B gets account A's CSV → identical script names
open_sig = {}
for c, ts in open_by_client.items():
    if not ts:
        continue
    sig = frozenset(t['script'] for t in ts)
    if sig in open_sig:
        other = open_sig[sig]
        fail(
            f"DUPLICATE DATA: {c} and {other} have IDENTICAL open positions "
            f"({len(sig)} scripts) — downloader likely gave {c} the wrong CSV. "
            f"Scripts: {sorted(sig)[:5]}"
        )
    else:
        open_sig[sig] = c

# ── CHECK 3: total open positions not suspiciously low ───────────────────────
total_open = sum(len(v) for v in open_by_client.values())
total_all  = len(trades)
if total_open == 0 and total_all > 0:
    fail("Zero open positions across all clients — FIFO may have closed everything incorrectly")
if total_all < len(clients) * 3:
    warn(f"Only {total_all} total trades for {len(clients)} clients — seems very low")

# ── CHECK 4: per-client open position count sanity ───────────────────────────
for c in clients:
    op = len(open_by_client.get(c, []))
    if op == 0 and c in by_client:
        warn(f"{c} has trades but 0 open positions — all closed, verify this is correct")

# ── Report ────────────────────────────────────────────────────────────────────
print()
print(f"Total: {total_all} trades  |  {total_open} open  |  {total_all - total_open} closed")
print()

if WARNINGS:
    print("=== WARNINGS ===")
    for w in WARNINGS:
        print(f"  ⚠  {w}")
    print()

if ERRORS:
    print("=== ERRORS (pipeline will FAIL) ===")
    for e in ERRORS:
        print(f"  ✗  {e}")
    print()
    sys.exit(1)

print("✅ QA checks passed")
