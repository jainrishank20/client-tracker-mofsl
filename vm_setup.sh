#!/bin/bash
# Runs ON the Oracle VM — installs Playwright, sets cron, fires first run.
set -e
export PATH=/usr/local/bin:/usr/bin:/bin:/home/opc/.local/bin:$PATH

echo "=== $(date) — Starting VM setup ==="

chmod +x /home/opc/client-tracker-mofsl/vm_daily_run.sh

echo "--- Installing/updating Playwright ---"
pip3 install playwright --quiet
# Install chromium only if not already present
playwright install chromium
# NOTE: playwright install-deps uses apt-get which doesn't exist on Oracle Linux (yum-based).
# System deps are already present since the user runs Playwright manually on this VM.
echo "Playwright: $(playwright --version)"

echo "--- Configuring crontab ---"
crontab -l 2>/dev/null | grep -v 'vm_daily_run' > /tmp/new_crontab || true
# Mon-Sat 7:30 PM IST = 14:00 UTC
echo "0 14 * * 1-6 /home/opc/client-tracker-mofsl/vm_daily_run.sh false" >> /tmp/new_crontab
# Sunday 11:00 AM IST = 05:30 UTC
echo "30 5 * * 0  /home/opc/client-tracker-mofsl/vm_daily_run.sh true"   >> /tmp/new_crontab
crontab /tmp/new_crontab
echo "Crontab installed:"
crontab -l

echo "--- Triggering immediate incremental run in background ---"
nohup /home/opc/client-tracker-mofsl/vm_daily_run.sh false >> /home/opc/vm_daily_run.log 2>&1 &
echo "VM run PID=$! started"

echo "=== Setup complete $(date) ==="
