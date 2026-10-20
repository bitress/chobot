@echo off
setlocal
cd /d "%~dp0"
title SysBot Bridge (INSECURE MODE)
echo Starting SysBot Bridge in INSECURE mode (Authentication DISABLED)...
call run.bat --insecure
