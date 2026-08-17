@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  stock-advisor-bot Windows 启动脚本
REM  用法: start-daemon.bat
REM ============================================================

set "BASE_DIR=%~dp0"
set "PID_FILE=%BASE_DIR%run\stock-advisor.pid"
set "LOG_FILE=%BASE_DIR%logs\monitor.log"
set "ENV_FILE=G:\hermes\.hermes\.env"

REM ── 从 Hermes .env 加载 API key（只加载 DEEPSEEK_API_KEY） ──
if exist "%ENV_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%ENV_FILE%") do (
        set "key=%%a"
        set "val=%%b"
        if "!key!"=="DEEPSEEK_API_KEY" set "DEEPSEEK_API_KEY=!val!"
        if "!key!"=="DEEPSEEK_BASE_URL" set "DEEPSEEK_BASE_URL=!val!"
        if "!key!"=="DEEPSEEK_MODEL" set "DEEPSEEK_MODEL=!val!"
    )
)

REM ── 检查是否已在运行 ──
if exist "%PID_FILE%" (
    set /p OLD_PID=<"%PID_FILE%"
    tasklist /FI "PID eq !OLD_PID!" 2>nul | findstr /I "!OLD_PID!" >nul
    if !errorlevel!==0 (
        echo stock-advisor already running: pid=!OLD_PID!
        exit /b 0
    ) else (
        echo stale pid file removed
        del "%PID_FILE%" 2>nul
    )
)

REM ── 创建目录 ──
if not exist "%BASE_DIR%logs" mkdir "%BASE_DIR%logs"
if not exist "%BASE_DIR%run" mkdir "%BASE_DIR%run"

REM ── 后台启动 monitor-daemon ──
cd /d "%BASE_DIR%"
start /b "" python -m stock_advisor.cli monitor-daemon --config "%BASE_DIR%config.yaml" >> "%LOG_FILE%" 2>&1

REM ── 记录 PID（start /b 无法直接拿到 PID，用 wmic/powershell 反查） ──
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*monitor-daemon*' -and $_.CommandLine -like '*stock-advisor-bot*' } | Select-Object -First 1 -ExpandProperty ProcessId" > "%BASE_DIR%run\_pid.tmp" 2>nul
set /p DAEMON_PID=<"%BASE_DIR%run\_pid.tmp" 2>nul
del "%BASE_DIR%run\_pid.tmp" 2>nul

if defined DAEMON_PID (
    echo !DAEMON_PID!> "%PID_FILE%"
    echo started stock-advisor: pid=!DAEMON_PID! log=%LOG_FILE%
) else (
    echo started stock-advisor (pid auto-detect pending), log=%LOG_FILE%
)

endlocal
