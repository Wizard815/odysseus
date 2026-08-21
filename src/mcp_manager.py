"""
mcp_manager.py

Manages connections to MCP (Model Context Protocol) tool servers.
Each server exposes tools that are made available to the agent loop.
"""

import json
import logging
import os
import re
import asyncio
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Set, Tuple
from src.database import McpServer, SessionLocal

from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)

def _format_mcp_connection_error(name: str, command: str = "", args: Optional[List[str]] = None, error: Exception = None) -> str:
    """Return a user-actionable MCP connection error message."""
    args = args or []
    raw_error = str(error) if error else "Unknown error"
    command_line = " ".join([command or "", *args]).strip()
    lower_command = command_line.lower()

    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\n\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\n\n"
            "npx -y @playwright/mcp@latest --version\n\n"
            "Then restart Odysseus and reconnect the Browser MCP server."
        )

    return raw_error


# Caps for rendering untrusted MCP tool schemas into the agent prompt (issue #2660).
# MCP servers are third-party/user-added, so field names and parameter counts are
# untrusted input — bound them so an odd or hostile schema cannot distort the prompt.
_MCP_PARAM_MAX = 12   # max params rendered per tool
_MCP_TOKEN_MAX = 40   # max chars per rendered name / type token
_MCP_HINT_MAX = 300   # total-length backstop for the whole hint


