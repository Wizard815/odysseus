"""The Argos connection suite's proxy, against a stand-in engine.

A real HTTP server on loopback rather than a mocked function, so the test
covers the actual request/parse path including status codes and error bodies.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeArgos(BaseHTTPRequestHandler):
    active = None
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeArgos.calls.append(("GET", self.path))
        if self.path == "/api/version":
            return self._send(200, {"version": "1.2.3", "commit": "abc1234", "build_date": "today"})
        if self.path == "/api/profiles":
            return self._send(200, {
                "active": FakeArgos.active,
                "profiles": [
                    {"id": "coding", "description": "long context", "pins": {"a": "a"}},
                    {"id": "fast", "description": "", "pins": {}},
                ],
            })
        if self.path == "/running":
            return self._send(200, {"running": []})
        if self.path == "/v1/models":
            return self._send(200, {"data": [
                {"id": "a", "status": {"value": "loaded"}},
                {"id": "b", "status": {"value": "unloaded"}},
            ]})
        return self._send(404, {"error": "no such path"})

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        FakeArgos.calls.append(("PUT", self.path, body))
        if self.path == "/api/profiles/active":
            FakeArgos.active = body.get("name")
            return self._send(200, {"active": FakeArgos.active})
        return self._send(404, {"error": "no such path"})


@pytest.fixture
def engine():
    FakeArgos.active = None
    FakeArgos.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeArgos)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def client(engine, monkeypatch, tmp_path):
    import routes.argos_routes as ar

    # The panel's writes are admin-gated; auth is covered elsewhere, so this
    # test is about the proxy's behaviour.
    monkeypatch.setattr(ar, "require_admin", lambda request: None)
    monkeypatch.setattr(ar, "_mcp_registered", lambda: False)

    from src import settings as st
    # Point at a scratch settings file AND drop the module cache, which would
    # otherwise serve the real settings for its TTL and make the
    # "unconfigured" case see a real URL.
    monkeypatch.setattr(st, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(st, "_settings_cache", None)

    app = FastAPI()
    app.include_router(ar.setup_argos_routes())
    return TestClient(app), ar


def test_status_reports_a_live_engine(client, engine):
    c, _ = client
    c.put("/api/argos/config", data={"url": engine})

    st = c.get("/api/argos/status").json()

    assert st["configured"] is True
    assert st["reachable"] is True
    assert st["version"] == "1.2.3"
    assert st["commit"] == "abc1234"
    assert st["model_count"] == 2
    assert st["loaded_count"] == 1
    assert [p["id"] for p in st["profiles"]] == ["coding", "fast"]
    assert st["active_profile"] is None


def test_activating_a_profile_reaches_the_engine(client, engine):
    c, _ = client
    c.put("/api/argos/config", data={"url": engine})

    assert c.put("/api/argos/profile", data={"name": "coding"}).status_code == 200
    assert FakeArgos.active == "coding"
    assert c.get("/api/argos/status").json()["active_profile"] == "coding"

    # An empty name clears it, and must go out as JSON null rather than "".
    assert c.put("/api/argos/profile", data={"name": ""}).status_code == 200
    assert FakeArgos.active is None
    sent = [c for c in FakeArgos.calls if c[0] == "PUT"]
    assert sent[-1][2] == {"name": None}


def test_a_wrong_url_is_reported_not_raised(client):
    c, _ = client
    c.put("/api/argos/config", data={"url": "http://127.0.0.1:9"})

    st = c.get("/api/argos/status").json()

    # A status panel has to survive a dead engine: it is how someone notices.
    assert st["configured"] is True
    assert st["reachable"] is False
    assert st["model_count"] is None


def test_unconfigured_is_reported_plainly(client):
    c, _ = client
    assert c.get("/api/argos/status").json() == {"configured": False}


def test_url_is_normalized_to_a_scheme(client):
    c, _ = client
    assert c.put("/api/argos/config", data={"url": "192.168.1.5:8080/"}).json()["url"] == "http://192.168.1.5:8080"


def test_a_partial_engine_still_answers(client, engine, monkeypatch):
    """One endpoint 404ing must not fail the whole panel."""
    c, ar = client
    c.put("/api/argos/config", data={"url": engine})

    real_try = ar._try

    def flaky(path, base):
        if path == "/api/profiles":
            return None
        return real_try(path, base)

    monkeypatch.setattr(ar, "_try", flaky)
    st = c.get("/api/argos/status").json()

    assert st["reachable"] is True          # version + models still answered
    assert st["profiles"] == []             # the missing one degrades quietly
    assert st["active_profile"] is None
    assert st["model_count"] == 2


def test_connect_requires_a_reachable_engine(client):
    c, _ = client
    r = c.post("/api/argos/connect", data={"url": "http://127.0.0.1:9"})
    # Refusing here is the point: a typo is reported now rather than becoming a
    # server that silently never connects.
    assert r.status_code == 502
