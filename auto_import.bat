@echo off
REM Auto-import: runs every morning at 8am via Task Scheduler
cd /d "%~dp0"
python import_all.py >> import_log.txt 2>&1
echo Import done at %date% %time% >> import_log.txt
