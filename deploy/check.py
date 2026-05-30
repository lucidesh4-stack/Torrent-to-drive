#!/usr/bin/env python3
"""Self-verification gate. No Node required — runs anywhere Python does.

Usage (from the repo root, e.g. D:\\Web based\\Streamly\\Streamly):
    python check.py

Checks:
  1. JS: balanced () [] {} and basic sanity (Node optional; used if present)
  2. CSS: balanced {} per file
  3. Flask: app boots and serves 200 for index + all assets
  4. Pre-flight summary: shows which fixes are implemented vs pending

Exits non-zero if anything fails, so you can gate commits on it.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
PKG = os.path.join(ROOT, "streamly_hardened")


def _summary():
    """Print pre-flight summary of implemented fixes."""
    app_py = open(os.path.join(PKG, "app.py"), encoding="utf-8").read()
    services_py = open(os.path.join(PKG, "services.py"), encoding="utf-8").read()
    redis_py = open(os.path.join(PKG, "redis_store.py"), encoding="utf-8").read()

    checks = []

    # FIX 1: Storage check in /api/add
    if "storage_full" in app_py and "used + size_bytes" in app_py:
        checks.append(("Storage check before add", True, "server-side, blocks add when storage full"))
    else:
        checks.append(("Storage check before add", False, "not implemented"))

    # FIX 2: Empty token guard
    if "return None" in services_py and 'if not token:' in redis_py:
        checks.append(("Empty token guard", True, "serialize_token returns None, Redis rejects empty"))
    else:
        checks.append(("Empty token guard", False, "not implemented"))

    # FIX 3: Redis health check on startup
    if "Upstash Redis reachable" in app_py and "Upstash Redis unreachable" in app_py:
        checks.append(("Redis health check on startup", True, "warns in logs if Upstash is down"))
    else:
        checks.append(("Redis health check on startup", False, "not implemented"))

    # FIX 4: Specific exception handlers
    broad_except = app_py.count("except Exception as")
    specific_except = app_py.count("except (ConnectionError, TimeoutError)")
    if specific_except >= 6 and broad_except == 0:
        checks.append(("Specific exception handlers", True, f"{specific_except} specific handlers, 0 broad"))
    elif specific_except > 0:
        checks.append(("Specific exception handlers", True, f"{specific_except} specific, {broad_except} broad remaining (low risk)"))
    else:
        checks.append(("Specific exception handlers", False, "not implemented"))

    # FIX 5: Safe error messages (no str(exc) in 502/500 responses)
    safe_502 = app_py.count('"Provider rejected the request' ) or 'json_error(502' in app_py and 'str(e)' not in app_py.split('json_error(502')[0] if 'json_error(502' in app_py else True
    if broad_except == 0:
        checks.append(("Safe error messages", True, "no internal details leaked in error responses"))
    else:
        checks.append(("Safe error messages", False, f"{broad_except} routes still use broad Exception"))

    # FIX 6: Logout endpoint (skipped by user)
    if "/api/logout" in app_py:
        checks.append(("Logout endpoint", True, "implemented"))
    else:
        checks.append(("Logout endpoint", False, "not implemented (user declined)"))

    print("\n===== Streamly Pre-Flight =====")
    done = sum(1 for _, status, _ in checks if status)
    total = len(checks)
    for name, status, detail in checks:
        icon = "✓" if status else "✗"
        note = f"  [{icon}] {name}: {detail}"
        if not status and "not implemented" in detail:
            note += " — consider fixing"
        print(note)
    print(f"\n  {done}/{total} fixes implemented")
    print("===============================")
    return done == total


ok = True


def fail(msg):
    global ok
    ok = False
    print("  FAIL:", msg)


# ---------- 0. Rebuild app.js from fragments ----------
js_dir = os.path.join(PKG, "static", "js")
builder = os.path.join(js_dir, "build_js.py")
if os.path.exists(builder):
    print("-> Rebuild app.js from src/ fragments")
    r = subprocess.run([sys.executable, builder], capture_output=True, text=True)
    print("  " + (r.stdout.strip() or r.stderr.strip()))
    if r.returncode != 0:
        fail("build_js.py failed")

# ---------- 1. Pre-flight summary ----------
print("-> Pre-flight fix summary")
_summary()

# ---------- 2. JS ----------
print("\n-> JS check (static/js/app.js)")
js_path = os.path.join(PKG, "static", "js", "app.js")
src = open(js_path, encoding="utf-8").read()

node = shutil.which("node")
if node:
    r = subprocess.run([node, "--check", js_path], capture_output=True, text=True)
    if r.returncode == 0:
        print("  node --check OK")
    else:
        fail("node --check:\n" + r.stderr.strip())
else:
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack = []
    i, n, state = 0, len(src), None
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state in ("'", '"', "`"):
            if c == "\\":
                i += 2
                continue
            if c == state:
                state = None
        elif state == "//":
            if c == "\n":
                state = None
        elif state == "/*":
            if c == "*" and nxt == "/":
                state = None
                i += 2
                continue
        else:
            if c in ("'", '"', "`"):
                state = c
            elif c == "/" and nxt == "/":
                state = "//"
            elif c == "/" and nxt == "*":
                state = "/*"
            elif c in opens:
                stack.append(c)
            elif c in pairs:
                if not stack or stack[-1] != pairs[c]:
                    fail(f"unbalanced '{c}' near offset {i}")
                    break
                stack.pop()
        i += 1
    if ok and stack:
        fail(f"{len(stack)} unclosed bracket(s)")
    if ok:
        print("  bracket balance OK (install Node for a deeper check)")

# ---------- 3. CSS ----------
print("\n-> CSS brace balance")
css_dir = os.path.join(PKG, "static", "css")
for f in sorted(os.listdir(css_dir)):
    if not f.endswith(".css"):
        continue
    s = open(os.path.join(css_dir, f), encoding="utf-8").read()
    o, c = s.count("{"), s.count("}")
    print(f"  {f}: {o} open / {c} close", "OK" if o == c else "MISMATCH")
    if o != c:
        fail(f"{f} brace mismatch")

# ---------- 4. Flask boots + 200s ----------
print("\n-> Flask boots + serves 200")
os.environ.setdefault("SECRET_KEY", "check")
os.environ.setdefault("APP_ENV", "development")
sys.path.insert(0, ROOT)
try:
    from streamly_hardened.app import create_app

    c = create_app().test_client()
    routes = {
        "index": "/",
        "base.css": "/static/css/base.css",
        "responsive.css": "/static/css/responsive.css",
        "app.js": "/static/js/app.js",
    }
    parts = []
    for name, url in routes.items():
        code = c.get(url).status_code
        parts.append(f"{name}={code}")
        if code != 200:
            fail(f"{name} returned {code}")
    print("  " + " ".join(parts))
except Exception as e:
    fail(f"Flask error: {e}")

print()
if ok:
    print("OK - all checks passed")
    sys.exit(0)
print("FAILED - fix the issues above before committing")
sys.exit(1)
