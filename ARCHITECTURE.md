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
│                              └─ OTP via Gmail IMAP (All Mail)    │
│  2. import_all.py      ──▶  Rebuilds trades.json (FIFO/ISIN)     │
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
│  └─ /open, /ledger, /pnl, /alert, /run, /addclient, etc.        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Components

### GitHub Actions — `daily_run.yml`
- **Trigger:** Cron `0 15 * * 1-5` (3 PM UTC = 8:30 PM IST), plus manual dispatch
- **Runner:** `ubuntu-latest` (7 GB RAM — plenty for Playwright)
- **Duration:** ~15–20 minutes per run
- **Steps:** CBOS download → import → GSheet sync → Telegram notify → SCP to VM

### `mo_downloader.py` — CBOS Scraper
- Uses Playwright (headless Chromium) to log into CBOS backoffice
- Handles OTP via Gmail IMAP — searches `[Gmail]/All Mail` (catches all folders)
- OTP retry: 3 attempts, 180s timeout, auto-clicks Resend button on failure
- Handles "session already active" popup before OTP is sent
- Downloads trade CSVs for all clients defined in `bot_config.json → clients`
- Scrapes ledger balances, saves to `ledger.json`
- **Key behaviour:** Uses `page.go_back()` between clients (CBOS URL params are session tokens)

### `import_all.py` — Trade Importer
- Reads all CSVs from `mo_csvs/`
- Normalises scrip names (handles NSE/BSE name differences via RAW dict)
- **FIFO matching keyed on ISIN** — exchange-agnostic, permanently fixes BSE/NSE name mismatch bugs
- If ISIN unavailable, falls back to normalised scrip name
- Excludes terminal `30023` (dealer terminal)
- Outputs `trades.json`: open positions + closed trades with full charge breakdown

### `vm_sync_gsheet.py` — Google Sheet Sync
- Reads `trades.json` + `ledger.json`
- Writes to Google Sheet `1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo`
- CMP column uses `=GOOGLEFINANCE("NSE:SYMBOL","price")` formula (live in sheet)
- Charge keys: `buy_txn`, `sell_txn` (not `buy_txn_chrg`)

### `send_notify.py` — Daily Telegram Notification
- Sends compact ledger balance table (client codes, fixed-width monospace)
- Sends to all chat IDs in `bot_config.json → allowed_chat_id`
- Runs once per day after GSheet sync (also manually runnable on VM)

### `telegram_bot.py` — Always-on Bot (VM)
- Polls Telegram every 30 seconds (socket timeout 35s to avoid spurious timeouts)
- Restarts automatically via systemd (`Restart=always`, `RestartSec=10`)
- **Commands:**
  - `/open` — all open positions (compact per-client summary)
  - `/ledger` — ledger balances (compact monospace table)
  - `/pnl` — realized P&L by client
  - `/summary` — snapshot (open/closed counts, total P&L)
  - `/run` — triggers GitHub Actions `workflow_dispatch` (full pipeline, ~15 min)
  - `/clients` — list all configured clients
  - `/addclient CODE NAME` — add client to `bot_config.json` + updates GitHub Secret
  - `/removeclient CODE` — remove client from `bot_config.json` + updates GitHub Secret
  - `/alert SYM PRICE [above|below]` — set price alert (polls every 5 min in market hours)
  - `/alerts` — list active alerts
  - `/cancelalert SYM` — remove alert
- Natural language queries routed to Groq (LLaMA 3.1 8b)
- Clients loaded fresh on every command (picks up `/addclient` changes without restart)

---

## File Inventory

### On GitHub (source of truth)
```
client-tracker-mofsl/
├── .github/
│   └── workflows/
│       └── daily_run.yml         # GitHub Actions pipeline
├── mo_downloader.py              # CBOS Playwright scraper
├── import_all.py                 # CSV → trades.json (FIFO/ISIN)
├── vm_sync_gsheet.py             # trades.json → Google Sheet
├── send_notify.py                # Daily Telegram ledger message
├── telegram_bot.py               # Always-on Telegram bot
├── ticker_overrides.json         # CBOS name → NSE ticker map (also a GitHub Secret)
├── ARCHITECTURE.md               # This file
└── .gitignore                    # Excludes: bot_config, gsheet_key, trades, ledger, csvs
```

### On Oracle VM (`/home/opc/client-tracker-mofsl/`) — 7 files only
```
telegram_bot.py       # Deployed manually via SCP when updated
send_notify.py        # Deployed manually via SCP when updated
bot_config.json       # NOT in git — credentials + client list (see Secrets)
gsheet_key.json       # NOT in git — Google service account key
ticker_overrides.json # Deployed manually via SCP when updated
trades.json           # Written by GitHub Actions nightly via SCP
ledger.json           # Written by GitHub Actions nightly via SCP
price_alerts.json     # Created by bot when alerts are set — NOT backed up
```

### On GitHub Actions (ephemeral — gone after each run)
```
bot_config.json       # Reconstructed from secret BOT_CONFIG
gsheet_key.json       # Reconstructed from secret GSHEET_KEY
ticker_overrides.json # Reconstructed from secret TICKER_OVERRIDES (deleted in cleanup)
mo_csvs/*.csv         # Downloaded from CBOS, used by import_all.py
trades.json           # Built by import_all.py, SCPed to VM
ledger.json           # Scraped by mo_downloader.py, SCPed to VM
```

---

## Secrets & Credentials

