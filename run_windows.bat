@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
    exit /b 0
)
echo Virtual environment not found. Run install_windows.bat first.
echo You can use run_debug_windows.bat to see the error details.
pause
exit /b 1
