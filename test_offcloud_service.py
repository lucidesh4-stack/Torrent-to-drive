"""
Isolated test for OffcloudService (streamly/offcloud_service.py).

Does NOT require a real Offcloud API key or live network access: httpx's
AsyncClient.post is monkeypatched to return controlled fake responses,
covering every response shape the service code is written to handle
(success, not_available with each documented reason, generic error, 401,
non-JSON body). This proves the actual shipped error-handling logic works
correctly for the response types Offcloud's own (historical) documentation
describes, and fails safely/clearly for unexpected shapes -- which matters
given the live-confirmed uncertainty about the current, post-rebuild API.
"""
import asyncio
import sys
sys.path.insert(0, "streamly/..")

from streamly.offcloud_service import OffcloudService, OffcloudError


class FakeResponse:
    def __init__(self, status_code, json_data=None, raise_json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self._raise_json_error = raise_json_error

    def json(self):
        if self._raise_json_error:
            raise ValueError("not valid json")
        return self._json_data


class FakeAsyncClient:
    """Stand-in for httpx.AsyncClient; queue up canned responses per call."""
    _next_response = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None):
        return FakeAsyncClient._next_response

    async def get(self, url):
        return FakeAsyncClient._next_response


async def main():
    failures = 0
    def check(cond, msg):
        nonlocal failures
        if not cond:
            print("FAIL:", msg)
            failures += 1
        else:
            print("PASS:", msg)

    import streamly.offcloud_service as mod
    mod.httpx.AsyncClient = FakeAsyncClient

    # ---- Test 1: unconfigured service (no API key) raises immediately, no network call ----
    svc = OffcloudService(api_key="")
    check(not svc.configured, "empty API key -> configured is False")
    try:
        await svc.add_magnet("magnet:?xt=urn:btih:test")
        check(False, "add_magnet with no API key should raise OffcloudError")
    except OffcloudError as e:
        check("not configured" in str(e).lower(), "add_magnet with no API key raises a clear config error")

    svc = OffcloudService(api_key="fake-test-key-123")
    check(svc.configured, "non-empty API key -> configured is True")

    # ---- Test 2: successful add_magnet ----
    FakeAsyncClient._next_response = FakeResponse(200, {
        "requestId": "req-1", "fileName": "Movie.mkv", "status": "created"
    })
    result = await svc.add_magnet("magnet:?xt=urn:btih:test")
    check(result["requestId"] == "req-1", "successful add_magnet returns requestId")
    check(result["fileName"] == "Movie.mkv", "successful add_magnet returns fileName")

    # ---- Test 3: each documented 'not_available' reason produces a readable error ----
    for reason in ["premium", "links", "proxy", "cloud", "video"]:
        FakeAsyncClient._next_response = FakeResponse(200, {"not_available": reason})
        try:
            await svc.add_magnet("magnet:?xt=urn:btih:test")
            check(False, f"not_available={reason} should raise")
        except OffcloudError as e:
            check(len(str(e)) > 10 and "not_available" not in str(e).lower() or reason in str(e).lower(),
                  f"not_available={reason} raises a readable (not raw-code) error message: {e}")

    # ---- Test 4: an unrecognized not_available reason still raises cleanly (forward-compat
    # with the API-rebuild uncertainty -- don't crash on an unknown reason code) ----
    FakeAsyncClient._next_response = FakeResponse(200, {"not_available": "some_new_reason_from_rebuilt_api"})
    try:
        await svc.add_magnet("magnet:?xt=urn:btih:test")
        check(False, "unknown not_available reason should still raise")
    except OffcloudError as e:
        check("some_new_reason_from_rebuilt_api" in str(e), "unknown not_available reason surfaces the raw code rather than crashing")

    # ---- Test 5: generic {"error": "..."} response ----
    FakeAsyncClient._next_response = FakeResponse(200, {"error": "Invalid magnet link"})
    try:
        await svc.add_magnet("magnet:?xt=urn:btih:test")
        check(False, "error response should raise")
    except OffcloudError as e:
        check("Invalid magnet link" in str(e), "generic error response message is surfaced")

    # ---- Test 6: 401 (bad/expired API key) is a clear, specific error ----
    FakeAsyncClient._next_response = FakeResponse(401, {})
    try:
        await svc.add_magnet("magnet:?xt=urn:btih:test")
        check(False, "401 should raise")
    except OffcloudError as e:
        check("api key" in str(e).lower(), "401 response raises a clear 'bad API key' error, not a generic one")

    # ---- Test 7: non-JSON response body (e.g. an HTML error page, exactly what
    # was observed live from stale/moved endpoints during development) doesn't
    # crash with an unhandled exception -- raises OffcloudError instead ----
    FakeAsyncClient._next_response = FakeResponse(200, raise_json_error=True)
    try:
        await svc.add_magnet("magnet:?xt=urn:btih:test")
        check(False, "non-JSON response should raise OffcloudError, not crash")
    except OffcloudError as e:
        check("non-json" in str(e).lower(), "non-JSON response raises a clear, specific error")
    except Exception as e:
        check(False, f"non-JSON response raised the WRONG exception type: {type(e).__name__}: {e}")

    # ---- Test 8: missing requestId in an otherwise-200/JSON response (e.g. if
    # the rebuilt API's response shape has changed) is caught, not silently
    # treated as success ----
    FakeAsyncClient._next_response = FakeResponse(200, {"unexpected": "shape"})
    try:
        await svc.add_magnet("magnet:?xt=urn:btih:test")
        check(False, "missing requestId should raise")
    except OffcloudError as e:
        check("unexpected" in str(e).lower(), "unexpected response shape (no requestId) is caught with a clear error")

    # ---- Test 9: get_status happy path ----
    FakeAsyncClient._next_response = FakeResponse(200, {"status": "downloaded", "url": "https://example/file.mkv"})
    status = await svc.get_status("req-1")
    check(status["status"] == "downloaded", "get_status returns the status field")

    # ---- Test 10: get_download_url extracts the url when present ----
    FakeAsyncClient._next_response = FakeResponse(200, {"status": "downloaded", "url": "https://example/file.mkv"})
    url = await svc.get_download_url("req-1")
    check(url == "https://example/file.mkv", "get_download_url extracts the url field")

    # ---- Test 11: get_download_url returns None (not a crash) when not yet ready ----
    FakeAsyncClient._next_response = FakeResponse(200, {"status": "created"})
    url = await svc.get_download_url("req-1")
    check(url is None, "get_download_url returns None when the file isn't ready yet (no url field)")

    # ---- Test 12: get_history happy path ----
    FakeAsyncClient._next_response = FakeResponse(200, [
        {"requestId": "req-1", "fileName": "File.mkv", "status": "downloaded", "size": 1000, "created": "2026-07-06T12:00:00Z"}
    ])
    history = await svc.get_history()
    check(isinstance(history, list) and len(history) == 1, "get_history returns a list with correct elements")
    check(history[0]["requestId"] == "req-1", "get_history contains correct requestId")

    print("\n" + ("ALL TESTS PASSED" if failures == 0 else f"{failures} TEST(S) FAILED"))
    return failures


if __name__ == "__main__":
    failures = asyncio.run(main())
    exit(1 if failures else 0)
