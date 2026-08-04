"""Regression coverage for the built-in MCP servers' SDK compatibility line.

Upstream pins mcp<2 (the v1 low-level Server decorator API). This fork
deliberately runs mcp>=2 instead — src/mcp_manager.py's Streamable HTTP
connect path has been patched to match v2's API (renamed
streamable_http_client, http_client= instead of headers=/auth=). This test
guards the opposite direction from upstream's: it fails if someone re-pins
to <2 without also reverting those v2-specific patches, since a v1 client
would no longer match what that code calls.
"""

from pathlib import Path


REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def test_mcp_requirement_allows_v2_sdk():
    requirements = [
        line.split("#", 1)[0].strip().replace(" ", "")
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    ]

    assert "mcp<2" not in requirements
    assert any(req == "mcp" or req.startswith("mcp>=") for req in requirements)