def _sanitize_schema_token(value: Any, limit: int = _MCP_TOKEN_MAX) -> str:
    """Make an untrusted JSON-Schema token safe to splice into the prompt.

    Replaces control chars / newlines with a space, collapses whitespace, and
    length-caps the result, so a weird field name or type cannot inject newlines
    or run on. Normal short identifiers pass through unchanged.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _format_mcp_params(input_schema: Any) -> str:
    """Render an MCP tool's JSON-Schema inputs as a compact prompt hint.

    Without this the agent only sees a tool's name + description and has to
    guess its arguments (issue #2509). Produces e.g.
    ` Args (JSON): {"path": string (required), "limit": integer}` — names,
    coarse types, and required-ness, kept short so it stays prompt-friendly.
    Returns "" when there are no parameters.

    MCP servers are third-party, so names/types are sanitized and the parameter
    count + total length are capped (issue #2660); normal schemas are unaffected.
    """
    if not isinstance(input_schema, dict):
        return ""
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(input_schema.get("required") or [])
    parts = []
    for pname, pinfo in list(props.items())[:_MCP_PARAM_MAX]:
        pinfo = pinfo if isinstance(pinfo, dict) else {}
        ptype = pinfo.get("type") or "any"
        if isinstance(ptype, list):
            ptype = "|".join(str(x) for x in ptype)
        tag = f'"{_sanitize_schema_token(pname)}": {_sanitize_schema_token(ptype)}'
        if pname in required:
            tag += " (required)"
        parts.append(tag)
    extra = len(props) - len(parts)
    if extra > 0:
        parts.append(f"…+{extra} more")
    hint = " Args (JSON): {" + ", ".join(parts) + "}"
    if len(hint) > _MCP_HINT_MAX:
        hint = hint[:_MCP_HINT_MAX - 1].rstrip() + "…"
    return hint


def _resolve_input_schema(tool: Any, server_label: str = "") -> Dict:
    """Get a tool's input schema, warning (not silently swallowing) when it's empty.

    mcp>=2.0 renamed Tool.inputSchema -> Tool.input_schema; the old
    hasattr(tool, "inputSchema") check silently returned False for every
    tool on every server after that upgrade, and every schema fell back to
    {} for a long time before anyone noticed — a model asked to call a tool
    with a required param it can't see just guesses blind or gives up. An
    empty schema is a strong signal something is wrong (a real rename, an
    unusual server, or a genuinely parameter-less tool), so log it instead
    of failing quietly the same way again.
    """
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    # Only warn when the schema is missing entirely (both attr names absent) —
    # a real zero-arg tool legitimately returns {"type": "object",
    # "properties": {}}, which must NOT trigger this warning on every such
    # tool on every connect.
    if not schema:
        logger.warning(
            "MCP tool '%s'%s resolved to an empty input schema — the model "
            "will not see any parameter names for it. If this tool actually "
            "takes parameters, the mcp package's Tool schema field may have "
            "been renamed again.",
            getattr(tool, "name", "?"),
            f" (server: {server_label})" if server_label else "",
        )
    return schema


# Tool-name prefixes that denote a read-only/inspection operation. Used to
# classify MCP tools for plan mode when the server provides no readOnlyHint.
# These are PREFIXES, not whole words (matched via str.startswith below), so a
# stem like "summar" intentionally covers "summarise"/"summarize"/"summary".
_MCP_READONLY_VERBS = (
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summar",
)


def mcp_tool_is_readonly(tool: Dict) -> bool:
    """Classify an MCP tool as safe (non-mutating) for plan mode.

    Prefer the server's own annotations (readOnlyHint / destructiveHint). When
    absent, fall back to a tool-name verb heuristic, and FAIL CLOSED (treat as
    write) for anything that doesn't clearly read — plan mode must not run a
    write tool just because its intent is ambiguous.
    """
    ann = tool.get("annotations")
    # annotations may be a dict or a pydantic model
    read_hint = None
    destructive = None
    if ann is not None:
        if isinstance(ann, dict):
            read_hint = ann.get("readOnlyHint")
            destructive = ann.get("destructiveHint")
        else:
            # Same rename risk as Tool.inputSchema -> input_schema on the
            # pydantic model's Python attribute name (the dict branch above
            # is unaffected -- that's deserialized JSON, which stays
            # camelCase per the MCP wire spec regardless of SDK attr naming).
            read_hint = getattr(ann, "read_only_hint", None)
            if read_hint is None:
                read_hint = getattr(ann, "readOnlyHint", None)
            destructive = getattr(ann, "destructive_hint", None)
            if destructive is None:
                destructive = getattr(ann, "destructiveHint", None)
    if read_hint is True:
        return True
    if read_hint is False or destructive is True:
        return False
    # No usable hint — heuristic on the tool name's leading verb.
    name = (tool.get("name") or "").lower()
    return name.startswith(_MCP_READONLY_VERBS)


class McpManager:
    """Manages MCP server connections and tool routing."""

    # No prior bound existed on an individual tools/call round trip -- a
    # hung/slow-to-respond server stalled the whole agent turn indefinitely.
    # Generous on purpose (some tools legitimately do slow work server-side,
    # e.g. Cookbook SSH probes), just needs to be finite.
    CALL_TOOL_TIMEOUT_S = 120

    def __init__(self):
        # server_id -> connection state
        self._connections: Dict[str, Dict[str, Any]] = {}
        # server_id -> list of tool schemas
        self._tools: Dict[str, List[Dict]] = {}
        # server_id -> MCP ClientSession
        self._sessions: Dict[str, Any] = {}
        # server_id -> the task that owns that connection's AsyncExitStack for
        # its entire lifetime (open through disconnect). anyio's cancel scopes
        # (used internally by mcp's transport/session context managers) must
        # be entered AND exited by the same asyncio task; previously the
        # stack was opened in whatever task called connect_server() -- which
        # normally returns almost immediately -- and closed later by
        # disconnect_server() from an unrelated caller task, which always
        # raised "Attempted to exit cancel scope in a different task than it
        # was entered in" (caught, logged, but a real leaked-connection risk
        # on a live reconnect). See _start_owned_connect/_run_owned_connection.
        self._connect_tasks: Dict[str, Any] = {}
        # server_id -> set once that server's owner task has finished
        # connecting (success or failure) -- lets connect_server() return
        # promptly without waiting for the owner task's full lifetime.
        self._ready_events: Dict[str, "asyncio.Event"] = {}
        # server_id -> set by disconnect_server() to tell the owner task to
        # exit its AsyncExitStack itself, from inside its own task.
        self._close_events: Dict[str, "asyncio.Event"] = {}
        # Tracking updates to tools/connections for RAG indexing / prompt cache
        self._generation = 0

    def _warn_tool_name_collisions(self, server_id: str, new_name: str, tools: List[Dict]) -> None:
        """Log when a newly-connected server's bare tool names collide with an
        already-connected server's.

        We can't control what a third-party MCP server names its tools, so
        this can't be prevented — but it's exactly the failure mode that
        caused a real incident: an agent silently called BoardNotes'
        `search`/`delete_item` when it meant Kanka's `find_entities`/
        `delete_entities`, because both servers expose generic verb-shaped
        tool names and nothing surfaced the overlap at connect time. Logging
        it here at least makes the collision visible in server logs the
        moment it's introduced, instead of only being discovered mid-incident
        via a model transcript days later.
        """
        new_bare_names = {t.get("name") for t in tools if t.get("name")}
        for other_id, other_tools in self._tools.items():
            if other_id == server_id:
                continue
            other_name = (self._connections.get(other_id, {}) or {}).get("name", other_id)
            other_bare_names = {t.get("name") for t in other_tools if t.get("name")}
            collisions = sorted(new_bare_names & other_bare_names)
            if collisions:
                logger.warning(
                    "MCP tool name collision: server '%s' and server '%s' both "
                    "expose tool name(s) %s. A model choosing by tool name alone "
                    "(rather than the full mcp__<server_id>__<tool_name>) can call "
                    "the wrong server's tool. Consider disabling unused tools on "
                    "one server, or keeping only one of them connected at a time.",
                    new_name, other_name, collisions,
                )

    async def connect_server(
        self,
        server_id: str,
        name: str,
        transport: str,
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Connect to an MCP server via stdio, SSE, or Streamable HTTP transport."""
        try:
            if transport == "stdio":
                res = await self._start_owned_connect(
                    server_id, name,
                    lambda stack: self._connect_stdio_body(stack, server_id, name, command, args or [], env or {}),
                )
            elif transport == "sse":
                res = await self._start_owned_connect(
                    server_id, name,
                    lambda stack: self._connect_sse_body(stack, server_id, name, url, headers),
                )
            elif transport == "http":
                res = await self._start_http_connect(server_id, name, url, headers=headers)
            else:
                logger.error(f"Unknown MCP transport: {transport}")
                res = False
            if res:
                self._generation += 1
            return res
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}", exc_info=True)
            error_message = _format_mcp_connection_error(name, command or "", args or [], e)
            self._connections[server_id] = {"status": "error", "error": error_message, "name": name}
            self._generation += 1
            return False

    async def _start_owned_connect(self, server_id: str, name: str, connect_body, wait: float = 25.0) -> bool:
        """Spawn the task that will own this connection's AsyncExitStack for
        its entire lifetime, and wait up to `wait` seconds for connect_body to
        finish (success or failure) -- not for the task itself to finish,
        since on success the task keeps running (parked on a close signal)
        until disconnect_server() is called. See _run_owned_connection for why.
        """
        ready = self._ready_events.setdefault(server_id, asyncio.Event())
        ready.clear()
        close_event = self._close_events.setdefault(server_id, asyncio.Event())
        close_event.clear()
        task = asyncio.create_task(self._run_owned_connection(server_id, connect_body, ready, close_event))
        self._connect_tasks[server_id] = task
        try:
            await asyncio.wait_for(ready.wait(), timeout=wait)
        except asyncio.TimeoutError:
            logger.error(f"MCP connect timed out after {wait}s: {name} ({server_id})")
            self._connections[server_id] = {"status": "error", "error": f"Connect timed out after {wait}s", "name": name}
            task.cancel()
            return False
        except asyncio.CancelledError:
            # Our own caller was cancelled (e.g. _connect_with_timeout's outer
            # wait_for) -- don't leave the owner task running unbounded in the
            # background with nothing left to ever signal it closed.
            task.cancel()
            raise
        return self._connections.get(server_id, {}).get("status") == "connected"

    async def _run_owned_connection(self, server_id: str, connect_body, ready: "asyncio.Event", close_event: "asyncio.Event") -> None:
        """Run connect_body(stack) and then, on success, block until
        disconnect_server() signals close -- all inside this ONE task.

        mcp's transport/session context managers use anyio cancel scopes,
        which must be entered AND exited by the same asyncio task. Previously
        each transport opened its AsyncExitStack inside whatever (short-lived)
        task called connect_server(), and disconnect_server() later closed it
        from an unrelated caller task -- which raised "Attempted to exit
        cancel scope in a different task than it was entered in" on every
        single disconnect (caught, logged, harmless at process shutdown, but
        a real leaked-connection risk on a live reconnect). Running the whole
        open/fail/wait/close lifecycle inside one unbroken `async with
        AsyncExitStack()` guarantees the exit always happens in the same task
        that did the entering, on every path (normal close, connect failure,
        or this task getting cancelled while parked on close_event.wait()).

        connect_body must not raise -- it should catch its own failures and
        record an error status on self._connections[server_id], same as
        before. If it does raise anyway, that's treated as a failed connect,
        not swallowed silently.
        """
        try:
            async with AsyncExitStack() as stack:
                try:
                    await connect_body(stack)
                finally:
                    ready.set()
                if self._connections.get(server_id, {}).get("status") == "connected":
                    await close_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"MCP connection owner task crashed for {server_id}", exc_info=True)
            self._connections[server_id] = {"status": "error", "error": "Connection owner task crashed", "name": server_id}
            ready.set()

    async def _connect_stdio_body(self, stack, server_id: str, name: str, command: str, args: List[str], env: Dict[str, str]) -> None:
        """Enter the stdio transport/session on `stack` and register on
        success. Must not close `stack` -- the owning task's `async with
        AsyncExitStack()` (see _run_owned_connection) does that, in the same
        task that opened it. On failure, record the error status and return
        normally rather than raising."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env={**os.environ, **env} if env else None,
            )

            transport = await stack.enter_async_context(stdio_client(server_params))
            read_stream, write_stream = transport
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

            await session.initialize()
            tools_result = await session.list_tools()

            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": _resolve_input_schema(tool, name),
                    # MCP tool annotations (readOnlyHint / destructiveHint) drive
                    # plan-mode read-only gating. Absent on many servers, so we
                    # fall back to a name heuristic in mcp_tool_is_readonly().
                    "annotations": getattr(tool, "annotations", None),
                })

            # Extract identity hints from env vars (e.g. email address, API name)
            # so tool descriptions can distinguish between multiple instances of
            # the same MCP server (e.g. two email accounts).
            identity_hints = []
            for k, v in (env or {}).items():
                k_lower = k.lower()
                if any(x in k_lower for x in ["email_address", "account", "user", "username"]):
                    identity_hints.append(v)
            identity = ", ".join(identity_hints) if identity_hints else ""

            self._sessions[server_id] = session
            self._tools[server_id] = tools
            self._warn_tool_name_collisions(server_id, name, tools)
            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "stdio",
                "tool_count": len(tools),
                "identity": identity,
            }
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via stdio")

        except ImportError as e:
            # A bare "not installed" message here is misleading when the
            # package IS installed but a specific name inside it doesn't
            # exist (e.g. a renamed export after an mcp version bump) —
            # include the real error so that case isn't mistaken for the
            # package missing entirely.
            logger.warning("MCP import failed (%s). If 'mcp' is installed, this is likely an API mismatch, not a missing package.", e)
            self._connections[server_id] = {
                "status": "error",
                "error": f"mcp import failed: {e}",
                "name": name,
            }
        except Exception as e:
            # Previously this propagated out to connect_server()'s own
            # try/except for the nicely-formatted message; now that this body
            # runs inside the owner task instead, format it here directly so
            # the error text doesn't regress to a bare str(e).
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}", exc_info=True)
            self._connections[server_id] = {
                "status": "error",
                "error": _format_mcp_connection_error(name, command or "", args or [], e),
                "name": name,
            }

    async def _connect_sse_body(self, stack, server_id: str, name: str, url: str, headers: Optional[Dict[str, str]] = None) -> None:
        """Enter the SSE transport/session on `stack` and register on
        success. See _connect_stdio_body for why `stack` isn't closed here."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            transport = await stack.enter_async_context(sse_client(url, headers=headers or {}))
            read_stream, write_stream = transport
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))

            await session.initialize()
            tools_result = await session.list_tools()

            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": _resolve_input_schema(tool, name),
                    # MCP tool annotations (readOnlyHint / destructiveHint) drive
                    # plan-mode read-only gating. Absent on many servers, so we
                    # fall back to a name heuristic in mcp_tool_is_readonly().
                    "annotations": getattr(tool, 'annotations', None),
                })

            self._sessions[server_id] = session
            self._tools[server_id] = tools
            self._warn_tool_name_collisions(server_id, name, tools)
            self._connections[server_id] = {
                "status": "connected",
                "name": name,
                "transport": "sse",
                "tool_count": len(tools),
            }
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via SSE")

        except ImportError as e:
            logger.warning("MCP import failed (%s). If 'mcp' is installed, this is likely an API mismatch, not a missing package.", e)
            self._connections[server_id] = {"status": "error", "error": f"mcp import failed: {e}", "name": name}
        except Exception as e:
            logger.error(f"Failed to connect MCP server {name} ({server_id}): {e}", exc_info=True)
            self._connections[server_id] = {
                "status": "error",
                "error": _format_mcp_connection_error(name, "", [], e),
                "name": name,
            }

    async def _start_http_connect(self, server_id: str, name: str, url: str, wait: float = 8.0, headers: Optional[Dict[str, str]] = None) -> bool:
        """Begin a Streamable HTTP connect in the background. Returns within
        `wait` seconds: True if it connected (cached-token path), otherwise the
        flow is awaiting browser authorization and status becomes 'needs_auth'."""
        self._connections[server_id] = {"status": "connecting", "name": name, "transport": "http"}
        ready = self._ready_events.setdefault(server_id, asyncio.Event())
        ready.clear()
        close_event = self._close_events.setdefault(server_id, asyncio.Event())
        close_event.clear()
        task = asyncio.create_task(self._run_owned_connection(
            server_id,
            lambda stack: self._connect_http_body(stack, server_id, name, url, headers),
            ready, close_event,
        ))
        self._connect_tasks[server_id] = task
        try:
            await asyncio.wait_for(ready.wait(), timeout=wait)
        except asyncio.TimeoutError:
            # Still running → awaiting OAuth authorization (only reached when
            # no headers were supplied and the server requires browser-based
            # auth). Leave the owner task running in the background; it will
            # set self._connections[server_id] itself once auth completes.
            from src.mcp_oauth import pop_auth_url
            cur = self._connections.get(server_id, {})
            if cur.get("status") != "needs_auth":
                self._connections[server_id] = {
                    "status": "needs_auth", "name": name, "transport": "http",
                    "auth_url": pop_auth_url(server_id),
                }
            return False
        except asyncio.CancelledError:
            task.cancel()
            raise
        return self._connections.get(server_id, {}).get("status") == "connected"

    async def _connect_http_body(self, stack, server_id: str, name: str, url: str, headers: Optional[Dict[str, str]] = None) -> None:
        """Enter the Streamable HTTP transport/session on `stack` and
        register on success. See _connect_stdio_body for why `stack` isn't
        closed here.

        When headers are provided (e.g. a Bearer token) OAuth is skipped and
        the headers are forwarded directly. Without headers the full OAuth flow
        runs as before.
        """
        try:
            from mcp import ClientSession
            # mcp>=2.0 renamed this export from streamablehttp_client to
            # streamable_http_client (extra underscore) — the old name no
            # longer exists, which surfaced as a bare "MCP package not
            # installed" (the ImportError handler couldn't tell a missing
            # package from a renamed symbol in an installed one).
            from mcp.client.streamable_http import streamable_http_client
            # mcp>=2.0 also dropped streamable_http_client's own headers=/auth=
            # kwargs — it now only accepts a pre-configured http_client
            # (httpx2.AsyncClient), which is where headers/auth are set
            # instead. terminate_on_close defaults to True, so the transport
            # itself still owns closing this client; no separate stack entry
            # needed for it.
            import httpx2

            if headers:
                _http_client = httpx2.AsyncClient(headers=headers)
                transport = await stack.enter_async_context(
                    streamable_http_client(url, http_client=_http_client)
                )
            else:
                from src.mcp_oauth import build_provider, clear_auth_url

                def _on_redirect(auth_url):
                    self._connections[server_id] = {
                        "status": "needs_auth", "name": name, "transport": "http",
                        "auth_url": auth_url,
                    }

                provider = build_provider(server_id, url, on_redirect=_on_redirect)
                _http_client = httpx2.AsyncClient(auth=provider)
                transport = await stack.enter_async_context(
                    streamable_http_client(url, http_client=_http_client)
                )
            # mcp>=2.0's streamable_http_client yields (read_stream, write_stream)
            # only — the session-id getter that used to be a third tuple
            # element is gone; session id is tracked internally on the
            # transport now.
            read_stream, write_stream = transport
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            tools_result = await session.list_tools()
            tools = []
            for tool in tools_result.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": _resolve_input_schema(tool, name),
                })

            self._sessions[server_id] = session
            self._tools[server_id] = tools
            self._warn_tool_name_collisions(server_id, name, tools)
            self._connections[server_id] = {
                "status": "connected", "name": name, "transport": "http",
                "tool_count": len(tools),
            }
            if not headers:
                clear_auth_url(server_id)
            # Tools changed (this can complete after connect_server already
            # returned, via the background OAuth flow), so bump the generation
            # to invalidate the tool-prompt cache.
            self._generation += 1
            logger.info(f"MCP server connected: {name} ({server_id}) - {len(tools)} tools via http")
        except ImportError as e:
            logger.warning("MCP import failed (%s). If 'mcp' is installed, this is likely an API mismatch, not a missing package.", e)
            self._connections[server_id] = {"status": "error", "error": f"mcp import failed: {e}", "name": name}
        except BaseExceptionGroup as e:
            # anyio's TaskGroup wraps a failure during the streamable_http_client
            # async-with setup (e.g. an OAuth registration 401 mid-connect) together
            # with a GeneratorExit raised while unwinding, and GeneratorExit is a
            # bare BaseException — so the resulting group is a BaseExceptionGroup,
            # which a plain `except Exception` does not catch. Observed in
            # production: this escaped all the way past connect_server() and
            # call_tool()'s callers with nothing left to catch it, killing the
            # in-flight SSE chat stream instead of just failing this one connect.
            logger.error(f"Failed to connect HTTP MCP server {name} ({server_id}) (exception group): {e.exceptions}", exc_info=True)
            self._connections[server_id] = {"status": "error", "error": str(e.exceptions[0]) if e.exceptions else str(e), "name": name}
        except Exception as e:
            logger.error(f"Failed to connect HTTP MCP server {name} ({server_id}): {e}", exc_info=True)
            self._connections[server_id] = {"status": "error", "error": str(e), "name": name}

    async def disconnect_server(self, server_id: str):
        """Disconnect from an MCP server."""
        try:
            from src.mcp_oauth import clear_auth_url
            clear_auth_url(server_id)
        except Exception:
            pass

        task = self._connect_tasks.pop(server_id, None)
        close_event = self._close_events.pop(server_id, None)
        self._ready_events.pop(server_id, None)

        if task is not None and not task.done():
            if close_event is not None:
                # Signal the owning task to exit its own AsyncExitStack --
                # from inside that same task -- instead of calling
                # stack.aclose() here from this (unrelated) caller task,
                # which is what used to raise "Attempted to exit cancel
                # scope in a different task than it was entered in" on
                # every single disconnect. See _run_owned_connection.
                close_event.set()
                try:
                    await asyncio.wait_for(task, timeout=10)
                except asyncio.TimeoutError:
                    logger.warning(f"MCP server {server_id} did not close within 10s; cancelling")
                    task.cancel()
                except Exception as e:
                    logger.warning(f"Error closing MCP server {server_id}: {e}", exc_info=True)
            else:
                # No close_event means this was never a fully-connected owned
                # task (e.g. still mid-OAuth) -- cancelling is the only signal
                # it understands.
                task.cancel()

        self._sessions.pop(server_id, None)
        self._tools.pop(server_id, None)
        self._connections.pop(server_id, None)
        self._generation += 1
        logger.info(f"MCP server disconnected: {server_id}")

    async def disconnect_all(self):
        """Disconnect from all MCP servers."""
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.disconnect_server(sid)


    async def connect_all_enabled(self):
        db = SessionLocal()
        try:
            servers = db.query(McpServer).filter(McpServer.is_enabled == True).all()

            tasks = [
                asyncio.create_task(self._connect_with_timeout(srv))
                for srv in servers
            ]

            await asyncio.gather(*tasks)
        finally:
            db.close()


    async def _connect_with_timeout(self, srv):
        args = json.loads(srv.args) if srv.args else []
        env = json.loads(srv.env) if srv.env else {}
        srv_headers = getattr(srv, "headers", None)
        headers = json.loads(srv_headers) if srv_headers else {}

        try:
            await asyncio.wait_for(
                self.connect_server(
                    server_id=srv.id,
                    name=srv.name,
                    transport=srv.transport,
                    command=srv.command,
                    args=args,
                    env=env,
                    url=srv.url,
                    headers=headers or None,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out connecting to %s", srv.name)
            self._connections[srv.id] = {
                "status": "timeout",
                "error": f"Timed out after 20 seconds",
                "name": srv.name,
            }

    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:
        """Call an MCP tool by its qualified name (mcp__{server_id}__{tool_name}).

        Returns a result dict compatible with agent_tools format.
        """
        parts = qualified_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}

        server_id = parts[1]
        tool_name = parts[2]

        session = self._sessions.get(server_id)
        if not session:
            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}

        arg_error = self._validate_tool_args(server_id, tool_name, arguments)
        if arg_error:
            # Fail here rather than at the server: the round trip is wasted
            # either way, but a schema-derived message names the exact
            # argument and the valid alternatives, where a server-side error
            # is usually a generic traceback the model can't act on.
            logger.warning(f"MCP tool call rejected before dispatch: {qualified_name}: {arg_error}")
            return {"error": arg_error, "exit_code": 1}

        try:
            result = await asyncio.wait_for(
                self._do_call(session, tool_name, arguments), timeout=self.CALL_TOOL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            # _do_call had no bound at all before this -- a hung/slow-to-respond
            # MCP server stalled the entire agent round indefinitely. Bounding
            # it here surfaces a clean error the model can react to instead of
            # a wedged turn the user has to notice and stop manually.
            logger.error(f"MCP tool call timed out after {self.CALL_TOOL_TIMEOUT_S}s: {qualified_name}")
            return {"error": f"MCP tool '{qualified_name}' timed out after {self.CALL_TOOL_TIMEOUT_S}s", "exit_code": 1}
        except BaseExceptionGroup as e:
            # Same anyio TaskGroup/GeneratorExit gap as the HTTP connect path
            # (see _connect_http) — a live call can hit it too if the
            # connection drops or the server errors mid-request. Treat it
            # like any other failed call instead of letting it escape.
            logger.error(f"MCP tool call failed (exception group): {qualified_name}: {e.exceptions}", exc_info=True)
            return {"error": str(e.exceptions[0]) if e.exceptions else str(e), "exit_code": 1}
        except Exception as e:
            # Auto-reconnect for builtin servers whose subprocess may have died
            if self.is_builtin(server_id):
                logger.warning(f"MCP call failed for {qualified_name}, attempting reconnect: {e}", exc_info=True)
                reconnected = await self._reconnect_builtin(server_id)
                if reconnected:
                    session = self._sessions.get(server_id)
                    if session:
                        try:
                            result = await asyncio.wait_for(
                                self._do_call(session, tool_name, arguments), timeout=self.CALL_TOOL_TIMEOUT_S
                            )
                        except asyncio.TimeoutError:
                            logger.error(f"MCP tool call timed out after reconnect ({self.CALL_TOOL_TIMEOUT_S}s): {qualified_name}")
                            return {"error": f"MCP tool '{qualified_name}' timed out after {self.CALL_TOOL_TIMEOUT_S}s", "exit_code": 1}
                        except Exception as e2:
                            logger.error(f"MCP tool call failed after reconnect: {qualified_name}: {e2}", exc_info=True)
                            return {"error": str(e2), "exit_code": 1}
                    else:
                        return {"error": f"Reconnected but no session for {server_id}", "exit_code": 1}
                else:
                    logger.error(f"MCP reconnect failed for {server_id}")
                    return {"error": f"MCP server crashed and reconnect failed: {server_id}", "exit_code": 1}
            else:
                logger.error(f"MCP tool call failed: {qualified_name}: {e}", exc_info=True)
                return {"error": str(e), "exit_code": 1}

        return result

    def _validate_tool_args(self, server_id: str, tool_name: str, arguments: Dict) -> Optional[str]:
        """Check arguments against the tool's advertised JSON Schema.

        Returns an actionable error string, or None when the call looks valid.

        MCP tools ship a full input schema, but nothing checked the model's
        arguments against it -- a call with a missing required field or a
        hallucinated parameter name was dispatched anyway and came back as
        whatever server-side error that produced, often a generic traceback
        with no indication of which argument was wrong. Small models bleed
        parameter conventions between similarly-named tools (a generic
        `query` where a server wants `search_term`), and that is exactly the
        case a schema-derived message can correct in one round.

        Deliberately conservative -- it only rejects what the schema is
        unambiguous about, since a false rejection blocks a call the server
        would have accepted:
          - no declared properties -> no opinion, skip entirely
          - explicit additionalProperties: true -> unknown args are allowed
        """
        if not isinstance(arguments, dict):
            return None

        tool = next(
            (t for t in self._tools.get(server_id, []) if t.get("name") == tool_name),
            None,
        )
        if not tool:
            return None

        schema = tool.get("input_schema") or {}
        if not isinstance(schema, dict):
            return None
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            # Server didn't describe its inputs; it alone can judge them.
            return None

        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [r for r in required if r not in arguments]

        unknown = []
        if schema.get("additionalProperties") is not True:
            unknown = [k for k in arguments if k not in props]

        if not missing and not unknown:
            return None

        # Report BOTH problems at once. A wrong parameter name usually shows up
        # as one unknown arg AND one missing required arg (`query` sent where
        # the server wants `search_term`); reporting only the first means the
        # model resends with both names and fails again on the second round.
        problems = []
        if missing:
            problems.append(f"missing required argument(s): {', '.join(sorted(missing))}")
        if unknown:
            problems.append(f"unknown argument(s): {', '.join(sorted(unknown))}")
        return (
            f"Invalid arguments for '{tool_name}' -- {'; '.join(problems)}. "
            f"This tool accepts: {', '.join(sorted(props))}."
        )

    async def _do_call(self, session, tool_name: str, arguments: Dict) -> Dict:
        """Execute a single MCP tool call and return result dict."""
        result = await session.call_tool(tool_name, arguments)
        output_parts = []
        images = []

        def _mime_of(obj, default):
            # Same rename risk as Tool.inputSchema -> input_schema: check
            # both names defensively rather than assume the old one.
            return getattr(obj, 'mime_type', None) or getattr(obj, 'mimeType', None) or default

        for content in result.content:
            ctype = getattr(content, 'type', '')
            if hasattr(content, 'text'):
                output_parts.append(content.text)
            elif ctype == 'image' and hasattr(content, 'data'):
                # Image content (e.g. Playwright screenshots)
                mime = _mime_of(content, 'image/png')
                images.append({"data": content.data, "mimeType": mime})
                output_parts.append(f"[Screenshot captured ({mime})]")
            elif ctype == 'resource':
                # EmbeddedResource carries its payload on .resource, so it has
                # neither .text nor .data and fell through every branch here --
                # silently contributing NOTHING to output. A server returning
                # an embedded resource looked to the model like a tool that
                # succeeded and returned empty. Surface it instead.
                res = getattr(content, 'resource', None)
                res_text = getattr(res, 'text', None)
                uri = getattr(res, 'uri', None) or 'resource'
                if res_text:
                    output_parts.append(str(res_text))
                elif getattr(res, 'blob', None):
                    # Binary: describe it rather than dumping base64 into context.
                    output_parts.append(
                        f"[Resource: {uri} (binary, {_mime_of(res, 'application/octet-stream')})]"
                    )
                else:
                    output_parts.append(f"[Resource: {uri}]")
            elif ctype == 'resource_link':
                # A pointer to a resource the server can read separately; the
                # URI is the whole payload and is otherwise dropped.
                output_parts.append(f"[Resource link: {getattr(content, 'uri', 'unknown')}]")
            elif ctype == 'audio':
                # AudioContent has .data, so the generic fallback below used to
                # str() a base64 audio blob straight into the model's context.
                output_parts.append(f"[Audio content ({_mime_of(content, 'audio/wav')})]")
            elif hasattr(content, 'data'):
                output_parts.append(str(content.data))

        output = "\n".join(output_parts)
        # Same rename risk as Tool.inputSchema -> input_schema: if
        # CallToolResult.isError were renamed to is_error, this would
        # silently report every failed tool call as a success (exit_code=0),
        # with the error text landing in stdout instead of stderr. Check
        # both names defensively.
        is_error = getattr(result, 'is_error', None)
        if is_error is None:
            is_error = getattr(result, 'isError', False)

        result_dict = {
            "stdout": output if not is_error else "",
            "stderr": output if is_error else "",
            "exit_code": 1 if is_error else 0,
        }
        if images:
            result_dict["images"] = images
        return result_dict

    async def _reconnect_builtin(self, server_id: str) -> bool:
        """Tear down and reconnect a crashed builtin MCP server."""
        import sys
        from src.builtin_mcp import _BUILTIN_SERVERS, builtin_python_env

        if server_id not in _BUILTIN_SERVERS:
            return False

        script_rel, name = _BUILTIN_SERVERS[server_id]
        base_dir = get_app_root()
        script_path = os.path.join(base_dir, script_rel)

        # Clean up old connection
        await self.disconnect_server(server_id)

        try:
            ok = await self.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=sys.executable,
                args=[script_path],
                env=builtin_python_env(base_dir),
            )
            if ok:
                logger.info(f"Reconnected builtin MCP server: {name}")
            return ok
        except Exception as e:
            logger.error(f"Failed to reconnect builtin MCP server {name}: {e}", exc_info=True)
            return False

    def get_all_openai_schemas(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return all MCP tools in OpenAI function-calling format.

        Tool names are namespaced as mcp__{server_id}__{tool_name}.
        disabled_map: optional {server_id: set_of_disabled_tool_names} to filter out.
        """
        schemas = []
        for server_id, tools in self._tools.items():
            # Skip builtin Python servers — they use the code-block tool format
            # But include NPX-based builtins (like browser) which need function calling
            if self.is_builtin(server_id) and server_id != "builtin_browser":
                continue
            conn = self._connections.get(server_id, {})
            server_name = conn.get("name", server_id)
            disabled = (disabled_map or {}).get(server_id, set())

            identity = conn.get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name

            for tool in tools:
                if tool["name"] in disabled:
                    continue
                qualified = f"mcp__{server_id}__{tool['name']}"
                schema = {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": f"[MCP:{label}] {tool['description']}",
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                schemas.append(schema)

        return schemas

    def get_all_tools(self, disabled_map: Optional[Dict[str, set]] = None) -> List[Dict]:
        """Return a flat list of all discovered tools with server info."""
        result = []
        for server_id, tools in self._tools.items():
            conn = self._connections.get(server_id, {})
            disabled = (disabled_map or {}).get(server_id, set())
            for tool in tools:
                result.append({
                    "server_id": server_id,
                    "server_name": conn.get("name", server_id),
                    "name": tool["name"],
                    "qualified_name": f"mcp__{server_id}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {},
                    "is_disabled": tool["name"] in disabled,
                })
        return result

    def plan_mode_blocked_mcp(self) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """Plan mode: block every MCP tool that isn't clearly read-only.

        Returns (disabled_map, qualified_names):
          - disabled_map: {server_id: {tool_name, ...}} to hide write tools from
            the prompt/schemas (merged into the existing mcp_disabled_map).
          - qualified_names: {"mcp__<server>__<tool>", ...} for runtime rejection
            in execute_tool_block (which matches the qualified name).
        """
        disabled_map: Dict[str, Set[str]] = {}
        qualified: Set[str] = set()
        for server_id, tools in self._tools.items():
            for tool in tools:
                if not mcp_tool_is_readonly(tool):
                    disabled_map.setdefault(server_id, set()).add(tool["name"])
                    qualified.add(f"mcp__{server_id}__{tool['name']}")
        return disabled_map, qualified

    def is_builtin(self, server_id: str) -> bool:
        """Check if a server is a built-in (auto-registered) server."""
        return server_id.startswith("builtin_") or server_id in {
            "image_gen",
            "memory",
            "rag",
            "email",
        }

    def get_server_status(self, server_id: str) -> Dict:
        """Get connection status for a server."""
        return self._connections.get(server_id, {"status": "disconnected"})

    def get_all_statuses(self) -> Dict[str, Dict]:
        """Get connection statuses for all servers."""
        return dict(self._connections)

    _cached_prompt_desc = None
    _cached_prompt_desc_key = None

    def get_tool_descriptions_for_prompt(self, disabled_map: Optional[Dict[str, set]] = None) -> str:
        """Generate text describing MCP tools for the agent system prompt. Cached."""
        cache_key = (
            frozenset((k, frozenset(v)) for k, v in (disabled_map or {}).items()),
            len(self._tools),
            self._generation,
        )
        if self._cached_prompt_desc is not None and self._cached_prompt_desc_key == cache_key:
            return self._cached_prompt_desc
        tools = self.get_all_tools(disabled_map)
        if not tools:
            return ""

        lines = ["\n\nYou also have access to external MCP tool servers. These tools are called via native function calling:"]
        by_server = {}
        for t in tools:
            # Skip builtin Python servers — they're already in the agent prompt
            # But include NPX-based builtins (like browser) which aren't hardcoded
            if self.is_builtin(t["server_id"]) and t["server_id"] != "builtin_browser":
                continue
            if t.get("is_disabled"):
                continue
            sn = t["server_name"]
            if sn not in by_server:
                by_server[sn] = []
            by_server[sn].append(t)

        if not by_server:
            return ""

        for server_name, server_tools in by_server.items():
            # Include identity (e.g. email address) if available
            sid = server_tools[0]["server_id"] if server_tools else ""
            identity = self._connections.get(sid, {}).get("identity", "")
            label = f"{server_name} ({identity})" if identity else server_name
            lines.append(f"\n**{label}:**")
            for t in server_tools:
                # Flatten to a single line first — a multi-line docstring
                # (e.g. one that documents params as its own "- name: ..."
                # bullets) would otherwise smuggle embedded newlines into
                # this one-line-per-tool prompt format, which tool_index.py
                # re-parses line-by-line and would misread each bullet as a
                # brand-new top-level tool (issue: colliding "mcp_targetType"
                # IDs across every tool that documents a targetType param).
                _flat_desc = (t['description'] or '').replace('\n', ' ').replace('\r', ' ')
                _flat_desc = ' '.join(_flat_desc.split())
                desc = _flat_desc[:120] + '...' if len(_flat_desc) > 120 else _flat_desc
                # Include the tool's declared inputs so the model calls it with
                # real argument names instead of guessing from the description
                # alone (issue #2509).
                args_hint = _format_mcp_params(t.get("input_schema"))
                lines.append(f"  - {t['qualified_name']}: {desc}{args_hint}")

        result = "\n".join(lines)
        self._cached_prompt_desc = result
        self._cached_prompt_desc_key = cache_key
        return result
