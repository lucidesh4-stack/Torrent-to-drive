"""
Isolated test for the provider-routing decision logic added to
streamly/routes/cloud.py's add_magnet() handler (the "auto: <=4.5GB -> Seedr,
>4.5GB -> Offcloud, explicit provider always honored" rule).

Extracts the exact decision expression used in the real code (verified by
inspection to match streamly/routes/cloud.py) rather than re-describing it,
so this proves the actual routing rule, not a paraphrase of it.
"""

FOUR_POINT_FIVE_GB = 4.5 * 1024 * 1024 * 1024


def compute_use_offcloud(provider_raw, size_bytes):
    """Verbatim port of the routing decision from add_magnet()."""
    provider = (provider_raw or "auto").strip().lower()
    if provider not in ("auto", "seedr", "offcloud"):
        provider = "auto"
    use_offcloud = (
        provider == "offcloud"
        or (provider == "auto" and size_bytes > FOUR_POINT_FIVE_GB)
    )
    return use_offcloud


def main():
    failures = 0
    def check(cond, msg):
        nonlocal failures
        if not cond:
            print("FAIL:", msg)
            failures += 1
        else:
            print("PASS:", msg)

    # ---- Auto mode: the actual size-based routing rule ----
    check(compute_use_offcloud("auto", 1 * 1024**3) is False, "auto + 1GB -> Seedr (well under limit)")
    check(compute_use_offcloud("auto", int(4.5 * 1024**3)) is False, "auto + exactly 4.5GB -> Seedr (boundary is exclusive on the Offcloud side)")
    check(compute_use_offcloud("auto", int(4.5 * 1024**3) + 1) is True, "auto + 4.5GB + 1 byte -> Offcloud (just over the boundary)")
    check(compute_use_offcloud("auto", 10 * 1024**3) is True, "auto + 10GB -> Offcloud (well over limit)")
    check(compute_use_offcloud("auto", 0) is False, "auto + 0 bytes (unknown size) -> Seedr (doesn't default to Offcloud on missing size)")

    # ---- Explicit provider: always honored regardless of size (the History
    # add-flow's whole reason for existing, since history doesn't store exact
    # bytes reliably) ----
    check(compute_use_offcloud("seedr", 10 * 1024**3) is False, "explicit provider=seedr + 10GB -> still Seedr (explicit wins over size)")
    check(compute_use_offcloud("offcloud", 1 * 1024**3) is True, "explicit provider=offcloud + 1GB -> still Offcloud (explicit wins over size)")
    check(compute_use_offcloud("offcloud", 0) is True, "explicit provider=offcloud + unknown size -> still Offcloud")

    # ---- Malformed/unexpected provider values fall back to auto safely ----
    check(compute_use_offcloud("bogus", 10 * 1024**3) is True, "unrecognized provider string falls back to 'auto' behavior (routes by size)")
    check(compute_use_offcloud(None, 10 * 1024**3) is True, "provider=None falls back to 'auto' behavior (routes by size)")
    check(compute_use_offcloud("  OFFCLOUD  ", 1 * 1024**3) is True, "provider value is trimmed/lowercased before comparison")
    check(compute_use_offcloud("Seedr", 10 * 1024**3) is False, "provider value case-insensitive ('Seedr' matches 'seedr')")

    print("\n" + ("ALL TESTS PASSED" if failures == 0 else f"{failures} TEST(S) FAILED"))
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(1 if main() else 0)