### GitHub Actions Secrets
| Secret | Contents | Used by |
|---|---|---|
| `BOT_CONFIG` | Full `bot_config.json` | mo_downloader, send_notify, telegram_bot |
| `GSHEET_KEY` | Full `gsheet_key.json` (GCP service account) | vm_sync_gsheet |
| `TICKER_OVERRIDES` | Full `ticker_overrides.json` | import_all, bot |
| `VM_SSH_KEY` | Private SSH key for `opc@152.67.164.204` | SCP of trades/ledger to VM |

### `bot_config.json` structure
```json
{
  "telegram_token":     "...",
  "groq_api_key":       "...",
  "allowed_chat_id":    "7100061306,1257819265",
  "mo_username":        "RRISHANKMK",
  "mo_password":        "...",
  "gmail_user":         "jainrishank20@gmail.com",
  "gmail_app_password": "...",
  "github_token":       "gho_... (expires — replace with classic PAT)",
  "github_repo":        "jainrishank20/client-tracker-mofsl",
  "clients": {
    "RIMK1205": "Siva Sankara Reddy",
    "RIMK1209": "Sathyavrath",
    ...
  }
}
```

> ⚠️ `github_token` is an OAuth token that expires. Replace with a classic PAT:
> github.com/settings/tokens → Generate new token (classic) → scope: `repo` → no expiry.
> Then run `set_secrets.py` to update all GitHub Secrets.

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

> To add a new client: send `/addclient RIMKXXXX Name` to the bot.
> Bot updates `bot_config.json` on VM and pushes to GitHub Secret automatically.

---

## Infrastructure

| Resource | Details |
|---|---|
| GitHub repo | `github.com/jainrishank20/client-tracker-mofsl` (private) |
| GitHub Actions | Free tier — ~20 min/run, well within 2000 min/month limit |
| Oracle VM | `152.67.164.204`, user `opc`, Oracle Linux 9, Always Free tier |
| SSH key | `ssh-key-2026-06-11.key` (stored locally + as GitHub Secret `VM_SSH_KEY`) |
| Google Sheet | ID `1RBaZYY8Eheet13UJy6eRMJIFUzU9Yii335l5x_H5KVo` |
| GCP project | `client-tracker-mofsl` (service account for GSheet API) |

---

## VM Setup (systemd)

```bash
# Service file: /etc/systemd/system/tgbot.service
[Unit]
Description=Client Tracker Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/client-tracker-mofsl
ExecStart=/usr/bin/python3 -u /home/opc/client-tracker-mofsl/telegram_bot.py
Restart=always
RestartSec=10
StartLimitIntervalSec=0
MemoryMax=180M

[Install]
WantedBy=multi-user.target
```

```bash
# Useful commands
systemctl status tgbot          # check status
systemctl restart tgbot         # restart
journalctl -u tgbot -f          # live logs
journalctl -u tgbot -n 50       # last 50 lines
```

---

## Deploying Code Changes to VM

GitHub Actions doesn't deploy code — only `trades.json` + `ledger.json`. For code changes:

```bash
# From your laptop
scp -i ssh-key-2026-06-11.key <file> opc@152.67.164.204:/home/opc/client-tracker-mofsl/
ssh -i ssh-key-2026-06-11.key opc@152.67.164.204 "sudo systemctl restart tgbot"
```

Files that need manual SCP when changed: `telegram_bot.py`, `send_notify.py`, `ticker_overrides.json`

---

## Common Operations

### Trigger a run manually
- **Telegram:** Send `/run` to the bot → triggers GitHub Actions workflow_dispatch
- **GitHub UI:** Actions tab → "Daily Trade Download" → "Run workflow"

### Add a new client
Send `/addclient RIMKXXXX Full Name` to the bot. Done. No code changes needed.

### Add a ticker override (CBOS name → NSE symbol)
1. Edit `ticker_overrides.json` on laptop
2. Push to GitHub
3. SCP to VM: `scp ... ticker_overrides.json opc@152.67.164.204:/home/opc/client-tracker-mofsl/`
4. Run `set_secrets.py` to update `TICKER_OVERRIDES` GitHub Secret

### Full rebuild (both financial years)
Trigger GitHub Actions manually with `--full` flag (edit workflow temporarily).

### Update GitHub Secrets after any config change
```bash
python C:\Users\jainr\AppData\Local\Temp\set_secrets.py
```

### If GitHub Actions run fails
1. Check Actions tab → click the failed run → click the failed step
2. Common failures:
   - OTP timeout → CBOS email delayed → retry via `/run` in Telegram
   - VM SSH unreachable → Oracle free tier blip → retry, or reboot from Oracle console
   - GSheet API quota → wait 1 min, retry

---

## Data Flow (end to end)

```
CBOS Backoffice
    │
    │  Playwright headless (GitHub Actions runner)
    │  OTP via Gmail IMAP → [Gmail]/All Mail
    ▼
mo_csvs/*.csv  +  ledger.json
    │
    │  import_all.py (FIFO, keyed on ISIN)
    ▼
trades.json
    │
    ├──▶  vm_sync_gsheet.py ──▶  Google Sheet (live CMP via GOOGLEFINANCE)
    │
    ├──▶  send_notify.py    ──▶  Telegram (compact ledger table, both IDs)
    │
    └──▶  SCP to VM         ──▶  telegram_bot.py (responds to queries)
```

---

## Known Limitations

- **Oracle free tier** may go down intermittently — bot auto-restarts when VM recovers
- **`price_alerts.json`** is only on VM — lost if VM is fully wiped (rare)
- **`github_token`** in bot_config.json expires — replace with classic PAT (no expiry)
- **GSheet CMP** uses `GOOGLEFINANCE` which has a ~15 min delay and daily quota limits
