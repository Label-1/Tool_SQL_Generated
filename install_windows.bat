@echo off
setlocal
cd /d "%~dp0"
title Visual SQL Studio - Windows Setup v0.3.1
echo ================================================
echo    Visual SQL Studio v0.3.1 - Windows Setup
echo ================================================
where py >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Python 3 not found. Install Python 3.11 or later.
        pause
        exit /b 1
    )
    set "PY_CMD=python"
)
if not exist ".venv\Scripts\python.exe" (
    echo Creating an isolated Python environment...
    %PY_CMD% -m venv .venv
    if errorlevel 1 goto fail
)
call ".venv\Scripts\activate.bat"
echo.
echo [1/3] Installing core SQL safety validator and MySQL driver...
python -m pip install --disable-pip-version-check "sqlglot>=25.0" "PyMySQL>=1.1" "cryptography>=42"
if errorlevel 1 goto fail
echo.
echo [2/3] Installing optional PostgreSQL support...
python -m pip install --disable-pip-version-check "psycopg2-binary>=2.9"
if errorlevel 1 echo WARNING: PostgreSQL driver unavailable. MySQL mode still works.
echo.
echo [3/3] Installing optional SQL Server Python support...
python -m pip install --disable-pip-version-check "pyodbc>=5.1"
if errorlevel 1 echo WARNING: SQL Server Python driver unavailable. MySQL mode still works.
echo.
echo ================================================
echo Installation complete.
echo Double-click run_windows.bat to start.
echo You only need Host, User, Password, Database and Port.
echo Click Test Connection, then Connect Database.
echo If TLS fails, open Advanced settings in the SAME window.
echo ================================================
pause
exit /b 0
:fail
echo.
echo ERROR: Core packages could not be installed.
echo Verify Internet or ask IT for the Python package mirror.
echo You can share the error above, without any passwords.
pause
exit /b 1
