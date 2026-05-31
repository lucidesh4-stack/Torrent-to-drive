@echo off
REM Rebuild app.js from the src/ fragments.
REM Run this (or check.bat, which also rebuilds) after editing static/js/src/*.js
setlocal
set "PYEXE=D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe"

if not exist "%PYEXE%" (
  echo [build] ERROR: Python not found at:
  echo         %PYEXE%
  echo         Check if the portable Python folder was moved.
  pause
  exit /b 1
)

cd /d "%~dp0..\streamly_hardened\static\js"
"%PYEXE%" build_js.py
echo.
pause
