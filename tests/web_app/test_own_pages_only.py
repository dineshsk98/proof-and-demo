# Offline tests for the app's Host and Origin check: only the app's own pages at 127.0.0.1:8000 or localhost:8000
# may call it, so another website open in the same browser can't start a paid job.
# The app is called directly as an ASGI app (no server, no extra packages). It reads a copy of tests/sample_workspace.
# Run: python tests/web_app/test_own_pages_only.py
import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

TMP = tempfile.mkdtemp(prefix="own-pages-")
WORKSPACE = os.path.join(TMP, "workspace")
shutil.copytree(os.path.join(REPO, "tests", "sample_workspace"), WORKSPACE)
os.environ["WORKSPACE_DIR"] = WORKSPACE

from proof_and_demo import app  # noqa: E402

SCRIPT_NAME = "2026-11-03-120000.json"  # the sample workspace's script


def request(method, path, host="127.0.0.1:8000", origin=None, query=""):
    # Sends one request through the whole app, middleware included. Returns (status, JSON body or None).
    headers = [(b"host", host.encode())] if host is not None else []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}
    sent = []
    received = []

    async def receive():
        # The (empty) body once; after that the client just stays connected, as a browser does while a file downloads.
        if not received:
            received.append(True)
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    asyncio.run(app.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def speak(**kwargs):
    # The paid narration job a foreign page would try to start. stand_in keeps it free if it ever got through.
    return request("POST", "/api/script/speak", query=f"file={SCRIPT_NAME}&stand_in=true", **kwargs)


def test_allowed():
    for host in ("127.0.0.1:8000", "localhost:8000"):
        status, body = request("GET", "/api/storylines", host=host)
        assert status == 200 and isinstance(body, list), (host, status, body)
    print("ok  allowed: a GET with Host 127.0.0.1:8000 or localhost:8000 and no Origin is served")
    for origin in ("http://127.0.0.1:8000", "http://localhost:8000"):
        status, body = request("POST", "/api/script/speak", origin=origin, query="file=missing.json&stand_in=true")
        assert status == 400 and "No such script" in body["error"], (origin, status, body)  # past the check, refused by the endpoint
    print("ok  allowed: a POST from the app's own page reaches the endpoint")
    status, _ = request("GET", "/")
    assert status == 200, status
    print("ok  allowed: the page itself loads")


def test_foreign_origin():
    for origin in ("http://evil.example", "https://127.0.0.1:8000", "http://127.0.0.1:8001", "http://localhost", "null"):
        status, body = speak(origin=origin)
        assert status == 403 and "own pages" in body["error"], (origin, status, body)
        status, _ = request("GET", "/api/storylines", origin=origin)
        assert status == 403, (origin, status)
    assert app.jobs["speak"]["state"] == "idle", app.jobs["speak"]  # no job was started
    print("ok  foreign Origin: 403 for POST and GET, and no job starts")


def test_foreign_host():
    for host in ("evil.example", "evil.example:8000", "127.0.0.1", "127.0.0.1:8001", "localhost", "0.0.0.0:8000", None):
        status, body = speak(host=host)
        assert status == 403 and "own pages" in body["error"], (host, status, body)
        status, _ = request("GET", "/api/storylines", host=host)
        assert status == 403, (host, status)
    status, _ = speak(host="evil.example:8000", origin="http://127.0.0.1:8000")  # a matching Origin doesn't excuse the Host
    assert status == 403, status
    assert app.jobs["speak"]["state"] == "idle", app.jobs["speak"]
    print("ok  foreign or missing Host: 403, and no job starts")
    # DNS rebinding: a foreign name that resolves to 127.0.0.1 must not read workspace files either.
    status, _ = request("GET", "/api/file", query="path=frames/s01.png")
    assert status == 200, status
    status, _ = request("GET", "/api/file", host="rebind.example:8000", query="path=frames/s01.png")
    assert status == 403, status
    print("ok  foreign Host: /api/file refuses a file it serves to the app's own pages")


if __name__ == "__main__":
    try:
        test_allowed()
        test_foreign_origin()
        test_foreign_host()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("All tests passed.")
