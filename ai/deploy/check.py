#!/usr/bin/env python3
"""Self-verification gate. No Node required — runs anywhere Python does.

Usage:
    python check.py

Checks: JS bracket balance, CSS brace balance, Flask boots + serves 200,
pre-flight summary of implemented fixes.
"""
import os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG  = os.path.join(ROOT, "streamly_hardened")


def preflight():
    app_py   = open(os.path.join(PKG, "app.py"), encoding="utf-8").read()
    cloud_py = open(os.path.join(PKG, "cloud_service.py"), encoding="utf-8").read()
    redis_py = open(os.path.join(PKG, "redis_store.py"), encoding="utf-8").read()

    checks = []

    # Storage check
    found_storage = False
    # Check app.py
    if "storage_full" in app_py and "used + size_bytes" in app_py:
        found_storage = True
    else:
        # Check routes/
        routes_dir = os.path.join(PKG, "routes")
        if os.path.exists(routes_dir):
            for f in os.listdir(routes_dir):
                if f.endswith(".py"):
                    content = open(os.path.join(routes_dir, f), encoding="utf-8").read()
                    if "storage_full" in content and "used + size_bytes" in content:
                        found_storage = True; break
    
    if found_storage:
        checks.append(("Storage check before add", True, "server-side"))
    else:
        checks.append(("Storage check before add", False, "not implemented"))

    if "return None" in cloud_py and 'if not token:' in redis_py:
        checks.append(("Empty token guard", True, "serialize returns None, Redis rejects empty"))
    else:
        checks.append(("Empty token guard", False, "not implemented"))

    if "Upstash Redis reachable" in app_py:
        checks.append(("Redis health check", True, "warns on startup if down"))
    else:
        checks.append(("Redis health check", False, "not implemented"))

    # Broad exception handlers are now split across app.py and routes/
    broad_count = app_py.count("except Exception as")
    for f in os.listdir(os.path.join(PKG, "routes")):
        if f.endswith(".py"):
            broad_count += open(os.path.join(PKG, "routes", f), encoding="utf-8").read().count("except Exception as")
            
    spec_count = app_py.count("except (ConnectionError, TimeoutError)")
    for f in os.listdir(os.path.join(PKG, "routes")):
        if f.endswith(".py"):
            spec_count += open(os.path.join(PKG, "routes", f), encoding="utf-8").read().count("except (ConnectionError, TimeoutError)")

    if spec_count >= 6 and broad_count == 0:
        checks.append(("Specific exception handlers", True, f"{spec_count} specific, 0 broad"))
    elif spec_count > 0:
        checks.append(("Specific exception handlers", True, f"{spec_count} specific, {broad_count} broad (low risk)"))
    else:
        checks.append(("Specific exception handlers", False, "not implemented"))

    if broad_count == 0:
        checks.append(("Safe error messages", True, "no internal details leaked"))
    else:
        checks.append(("Safe error messages", False, f"{broad_count} broad handlers remain"))

    if "/api/logout" in app_py:
        checks.append(("Logout endpoint", True, "implemented"))
    else:
        checks.append(("Logout endpoint", False, "not implemented (user declined)"))

    print("\n===== Streamly Pre-Flight =====")
    done = sum(1 for _, s, _ in checks if s)
    for name, status, detail in checks:
        icon = "OK" if status else "XX"
        print(f"  [{icon}] {name}: {detail}")
    print(f"\n  {done}/{len(checks)} fixes implemented")
    print("===============================")
    return done == len(checks) - 1   # -1 for logout (user declined)


ok = True


def fail(msg):
    global ok
    ok = False
    print("  FAIL:", msg)


# Rebuild app.js
js_dir = os.path.join(PKG, "static", "js")
builder = os.path.join(js_dir, "build_js.py")
if os.path.exists(builder):
    print("-> Rebuild app.js")
    r = subprocess.run([sys.executable, builder], capture_output=True, text=True)
    print("  " + (r.stdout.strip() or r.stderr.strip()))
    if r.returncode != 0:
        fail("build_js.py failed")

print("-> Pre-flight summary")
preflight()

# JS check
print("\n-> JS bracket balance")
js_path = os.path.join(PKG, "static", "js", "app.js")
src = open(js_path, encoding="utf-8").read()

node = shutil.which("node")
if node:
    r = subprocess.run([node, "--check", js_path], capture_output=True, text=True)
    if r.returncode == 0:
        print("  node --check OK")
    else:
        fail("node --check: " + r.stderr.strip())
else:
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack, i, n, state = [], 0, len(src), None
    while i < n:
        c, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if state in ("'", '"', "`"):
            if c == "\\": i += 2; continue
            if c == state: state = None
        elif state == "//":
            if c == "\n": state = None
        elif state == "/*":
            if c == "*" and nxt == "/": state = None; i += 2; continue
        else:
            if c in ("'", '"', "`"): state = c
            elif c == "/" and nxt == "/": state = "//"
            elif c == "/" and nxt == "*": state = "/*"
            elif c in opens: stack.append(c)
            elif c in pairs:
                if not stack or stack[-1] != pairs[c]: fail(f"unbalanced '{c}' at {i}"); break
                stack.pop()
        i += 1
    if ok and stack: fail(f"{len(stack)} unclosed")
    if ok: print("  bracket balance OK")

# CSS check
print("\n-> CSS brace balance")
for f in sorted(os.listdir(os.path.join(PKG, "static", "css"))):
    if not f.endswith(".css"): continue
    s = open(os.path.join(PKG, "static", "css", f), encoding="utf-8").read()
    o, c = s.count("{"), s.count("}")
    print(f"  {f}: {o} open / {c} close", "OK" if o == c else "MISMATCH")
    if o != c: fail(f"{f} brace mismatch")

# Flask check
print("\n-> Flask boots + serves 200")
os.environ.setdefault("SECRET_KEY", "check")
os.environ.setdefault("APP_ENV", "development")
sys.path.insert(0, ROOT)
try:
    from streamly_hardened.app import create_app
    c = create_app().test_client()
    for name, url in [("index", "/"), ("base.css", "/static/css/base.css"),
                      ("responsive.css", "/static/css/responsive.css"), ("app.js", "/static/js/app.js")]:
        code = c.get(url).status_code
        print(f"  {name}={code}", end="")
        if code != 200: fail(f"{name} returned {code}")
    print()
except Exception as e:
    fail(f"Flask error: {e}")

print()
if ok:
    print("OK - all checks passed")
    sys.exit(0)
print("FAILED - fix the issues above before committing")
sys.exit(1)
