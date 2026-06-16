# CI / continuous regression gate — setup

Turns the verification harness (`bughunt/auto/run.py`) into an automatic safety net so a
future change can't silently re-break a fixed issue. Two workflows, pick based on your setup.

## The catch you need to know
Your app is deployed on **Hugging Face Spaces**, not GitHub. GitHub Actions only runs on a
**GitHub** repo. So:

| Your situation | Use | Fires on |
|---|---|---|
| You push code to GitHub (and mirror/deploy to HF) | `ci.yml` | every push / PR — blocks the merge if a check regresses |
| You only push to the HF Space (no GitHub) | `live-monitor.yml` | a schedule (cron) — checks the LIVE deployed Space and alerts if prod regresses |

You can use both. `ci.yml` is the real *gate* (catches it before deploy); `live-monitor.yml` is a
*watchdog* on production.

## Install
1. Put the `bughunt/` folder (at least `auto/run.py`, `auto/checks.py`, and for the monitor,
   `tools/pull.sh`) in a GitHub repo.
   - For `ci.yml`: this should be the repo that also holds the app code (`streamly_hardened/`).
   - For `live-monitor.yml`: can be ANY repo (even a tiny ops-only repo) — `pull.sh` fetches the
     app from the live Space.
2. Copy the workflow(s) into `.github/workflows/`:
   ```
   cp bughunt/auto/ci/ci.yml            .github/workflows/ci.yml
   cp bughunt/auto/ci/live-monitor.yml  .github/workflows/live-monitor.yml
   ```
3. Commit + push. Watch the **Actions** tab.

## How it decides pass/fail
- `run.py` exit code = number of **BLOCKING** failures → non-zero fails the GitHub job (red ❌).
- `--allow <ids>` marks known/accepted FAILs as non-blocking (they still show in the report).
  Both workflows currently pass `--allow BUNDLE` because the app.js BOM is a known cosmetic item
  (see `cases/13_appjs_bom_cleanup_PROMPT.md`). **Remove `--allow BUNDLE` once that ships** to make
  the gate fully strict.
- The JSON report is uploaded as a build artifact each run.

## Proven to actually catch regressions
Tested locally: injecting the old SSRF bug (putting `test_download_speed` back in `exempt_routes`)
makes the harness exit non-zero (both the static AND the dynamic check fire: "logged-out access not
blocked"). With `--allow BUNDLE`, a genuine regression STILL blocks — the allow-list only silences
the named cosmetic check, never real ones.

## Local use (same engine)
```bash
python3 bughunt/auto/run.py --pull --allow BUNDLE     # check the live Space now
python3 bughunt/auto/run.py --root . --only C1,S2     # check a local checkout, subset
```

## Maintenance
- Add a check when you fix a new issue: one `@check(...)` function in `auto/checks.py` (see its header).
- Keep check IDs aligned with `cases/00_ANTIGRAVITY_MASTER_HANDOFF.md`.
- When a known-cosmetic item is fixed, drop it from `--allow` so the gate tightens automatically.
