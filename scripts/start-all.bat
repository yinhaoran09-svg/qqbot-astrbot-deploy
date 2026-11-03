@echo off
chcp 65001 >nul
title QQ Bot Launcher

REM ===== 改这两行成你自己的路径 =====
set ASTRBOT_DIR=D:\qqbot\AstrBot
set NAPCAT_DIR=D:\qqbot\NapCat\NapCat.52230.Shell
REM ==================================

netstat -ano | findstr ":6185" | findstr LISTENING >nul
if errorlevel 1 (
  echo [1/2] AstrBot starting ...
  start "AstrBot" cmd /c "cd /d %ASTRBOT_DIR% && venv\Scripts\python.exe main.py"
  REM 必须等它起来再拉 NapCat，否则 WebSocket 连不上
  timeout /t 12 >nul
) else (
  echo [1/2] AstrBot already running, skip.
)

echo [2/2] NapCat starting ...
cd /d "%NAPCAT_DIR%"
NapCatWinBootMain.exe
pause
