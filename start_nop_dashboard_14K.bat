@echo off
cd /d "%~dp0"
echo Starting NOP Dashboard with 14K conversion...
python nop_dashboard_14K.py
if errorlevel 1 (
    echo.
    echo Dashboard failed to start.
    echo Make sure Python is installed and AutomationTrx.exe has been built.
    pause
)
