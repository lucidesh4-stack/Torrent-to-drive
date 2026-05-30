#!/usr/bin/env python3
"""Self-verification gate. No Node required — runs anywhere Python does.

Usage (from the repo root, e.g. D:\\Web based\\Streamly\\Streamly):
    python check.py

It checks:
  1. JS: balanced () [] {} and basic sanity (Node optional; used if present)
  2. CSS: balanced {} per file
  3. Flask: app boots and serves 200 for index + all assets
Exits non-zero if anything fails, so you can gate commits on it.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (deploy/ is one level down)
PKG = os.path.join(ROOT, "streamly_hardened")
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

# ---------- 1. JS ----------
print("-> JS check (static/js/app.js)")
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
    # Bracket-balance fallback that ignores strings, template literals and comments
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack = []
    i, n = 0, len(src)
    state = None  # None | "'" | '"' | '`' | "//" | "/*"
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

# ---------- 2. CSS ----------
print("-> CSS brace balance")
css_dir = os.path.join(PKG, "static", "css")
for f in sorted(os.listdir(css_dir)):
    if not f.endswith(".css"):
        continue
    s = open(os.path.join(css_dir, f), encoding="utf-8").read()
    o, c = s.count("{"), s.count("}")
    print(f"  {f}: {o} open / {c} close", "OK" if o == c else "MISMATCH")
    if o != c:
        fail(f"{f} brace mismatch")

# ---------- 3. Flask boots + 200s ----------
print("-> Flask boots + serves 200")
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
