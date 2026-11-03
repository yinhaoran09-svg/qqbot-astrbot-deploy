@echo off
chcp 65001 >nul
title NapCat

REM ===== change this to your own path =====
set NAPCAT_DIR=%~dp0..\NapCat\NapCat.52230.Shell
REM ======================================

REM chcp 65001 above is THE fix for garbled Chinese in this window:
REM NapCat prints UTF-8 bytes, while a zh-CN console defaults to GBK (936).

cd /d "%NAPCAT_DIR%"
NapCatWinBootMain.exe %QQ_NUMBER%
