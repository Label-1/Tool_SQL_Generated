@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install_windows.bat first.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"
python -m pip install pyinstaller
if errorlevel 1 goto fail
pyinstaller --clean --noconfirm --windowed --onedir --name VisualSQLStudio --hidden-import=pymysql --hidden-import=psycopg2 --hidden-import=pyodbc --hidden-import=sqlglot app.py
if errorlevel 1 goto fail
echo.
echo Application built at dist\VisualSQLStudio\VisualSQLStudio.exe
pause
exit /b 0
:fail
echo Build failed. Check the output above.
pause
exit /b 1
