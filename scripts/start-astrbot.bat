@echo off
setlocal
chcp 65001 >nul
title AstrBot

REM ===== change these two lines to your own paths =====
set BOT_ROOT=%~dp0..
set ASTRBOT_DIR=%BOT_ROOT%\AstrBot
set PYW=%ASTRBOT_DIR%\venv\Scripts\pythonw.exe
REM ===================================================

REM pythonw = no console window. A console window that gets closed sends
REM CTRL_CLOSE/CTRL_C to the child process, which silently kills AstrBot.
REM That is why this launcher avoids python.exe on purpose.

netstat -ano | findstr ":6185" | findstr LISTENING >nul
if not errorlevel 1 (
  echo [AstrBot] already running, nothing to do.
  timeout /t 4 >nul
  exit /b 0
)

if not exist "%PYW%" (
  echo [AstrBot] pythonw not found: %PYW%
  pause
  exit /b 1
)
if not exist "%BOT_ROOT%\logs" mkdir "%BOT_ROOT%\logs"

echo [AstrBot] starting in background (no window)
echo [AstrBot] log  : %BOT_ROOT%\logs\astrbot.log
echo [AstrBot] panel: http://localhost:6185

set ASTRBOT_DIR=%ASTRBOT_DIR%
start "" "%PYW%" "%~dp0launcher.py"
exit /b 0
