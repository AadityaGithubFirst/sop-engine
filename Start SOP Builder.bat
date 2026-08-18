@echo off
REM ===========================================================================
REM  SOP Builder - one-click launcher
REM  Double-click this file. It sets everything up the first time, then opens
REM  the SOP Builder in your web browser.
REM ===========================================================================
title SOP Builder
cd /d "%~dp0"
color 1F
cls

echo.
echo   ============================================================
echo      S O P   B U I L D E R
echo   ============================================================
echo.
echo   Starting up. Please wait - do not close this window.
echo   (The first time you run this, it may take a few minutes.)
echo.

REM --- 1. Is Python installed? ----------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    color 4F
    echo   [X] Python is not installed on this computer.
    echo.
    echo       Please ask IT support to install Python 3.11 or newer from:
    echo       https://www.python.org/downloads/
    echo.
    echo       Important: tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
echo   [OK] Python is installed.

REM --- 2. First-run setup ----------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo   [..] First-time setup: preparing the program. Please wait.
    python -m venv .venv
    if errorlevel 1 (
        color 4F
        echo   [X] Could not prepare the program folder.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        color 4F
        echo   [X] Could not install the required components.
        echo       If this computer has no internet access, ask IT support to
        echo       install the packages listed in requirements.txt.
        pause
        exit /b 1
    )
    echo   [OK] Setup finished.
) else (
    echo   [OK] Program is already set up.
)

REM --- 3. Is the writing engine running? ------------------------------------
".venv\Scripts\python.exe" -c "import urllib.request,sys;urllib.request.urlopen('http://localhost:11434/api/tags',timeout=3)" >nul 2>&1
if errorlevel 1 (
    echo   [..] Starting the writing engine ^(Ollama^)...
    where ollama >nul 2>&1
    if errorlevel 1 (
        color 6F
        echo.
        echo   [!] The writing engine ^(Ollama^) is not installed.
        echo.
        echo       Ask IT support to install it once from:  https://ollama.com/download
        echo       Then run this command once:              ollama pull deepseek-r1:8b
        echo.
        echo       The SOP Builder will still open, but it cannot write
        echo       documents until this is done.
        echo.
        pause
    ) else (
        start "" /min ollama serve
        timeout /t 5 /nobreak >nul
        echo   [OK] Writing engine started.
    )
) else (
    echo   [OK] Writing engine is already running.
)

REM --- 4. Start the application ---------------------------------------------
echo.
echo   [..] Opening SOP Builder in your web browser...
echo.
echo   ------------------------------------------------------------
echo      Keep this window open while you use the SOP Builder.
echo      To close the program, close this window.
echo   ------------------------------------------------------------
echo.

start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo   SOP Builder has stopped.
pause
