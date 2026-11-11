@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Visual SQL Studio - Local AI - BALANCED

rem This installer downloads Ollama from the official website (if missing),
rem checks the Authenticode publisher, then downloads one local coding model.
rem Nothing here connects to MySQL, SQL Server, or PostgreSQL.
set "MODEL=qwen2.5-coder:3b"
set "TIER=BALANCED"
set "RAM_TIP=16 GB recommended"
set "OLLAMA_EXE="
set "OLLAMA_SETUP_PATH=%TEMP%\VisualSQL_OllamaSetup.exe"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NO_CLOUD=1"
set "TRIES=0"

echo.
echo ==============================================================
echo       Visual SQL Studio - LOCAL AI INSTALLER
 echo ==============================================================
echo   Profile : %TIER%
echo   Model   : %MODEL%
echo   RAM     : %RAM_TIP%
echo   Source  : https://ollama.com
 echo ==============================================================
echo.
echo NOTE: This installs a local AI model, not the Visual SQL app.
echo No database credentials or customer data are read by this script.
echo An internet connection is needed only for installing/downloading.
echo Corporate laptops may require IT approval.
echo.

rem Check Ollama in its usual per-user Windows directory first.
call :find_ollama
if defined OLLAMA_EXE goto have_ollama

echo Ollama was not found on this computer.
echo The official Windows installer will be downloaded and opened.
choice /C YN /M "Download and install Ollama from ollama.com?"
if errorlevel 2 goto cancelled

echo.
echo [1/4] Downloading the official Ollama installer...
where curl.exe >nul 2>&1
if errorlevel 1 goto download_powershell
curl.exe --location --fail --retry 2 --connect-timeout 25 --progress-bar --output "%OLLAMA_SETUP_PATH%" "https://ollama.com/download/OllamaSetup.exe"
if errorlevel 1 goto download_error
goto download_finished

:download_powershell
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile $env:OLLAMA_SETUP_PATH -UseBasicParsing"
if errorlevel 1 goto download_error

:download_finished
if not exist "%OLLAMA_SETUP_PATH%" goto download_error
echo.
echo [2/4] Checking publisher signature (Ollama Inc.)...
powershell.exe -NoProfile -Command "$s=Get-AuthenticodeSignature -FilePath $env:OLLAMA_SETUP_PATH; if ($s.Status -ne 'Valid' -or $null -eq $s.SignerCertificate -or $s.SignerCertificate.Subject -notmatch '(^|,\s*)O=Ollama Inc\.(,|$)') { Write-Host 'Installer signature NOT trusted. Installation stopped.'; exit 1 }; Write-Host 'Installer signature verified: Ollama Inc.'"
if errorlevel 1 goto signature_error

echo.
echo The verified installer will open now. Follow the Windows installer.
start "" /wait "%OLLAMA_SETUP_PATH%"
if errorlevel 1 goto installer_error
call :find_ollama
if not defined OLLAMA_EXE goto installer_not_found
del /q "%OLLAMA_SETUP_PATH%" >nul 2>&1

:have_ollama
echo.
echo Ollama detected: "%OLLAMA_EXE%"
echo.
echo [3/4] Setting private, local-only defaults for Ollama...
rem These values persist for the current Windows user after the next Ollama restart.
setx OLLAMA_NO_CLOUD 1 >nul 2>&1
if errorlevel 1 echo WARNING: Could not save the no-cloud user preference.
setx OLLAMA_HOST "127.0.0.1:11434" >nul 2>&1
if errorlevel 1 echo WARNING: Could not save the localhost-only user preference.
echo Private defaults saved for future Ollama launches.
echo If Ollama is already running, quit it from the tray and restart later
 echo so the saved preferences also apply to its background server.
echo.
echo Checking the local service...
"%OLLAMA_EXE%" list >nul 2>&1
if not errorlevel 1 goto service_ready

echo Starting Ollama local server...
start "Ollama Local Server" /min "%OLLAMA_EXE%" serve

:wait_server
set /a TRIES+=1
timeout /t 2 /nobreak >nul
"%OLLAMA_EXE%" list >nul 2>&1
if not errorlevel 1 goto service_ready
if %TRIES% GEQ 15 goto service_error
goto wait_server

:service_ready
rem Refuse to proceed if an existing Ollama server is exposed on non-loopback IPs.
powershell.exe -NoProfile -Command "if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) { $a=@(Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -notin @('127.0.0.1','::1') }); if ($a.Count -gt 0) { Write-Host 'WARNING: Ollama is listening on a network address. Contact IT.'; exit 7 } }"
if errorlevel 1 goto network_exposure

echo.
echo [4/4] Downloading local model: %MODEL%
echo Existing model files will be reused if already downloaded.
"%OLLAMA_EXE%" pull "%MODEL%"
if errorlevel 1 goto model_error

echo.
echo ==============================================================
echo SUCCESS! Local AI model installed: %MODEL%
echo ==============================================================
echo.
echo Next steps:
echo 1. Open Visual SQL Studio and connect to your database.
echo 2. Click the button to detect local AI models.
echo 3. Choose %MODEL% from the list.
echo 4. Type your Thai SQL question, then click Generate SQL.
echo.
echo If Ollama was previously running, restart Ollama from the tray
 echo before using sensitive company data to apply privacy settings.
echo.
echo Your local models:
"%OLLAMA_EXE%" list
goto finish

:find_ollama
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA_EXE exit /b
for /f "delims=" %%I in ('where ollama.exe 2^>nul') do if not defined OLLAMA_EXE set "OLLAMA_EXE=%%I"
exit /b

:cancelled
echo Installation cancelled by user.
goto finish

:download_error
echo ERROR: Failed to download Ollama from the official site.
echo Check your internet connection, proxy, or ask your IT team.
goto fail

:signature_error
echo ERROR: The installer is unsigned, modified, or has an invalid signature.
echo For safety this script will not run it. Do not bypass this check.
echo Ask IT, or download and verify a trusted version from ollama.com.
del /q "%OLLAMA_SETUP_PATH%" >nul 2>&1
goto fail

:installer_error
echo ERROR: Ollama setup returned a failure or was cancelled.
goto fail

:installer_not_found
echo Ollama setup finished, but ollama.exe was not detected.
echo Restart Windows and try this BAT file again. If needed, ask IT.
goto fail

:service_error
echo ERROR: Ollama did not respond at http://127.0.0.1:11434 .
echo Check that Ollama is running and port 11434 is available.
goto fail

:network_exposure
echo ERROR: Existing Ollama server may be available to other computers.
echo For safety, stop here. Set OLLAMA_HOST to 127.0.0.1:11434,
echo restart Ollama, then run this installer again.
goto fail

:model_error
echo ERROR: Model download failed. Check your connection and disk space.
echo You can rerun this BAT. Ollama should resume/reuse downloads.
goto fail

:fail
echo.
echo Installation did not complete. No database was accessed.
pause
exit /b 1

:finish
echo.
pause
exit /b 0
