"""Argos connection suite.

Argos is the model-serving engine this instance talks to: a llama-swap fork
that loads models on demand and routes profiles. It is a first-class connection
here rather than one more entry in the Integrations list, because everything
else in Odysseus (chat, embeddings, Cookbook) is pointed at it.

The browser cannot call Argos directly -- it is a separate origin and Argos
sends no CORS headers -- so this module proxies the handful of calls the panel
needs, and keeps the base URL in the settings file like every other integration.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from fastapi import APIRouter, Form, HTTPException, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)

def setup_argos_routes() -> APIRouter:
    """Build the Argos connection-suite router (see the module docstring)."""
    return router


router = APIRouter(prefix="/api/argos", tags=["argos"])

# Argos answers in milliseconds when it is up; a long timeout here would only
# make a wrong URL feel like a hang.
_TIMEOUT_S = 6.0

# The id Argos is registered under as an MCP server. Stable on purpose: a
# reconnect updates the existing row instead of accumulating `argos-2`,
# `argos-3`, and its tools stay named mcp__argos__*.
MCP_SERVER_ID = "argos"


def _base_url() -> str:
    from src.settings import get_setting
    return str(get_setting("argos_url", "") or "").strip().rstrip("/")


def _request(method: str, url: str, body: Optional[dict] = None, timeout: float = _TIMEOUT_S) -> Any:
    """One request to Argos. Returns parsed JSON, or raises a readable error."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured URL
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(e.code, f"Argos returned {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise HTTPException(502, f"could not reach Argos at {url}: {e.reason}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _try(path: str, base: str) -> Any:
    """Fetch one endpoint, returning None instead of raising.

    A status panel must survive a partially-working engine: a 404 on one
    endpoint is information to show, not a reason to fail the whole read.
    """
    try:
        return _request("GET", f"{base}{path}")
    except HTTPException as e:
        logger.debug("argos %s: %s", path, e.detail)
        return None


@router.get("/config")
def get_config(request: Request):
    """The configured Argos base URL."""
    return {"url": _base_url()}


@router.put("/config")
def set_config(request: Request, url: str = Form("")):
    """Save the Argos base URL."""
    require_admin(request)
    from src.settings import load_settings, save_settings

    cleaned = url.strip().rstrip("/")
    if cleaned and not cleaned.startswith(("http://", "https://")):
        cleaned = "http://" + cleaned
    settings = load_settings()
    settings["argos_url"] = cleaned
    save_settings(settings)
    return {"url": cleaned}


@router.get("/status")
def status(request: Request):
    """Everything the panel shows, in one call.

    Readable by any signed-in user: it is the same information the Argos page
    shows, and the panel is how someone notices the engine is down.
    """
    base = _base_url()
    if not base:
        return {"configured": False}

    version = _try("/api/version", base)
    profiles = _try("/api/profiles", base)
    running = _try("/running", base)
    models = _try("/v1/models", base)

    reachable = version is not None or profiles is not None or models is not None
    out: dict[str, Any] = {
        "configured": True,
        "url": base,
        "reachable": reachable,
        "version": (version or {}).get("version") if isinstance(version, dict) else None,
        "commit": (version or {}).get("commit") if isinstance(version, dict) else None,
    }

    if isinstance(profiles, dict):
        out["active_profile"] = profiles.get("active")
        out["profiles"] = [
            {
                "id": p.get("id"),
                "description": p.get("description", ""),
                "pin_count": len(p.get("pins") or {}),
            }
            for p in (profiles.get("profiles") or [])
            if isinstance(p, dict)
        ]
    else:
        out["active_profile"] = None
        out["profiles"] = []

    if isinstance(models, dict):
        data = models.get("data") or []
        out["model_count"] = len(data)
        out["loaded_count"] = sum(
            1 for m in data
            if isinstance(m, dict) and (m.get("status") or {}).get("value") == "loaded"
        )
    else:
        out["model_count"] = None
        out["loaded_count"] = None

    out["running"] = [
        {"model": m.get("model"), "state": m.get("state"), "name": m.get("name")}
        for m in ((running or {}).get("running") or [])
        if isinstance(m, dict)
    ]
    out["mcp_registered"] = _mcp_registered()
    return out


def _mcp_registered() -> bool:
    """Whether Argos is currently registered as an MCP server."""
    try:
        from core.database import McpServer, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(McpServer).filter(McpServer.id == MCP_SERVER_ID).first()
            return bool(row and row.is_enabled)
        finally:
            db.close()
    except Exception:
        return False


@router.put("/profile")
async def set_profile(request: Request, name: str = Form("")):
    """Activate an Argos profile, or clear it with an empty name."""
    require_admin(request)
    base = _base_url()
    if not base:
        raise HTTPException(400, "Argos is not configured yet")
    result = _request("PUT", f"{base}/api/profiles/active", {"name": name.strip() or None})
    return {"ok": True, "result": result}


@router.post("/connect")
async def connect(request: Request, url: str = Form("")):
    """Register Argos's MCP endpoint as a server, and connect it now.

    This is the one-click half of the suite: it saves the URL, upserts the
    McpServer row for <base>/api/mcp, and connects so Argos's tools are callable
    without going through the generic Integrations form.
    """
    require_admin(request)
    from src.settings import load_settings, save_settings

    cleaned = url.strip().rstrip("/")
    if cleaned and not cleaned.startswith(("http://", "https://")):
        cleaned = "http://" + cleaned
    if not cleaned:
        cleaned = _base_url()
    if not cleaned:
        raise HTTPException(400, "An Argos URL is required")

    settings = load_settings()
    settings["argos_url"] = cleaned
    save_settings(settings)

    mcp_url = f"{cleaned}/api/mcp"
    # Probe before writing a row, so a typo is reported here rather than
    # showing up later as a server that silently never connects.
    probe = _try("/api/version", cleaned)
    if probe is None:
        raise HTTPException(502, f"could not reach Argos at {cleaned}")

    from core.database import McpServer, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(McpServer).filter(McpServer.id == MCP_SERVER_ID).first()
        if row is None:
            row = McpServer(id=MCP_SERVER_ID, name="Argos")
            db.add(row)
        row.name = "Argos"
        row.transport = "http"
        row.url = mcp_url
        row.command = None
        row.args = None
        row.env = None
        row.is_enabled = True
        db.commit()
    finally:
        db.close()

    connected = False
    error = None
    try:
        from src.tool_utils import get_mcp_manager
        mgr = get_mcp_manager()
        if mgr:
            connected = await mgr.connect_server(
                server_id=MCP_SERVER_ID, name="Argos", transport="http", url=mcp_url
            )
        else:
            error = "the MCP manager is not running"
    except Exception as e:  # noqa: BLE001 - reported to the panel, not fatal
        error = str(e)
        logger.warning("argos MCP connect failed", exc_info=True)

    return {
        "ok": True,
        "url": cleaned,
        "mcp_url": mcp_url,
        "connected": connected,
        "error": error,
    }


@router.delete("/connect")
async def disconnect(request: Request):
    """Stop the Argos MCP server and disable its row (tools stop being offered)."""
    require_admin(request)
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(McpServer).filter(McpServer.id == MCP_SERVER_ID).first()
        if row is not None:
            row.is_enabled = False
            db.commit()
    finally:
        db.close()

    try:
        from src.tool_utils import get_mcp_manager
        mgr = get_mcp_manager()
        if mgr:
            await mgr.disconnect_server(MCP_SERVER_ID)
    except Exception:
        logger.debug("argos MCP disconnect failed", exc_info=True)

    return {"ok": True}
