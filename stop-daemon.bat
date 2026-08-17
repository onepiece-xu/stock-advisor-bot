@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  stock-advisor-bot Windows 停止脚本
REM  用法: stop-daemon.bat
REM ============================================================

set "BASE_DIR=%~dp0"
set "PID_FILE=%BASE_DIR%run\stock-advisor.pid"

if not exist "%PID_FILE%" (
    echo stock-advisor is not running
    exit /b 0
)

set /p PID=<"%PID_FILE%"
tasklist /FI "PID eq %PID%" 2>nul | findstr /I "%PID%" >nul
if !errorlevel!==0 (
    taskkill /PID %PID% /F >nul 2>&1
    echo stopped stock-advisor: pid=%PID%
) else (
    echo stale pid file removed
)

del "%PID_FILE%" 2>nul
endlocal
