"""The Codex bridge exposes the gallery, scheduled tasks and skills to scoped API
tokens. Each needs its own scope, and the wrapped route sees the token's owner,
never the "api" pseudo-user."""
import asyncio

import pytest
from fastapi import APIRouter, HTTPException
from starlette.requests import Request

import routes.codex_routes as codex_routes
from routes.api_token_routes import ALLOWED_SCOPES, _normalize_scopes


def _stub_routers(calls):
    gallery, tasks, skills = APIRouter(), APIRouter(), APIRouter()

    @gallery.get("/api/gallery/library")
    async def library(request: Request, search=None, tag=None, model=None, album=None, favorites=False,
                      sort="recent", seed=None, offset=0, limit=24):
        calls.append(("gallery", request.state.current_user, search, offset, limit))
        return {"images": [{"id": "img1"}], "total": 1}

    @tasks.get("/api/tasks")
    async def list_tasks(request: Request, status=None, include_last_run=False):
        calls.append(("tasks", request.state.current_user, status, include_last_run))
        return {"tasks": [{"id": "t1", "name": "nightly"}]}

    @tasks.post("/api/tasks/{task_id}/run")
    async def run(request: Request, task_id: str, force: bool = False):
        calls.append(("run", request.state.current_user, task_id, force))
        return {"ok": True}

    @skills.get("/api/skills")
    async def list_skills(request: Request):
        calls.append(("skills", request.state.current_user))
        return {"skills": [{"name": "web_search"}], "count": 1}

    return gallery, tasks, skills


def _router(calls):
    gallery, tasks, skills = _stub_routers(calls)
    return codex_routes.setup_codex_routes(gallery_router=gallery, task_router=tasks, skills_router=skills)


def _endpoint(router, path, method):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not found")


def _request(scopes):
    request = Request({"type": "http", "method": "GET", "path": "/api/codex", "headers": [], "state": {}})
    request.state.api_token = True
    request.state.api_token_owner = "alice"
    request.state.api_token_scopes = list(scopes)
    request.state.current_user = "api"
    return request


def test_new_scopes_are_allowed_and_run_implies_read():
    assert {"gallery:read", "tasks:read", "tasks:run", "skills:read"} <= ALLOWED_SCOPES
    assert _normalize_scopes("tasks:run") == ["tasks:read", "tasks:run"]


def test_gallery_needs_its_scope_and_runs_as_the_owner():
    calls = []
    router = _router(calls)
    gallery = _endpoint(router, "/api/codex/gallery", "GET")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(gallery(_request(["documents:read"]), search=None, tag=None, offset=0, limit=48))
    assert exc.value.status_code == 403
    out = asyncio.run(gallery(_request(["gallery:read"]), search="cat", tag=None, offset="5", limit="500"))
    assert out["images"][0]["id"] == "img1"
    assert calls == [("gallery", "alice", "cat", 5, 100)], "owner passed through, limit clamped"


def test_tasks_list_and_run_need_separate_scopes():
    calls = []
    router = _router(calls)
    list_tasks = _endpoint(router, "/api/codex/tasks", "GET")
    run = _endpoint(router, "/api/codex/tasks/{task_id}/run", "POST")
    assert asyncio.run(list_tasks(_request(["tasks:read"]), status=None))["tasks"][0]["id"] == "t1"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(run(_request(["tasks:read"]), "t1"))
    assert exc.value.status_code == 403, "reading tasks does not allow running them"
    assert asyncio.run(run(_request(["tasks:run"]), "t1")) == {"ok": True}
    assert calls[-1] == ("run", "alice", "t1", False), "never forced"


def test_skills_need_their_scope():
    calls = []
    skills = _endpoint(_router(calls), "/api/codex/skills", "GET")
    with pytest.raises(HTTPException):
        asyncio.run(skills(_request(["chat"])))
    assert asyncio.run(skills(_request(["skills:read"])))["count"] == 1
    assert calls == [("skills", "alice")]


def test_request_state_is_restored_after_the_call():
    calls = []
    skills = _endpoint(_router(calls), "/api/codex/skills", "GET")
    req = _request(["skills:read"])
    asyncio.run(skills(req))
    assert req.state.current_user == "api" and req.state.api_token is True


def test_capabilities_list_the_new_areas():
    caps = _endpoint(_router([]), "/api/codex/capabilities", "GET")(_request(["gallery:read", "tasks:run"]))
    tools = caps["tools"]
    assert tools["gallery"]["read"] and tools["tasks"]["read"] and tools["tasks"]["run"]
    assert not tools["skills"]["read"] and tools["skills"]["available"]


def test_without_routers_the_endpoints_say_unavailable():
    router = codex_routes.setup_codex_routes()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_endpoint(router, "/api/codex/skills", "GET")(_request(["skills:read"])))
    assert exc.value.status_code == 503
