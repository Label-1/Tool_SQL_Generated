@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "%~dp0app.py"
) else (
    py -3 "%~dp0app.py"
)
if errorlevel 1 (
    echo.
    echo Check logs\startup_error.log for details.
    pause
)
