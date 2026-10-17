@echo off
cd /d "%~dp0"
echo Starting NOP Dashboard - 14K + Principal...
python nop_dashboard_14K_principal.py
if errorlevel 1 (
  echo.
  echo Dashboard failed to start.
  pause
)
