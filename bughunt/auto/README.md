# auto/ — Automated verification harness

Turns the manual "find → fix → verify" loop into a repeatable, AI-runnable pipeline.
This is the **testing/verification** engine. It does NOT push to the repo and needs no credentials.

## What it does
1. (optional) Pulls the live repo into `bughunt/live/` and records HEAD.
2. Builds the REAL Flask app + test client (best-effort) for dynamic checks.
3. Runs every check in `checks.py` against the mirror + live app.
4. Prints a PASS/FAIL/SKIP report and optional JSON. Exit code = number of FAILs
   (0 = all green) so a CI job / agent loop can gate on it.

## Run it
```bash
cd bughunt
pip install flask telethon requests httpx      # once per session (for dynamic checks)
python3 auto/run.py --pull                      # pull live + run everything
python3 auto/run.py --only C1,S2,S10            # subset
python3 auto/run.py --json out/report.json      # machine-readable
```
- **dynamic=on** in the header means the Flask test client booted (real 401/400/health assertions ran).
- **dynamic=off** = flask not installed; static checks still run, dynamic ones SKIP.

## Add a new issue check
One entry in `checks.py`:
```python
@check("X9", "short title")
def x9(ctx):
    src = ctx.read("routes/telegram.py")      # reads from the live mirror
    return _ok(...) if good else _bad(...)     # or _skip(...)
    # ctx.app_client is a flask test client (or None) for dynamic assertions
```
Checks should be cheap, deterministic, and prefer a static source signal + a dynamic
confirmation where it matters (see C1 / C1-dyn, S10 / S10-dyn).

## How it fits the fix program
- After Antigravity deploys a phase, run `python3 auto/run.py --pull`. Green = the fix is
  live; red = not-yet-fixed or a regression. This is the owner's "tell me to test it" step,
  now one command.
- The check registry mirrors the issue IDs in `cases/00_ANTIGRAVITY_MASTER_HANDOFF.md`.

## Honesty / anti-false-positive notes
- Static checks use scoped regex (e.g. M3 inspects only the `delete()` body) to avoid matching
  unrelated code. Where a static signal is weak, a dynamic check confirms behaviour.
- A check returns SKIP (not PASS) when it cannot truly evaluate — never a silent green.
- Verified working: on a fresh pull it correctly flagged the real `BUNDLE` drift (BOM in app.js)
  while passing the genuinely-fixed items, and confirmed C1 live (401 logged-out / 400 metadata).

## Current baseline (HEAD 555a3a5, 2026-06-15)
12 PASS / 1 FAIL / 0 SKIP. Only FAIL: `BUNDLE` — deployed app.js carries a UTF-8 BOM so it isn't
byte-reproducible from build_js.py (benign at runtime; clean rebuild on next JS change fixes it).
NOTE: the live repo had already shipped most audit fixes (C1/C2/H2/H5/S6/M3...) by this HEAD.
