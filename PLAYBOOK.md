# Client Tracker MOFSL — System Playbook

## 1. What This System Does

Pulls trade CSVs from MOFSL CBOS portal every evening, processes them into a JSON store,
syncs to Google Sheets, and serves a Telegram bot for real-time queries.

---

## 2. File Map

| File | Where it runs | What it does |
|---|---|---|
| `mo_downloader.py` | GitHub Actions | Logs into CBOS via Playwright, downloads trade CSVs + scrapes ledger balances |
| `import_all.py` | GitHub Actions | Reads all CSVs → builds `trades.json` (FIFO matching, charge calculation) |
| `vm_sync_gsheet.py` | GitHub Actions | Reads `trades.json` → writes all GSheet tabs |
| `telegram_bot.py` | Oracle VM (always running) | Handles all bot messages, reads `trades.json` + `ledger.json` from disk |
| `symbol_map.py` | Both | Maps CBOS script names → NSE tickers (e.g. ALKYL AMINES → ALKYLAMINE) |
| `ticker_overrides.json` | Both | Same as symbol_map but editable at runtime without code deploy |
| `trades.json` | VM disk | Master trade store. Bot reads this on every message |
| `ledger.json` | VM disk | Ledger balances per client. Refreshed every daily run |
| `bot_config.json` | Both | Telegram token, client list, credentials |
| `gsheet_key.json` | GitHub Actions only | Google service account key for GSheet write access |
| `send_notify.py` | GitHub Actions | Sends Telegram message with ledger summary after each run |

---

## 3. Data Flow (Daily Run — 8:30 PM IST Mon–Fri)

```
GitHub Actions (ubuntu-latest runner)
  │
  ├─ 1. mo_downloader.py
  │     Login to CBOS with Playwright
  │     Download FY26-27 trade CSVs → mo_csvs/
  │     Scrape Financial Summary → ledger.json
  │        (reads Voucher Ledger from COMBINED + MTF popup within modal)
  │
  ├─ 2. import_all.py
  │     For each CSV: parse rows, apply FIFO matching
  │     Output: trades.json  (open + closed trades, charges, net P&L)
  │     Excludes terminal 30023 (dealer terminal)
  │
  ├─ 3. tests/test_import_counts.py
  │     QA gate: fails the pipeline if per-client open/closed counts
  │     deviate by more than 20% from expected
  │
  ├─ 4. vm_sync_gsheet.py
  │     Reads trades.json → writes Google Sheet
  │     After sync: QA check — scans CMP column for #N/A
  │       If found: tries to auto-resolve via yfinance
  │       If resolved: saves to ticker_overrides.json + re-syncs
  │       If unresolved: Telegram alert to add manually
  │
  ├─ 5. send_notify.py
  │     Sends Telegram: Ledger Balance summary + trade counts
  │
  └─ 6. SCP push to VM
        trades.json, ledger.json, symbol_map.py, ticker_overrides.json
        → /home/opc/client-tracker-mofsl/ on Oracle VM

Oracle VM (152.67.164.204) — always on
  └─ tgbot.service (systemd, Restart=always)
        telegram_bot.py reads trades.json + ledger.json from disk
        Responds to Telegram messages
```

**Sunday 7:30 AM IST:** Full history re-download (both FY25-26 + FY26-27) to fix ghost positions.

---

## 4. Google Sheet Structure

| Tab | What it shows | Key formula |
|---|---|---|
| 📋 Open Positions | All open trades, live CMP, Unrealized P&L | CMP = `=IFERROR(GOOGLEFINANCE("NSE:TICKER","price"),"—")` |
| ✅ Closed Trades | All closed trades with net P&L | Static values written by sync |
| 📒 Ledger Entries | Per-trade charge breakdown | Static values |
| 💰 P&L Summary | Per-client: Booked P&L, Unrealized P&L, Total | Unrealized = `SUMIF` from Open Positions K column |
| 📊 Monthly P&L | Month-wise net P&L per client | Computed from closed trades |
| 💼 Capital | Capital deployed per client | Sum of open invested values |
| 🔍 Overview | One-row-per-client summary | Unrealized = `SUMIF` from Open Positions K column |

**CMP column (G) in Open Positions** uses `GOOGLEFINANCE` formula written by `vm_sync_gsheet.py`.  
The ticker is resolved via: `ticker_overrides.json` → `symbol_map.py` → strip non-alphanumeric fallback.

