#!/bin/bash
# Daily automation: download → import → sync gsheet → notify → restart streamlit
# Cron: 30 20 * * 1-5 /home/opc/client-tracker-mofsl/vm_daily.sh >> /home/opc/client-tracker-mofsl/daily.log 2>&1

BASE="/home/opc/client-tracker-mofsl"
PYTHON="python3"
LOG="$BASE/daily.log"

echo ""
echo "=========================================="
echo "Daily run started: $(date)"
echo "=========================================="

# Step 1: Stop Streamlit to free RAM
echo "[1/5] Stopping Streamlit..."
pkill -f streamlit
sleep 3

# Step 2: Download CSVs from CBOS
echo "[2/5] Downloading CSVs from CBOS..."
cd "$BASE"
$PYTHON vm_downloader.py
if [ $? -ne 0 ]; then
    echo "ERROR: Download failed. Restarting Streamlit and aborting."
    nohup streamlit run "$BASE/app.py" --server.port 8501 --server.headless true >> "$BASE/streamlit.log" 2>&1 &
    exit 1
fi

# Step 3: Import all CSVs → rebuild trades.json
echo "[3/5] Importing CSVs (Full Rebuild)..."
$PYTHON import_all.py
if [ $? -ne 0 ]; then
    echo "ERROR: Import failed."
fi

# Step 4: Sync Google Sheet
echo "[4/5] Syncing Google Sheet..."
$PYTHON vm_sync_gsheet.py
if [ $? -ne 0 ]; then
    echo "WARNING: GSheet sync failed."
fi

# Step 5: Send Telegram notification
echo "[5/5] Sending Telegram notification..."
$PYTHON send_notify.py
if [ $? -ne 0 ]; then
    echo "WARNING: Telegram notification failed."
fi

# Restart Streamlit
echo "[Done] Restarting Streamlit..."
nohup streamlit run "$BASE/app.py" --server.port 8501 --server.headless true >> "$BASE/streamlit.log" 2>&1 &

echo "Daily run completed: $(date)"
echo "=========================================="
