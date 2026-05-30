#!/usr/bin/env python3
"""Streamly batch deploy script.

Usage:
    python3 deploy/deploy_all.py

Reads changes.json → writes files to disk → rebuilds app.js →
runs check.py → auto-updates CHANGELOG/CONTEXT/ACTIVITY_LOG → creates project.zip

changes.json format:
{
  "session": "YYYY-MM-DD — brief description",
  "changes": [{"file": "path", "content": "..."}, ...]
}
"""
import datetime
import json
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG  = os.path.join(ROOT, "streamly_hardened")
CHANGES_JSON = os.path.join(ROOT, "ai", "changes.json")


def _load_changes():
    if not os.path.exists(CHANGES_JSON):
        print(f"  ERROR: changes.json not found at {CHANGES_JSON}")
        print("  Create changes.json first. See CHANGES.md for template.")
        sys.exit(1)
    with open(CHANGES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    session = data.get("session", "unknown")
    changes = data.get("changes", [])
    if not changes:
        print("  WARNING: changes array is empty. Nothing to deploy.")
        sys.exit(0)
    return session, changes


def _git_snapshot(session):
    """Commit current state as a revert point before applying changes."""
    msg = f"auto-snapshot before: {session}"
    # Stage all current files
    r = subprocess.run(
        ["git", "add", "-A"],
        cwd=ROOT, capture_output=True, text=True
    )
    # Check if anything is staged
    r2 = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=ROOT, capture_output=True
    )
    if r2.returncode != 0:
        # Only commit if there are staged changes
        r3 = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=ROOT, capture_output=True, text=True
        )
        tag = f"before-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        subprocess.run(["git", "tag", tag], cwd=ROOT, capture_output=True)
        print(f"  Git snapshot created: tag={tag}")
    else:
        print("  Git: no changes to snapshot (working tree clean)")


def _write_files(changes):
    # Skip deploy infrastructure scripts — they must not be overwritten by changes.json
    # (deploy_all.py and check.py have complex path dependencies that must stay intact)
    SKIP_FILES = {"ai/deploy/deploy_all.py", "ai/deploy/check.py"}
    for item in changes:
        rel = item["file"]
        if rel in SKIP_FILES:
            print(f"  skipped (infra): {rel}")
            continue
        content = item["content"]
        full = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  written: {rel} ({len(content)} bytes)")


def _rebuild_appjs():
    src_dir = os.path.join(PKG, "static", "js", "src")
    wrap_open  = open(os.path.join(src_dir, "_wrap_open.txt"),  encoding="utf-8").read()
    wrap_close = open(os.path.join(src_dir, "_wrap_close.txt"), encoding="utf-8").read()
    frags = sorted(f for f in os.listdir(src_dir) if f.endswith(".js"))
    body = "".join(open(os.path.join(src_dir, f), encoding="utf-8").read() for f in frags)
    out = wrap_open + "\n" + body + wrap_close
    if not out.endswith("\n"):
        out += "\n"
    open(os.path.join(PKG, "static", "js", "app.js"), "w", encoding="utf-8").write(out)
    print(f"  app.js rebuilt from {len(frags)} fragments, {out.count(chr(10))} lines")


def _run_check():
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "ai", "deploy", "check.py")],
        capture_output=True, text=True
    )
    print(r.stdout)
    if r.stderr:
        print(r.stderr)
    if r.returncode != 0:
        print("  WARNING: check.py failed — review above before deploying")
    return r.returncode == 0


