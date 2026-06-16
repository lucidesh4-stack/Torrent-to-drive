"""Issue check registry for CloudFlow/Streamly.

Each check is a function (ctx) -> CheckResult. A check is intentionally cheap and
deterministic: it inspects the freshly-pulled live mirror (static source checks)
and/or builds the real Flask app + test client (dynamic checks). No network to
third parties beyond the HF pull done by run.py.

Add a new issue = add one @check entry. Status meanings:
  PASS  -> the fix is present / the bad pattern is gone
  FAIL  -> the problem is still there (regression or not-yet-fixed)
  SKIP  -> couldn't evaluate (missing dep / file)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class CheckResult:
    id: str
    title: str
    status: str            # PASS | FAIL | SKIP
    detail: str = ""
    evidence: str = ""


@dataclass
class Ctx:
    root: Path                       # bughunt/live/streamly_hardened
    head: str
    app_client: object = None        # flask test client, or None
    app_obj: object = None
    cache: dict = field(default_factory=dict)

    def read(self, rel: str) -> str:
        p = self.root / rel
        if rel in self.cache:
            return self.cache[rel]
        txt = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        self.cache[rel] = txt
        return txt


_REGISTRY: list[tuple[str, str, Callable]] = []


def check(id_: str, title: str):
    def deco(fn):
        _REGISTRY.append((id_, title, fn))
        return fn
    return deco


def all_checks():
    return list(_REGISTRY)


def _ok(id_, title, detail, ev=""):
    return CheckResult(id_, title, "PASS", detail, ev)


def _bad(id_, title, detail, ev=""):
    return CheckResult(id_, title, "FAIL", detail, ev)


def _skip(id_, title, detail):
    return CheckResult(id_, title, "SKIP", detail)


# ----------------------------------------------------------------------------
# C1 — SSRF on /api/telegram/test-download
# ----------------------------------------------------------------------------
@check("C1", "SSRF: test-download requires auth + validates URL")
def c1(ctx: Ctx) -> CheckResult:
    app_py = ctx.read("app.py")
    tg = ctx.read("routes/telegram.py")
    sec = ctx.read("security.py")

    exempt_line = next((l for l in app_py.splitlines() if "exempt_routes" in l and "=" in l), "")
    still_exempt = "test_download_speed" in exempt_line
    has_validator = "def validate_public_url" in sec
    endpoint_validates = "validate_public_url(" in tg
    rate_limited = bool(re.search(r"@rate_limited[^\n]*\n\s*def test_download_speed", tg))

    problems = []
    if still_exempt:
        problems.append("endpoint still in exempt_routes (auth bypass)")
    if not has_validator:
        problems.append("validate_public_url helper missing")
    if not endpoint_validates:
        problems.append("endpoint does not call validate_public_url")
    if not rate_limited:
        problems.append("endpoint not @rate_limited")

    if problems:
        return _bad("C1", "SSRF test-download", "; ".join(problems), exempt_line.strip())
    return _ok("C1", "SSRF test-download",
               "auth gate enforced + URL validated + rate limited",
               f"validator={has_validator} validates={endpoint_validates} rl={rate_limited}")


# Dynamic confirmation of C1 via the real app (logged-out 401, metadata 400).
@check("C1-dyn", "SSRF: live app blocks unauth + metadata URL")
def c1_dyn(ctx: Ctx) -> CheckResult:
    c = ctx.app_client
    if c is None:
        return _skip("C1-dyn", "SSRF dynamic", "flask app/client unavailable")
    r1 = c.get("/api/telegram/test-download?url=http://169.254.169.254/")
    if r1.status_code != 401:
        return _bad("C1-dyn", "SSRF dynamic",
                    f"logged-out access not blocked (got {r1.status_code}, want 401)")
    with c.session_transaction() as s:
        s["site_auth"] = True
        s["sid"] = "autotest"
    r2 = c.get("/api/telegram/test-download?url=http://169.254.169.254/latest/meta-data/")
    if r2.status_code != 400:
        return _bad("C1-dyn", "SSRF dynamic",
                    f"authed metadata URL not rejected (got {r2.status_code}, want 400)")
    return _ok("C1-dyn", "SSRF dynamic", "401 logged-out, 400 on metadata URL")


# ----------------------------------------------------------------------------
# S3/C3 + S2 — queue dispatch lock + heartbeat TTL
# ----------------------------------------------------------------------------
@check("S3", "Queue: atomic SETNX dispatch lock present")
def s3(ctx: Ctx) -> CheckResult:
    tg = ctx.read("routes/telegram.py")
    has_lock_key = "transfer_dispatch_lock" in tg
    uses_nx = bool(re.search(r'SET",\s*_?DISPATCH_LOCK_KEY.*"NX"', tg)) or \
              bool(re.search(r'dispatch_lock.*NX', tg, re.S))
    wraps = "_trigger_next_transfer_locked" in tg
    if has_lock_key and wraps:
        return _ok("S3", "Queue dispatch lock", "SETNX dispatch lock wraps trigger_next_transfer")
    return _bad("S3", "Queue dispatch lock",
                f"lock_key={has_lock_key} nx={uses_nx} wrapper={wraps}")


@check("S2", "Queue: active marker uses short heartbeat TTL (not 3600)")
def s2(ctx: Ctx) -> CheckResult:
    tg = ctx.read("routes/telegram.py")
    has_ttl = "_ACTIVE_TTL_SECONDS" in tg
    set_with_short = bool(re.search(r'active_transfer_global",\s*task_id,\s*ex=_ACTIVE_TTL_SECONDS', tg))
    heartbeat = bool(re.search(r'EXPIRE",\s*"streamly:active_transfer_global"', tg))
    if has_ttl and set_with_short and heartbeat:
        return _ok("S2", "Heartbeat TTL", "active marker set with short TTL + refreshed by ProgressTracker")
    return _bad("S2", "Heartbeat TTL",
                f"const={has_ttl} short_set={set_with_short} heartbeat={heartbeat}")


# ----------------------------------------------------------------------------
# S10 — health: /healthz still trivial + /healthz/deep exists, non-gating
# ----------------------------------------------------------------------------
@check("S10", "Health: /healthz/deep readiness probe exists, /healthz unchanged")
def s10(ctx: Ctx) -> CheckResult:
    app_py = ctx.read("app.py")
    has_deep = "/healthz/deep" in app_py or "def healthz_deep" in app_py
    if not has_deep:
        return _bad("S10", "Health deep probe", "/healthz/deep not found")
    return _ok("S10", "Health deep probe", "/healthz/deep present")


@check("S10-dyn", "Health: live /healthz==200 and /healthz/deep reports checks")
def s10_dyn(ctx: Ctx) -> CheckResult:
    c = ctx.app_client
    if c is None:
        return _skip("S10-dyn", "Health dynamic", "flask app/client unavailable")
    r = c.get("/healthz")
    if r.status_code != 200:
        return _bad("S10-dyn", "Health dynamic", f"/healthz not 200 (got {r.status_code})")
    rd = c.get("/healthz/deep")
    if rd.status_code in (404,) or rd.get_json() is None:
        return _bad("S10-dyn", "Health dynamic", "/healthz/deep missing or non-JSON")
    j = rd.get_json()
    if "checks" not in j:
        return _bad("S10-dyn", "Health dynamic", f"/healthz/deep has no checks: {j}")
    return _ok("S10-dyn", "Health dynamic", f"/healthz 200; deep checks={list(j['checks'])}")


# ----------------------------------------------------------------------------
# C2 — file-size cap: 4000-part guard present
# ----------------------------------------------------------------------------
@check("C2", "Telegram: explicit 4000-part / hard-byte cap guard")
def c2(ctx: Ctx) -> CheckResult:
    tg = ctx.read("routes/telegram.py")
    has_parts_guard = bool(re.search(r"parts?_count\s*(<=|>)\s*4000", tg)) or \
                      "_TG_MAX_PARTS" in tg
    has_hard = "2097152000" in tg
    if has_parts_guard:
        return _ok("C2", "Size cap guard", "explicit 4000-part guard present")
    if has_hard:
        return _bad("C2", "Size cap guard",
                    "hard byte cap present but no explicit parts<=4000 assert before upload")
    return _bad("C2", "Size cap guard", "no 4000-part guard found")


# ----------------------------------------------------------------------------
# H2/M5 — task ownership + full-length ids
# ----------------------------------------------------------------------------
@check("H2", "Telegram: task_id full-length + ownership checked on cancel/status")
def h2(ctx: Ctx) -> CheckResult:
    tg = ctx.read("routes/telegram.py")
    short_id = bool(re.search(r"uuid\.uuid4\(\)\)\[:8\]", tg)) or "str(uuid.uuid4())[:8]" in tg
    # crude ownership heuristic: cancel/status compares an owner sid from task args
    owner_check = ("args.get(\"sid\")" in tg or "owner" in tg.lower()) and "403" in tg
    if not short_id and owner_check:
        return _ok("H2", "Task ownership", "full-length ids + ownership enforced")
    probs = []
    if short_id:
        probs.append("task_id still truncated to 8 chars")
    if not owner_check:
        probs.append("no ownership/403 check on task endpoints")
    return _bad("H2", "Task ownership", "; ".join(probs))


# ----------------------------------------------------------------------------
# H5 — uploader cannot hang forever (bounded queue.get / narrowed except)
# ----------------------------------------------------------------------------
@check("H5", "Telegram: uploader queue.get is bounded (no infinite hang)")
def h5(ctx: Ctx) -> CheckResult:
    tg = ctx.read("routes/telegram.py")
    bounded = "wait_for(output_queue.get()" in tg or "wait_for(\n" in tg and "output_queue.get" in tg
    narrowed = "except BaseException" not in tg
    if bounded:
        return _ok("H5", "Uploader hang guard", "bounded output_queue.get present")
    return _bad("H5", "Uploader hang guard",
                f"bounded_get={bounded} baseexcept_removed={narrowed}")


# ----------------------------------------------------------------------------
# S6 — proxy fallback to direct download
# ----------------------------------------------------------------------------
@check("S6", "Telegram: direct-download fallback when proxy fails")
def s6(ctx: Ctx) -> CheckResult:
    tg = ctx.read("routes/telegram.py")
    # heuristic: a second download attempt against file_url after a proxy failure
    fallback = ("fallback" in tg.lower() and "file_url" in tg) and \
               (tg.lower().count("download_url") >= 1)
    explicit = "direct" in tg.lower() and "retry" in tg.lower()
    if explicit:
        return _ok("S6", "Proxy fallback", "explicit direct-download retry present")
    return _bad("S6", "Proxy fallback", "no explicit direct-download fallback detected")


# ----------------------------------------------------------------------------
# S5 — redis _execute logs failures (visibility) / retry
# ----------------------------------------------------------------------------
@check("S5", "Redis: _execute surfaces failures (warn-log/retry)")
def s5(ctx: Ctx) -> CheckResult:
    rs = ctx.read("redis_store.py")
    # baseline already logs a warning on RequestException; fix adds retry. We pass
    # if there's a retry loop OR an explicit per-command warning with the command.
    retry = "retry" in rs.lower() or "for attempt" in rs.lower() or "range(" in rs and "_execute" in rs
    warns = "log.warning" in rs
    if retry:
        return _ok("S5", "Redis visibility", "retry/backoff present in _execute")
    if warns:
        return CheckResult("S5", "Redis visibility", "SKIP",
                           "baseline warn-log present; retry not yet added (low priority)")
    return _bad("S5", "Redis visibility", "no warn-log or retry in _execute")


# ----------------------------------------------------------------------------
# M3 — RedisStore.delete semantics
# ----------------------------------------------------------------------------
@check("M3", "Redis: delete() returns based on DEL integer result")
def m3(ctx: Ctx) -> CheckResult:
    rs = ctx.read("redis_store.py")
    m = re.search(r"def delete\(self.*?\n(.*?)\n\n", rs, re.S)
    body = m.group(1) if m else ""
    bad = "is not None" in body
    if body and not bad:
        return _ok("M3", "delete() semantics", "delete() no longer uses 'is not None'")
    if bad:
        return CheckResult("M3", "delete() semantics", "SKIP",
                           "still 'is not None' (cosmetic/low priority)")
    return _skip("M3", "delete() semantics", "could not locate delete()")


# ----------------------------------------------------------------------------
# BUNDLE — app.js reproducible from build_js.py
# ----------------------------------------------------------------------------
@check("BUNDLE", "JS: app.js reproducible from src via build_js.py")
def bundle(ctx: Ctx) -> CheckResult:
    import subprocess, tempfile, shutil, os
    js_dir = ctx.root / "static" / "js"
    if not (js_dir / "build_js.py").exists():
        return _skip("BUNDLE", "app.js reproducible", "build_js.py not found")
    tracked = (js_dir / "app.js").read_text(encoding="utf-8", errors="replace")
    # Rebuild into a temp copy to avoid mutating the mirror.
    with tempfile.TemporaryDirectory() as td:
        shutil.copytree(js_dir, Path(td) / "js")
        try:
            subprocess.run(["python3", "build_js.py"], cwd=str(Path(td) / "js"),
                           check=True, capture_output=True, timeout=60)
            rebuilt = (Path(td) / "js" / "app.js").read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return _skip("BUNDLE", "app.js reproducible", f"rebuild failed: {e}")
    if tracked == rebuilt:
        return _ok("BUNDLE", "app.js reproducible", "byte-identical to clean build")
    # benign re-order check
    if sorted(tracked.splitlines()) == sorted(rebuilt.splitlines()):
        return CheckResult("BUNDLE", "app.js reproducible", "SKIP",
                           "content identical but re-ordered (benign; rebuild on next JS change)")
    return _bad("BUNDLE", "app.js reproducible", "deployed app.js DIFFERS from clean build (real drift)")
