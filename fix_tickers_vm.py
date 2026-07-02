"""
Run on VM: reads trades.json to find exact script names, then writes ticker_overrides.json.
Usage:  python3 fix_tickers_vm.py
"""
import json, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
OVERRIDES_FILE = os.path.join(BASE, "ticker_overrides.json")
TRADES_FILE    = os.path.join(BASE, "trades.json")

# Map: keyword (upper) -> NSE ticker
KEYWORD_MAP = {
    "BALKRISHNA IND":        "BALKRISIND",
    "BALKRISHNA INDUSTRIES": "BALKRISIND",
    "CARBORUNDUM UNIVERSAL": "CARBORUNIV",
    "HCL TECHNOLOGIES":      "HCLTECH",
    "KFIN TECHNOLOGIES":     "KFINTECH",
    "THOMAS COOK":           "THOMASCOOK",
    "OLECTRA GREENTECH":     "OLECTRA",   # update if correct ticker differs
}

# Load existing overrides
if os.path.exists(OVERRIDES_FILE):
    with open(OVERRIDES_FILE) as f:
        overrides = json.load(f)
else:
    overrides = {}

# Load trades to find exact script names used
if os.path.exists(TRADES_FILE):
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    scripts = set(t.get("script", "") for t in trades)
else:
    scripts = set()

added = []
for script in scripts:
    su = script.strip().upper()
    for keyword, ticker in KEYWORD_MAP.items():
        if keyword in su:
            if overrides.get(script) != ticker:
                overrides[script] = ticker
                added.append(f"  {script!r:45s} -> {ticker}")
            break

if added:
    with open(OVERRIDES_FILE, "w") as f:
        json.dump(overrides, f, indent=2)
    print("Saved ticker_overrides.json. New/updated mappings:")
    for a in added:
        print(a)
else:
    print("No new mappings needed.")

print(f"\nFull overrides ({len(overrides)} entries):")
for k, v in sorted(overrides.items()):
    print(f"  {k!r:45s}: {v!r}")
