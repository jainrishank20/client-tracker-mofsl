# Client Tracker MOFSL — Runbook

VM: `140.245.200.121` (Oracle Linux 9), user `opc`, app at `/home/opc/app/`
SSH key: `~Downloads/ssh-key-2026-07-27 (1).key`

---

## Daily Pipeline

Normal flow (triggered by GHA `daily_run.yml` at ~9:30 AM IST):
1. `vm_daily_run.sh` runs on VM
2. Downloads CSVs via Playwright/Chromium headless
3. `import_all.py` parses CSVs → `trades.json`
4. `send_notify.py` sends Telegram summary
5. GHA `sync_sheet.yml` updates GSheet CMP via yfinance

Manual trigger (skip download, just re-notify/sync):
```
# Via GitHub Actions UI → daily_run.yml → Run workflow → skip_download=true
# Or via API:
curl -X POST -H "Authorization: token $PAT" \
  "https://api.github.com/repos/jainrishank20/client-tracker-mofsl/actions/workflows/sync_sheet.yml/dispatches" \
  -d '{"ref":"main"}'
```

Deploy bot only (after code changes):
```
curl -X POST -H "Authorization: token $PAT" \
  "https://api.github.com/repos/jainrishank20/client-tracker-mofsl/actions/workflows/deploy_bot.yml/dispatches" \
  -d '{"ref":"main"}'
```

---

## VM Setup — Chromium Stubs

### Problem
Playwright's `chromium.launch()` crashes with SIGSEGV on Oracle Linux 9 because
system libs (ATK, ALSA, xkbcommon, XFixes, etc.) are missing. Python-generated
ELF stubs with no exported symbols cause the crash.

### Fix
`vm_daily_run.sh` compiles proper GCC stubs on first run. Marker file controls
one-time setup:
- **Marker**: `/home/opc/.chromium_setup_v2`  (v1 = old broken Python stubs, delete it)
- **Libs**: `/home/opc/lib/` — libatk-1.0.so.0, libatk-bridge-2.0.so.0, libatspi.so.0,
  libgbm.so.1, libxkbcommon.so.0, libasound.so.2, libXfixes.so.3, libXdamage.so.1,
  libXcomposite.so.1, libXrandr.so.2
- **LD path**: `LD_LIBRARY_PATH=/home/opc/lib` set in `vm_daily_run.sh`

### Re-trigger setup (e.g. after VM rebuild)
```bash
ssh -i ~/ssh-key.key opc@140.245.200.121
rm -f /home/opc/.chromium_setup_v2
# Then re-run the script — it will recompile all stubs
bash /home/opc/app/vm_daily_run.sh
```

### Key versioned symbols required
- `libasound.so.2`: needs version scripts `ALSA_0.9` and `ALSA_0.9.0rc4`
- `libxkbcommon.so.0`: needs version script `V_0.5.0`

---

## VM SSH Down (Common After Heavy Runs)

Symptom: `ssh: connect to host 140.245.200.121 port 22: Connection refused`

Fix via OCI Cloud Shell:
1. OCI Console → Cloud Shell (top right)
2. Upload SSH key or paste it:
   ```bash
   nano ~/ssh-key.key   # paste private key content, Ctrl+X to save
   chmod 600 ~/ssh-key.key
   ```
3. SSH: `ssh -i ~/ssh-key.key opc@140.245.200.121`
4. Check sshd: `sudo systemctl status sshd` / `sudo systemctl restart sshd`

---

## Symbol Resolution — ticker_overrides.json

### Problem
`vm_sync_gsheet.py` uses yfinance with `.NS` suffix. CBOS trade CSVs use long
company names that don't match NSE tickers. `symbol_map.py` has a fallback chain
but fails for non-obvious names.

### How to fix a #N/A in GSheet
1. Find the unresolved company name (GSheet QA message in Telegram shows them)
2. Add to `ticker_overrides.json` in both spaced and non-spaced forms:
   ```json
   "CEMINDIA PROJECTS": "CEMPRO",
   "CEMINDIAPROJECTS": "CEMPRO"
   ```
3. Update via GitHub API or direct commit
4. Re-trigger `sync_sheet.yml`

### Known permanent overrides (as of Aug 2026)
- `MISHRA DHATU NIGAM` / `MISHRADHATUNIGAM` → `MIDHANI`
- `CEMINDIA PROJECTS` / `CEMINDIAPROJECTS` → `CEMPRO`
- `DECCAN GOLD MINES` / `DECCANGOLDMINES` → `DECCANGOLD`
- `TATAMOTORS` → `TATAMOTORS` (explicit to prevent strip logic breaking it)

### BSE-only stocks (CMP will always show N/A)
`DECCANGOLD` is BSE-only — yfinance `.NS` returns 404. Workaround would require
`.BO` fallback in `vm_sync_gsheet.py` (not yet implemented). Known limitation.

---

## Trade Count — Open Positions

### Problem
`import_all.py` uses FIFO matching → creates one trade row per buy tranche, not
per unique company. A client holding 3 tranches of RELIANCE = 3 open rows.

### Rule
**Never change `import_all.py` FIFO logic** — it correctly handles partial exits
and exit price averaging.

### Display fix (send_notify.py)
Open count = unique `(client, script)` pairs where net `buy_qty - sell_qty > 0`.
Already fixed as of Aug 2026. If it regresses, the fix is lines ~33-47:

```python
from collections import defaultdict as _dd
_net = _dd(float)
_closed_scripts = _dd(set)
for t in trades:
    if not t.get('exit_date'):
        _net[(t['client'], t['script'])] += (t.get('buy_qty') or 0) - (t.get('sell_qty') or 0)
    else:
        _closed_scripts[t['client']].add(t['script'])

open_count   = sum(1 for v in _net.values() if v > 0)
closed_count = len(set(s for ss in _closed_scripts.values() for s in ss))
```

---

## New Client Onboarding

Every new client added to `bot_config.json` must also be added to `NO_HISTORY_FY`
in `mo_downloader.py`. Most clients have no FY25-26 trades — without this flag
the downloader tries to fetch history that doesn't exist and errors.

---

## Terminal Filter

Terminal ID `30023` is the dealer terminal — always excluded from CSV imports.
Controlled by `EXCLUDED_TERMINALS` list in `import_all.py`. Do not remove it.

---

## GSheet Sync — CMP Column

The CMP column in GSheet is a formula string, not a number. Do not overwrite it
with a numeric value during sync — `vm_sync_gsheet.py` writes to a separate
raw-CMP column and the formula references that. Trace data flow before editing
any GSheet sync code.

---

## GitHub Workflows Quick Reference

| Workflow | File | Purpose |
|---|---|---|
| Daily Trade Download | `daily_run.yml` | Full pipeline (download + import + notify) |
| Deploy Bot to VM | `deploy_bot.yml` | Push code changes to VM |
| Restart Telegram Bot | `restart_bot.yml` | Restart bot process on VM |
| Setup VM Downloader | `setup_vm_downloader.yml` | One-time VM env setup |
| Stale Data Alert | `stale_alert.yml` | Alert if trades.json is stale |
| Sync GSheet Only | `sync_sheet.yml` | Re-sync CMP/GSheet without download |
