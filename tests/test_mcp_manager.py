import asyncio
from unittest.mock import patch

from src.mcp_manager import _format_mcp_connection_error, McpManager


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Odysseus" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    # connect_server passes headers= through; a fake without it turns a routing
    # assertion into a TypeError swallowed by connect_server's except block.
    async def fake_start(server_id, name, url, headers=None):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()


def _mgr_with_schema(schema):
    mgr = McpManager()
    mgr._tools["srv"] = [{"name": "find_entities", "input_schema": schema}]
    return mgr


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"search_term": {"type": "string"}, "page": {"type": "integer"}},
    "required": ["search_term"],
}


def test_missing_required_argument_is_rejected_with_the_valid_names():
    err = _mgr_with_schema(_SEARCH_SCHEMA)._validate_tool_args("srv", "find_entities", {})

    assert err is not None
    assert "search_term" in err
    assert "missing required" in err


def test_hallucinated_argument_name_reports_both_problems_at_once():
    """The known failure: a generic `query` where the server wants `search_term`.

    This arrives as one unknown arg AND one missing required arg. Reporting
    only the first would cost a second round, so both must appear together.
    """
    err = _mgr_with_schema(_SEARCH_SCHEMA)._validate_tool_args(
        "srv", "find_entities", {"query": "dragons"}
    )

    assert err is not None
    assert "unknown argument(s): query" in err
    assert "missing required argument(s): search_term" in err


def test_valid_arguments_pass():
    mgr = _mgr_with_schema(_SEARCH_SCHEMA)

    assert mgr._validate_tool_args("srv", "find_entities", {"search_term": "x"}) is None
    assert mgr._validate_tool_args("srv", "find_entities", {"search_term": "x", "page": 2}) is None


def test_schema_without_properties_is_not_second_guessed():
    """No declared inputs means the server alone can judge them."""
    mgr = _mgr_with_schema({"type": "object", "properties": {}})

    assert mgr._validate_tool_args("srv", "find_entities", {"anything": 1}) is None


def test_additional_properties_true_allows_extras():
    mgr = _mgr_with_schema({**_SEARCH_SCHEMA, "additionalProperties": True})

    assert mgr._validate_tool_args("srv", "find_entities", {"search_term": "x", "extra": 1}) is None


def test_unknown_tool_is_left_to_the_server():
    mgr = _mgr_with_schema(_SEARCH_SCHEMA)

    assert mgr._validate_tool_args("srv", "some_new_tool", {"whatever": 1}) is None


class _Block:
    """Stand-in for an MCP content block; only the attrs set here exist."""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


class _Result:
    def __init__(self, content):
        self.content = content
        self.isError = False


def _run_do_call(blocks):
    mgr = McpManager()

    class _Session:
        async def call_tool(self, name, arguments):
            return _Result(blocks)

    return asyncio.run(mgr._do_call(_Session(), "t", {}))


def test_embedded_resource_content_is_not_silently_dropped():
    """EmbeddedResource carries its payload on .resource, not .text/.data.

    Regression: it matched no branch, so a tool returning one looked to the
    model like it had succeeded and returned nothing at all.
    """
    block = _Block(type="resource", resource=_Block(text="the payload", uri="file://x"))

    assert "the payload" in _run_do_call([block])["stdout"]


def test_binary_embedded_resource_is_described_not_dumped():
    blob = "QUJD" * 500
    block = _Block(
        type="resource",
        resource=_Block(blob=blob, uri="file://big.bin", mimeType="application/pdf"),
    )

    out = _run_do_call([block])["stdout"]

    assert blob not in out, "binary payload must not be dumped into model context"
    assert "file://big.bin" in out
    assert "application/pdf" in out


def test_audio_content_is_described_not_dumped():
    """AudioContent has .data, so the generic fallback used to str() base64 audio."""
    blob = "SUQz" * 500
    block = _Block(type="audio", data=blob, mimeType="audio/mpeg")

    out = _run_do_call([block])["stdout"]

    assert blob not in out
    assert "audio/mpeg" in out


def test_resource_link_surfaces_its_uri():
    block = _Block(type="resource_link", uri="https://example.com/doc")

    assert "https://example.com/doc" in _run_do_call([block])["stdout"]


def test_text_and_image_blocks_still_work():
    """The two block types that already worked must not regress."""
    blocks = [
        _Block(type="text", text="hello"),
        _Block(type="image", data="aW1n", mimeType="image/png"),
    ]

    result = _run_do_call(blocks)

    assert "hello" in result["stdout"]
    assert result["images"] == [{"data": "aW1n", "mimeType": "image/png"}]
