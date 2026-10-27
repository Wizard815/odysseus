"""POST /api/codex/cookbook/presets/delete removes saved serve presets by
their launch cmd, so a tool that pulled them (Argos) can delete one for good
instead of having it come back on the next sync."""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routes.codex_routes as codex_routes


def _endpoint():
    router = codex_routes.setup_codex_routes()
    for route in router.routes:
        if route.path.endswith("/cookbook/presets/delete") and "POST" in route.methods:
            return route.endpoint
    raise AssertionError("delete route not found")


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "cookbook_state.json"
    path.write_text(json.dumps({"tasks": [{"id": "t"}], "presets": [
        {"name": "Qwen fast", "cmd": "llama-server -m q.gguf"},
        {"name": "Qwen long", "cmd": "llama-server -m q.gguf"},
        {"name": "Gemma", "cmd": "llama-server -m g.gguf -c 8192"},
    ]}))
    monkeypatch.setattr(codex_routes, "COOKBOOK_STATE_FILE", str(path))
    scopes = []
    monkeypatch.setattr(codex_routes, "_require_cookbook_scope", lambda request, allowed: scopes.append(allowed) or "u")
    return path, scopes


def _delete(body):
    return asyncio.run(_endpoint()(object(), body))


def _names(path):
    return [p["name"] for p in json.loads(path.read_text())["presets"]]


def test_delete_preset_by_cmd_and_name(state_file):
    path, scopes = state_file
    assert _delete({"cmd": "llama-server  -m q.gguf", "name": "qwen LONG"}) == {"ok": True, "deleted": 1}
    assert _names(path) == ["Qwen fast", "Gemma"], "the name picks one of the presets sharing a cmd"
    assert json.loads(path.read_text())["tasks"] == [{"id": "t"}], "the rest of the state is untouched"
    assert scopes == [codex_routes.COOKBOOK_LAUNCH_SCOPES], "it rewrites the state: launch scope, not read"


def test_delete_preset_renamed_elsewhere_is_still_found_by_cmd(state_file):
    path, _ = state_file
    assert _delete({"cmd": "llama-server -m g.gguf -c 8192", "name": "gemma (renamed in Argos)"})["deleted"] == 1
    assert _names(path) == ["Qwen fast", "Qwen long"]
    assert _delete({"cmd": "llama-server -m none.gguf"})["deleted"] == 0


def test_delete_preset_needs_a_cmd(state_file):
    with pytest.raises(HTTPException) as exc:
        _delete({"name": "Gemma"})
    assert exc.value.status_code == 400
