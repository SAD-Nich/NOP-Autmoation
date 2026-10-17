@echo off
cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python nop_dashboard.py
) else (
    where py >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        py nop_dashboard.py
    ) else (
        echo Python was not found in PATH.
        echo Install Python or add it to PATH, then run this file again.
        pause
    )
)
