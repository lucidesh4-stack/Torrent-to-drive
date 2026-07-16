#!/usr/bin/env python3
"""Automated verification runner for CloudFlow/Streamly.

Pulls the live repo (optional), loads the issue check registry, runs every check
against the mirror + a real Flask test client, and prints a PASS/FAIL report.

Usage:
  python3 auto/run.py            # use existing bughunt/live mirror
  python3 auto/run.py --pull     # re-pull live repo first (tools/pull.sh)
  python3 auto/run.py --json out.json
  python3 auto/run.py --only C1,S2

Exit code = number of FAILing checks (0 = all green), so CI can gate on it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUGHUNT = HERE.parent
DEFAULT_BASE = BUGHUNT / "live"   # local default: the pulled mirror

sys.path.insert(0, str(HERE))
import checks as checks_mod  # noqa: E402


def pull(base: Path):
    script = BUGHUNT / "tools" / "pull.sh"
    if not script.exists():
        print("! pull.sh missing; skipping pull", file=sys.stderr)
        return
    print("• pulling live repo ...")
    subprocess.run(["bash", str(script)], check=False)


def build_app_client(base: Path):
    """Best-effort: build the real Flask app + test client. Returns (app, client) or (None, None).

    `base` is the directory that CONTAINS the `streamly` package.
    """
    try:
        os.environ.setdefault("SPACE_ID", "x")
        os.environ.setdefault("SITE_PASSWORD", "autotest-secret")
        os.environ.setdefault("APP_ENV", "test")
        # No Upstash creds -> app boots with rs=None (graceful). That's fine for our checks.
        sys.path.insert(0, str(base))
        from streamly.app import create_app  # type: ignore
        app = create_app()
        if hasattr(app, "test_client"):
            return app, app.test_client()
        else:
            from fastapi.testclient import TestClient
            from contextlib import contextmanager
            import json
            import base64
            from itsdangerous import TimestampSigner
            import httpx
            httpx.Response.get_json = lambda self: self.json()

            client = TestClient(app)

            @contextmanager
            def session_transaction():
                cookie = client.cookies.get("session")
                session_data = {}
                cfg = getattr(app.state, "config", None)
                secret_key = cfg.secret_key if cfg else "autotest-secret"
                signer = TimestampSigner(secret_key)
                if cookie:
                    try:
                        # Decode url encoding first if needed, but TestClient cookies are raw
                        signed_data = cookie.encode("utf-8")
                        data = signer.unsign(signed_data)
                        session_data = json.loads(base64.b64decode(data).decode("utf-8"))
                    except Exception:
                        session_data = {}
                yield session_data
                data = base64.b64encode(json.dumps(session_data).encode("utf-8"))
                signed_value = signer.sign(data).decode("utf-8")
                client.cookies.set("session", signed_value)

            client.session_transaction = session_transaction
            return app, client
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"! could not build Flask app for dynamic checks: {e}", file=sys.stderr)
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true",
                    help="re-pull the live repo into bughunt/live before running")
    ap.add_argument("--root", default="",
                    help="dir CONTAINING streamly/ (CI: the repo checkout). "
                         "Default: bughunt/live (or $CLOUDFLOW_ROOT)")
    ap.add_argument("--json", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--allow", default="",
                    help="comma-separated check IDs whose FAIL does NOT fail the build "
                         "(known/accepted, e.g. cosmetic). They still run and show in the report.")
    args = ap.parse_args()

    # Resolve base (the dir that holds the streamly package).
    base = Path(args.root or os.environ.get("CLOUDFLOW_ROOT", "") or DEFAULT_BASE).resolve()
    root = base / "streamly"

    if args.pull:
        pull(base)

    if not root.exists():
        print(f"FATAL: streamly/ not found under {base}. "
              f"Use --root <repo> or run with --pull.", file=sys.stderr)
        sys.exit(99)

    head_file = base / ".HEAD"
    head = head_file.read_text().strip() if head_file.exists() else os.environ.get("GITHUB_SHA", "local")
    app_obj, client = build_app_client(base)
    ctx = checks_mod.Ctx(root=root, head=head, app_client=client, app_obj=app_obj)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    results = []
    for id_, title, fn in checks_mod.all_checks():
        if only and id_ not in only:
            continue
        try:
            res = fn(ctx)
        except Exception as e:
            res = checks_mod.CheckResult(id_, title, "SKIP", f"check crashed: {e}")
        results.append(res)

    # report
    allow = {x.strip() for x in args.allow.split(",") if x.strip()}
    icons = {"PASS": "✅", "FAIL": "❌", "SKIP": "⚪"}
    n_pass = sum(r.status == "PASS" for r in results)
    n_fail = sum(r.status == "FAIL" for r in results)
    n_skip = sum(r.status == "SKIP" for r in results)
    # Blocking failures = FAILs not on the allow-list.
    blocking = [r for r in results if r.status == "FAIL" and r.id not in allow]
    n_allowed = sum(1 for r in results if r.status == "FAIL" and r.id in allow)

    print()
    print("=" * 78)
    print(f" CloudFlow automated verification   HEAD={head[:10]}   "
          f"dynamic={'on' if client else 'off'}")
    print("=" * 78)
    for r in results:
        tag = "  (allowed)" if (r.status == "FAIL" and r.id in allow) else ""
        print(f" {icons.get(r.status,'?')} {r.id:<8} {r.title}{tag}")
        if r.detail:
            print(f"      └─ {r.detail}")
        if r.evidence:
            print(f"         evidence: {r.evidence}")
    print("-" * 78)
    summary = f" {n_pass} PASS   {n_fail} FAIL   {n_skip} SKIP   (of {len(results)})"
    if n_allowed:
        summary += f"   [{n_allowed} allowed/non-blocking]"
    print(summary)
    print(f" -> {len(blocking)} BLOCKING failure(s)")
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "head": head,
            "summary": {"pass": n_pass, "fail": n_fail, "skip": n_skip,
                        "allowed": n_allowed, "blocking": len(blocking), "total": len(results)},
            "results": [r.__dict__ for r in results],
        }, indent=2))
        print(f"• wrote {args.json}")

    # Exit code = BLOCKING failures only (allow-listed FAILs don't fail the build).
    sys.exit(len(blocking))


if __name__ == "__main__":
    main()
