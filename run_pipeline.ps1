# Daily trade pipeline — runs on Windows PC via Task Scheduler
# Replaces Oracle VM vm_daily_run.sh
# Usage: .\run_pipeline.ps1 [true|false]    true = full FY history
# Schedule: Mon-Sat 7:30 PM IST via Task Scheduler (run setup_task_scheduler.ps1 once)

param([string]$IsFullArg = "false")
$IS_FULL = $IsFullArg.ToLower()

$REPO_DIR   = "C:\Users\jainr\Desktop\client-tracker"
$LOG        = "$env:USERPROFILE\vm_daily_run.log"
$REPO       = "jainrishank20/client-tracker-mofsl"
$PYTHON     = "python"

Set-Location $REPO_DIR

# ── Load config ────────────────────────────────────────────────────────────────
$cfg          = Get-Content "$REPO_DIR\bot_config.json" -Raw | ConvertFrom-Json
$GITHUB_TOKEN = $cfg.github_token
$TG_TOKEN     = $cfg.telegram_token
$TG_CHAT      = [string]$cfg.allowed_chat_id

function Send-Telegram([string]$msg) {
    if (-not $TG_TOKEN -or -not $TG_CHAT) { return }
    try {
        $body = @{ chat_id = $TG_CHAT; text = $msg }
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$TG_TOKEN/sendMessage" `
            -Method POST -ContentType "application/json" `
            -Body ($body | ConvertTo-Json) | Out-Null
    } catch {}
}

function Get-IST { (Get-Date).ToUniversalTime().AddHours(5).AddMinutes(30).ToString("dd MMM hh:mm tt") + " IST" }

# ── Log header ─────────────────────────────────────────────────────────────────
Add-Content $LOG ""
Add-Content $LOG "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') — IS_FULL=$IS_FULL ==="

$success = $false
$errMsg  = ""

try {
    # Pull latest Python scripts from GitHub
    Add-Content $LOG "Pulling latest code from GitHub..."
    git pull origin main 2>&1 | Add-Content $LOG
    Add-Content $LOG "Code updated."

    # Download CSVs from CBOS (browser will open on screen — that's normal)
    if ($IS_FULL -eq "true") {
        Add-Content $LOG "Running FULL download (FY25-26 + FY26-27)..."
        & $PYTHON mo_downloader.py --full 2>&1 | Add-Content $LOG
    } else {
        Add-Content $LOG "Running incremental download (FY26-27)..."
        & $PYTHON mo_downloader.py 2>&1 | Add-Content $LOG
    }
    if ($LASTEXITCODE -ne 0) { throw "mo_downloader.py failed (exit $LASTEXITCODE)" }

    # Import CSVs → trades.json
    Add-Content $LOG "Importing CSVs..."
    & $PYTHON import_all.py 2>&1 | Add-Content $LOG
    if ($LASTEXITCODE -ne 0) { throw "import_all.py failed (exit $LASTEXITCODE)" }

    # Push trades.json + ledger.json to GitHub via API
    Add-Content $LOG "Pushing trades.json + ledger.json to GitHub..."
    $pushScript = @"
import json, base64, urllib.request, urllib.error, os, sys

TOKEN = "$GITHUB_TOKEN"
REPO  = "$REPO"
HEADERS = {
    'Authorization': f'token {TOKEN}',
    'Content-Type':  'application/json',
}

def gh_put(path, content_bytes, message):
    url = f'https://api.github.com/repos/{REPO}/contents/{path}'
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req) as r:
            sha = json.load(r).get('sha', '')
    except urllib.error.HTTPError as e:
        sha = '' if e.code == 404 else (_ for _ in ()).throw(e)
    body = {'message': message, 'content': base64.b64encode(content_bytes).decode(), 'branch': 'main'}
    if sha:
        body['sha'] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HEADERS, method='PUT')
    with urllib.request.urlopen(req) as r:
        resp = json.load(r)
        print(f'  Pushed {path} — commit {resp["commit"]["sha"][:7]}')

for fname in ('trades.json', 'ledger.json'):
    if os.path.exists(fname):
        gh_put(fname, open(fname, 'rb').read(), f'chore: update {fname} from PC [skip ci]')
    else:
        print(f'  Skipping {fname} (not found)')
"@
    & $PYTHON -c $pushScript 2>&1 | Add-Content $LOG
    if ($LASTEXITCODE -ne 0) { throw "GitHub push failed" }

    # Trigger GHA: sync GSheet + send Telegram
    Add-Content $LOG "Triggering GHA workflow..."
    $dispatchBody = @{
        ref    = "main"
        inputs = @{ skip_download = "true"; full_history = $IS_FULL }
    } | ConvertTo-Json
    $resp = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/$REPO/actions/workflows/daily_run.yml/dispatches" `
        -Method POST `
        -Headers @{ Authorization = "token $GITHUB_TOKEN"; "Content-Type" = "application/json" } `
        -Body $dispatchBody `
        -SkipHttpErrorCheck
    Add-Content $LOG "GHA dispatch done."

    Add-Content $LOG "=== Done $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="
    $success = $true

} catch {
    $errMsg = $_.Exception.Message
    Add-Content $LOG "ERROR: $errMsg"
}

# ── Telegram notification ──────────────────────────────────────────────────────
if ($success) {
    Send-Telegram "Pipeline done ($(Get-IST)) — GHA triggered. Check dashboard."
} else {
    Send-Telegram "Pipeline FAILED at $(Get-IST). Error: $errMsg. Check $LOG"
}
