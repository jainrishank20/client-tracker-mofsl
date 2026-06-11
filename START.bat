@echo off
echo Starting Raghava Portfolio Tracker...
echo Dashboard will open in your browser automatically.
echo Press Ctrl+C to stop.
echo.
streamlit run "%~dp0app.py" --server.port 8501 --browser.gatherUsageStats false
pause
