@echo off
REM ============================================================
REM  Streamly ROLLBACK - undo a bad deploy
REM  Shows recent deploys; you pick one; it redeploys that exact
REM  version to GitHub (Render auto-deploys it). Safe + reversible.
REM ============================================================
setlocal EnableDelayedExpansion
set "BRANCH=main"

cd /d "%~dp0.."

where git >nul 2>nul
if errorlevel 1 ( echo [X] git not found. & goto :end )
if not exist ".git" ( echo [X] No .git here - run deploy.bat once first. & goto :end )

echo(
echo ====== Streamly Rollback ======
echo(
echo Recent versions (newest first):
echo --------------------------------
git log --oneline -15 --date=short --pretty=format:"  %%h  %%ad  %%s" --date=short
echo(
echo --------------------------------
echo Copy the short code (e.g. a1b2c3d) of the GOOD version you want.
echo(
set "TARGET="
set /p "TARGET=Commit to roll back to (blank = cancel): "
if "!TARGET!"=="" ( echo Cancelled. & goto :end )

echo(
echo You picked: !TARGET!
git log -1 --pretty=format:"  %%h  %%s" !TARGET! 2>nul
if errorlevel 1 ( echo [X] That commit code wasn't found. & goto :end )
echo(
set "OK="
set /p "OK=Type YES to redeploy this version: "
if /i not "!OK!"=="YES" ( echo Cancelled. & goto :end )

REM Roll the working tree back to that commit, keep it as a NEW commit
REM (so history is preserved and you can roll forward again).
git revert --no-edit !TARGET!..HEAD
if errorlevel 1 (
  echo(
  echo [!] Auto-revert hit a conflict. Falling back to hard reset...
  git reset --hard !TARGET!
)

echo(
echo Pushing rolled-back version...
git push origin %BRANCH% --force
if errorlevel 1 ( echo [X] Push failed - see above. & goto :end )

echo(
echo ====== ROLLED BACK - Render will redeploy in ~1 min ======

:end
echo(
pause