def _get_consistent_date():
    """Use Arena's system date (Asia/Calcutta) aligned with CHANGELOG."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    cl_path = os.path.join(ROOT, "ai", "CHANGELOG.md")
    if os.path.exists(cl_path):
        content = open(cl_path, encoding="utf-8").read()
        for line in reversed(content.splitlines()):
            if line.startswith("## 20") and len(line) >= 13:
                latest = line[3:13]
                if latest != today:
                    today = latest
                break
    return today


def _auto_docs(session, changes):
    today = _get_consistent_date()
    files_changed = [c["file"] for c in changes]

    # CHANGELOG
    cl_path = os.path.join(ROOT, "ai", "CHANGELOG.md")
    cl = open(cl_path, encoding="utf-8").read() if os.path.exists(cl_path) else ""
    marker = f"## {today}"
    if marker not in cl:
        entry = f"\n## {today} — {session}\n\n- Files changed: {', '.join(files_changed)}\n"
        cl += entry
        open(cl_path, "w", encoding="utf-8").write(cl)
        print(f"  CHANGELOG.md: {today} entry added")
    else:
        # Append to existing section
        idx = cl.index(marker)
        end = cl.find("\n## ", idx + len(marker))
        existing = cl[idx:end] if end > 0 else cl[idx:]
        if all(f not in existing for f in files_changed):
            cl = cl[:idx] + existing.rstrip() + f"\n- Files changed: {', '.join(files_changed)}\n" + cl[end if end > 0 else len(cl):]
            open(cl_path, "w", encoding="utf-8").write(cl)
            print(f"  CHANGELOG.md: appended files to {today}")
        else:
            print(f"  CHANGELOG.md: {today} already has these files, skipping")

    # CONTEXT recent changes
    ctx_path = os.path.join(ROOT, "ai", "CONTEXT.md")
    ctx = open(ctx_path, encoding="utf-8").read()
    change_entry = f"- **{today}** — {session}. Changed: {', '.join(files_changed)}.\n"
    if "## RECENT CHANGES LOG" in ctx and change_entry.strip() not in ctx:
        idx = ctx.index("## RECENT CHANGES LOG") + len("## RECENT CHANGES LOG")
        ctx = ctx[:idx] + "\n" + change_entry + ctx[idx:]
        open(ctx_path, "w", encoding="utf-8").write(ctx)
        print(f"  CONTEXT.md: recent changes updated")

    # ACTIVITY_LOG
    act_path = os.path.join(ROOT, "ai", "ACTIVITY_LOG.md")
    act_entry = f"[{today}] {session} — {', '.join(files_changed)}\n"
    act = open(act_path, encoding="utf-8").read() if os.path.exists(act_path) else "# Streamly Deployment Activity Log\n\n"
    if act_entry.strip() not in act:
        open(act_path, "w", encoding="utf-8").write(act + act_entry)
        print(f"  ACTIVITY_LOG.md: entry added")
    else:
        print(f"  ACTIVITY_LOG.md: no change since last run")


def _create_zip():
    zip_path = "/home/user/project.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root_dir, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (
                "__pycache__", ".pytest_cache", ".git", ".venv", "venv",
                "*.egg-info", "node_modules", ".cache", ".ruff_cache", ".arena"
            )]
            for file in files:
                if file.endswith((".pyc",)):
                    continue
                full = os.path.join(root_dir, file)
                arcname = os.path.relpath(full, ROOT)
                zf.write(full, arcname)
    size = os.path.getsize(zip_path)
    print(f"  project.zip: {size // 1024} KB at {zip_path}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
print("=== deploy_all.py ===\n")

# 1. Load changes
print(f"Loading: {CHANGES_JSON}")
session, changes = _load_changes()
print(f"  Session: {session}")
print(f"  Files: {len(changes)}\n")

# 2. Git snapshot (only if git repo exists)
if os.path.exists(os.path.join(ROOT, ".git")):
    print("=== Git snapshot (revert point) ===")
    _git_snapshot(session)
    print()

# 3. Write files
print("=== Writing files ===")
_write_files(changes)
print()

# 4. Rebuild app.js
print("=== Rebuild app.js ===")
_rebuild_appjs()
print()

# 5. Run check.py
print("=== Running check.py ===")
check_ok = _run_check()
print()

# 6. Auto-update docs
print("=== Auto-updating docs ===")
_auto_docs(session, changes)
print()

# 7. Create zip
print("=== Creating project.zip ===")
_create_zip()
print()

# Done
if check_ok:
    print("=== DONE ===")
    print("All checks passed. Deploy with: double-click ai/deploy/deploy.bat")
else:
    print("=== DONE (with warnings) ===")
    print("Check.py reported issues. Review above before deploying.")
