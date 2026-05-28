@echo off
setlocal EnableExtensions EnableDelayedExpansion
title 飞书 × ChatGPT 机器人
powershell -NoProfile -Command "$Host.UI.RawUI.WindowTitle='飞书 × ChatGPT 机器人'" >nul 2>&1

echo ================================================
echo   Feishu x ChatGPT Bot - Restarting...
echo ================================================
echo.

cd /d "%~dp0"

for /f "usebackq delims=" %%i in (`python -c "import os; from app_config import settings; workspace = settings.agents_path if getattr(settings, 'agents_path', '') and os.path.isdir(settings.agents_path) else os.path.dirname(os.path.abspath(r'%~dp0bot.py')); print(os.path.join(workspace, 'runtime_data', 'bot.pid'))" 2^>nul`) do set PID_FILE=%%i
if not defined PID_FILE set PID_FILE=%~dp0runtime_data\bot.pid

if exist "%PID_FILE%" (
    set /p OLD_PID=<"%PID_FILE%"
    if not defined OLD_PID (
        echo [1/3] PID file is empty.
    ) else (
        echo [1/3] Killing old process PID !OLD_PID!...
        taskkill /PID !OLD_PID! /T /F >nul 2>&1
        if errorlevel 1 (
            echo       Process not found or already stopped.
        ) else (
            echo       Killed.
        )
    )
    del "%PID_FILE%" >nul 2>&1
    timeout /t 1 /nobreak >nul
) else (
    echo [1/3] No running bot found.
)

echo.
echo [2/3] Starting bot...
echo       Close this window to stop.
echo.

powershell -NoProfile -Command "$Host.UI.RawUI.WindowTitle='飞书 × ChatGPT 机器人'" >nul 2>&1
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
powershell -NoProfile -Command "$Host.UI.RawUI.WindowTitle='飞书 × ChatGPT 机器人'" >nul 2>&1

echo.
echo [3/3] Bot stopped.
pause
