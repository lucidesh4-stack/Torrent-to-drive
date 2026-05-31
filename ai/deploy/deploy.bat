@echo off
REM ============================================================
REM  Streamly deploy (self-healing)
REM  Double-click this file. Does: verify → commit → push → Render auto-deploys
REM ============================================================
setlocal EnableDelayedExpansion

REM --- Python Path Configuration ---
set "PYEXE=D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe"

set "REPO_URL=https://github.com/lucidesh4-stack/Torrent-to-drive.git"
set "BRANCH=main"

cd /d "%~dp0..\.."
set "REPO=%cd%"

echo.
echo ====== Streamly Deploy ======
echo Repo: %REPO%
echo.

REM --- sanity: python + git present ---
if not exist "%PYEXE%" (
  echo [X] Python not found at: %PYEXE%
  echo     Please check if the portable Python folder was moved or deleted.
  goto :end
)
where git >nul 2>nul
if errorlevel 1 (
  echo [X] git is not installed / not on PATH.
  goto :end
)

REM --- 1. Rebuild app.js + run check.py ---
echo [1/5] Rebuilding app.js and running check.py...
"%PYEXE%" "%~dp0check.py"
if errorlevel 1 (
  echo.
  echo [X] check.py FAILED — nothing was committed.
  goto :end
)

REM --- 2. Local Flask smoke test (portable Python) ---
echo.
echo [2/5] Running local Flask smoke test...
"%PYEXE%" "%~dp0verify_flask.py"
if errorlevel 1 (
  echo.
  echo [X] Local Flask smoke test FAILED.
  echo    Fix the errors above before deploying.
  goto :end
)

REM --- 3. Git init/remote ---
echo.
echo [3/5] Setting up git...
if not exist ".git" (
  git init -b %BRANCH%
  git remote add origin "%REPO_URL%"
  git fetch origin %BRANCH%
  if not errorlevel 1 (
    git reset --soft origin/%BRANCH%
  )
) else (
  git remote remove origin >nul 2>nul
  git remote add origin "%REPO_URL%"
)

REM --- 4. Stage + commit ---
echo.
echo [4/5] Staging and committing...
git add -A
git diff --cached --quiet
if not errorlevel 1 (
  echo     Nothing changed. Working tree is clean.
  goto :end
)

set "MSG="
set /p "MSG=[5/5] Commit message (Enter for auto): "
if "!MSG!"=="" set "MSG=deploy %DATE% %TIME%"
git commit -m "!MSG!"

REM --- 5. Push ---
echo.
echo [6/6] Pushing to %BRANCH%...
git push -u origin %BRANCH% --force
if errorlevel 1 (
  echo.
  echo [X] Push failed. Check the error above.
  goto :end
)

REM --- Tag ---
set "STAMP=%DATE:/=-%_%TIME::=-%"
set "STAMP=!STAMP: =0!"
git tag "good-!STAMP!" >nul 2>nul
git push origin --tags >nul 2>nul

echo.
echo ====== DONE ======
echo Render will auto-deploy in ~1 min.
echo To rollback: double-click ai\deploy\rollback.bat

:end
echo.
pause
