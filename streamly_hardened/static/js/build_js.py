#!/usr/bin/env python3
"""Concatenate js/src/*.js fragments into the deployed app.js.

Why: app.js was one 1100-line file. We now edit small fragments in src/ and
bundle them so the browser still loads a single app.js (zero runtime change).

Run from this folder:  python build_js.py
Order is numeric by filename prefix (1-core, 2-cloud, ...).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")

frags = sorted(f for f in os.listdir(SRC) if f.endswith(".js"))
wrap_open = open(os.path.join(SRC, "_wrap_open.txt"), encoding="utf-8").read()
wrap_close = open(os.path.join(SRC, "_wrap_close.txt"), encoding="utf-8").read()

# Each fragment file ends with exactly one trailing newline. Concatenating the
# raw contents reproduces the original body exactly (the split happened at line
# boundaries), so just join wrapper + fragments + closer with newlines.
body = "".join(open(os.path.join(SRC, f), encoding="utf-8").read() for f in frags)
out = wrap_open + "\n" + body + wrap_close
if not out.endswith("\n"):
    out += "\n"
open(os.path.join(HERE, "app.js"), "w", encoding="utf-8").write(out)
print(f"app.js rebuilt from {len(frags)} fragment(s), {out.count(chr(10))} lines")
