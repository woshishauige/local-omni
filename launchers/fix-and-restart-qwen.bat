@echo off
chcp 437 >nul
title Qwen repair and restart (needs admin)
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo [ERROR] This window is NOT elevated, so it cannot stop the old backend.
  echo         Close this window, right-click this file, then choose
  echo         "Run as administrator".
  echo.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix-and-restart-qwen.ps1"
echo.
pause
