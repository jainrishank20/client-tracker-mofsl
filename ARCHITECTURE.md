# Client Tracker MOFSL — Architecture

> Last updated: July 2026

---

## Overview

Fully automated end-to-end pipeline that:
1. Downloads trade data from Motilal Oswal CBOS backoffice
2. Rebuilds a clean trade history with FIFO P&L matching
3. Syncs everything to a Google Sheet
4. Notifies via Telegram
5. Keeps a 24/7 Telegram bot running for live queries

**No laptop required.** Everything runs automatically at 8:30 PM IST, Mon–Fri.

---

## System Map

```
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Actions  (runs at 8:30 PM IST, Mon-Fri)                 │
│                                                                   │
│  1. mo_downloader.py   ──▶  CBOS backoffice (Playwright)         │
│                              └─ OTP via Gmail IMAP               │
│  2. import_all.py      ──▶  Rebuilds trades.json (FIFO)          │
│  3. vm_sync_gsheet.py  ──▶  Google Sheets API                    │
│  4. send_notify.py     ──▶  Telegram (ledger summary)            │
│  5. SCP                ──▶  trades.json + ledger.json → VM       │
└─────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Oracle VM  152.67.164.204  (always-on, free tier)              │
│                                                                   │
│  telegram_bot.py  (systemd service: tgbot)                       │
│  └─ reads trades.json + ledger.json locally                      │
│  └─ responds to /open, /alert, /pnl, /ledger, /run, etc.        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Components

### GitHub Actions — `daily_run.yml`
- **Trigger:** Cron `0 15 * * 1-5` (3 PM UTC = 8:30 PM IST), plus manual dispatch
- **Runner:** `ubuntu-latest` (7 GB RAM — plenty for Playwright)
- **Duration:** ~15–20 minutes per run
- **What it does:** Full pipeline steps 1–5, then SCPs results to VM

### `mo_downloader.py` — CBOS Scraper
- Uses Playwright (headless Chromium) to log into CBOS backoffice
- Handles OTP via Gmail IMAP polling
- Downloads trade CSVs for all 10 clients (current FY by default, `--full` for both FYs)
- Scrapes ledger balances via the ClientDashboard page
- Saves CSVs to `mo_csvs/` and ledger to `ledger.json`
- **Key behaviour:** Uses `page.go_back()` between clients (not `page.goto()`), because CBOS URL params are single-use session tokens

### `import_all.py` — Trade Importer
- Reads all CSVs from `mo_csvs/`
- Normalises scrip names (handles NSE/BSE name differences)
- **FIFO matching keyed on ISIN** (exchange-agnostic — prevents BSE/NSE name mismatch bugs)
- Outputs `trades.json` with full history: open positions + closed trades with P&L
- Excludes terminal `30023` (dealer terminal)

### `vm_sync_gsheet.py` — Google Sheet Sync
- Reads `trades.json` + `ledger.json`
- Writes to Google Sheet `1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo`
- Tabs: Overview, Open Positions, Closed Trades, P&L Summary, Ledger, per-client sheets
- CMP column uses `=GOOGLEFINANCE("NSE:SYMBOL","price")` formula (live in sheet)
- Unrealized P&L in Overview uses `=SUMIF(...)` pulling from Open Positions tab

### `send_notify.py` — Daily Telegram Notification
- Sends formatted ledger balance table to all configured chat IDs
- Runs once per day after GSheet sync

### `telegram_bot.py` — Always-on Bot (VM)
- Polls Telegram every 30 seconds
- **Commands:**
  - `/open` — all open positions with live unrealized P&L (via yfinance)
  - `/ledger` — all ledger balances
  - `/pnl` — realized P&L by client
  - `/summary` — quick snapshot
  - `/run` — trigger `run_daily_vm.py` on VM (re-import + sync without CBOS download)
  - `/alert SYMBOL PRICE` — set live price alert (polls every 5 min during market hours)
  - `/alerts` — list active alerts
  - `/cancelalert SYMBOL` — remove alert
- Natural language queries routed to Groq (LLaMA 3.1)
- Alerts stored in `price_alerts.json`

### `run_daily_vm.py` — VM-local Pipeline (for `/run` command)
- Triggered by Telegram `/run` via the bot
- Skips CBOS download (uses existing CSVs)
- Runs: import_all → vm_sync_gsheet → send_notify
- Useful for re-syncing after a manual fix without waiting for the nightly run

---

## File Inventory

### On GitHub (source of truth)
```
client-tracker-mofsl/
├── .github/
│   └── workflows/
│       └── daily_run.yml       # GitHub Actions pipeline
├── mo_downloader.py            # CBOS Playwright scraper
├── import_all.py               # CSV → trades.json (FIFO)
├── vm_sync_gsheet.py           # trades.json → Google Sheet
├── send_notify.py              # Daily Telegram ledger message
├── telegram_bot.py             # Always-on Telegram bot
├── run_daily_vm.py             # VM-local re-sync (no CBOS)
├── ticker_overrides.json       # CBOS name → NSE ticker map
├── run_daily.py                # Legacy laptop runner (kept for reference)
├── ARCHITECTURE.md             # This file
└── .gitignore                  # Excludes: bot_config, gsheet_key, trades, ledger, csvs
```

### On Oracle VM (`/home/opc/client-tracker-mofsl/`)
```
telegram_bot.py         # Pulled from GitHub
run_daily_vm.py         # Pulled from GitHub
ticker_overrides.json   # Pulled from GitHub (also a GitHub Secret)
bot_config.json         # NOT in git — credentials file (see Secrets)
gsheet_key.json         # NOT in git — Google service account key
trades.json             # Generated — written by GitHub Actions via SCP
ledger.json             # Generated — written by GitHub Actions via SCP
price_alerts.json       # Generated — bot writes alerts here
logs/                   # Log files from run_daily_vm.py
```

### On GitHub Actions (ephemeral — gone after each run)
```
bot_config.json         # Reconstructed from secret BOT_CONFIG
gsheet_key.json         # Reconstructed from secret GSHEET_KEY
ticker_overrides.json   # Reconstructed from secret TICKER_OVERRIDES
mo_csvs/*.csv           # Downloaded from CBOS, used by import_all.py
trades.json             # Built by import_all.py, SCPed to VM
ledger.json             # Scraped by mo_downloader.py, SCPed to VM
```

---

## Secrets & Credentials

### GitHub Actions Secrets
| Secret | Contents | Used by |
|---|---|---|
| `BOT_CONFIG` | Full `bot_config.json` (all credentials) | mo_downloader, send_notify, telegram_bot |
| `GSHEET_KEY` | Full `gsheet_key.json` (GCP service account) | vm_sync_gsheet |
| `TICKER_OVERRIDES` | Full `ticker_overrides.json` | import flow, bot |
| `VM_SSH_KEY` | Private SSH key for `opc@152.67.164.204` | SCP of trades/ledger to VM |

### `bot_config.json` (on VM + reconstructed in Actions)
```json
{
  "telegram_token":      "...",
  "groq_api_key":        "...",
  "allowed_chat_id":     "7100061306,1257819265",
  "mo_username":         "RRISHANKMK",
  "mo_password":         "...",
  "gmail_user":          "jainrishank20@gmail.com",
  "gmail_app_password":  "..."
}
```

---

## Clients

| Code | Name |
|---|---|
| RIMK1205 | Siva Sankara Reddy |
| RIMK1209 | Sathyavrath |
| RIMK1215 | Malleswari |
| RIMK1220 | Kalpana |
| RIMK1238 | Iranna |
| RIMK1247 | Srujana |
| RIMK1248 | Udayakumar |
| RIMK1249 | Sundareshwari |
| RIMK1252 | Savitha |
| RIMK1256 | Sheeba |

---

## Infrastructure

| Resource | Details |
|---|---|
| GitHub repo | `github.com/jainrishank20/client-tracker-mofsl` (private) |
| GitHub Actions | Free tier — ~300 min/month used (well within 2000 min limit) |
| Oracle VM | `152.67.164.204`, user `opc`, Oracle Linux 9, Always Free tier |
| SSH key | `ssh-key-2026-06-11.key` (stored locally + as GitHub Secret) |
| Google Sheet | ID `1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo` |
| GCP project | `client-tracker-mofsl` |

---

## VM Setup

The VM runs only the Telegram bot. Nothing else.

```bash
# Service file: /etc/systemd/system/tgbot.service
# To check status:
systemctl status tgbot

# To restart after code update:
systemctl restart tgbot

# To view logs:
journalctl -u tgbot -f
```

### Updating bot code on VM
```bash
ssh opc@152.67.164.204
cd /home/opc/client-tracker-mofsl
git pull origin main
systemctl restart tgbot
```

---

## Common Operations

### Trigger a run manually
- **GitHub UI:** Go to Actions tab → "Daily Trade Download" → "Run workflow"
- **Telegram:** Send `/run` to the bot (re-syncs without CBOS download)

### Add a new client
1. Add client code + name to `NAMES` dict in `telegram_bot.py`
2. Add to `CLIENTS` list in `send_notify.py`
3. Add to `CLIENTS` list in `vm_sync_gsheet.py`
4. Add to `CLIENT_NAMES` dict in `vm_sync_gsheet.py`
5. Push to GitHub — next run picks it up automatically

### Add a new ticker override (CBOS name → NSE symbol)
1. Edit `ticker_overrides.json` locally
2. Push to GitHub
3. Update GitHub Secret `TICKER_OVERRIDES` with new file contents
4. Or: update directly on VM and in the secret

### Full rebuild (both financial years)
- GitHub Actions: edit workflow to pass `--full` to `mo_downloader.py`
- Or run locally: `python run_daily.py --full`

### If GitHub Actions run fails
1. Check the Actions tab for error logs
2. Common failures:
   - CBOS login timeout → OTP email delay → retry manually
   - VM SSH unreachable → Oracle free tier intermittent → retry
   - GSheet API quota → wait 1 minute, re-run

---

## Data Flow (end to end)

```
CBOS Backoffice
    │
    │  Playwright (headless Chrome)
    │  OTP via Gmail IMAP
    ▼
mo_csvs/*.csv  +  ledger.json
    │
    │  import_all.py (FIFO, ISIN-keyed)
    ▼
trades.json
    │
    ├──▶  vm_sync_gsheet.py ──▶  Google Sheet (live CMP via GOOGLEFINANCE)
    │
    ├──▶  send_notify.py    ──▶  Telegram (daily ledger table)
    │
    └──▶  SCP to VM         ──▶  telegram_bot.py (responds to queries)
```
