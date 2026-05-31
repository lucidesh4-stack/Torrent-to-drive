@echo off
REM One-click verifier for Streamly.
REM Double-click this file, or run `check` from this folder.
setlocal
set "PYEXE=D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe"

if not exist "%PYEXE%" (
  echo [check] ERROR: Python not found at:
  echo         %PYEXE%
  echo         Check if the portable Python folder was moved.
  pause
  exit /b 1
)

cd /d "%~dp0"
"%PYEXE%" check.py
echo.
pause
