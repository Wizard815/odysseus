"""Per-chat Connector toggles must be honored by tool retrieval.

The Connectors panel switches an MCP server off for one conversation. That was
enforced on the schema/prompt side but NOT in tool-RAG retrieval, which kept
the disabled servers' tools in its index and kept returning them.

Confirmed incident: a chat had Kanka ON and four other servers OFF. Retrieval
returned 7 tools, of which 4 belonged to two switched-off servers (N8N,
BoardNotes) -- leaving exactly ONE Kanka tool (`get_archives`) for the server
the user actually wanted. The model then burned 8 rounds re-calling that one
tool because the tools it needed were never sent.

Two root causes, both covered here:
  1. retrieve() didn't filter by server, and filtering after the top-K trim
     would still shrink the result -- so exclusion happens before the trim,
     with an over-fetch.
  2. index_mcp_tools() short-circuits on mcp_mgr._generation, which only moves
     on connect/disconnect -- so a per-chat toggle never triggered a reindex.
     The index is therefore built unfiltered and is chat-agnostic, which makes
     that short-circuit correct rather than a staleness bug.
"""
from src.tool_index import ToolIndex


def _index(rows):
    """A ToolIndex whose lane query returns `rows` (name, distance) pairs."""
    ti = ToolIndex.__new__(ToolIndex)

    class _Lane:
        name = "fastembed"

        def count(self):
            return len(rows)

        def encode(self, docs):
            return [[0.0] for _ in docs]

        class collection:
            @staticmethod
            def query(query_embeddings, n_results, include):
                sliced = rows[:n_results]
                return {
                    "metadatas": [[{"tool_name": n, "tool_type": "mcp"} for n, _ in sliced]],
                    "distances": [[d for _, d in sliced]],
                }

    ti._lanes = [_Lane()]
    return ti


def test_disabled_server_tools_are_excluded_from_retrieval():
    ti = _index([
        ("mcp__n8n__get_node", 0.10),
        ("mcp__boardnotes__get_item", 0.20),
        ("mcp__kanka__find_entities", 0.30),
    ])

    got = ti.retrieve("find my campaign entities", k=8, exclude_servers={"n8n", "boardnotes"})

    assert got == ["mcp__kanka__find_entities"]


def test_excluded_servers_do_not_consume_the_topk_budget():
    """The enabled server must still fill K even when better-scoring tools are off.

    This is the actual incident shape: without the over-fetch, the disabled
    servers' higher-ranked tools eat the budget and the enabled server comes
    back with almost nothing.
    """
    rows = [(f"mcp__n8n__tool_{i}", 0.01 * i) for i in range(8)]
    rows += [(f"mcp__kanka__tool_{i}", 0.5 + 0.01 * i) for i in range(8)]

    ti = _index(rows)
    got = ti.retrieve("kanka work", k=8, exclude_servers={"n8n"})

    assert len(got) == 8, "enabled server should still fill the top-K budget"
    assert all(n.startswith("mcp__kanka__") for n in got)


def test_no_exclusion_preserves_previous_behavior():
    rows = [("mcp__kanka__a", 0.1), ("mcp__n8n__b", 0.2)]

    assert _index(rows).retrieve("x", k=8) == ["mcp__kanka__a", "mcp__n8n__b"]


def test_server_name_mention_does_not_resurrect_a_disabled_server():
    """Naming a switched-off server in chat must not force-include its tools."""
    ti = _index([])
    ti._mcp_tools_by_server = {
        "kanka": {"mcp__kanka__find_entities"},
        "n8n": {"mcp__n8n__get_node"},
    }

    tools = ti.get_tools_for_query(
        "check kanka and n8n for me", k=8, exclude_servers={"n8n"}
    )

    assert "mcp__kanka__find_entities" in tools
    assert "mcp__n8n__get_node" not in tools


def test_index_is_built_unfiltered_so_it_stays_chat_agnostic():
    """index_mcp_tools must not narrow the shared index by a per-chat map."""
    seen = {}

    class _Mgr:
        _generation = 1

        def get_tool_descriptions_for_prompt(self, disabled_map):
            seen["disabled_map"] = disabled_map
            return ""

    ti = ToolIndex.__new__(ToolIndex)
    ti._lanes = []
    ti._mcp_generation = 0

    # A caller passing a per-chat disabled map must not have it applied to the
    # process-wide index; it would leak one chat's toggles into every chat.
    ti.index_mcp_tools(_Mgr(), {"n8n": {"get_node"}})

    assert seen["disabled_map"] == {}
