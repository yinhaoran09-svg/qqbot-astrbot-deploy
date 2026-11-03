@echo off
chcp 65001 >nul
title QQ Bot Controller
echo ============================================
echo   QQ Bot Control Panel
echo ============================================
echo   [1] Full stop   - AstrBot + NapCat/QQ offline
echo   [2] Pause only  - stop AstrBot, QQ stays online
echo   [3] Status      - show what is running now
echo ============================================
choice /c 123 /n /m "Choose 1 / 2 / 3 : "
if errorlevel 3 goto :status
if errorlevel 2 goto :pauseonly
if errorlevel 1 goto :fullstop

:fullstop
echo.
echo [1/2] Stopping AstrBot ...
call :killastrbot
echo [2/2] Stopping NapCat and its QQ ...
taskkill /F /IM NapCatWinBootMain.exe >nul 2>&1
REM 必须按路径过滤，否则会把你自己的 QQ 一起杀掉
powershell -NoProfile -Command "Get-Process QQ -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*NapCat*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
echo.
echo Done. Bot and QQ are both offline.
goto :end

:pauseonly
echo.
echo [1/1] Stopping AstrBot only ...
call :killastrbot
echo.
echo Done. Bot no longer replies, QQ still online.
goto :end

:status
echo.
netstat -ano | findstr ":6185" | findstr "LISTENING" >nul
if errorlevel 1 (echo AstrBot : STOPPED) else (echo AstrBot : RUNNING  http://localhost:6185)
tasklist /FI "IMAGENAME eq NapCatWinBootMain.exe" 2>nul | findstr /I "NapCatWinBootMain" >nul
if errorlevel 1 (echo NapCat  : STOPPED) else (echo NapCat  : RUNNING)
powershell -NoProfile -Command "$n=(Get-Process QQ -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*NapCat*' }).Count; if ($n -gt 0) { Write-Host ('QQ      : RUNNING  ' + $n + ' process(es)') } else { Write-Host 'QQ      : STOPPED' }"
goto :end

:killastrbot
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":6185" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)
echo   AstrBot stopped.
exit /b 0

:end
echo.
pause
