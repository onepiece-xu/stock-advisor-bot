@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  stock-advisor-bot Windows 状态脚本
REM  用法: status-daemon.bat
REM ============================================================

set "BASE_DIR=%~dp0"
set "PID_FILE=%BASE_DIR%run\stock-advisor.pid"
set "LOG_FILE=%BASE_DIR%logs\monitor.log"

if not exist "%PID_FILE%" (
    echo stock-advisor is not running
    exit /b 0
)

set /p PID=<"%PID_FILE%"
tasklist /FI "PID eq %PID%" 2>nul | findstr /I "%PID%" >nul
if !errorlevel!==0 (
    echo stock-advisor is running: pid=%PID%
    echo log: %LOG_FILE%
) else (
    echo stock-advisor pid file exists but process is not running
)
endlocal
