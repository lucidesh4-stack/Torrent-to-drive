Streamly — dead-simple deploy
=============================

THE ONLY STEPS YOU NEED:
  1. Download project.zip
  2. Wipe the project folder, paste the zip's contents into it
  3. Open the  deploy\  folder and DOUBLE-CLICK  deploy.bat
  4. Type a commit message (or press Enter) -> wait for "DONE"

That's it. deploy.bat is SELF-HEALING:
  - If .git is missing (because you wiped the folder) it re-links to GitHub
    automatically: https://github.com/lucidesh4-stack/Torrent-to-drive.git
  - Rebuilds app.js from src/ fragments
  - Verifies (JS + CSS + Flask 200) and STOPS if anything fails
  - Commits + pushes -> Render auto-deploys (~1 min)

ONE-TIME REQUIREMENTS:
  - Git for Windows installed
  - Flask in your portable Python:
      "D:\Downloads\Projects\Python Project\WPy64-31241\python-3.12.4.amd64\python.exe" -m pip install Flask
  - First push may ask you to sign in to GitHub once.

OTHER TOOLS IN THIS FOLDER (optional):
  check.bat  -> rebuild + verify only (no push)
  build.bat  -> rebuild app.js only
  check.py   -> the verifier

IF YOU MOVE PYTHON: edit the PYEXE line at the top of deploy.bat
