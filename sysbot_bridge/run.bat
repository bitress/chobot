@echo off
setlocal enabledelayedexpansion

:: Set console encoding to UTF-8
chcp 65001 >nul 2>&1

:: Navigate to script directory
cd /d "%~dp0"
title SysBot Bridge & Cloudflare Gateway

echo ========================================================
echo        SysBot Bridge & Cloudflare Gateway
echo ========================================================
echo Directory: %CD%

:: Determine Python executable
set "PYTHON_EXE=python"
if exist "..\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=..\.venv\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
)

echo Using Python: %PYTHON_EXE%
echo.

:loop
echo [%DATE% %TIME%] Starting SysBot Bridge on port 8200...
"%PYTHON_EXE%" server.py
set "EXIT_CODE=%ERRORLEVEL%"

echo [%DATE% %TIME%] SysBot Bridge process stopped (code %EXIT_CODE%).
echo Restarting in 3 seconds... (Press Ctrl+C to stop)
timeout /t 3 /nobreak >nul
goto loop
