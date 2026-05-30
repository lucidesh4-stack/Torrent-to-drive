@echo off
REM ============================================================
REM  Streamly one-click deploy (self-healing)
REM  Works even if you wiped the folder and pasted fresh files
REM  (no .git needed). Just double-click this file.
REM
REM  Does: ensure git repo -> rebuild app.js -> verify ->
REM        commit -> push to GitHub  (Render auto-deploys)
REM ============================================================
setlocal EnableDelayedExpansion

REM --- settings (edit these two if they ever change) ---
set "PYEXE=D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe"
set "REPO_URL=https://github.com/lucidesh4-stack/Torrent-to-drive.git"
set "BRANCH=main"

REM --- repo root = the folder ABOVE this deploy\ folder ---
cd /d "%~dp0..\.."
set "REPO=%cd%"

echo(
echo ====== Streamly Deploy ======
echo Repo folder: %REPO%
echo(

REM --- sanity: python + git present ---
if not exist "%PYEXE%" (
  echo [X] Python not found at:
  echo     %PYEXE%
  echo     Fix the PYEXE line in deploy\deploy.bat
  goto :end
)
where git >nul 2>nul
if errorlevel 1 (
  echo [X] git is not installed / not on PATH. Install Git for Windows.
  goto :end
)

REM --- 1. ensure this folder is a git repo wired to GitHub ---
if not exist ".git" (
  echo [git] No .git here - initializing and linking to GitHub...
  git init -b %BRANCH%
  git remote add origin "%REPO_URL%"
  REM bring in remote history so our push is a normal fast-forward-able commit
  git fetch origin %BRANCH%
  if not errorlevel 1 (
    git reset --soft origin/%BRANCH%
  )
) else (
  REM make sure origin points at the right place
  git remote remove origin >nul 2>nul
  git remote add origin "%REPO_URL%"
)

REM --- 2. rebuild + verify ---
echo(
echo [1/4] Rebuilding app.js and verifying...
"%PYEXE%" "%~dp0check.py"
if errorlevel 1 (
  echo(
  echo [X] Checks FAILED - nothing was committed or pushed.
  goto :end
)

REM --- 3. stage + commit ---
echo(
echo [2/4] Staging changes...
git add -A
git diff --cached --quiet
if not errorlevel 1 (
  echo     Nothing changed since last deploy. Working tree is clean.
  goto :end
)

echo(
set "MSG="
set /p "MSG=[3/4] Commit message (Enter for auto): "
if "!MSG!"=="" set "MSG=deploy %DATE% %TIME%"
git commit -m "!MSG!"

REM --- 4. push ---
echo(
echo [4/4] Pushing to %BRANCH%...
git push -u origin %BRANCH% --force
if errorlevel 1 (
  echo(
  echo [X] Push failed - see the error above.
  echo     (If it asks for login, sign in to GitHub once and re-run.)
  goto :end
)

REM --- tag this deploy as a restore point (good-YYYYMMDD-HHMMSS) ---
set "STAMP=%DATE:/=-%_%TIME::=-%"
set "STAMP=!STAMP: =0!"
git tag "good-!STAMP!" >nul 2>nul
git push origin --tags >nul 2>nul

echo(
echo ====== DONE - Render will auto-deploy in ~1 min ======
echo Restore point saved. To undo later: double-click rollback.bat

:end
echo(
pause