**Unrealized P&L (K) in Open Positions** = `=IF(ISNUMBER(G),(G-E)*D,"")` — live, updates with CMP.

---

## 5. Bot Commands

| Command | What it does |
|---|---|
| `/run` | Trigger a fresh daily pipeline manually |
| `/update` | Pull latest code from GitHub and restart bot on VM |
| `/addticker SYMBOL TICKER` | Fix #N/A CMP — e.g. `/addticker EIMCOELECONINDIA EIMCOELECO` |
| `/alert SYM PRICE [above\|below]` | Set a price alert |
| `/alerts` | List active price alerts |
| `/pnl` | Realized P&L summary |
| Plain query | Natural language — "who holds Vikram Solar", "Sathyavrath open positions" |

---

## 6. Deployment

### Deploy bot to VM (telegram_bot.py changed)
Workflow: **Deploy Bot to VM** (`.github/workflows/deploy_bot.yml`)  
Copies: `telegram_bot.py`, `symbol_map.py`, `ticker_overrides.json` → VM  
Then restarts `tgbot` systemd service.

### Update GSheet logic (vm_sync_gsheet.py changed)
Workflow: **Daily Trade Download** (`.github/workflows/daily_run.yml`)  
Runs full pipeline including GSheet sync. Trigger manually via GitHub → Actions.

### Update ticker mapping without code deploy
Option A: `/addticker SYMBOL TICKER` in Telegram bot  
Option B: Edit `ticker_overrides.json` → push → trigger Daily Trade Download

---

## 7. Common Issues & Fixes

### Ghost open positions (stock shows open but was sold)
**Cause:** Sell CSV not yet downloaded, or FY boundary (FY25-26 buy + FY26-27 sell).  
**Fix:** Trigger `/run` in bot, or run Daily Trade Download with `full_history=true`.  
**Prevention:** Sunday auto-run does full history every week.

### CMP shows #N/A in GSheet
**Cause:** `symbol_map.py` doesn't have the NSE ticker for a CBOS script name.  
**Fix:** `/addticker CBOS_NAME NSE_TICKER` in Telegram bot (e.g. `/addticker EIMCOELECONINDIA EIMCOELECO`).  
**Auto-fix:** After every sync, the QA check tries to resolve via yfinance and alerts if it can't.  
**Note:** Some stocks are not on Google Finance even if the NSE ticker is correct — rare.

### Ledger balance shows 0 or wrong value
**Cause:** Race condition in popup reading (COMBINED popup still in DOM when MTF reads).  
**Fix (applied bb4581e):** Popup reader now scopes to modal container and waits for popup to fully close.  
**Verify:** Check GitHub Actions logs for `COMBINED = X.XX  MTF = Y.YY` lines.

### Bot shows wrong/old data after code change
**Fix:** Run `Deploy Bot to VM` workflow, then `/update` in Telegram as backup.

### RIMK1205 ledger always 0
**Cause:** First client in loop — page freshly loaded, popup timing.  
**Fix (applied):** Wait for popup title to confirm correct modal before reading.

---

## 8. Where Things Live

| Item | Location |
|---|---|
| Source code | `C:\Users\jainr\Desktop\client-tracker\` (local) + GitHub: `jainrishank20/client-tracker-mofsl` |
| VM code | `/home/opc/client-tracker-mofsl/` on `152.67.164.204` |
| Bot service | `sudo systemctl status tgbot` on VM |
| Bot logs | `sudo journalctl -u tgbot -n 100` on VM |
| GSheet | [Client-tracker-MOFSL](https://docs.google.com/spreadsheets/d/1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo) |
| GitHub Actions | `jainrishank20/client-tracker-mofsl` → Actions tab |
| Secrets (tokens, keys) | GitHub → Settings → Secrets and Variables → Actions |

---

## 9. Key Rules (Never Break These)

1. **Terminal 30023 is always excluded** from CSV imports — it's the dealer terminal.
2. **CMP column (K) in Open Positions is a formula**, not a number — never overwrite with `apply_num_cols`.
3. **trades.json age guard** — `vm_sync_gsheet.py` refuses to sync if `trades.json` is older than 6 hours.
4. **FIFO matching uses ISIN** as the key (exchange-agnostic), falls back to normalized script name.
5. **net_pnl** in closed trades = gross − all charges (brokerage + STT + GST + stamp + txn + SEBI/IPFT).
6. **ticker_overrides.json** is checked FIRST in `resolve()` before `symbol_map.py` — use overrides for runtime fixes.
