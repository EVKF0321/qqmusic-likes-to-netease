@echo off
cd /d "%~dp0"
if exist "Windows\MusicBridge\MusicBridge.exe" (
  start "" "Windows\MusicBridge\MusicBridge.exe"
  exit /b
)
if not exist ".venv\Scripts\python.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 (pause & exit /b 1)
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
