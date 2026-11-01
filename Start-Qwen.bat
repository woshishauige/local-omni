@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
rem This is the local-only entry point: no tunnel, no shared address.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Qwen.ps1" %*
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
