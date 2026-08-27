"""Node-driven tests for isUnsupportedToolsError (util/toolSupportError.js).

Regression: chat.js used to decide "this model doesn't support agent tools"
via a bare substring check -- errText.includes('tool') || errText.includes
('auto') -- on ANY non-2xx /api/chat_stream error, then auto-switched the UI
to Chat mode. That fired on errors that merely mention "tool"/"auto" for
completely unrelated reasons, silently hiding the real cause and telling the
user their tool-capable model doesn't support tools. Two confirmed real
triggers: "Invalid tool approval decision." (400 from the tool-approval
flow) and any MCP validation/timeout message this repo generates itself
("MCP tool 'x' timed out", "Invalid arguments for 'y' ... This tool
accepts: ...").
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "util" / "toolSupportError.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not on PATH")


def _classify(text):
    script = f"""
      import {{ isUnsupportedToolsError }} from '{_HELPER.as_posix()}';
      console.log(JSON.stringify(isUnsupportedToolsError({json.dumps(text)})));
    """
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.parametrize("text", [
    "Invalid tool approval decision.",
    "MCP tool 'mcp__kanka__find_entities' timed out after 120s",
    "Invalid arguments for 'find_entities' -- unknown argument(s): query. This tool accepts: search_term.",
    "MCP tool call rejected before dispatch",
    "Unknown argument(s) for 'find_entities': query.",
])
def test_unrelated_tool_mentioning_errors_are_not_misclassified(text):
    """These all contain the word 'tool' but have nothing to do with model capability."""
    assert _classify(text) is False


@pytest.mark.parametrize("text", [
    "This model does not support tools",
    "The model does not support function calling",
    "tool_choice is not supported for this model",
    "tools is not supported by this backend",
    "Unsupported parameter: 'tools' is not supported with this model",
])
def test_genuine_unsupported_tools_errors_are_classified(text):
    assert _classify(text) is True


def test_empty_and_unrelated_text_is_false():
    assert _classify("") is False
    assert _classify("Connection refused") is False
    assert _classify("automatic backup completed") is False
