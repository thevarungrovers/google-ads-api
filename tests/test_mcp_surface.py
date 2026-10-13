"""The MCP tool surface is a contract, not an accident.

An agent can call anything registered. Widening that set is a security-relevant
change, so it is pinned here: adding a tool means editing this file, which
means someone reviews it.
"""

import asyncio

import pytest

from googleads_mcp import server

READ_TOOLS = frozenset({
    "list_accounts",
    "find_campaigns",
    "find_ad_groups",
    "run_report",
    "get_guardrails",
    "list_my_changes",
})

PREVIEW_TOOLS = frozenset({
    "preview_campaign_status",
    "preview_campaign_daily_budget",
    "preview_ad_group_status",
    "preview_ad_group_cpc_bid",
})

APPLY_TOOLS = frozenset({
    "apply_campaign_status",
    "apply_campaign_daily_budget",
    "apply_ad_group_status",
    "apply_ad_group_cpc_bid",
    "revert_change",
})

EXPECTED = READ_TOOLS | PREVIEW_TOOLS | APPLY_TOOLS


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_the_surface_is_exactly_what_is_expected(tools):
    assert set(tools) == EXPECTED, {
        "unexpected": sorted(set(tools) - EXPECTED),
        "missing": sorted(EXPECTED - set(tools)),
    }


def test_every_apply_has_a_matching_preview(tools):
    """An apply with no preview could be called cold, with nothing shown first."""
    for name in APPLY_TOOLS - {"revert_change"}:
        assert name.replace("apply_", "preview_", 1) in PREVIEW_TOOLS


def test_no_tool_takes_a_dry_run_flag(tools):
    """Separate names are the whole design: permissions key on the tool NAME.

    With a dry_run flag, one "always allow" clicked during a harmless preview
    would silently authorise every future real write.
    """
    for name, tool in tools.items():
        properties = (tool.input_schema or {}).get("properties", {})
        for flag in ("dry_run", "dryRun", "apply", "confirm", "force"):
            assert flag not in properties, f"{name} exposes {flag}"


ALL_HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")

#: The only two tools that answer from this process without calling Google, and
#: so the only two that carry open_world_hint=False.
LOCAL_ONLY_TOOLS = frozenset({"get_guardrails", "list_my_changes"})


def test_every_tool_declares_all_four_hints(tools):
    """An omitted hint is not "unknown" -- it is the spec's default.

    `destructiveHint` defaults to TRUE when absent, so a tool that forgets it is
    published as possibly destructive and gets rendered with the same warning as
    a budget change. Directories that index MCP servers reject a tool whose four
    hints are not all present and boolean. Spell all four out, every time.
    """
    for name, tool in sorted(tools.items()):
        assert tool.annotations is not None, f"{name} has no annotations at all"
        for hint in ALL_HINTS:
            value = getattr(tool.annotations, hint, None)
            assert isinstance(value, bool), (
                f"{name}.{hint} is {value!r}, not an explicit True/False. An "
                f"absent hint falls back on the spec default, which for "
                f"destructiveHint is True."
            )


def test_only_the_local_tools_are_closed_world(tools):
    """open_world_hint says whether the tool reaches an external service.

    get_guardrails reads config and in-memory counters; list_my_changes reads
    the local ledger. Neither calls Google, and claiming otherwise overstates
    what they touch.
    """
    for name, tool in sorted(tools.items()):
        expected = name not in LOCAL_ONLY_TOOLS
        assert tool.annotations.open_world_hint is expected, (
            f"{name} open_world_hint={tool.annotations.open_world_hint}; expected "
            f"{expected}. Only {sorted(LOCAL_ONLY_TOOLS)} answer without calling Google."
        )


def test_reads_and_previews_are_never_destructive(tools):
    """They cannot be: neither writes anything to the account."""
    for name in READ_TOOLS | PREVIEW_TOOLS:
        assert tools[name].annotations.destructive_hint is False, name
        assert tools[name].annotations.idempotent_hint is True, name


def test_applies_are_never_idempotent(tools):
    """A preview_token is single-use, so a second identical call is an error."""
    for name in APPLY_TOOLS:
        assert tools[name].annotations.idempotent_hint is False, name


def test_previews_are_marked_read_only(tools):
    for name in PREVIEW_TOOLS | READ_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name


def test_applies_are_not_marked_read_only(tools):
    for name in APPLY_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name


def test_money_moving_applies_are_marked_destructive(tools):
    for name in APPLY_TOOLS - {"revert_change"}:
        assert tools[name].annotations.destructive_hint is True, name


def test_revert_is_not_marked_destructive(tools):
    """It restores a previous value. Crying wolf trains people to click through."""
    assert tools["revert_change"].annotations.destructive_hint is False


def test_every_apply_takes_only_a_preview_token(tools):
    """An apply must not accept the values directly -- that would bypass preview."""
    for name in APPLY_TOOLS - {"revert_change"}:
        properties = (tools[name].input_schema or {}).get("properties", {})
        assert set(properties) == {"preview_token"}, (name, sorted(properties))


def test_read_only_mode_publishes_no_write_tool():
    """GOOGLE_ADS_MCP_READ_ONLY must remove every apply, and keep every preview.

    Claude Code can allowlist tools by name, so there a read-only install is
    something the CLIENT can express. Claude Desktop, Cursor and Codex have no
    per-tool allowlist at all, so for them this is the only honest way to offer
    one -- which makes it worth a test rather than a comment.

    Run in a SUBPROCESS: registration happens at import, this module has
    already imported the server, and re-importing would not re-register. A real
    child process is also what every client does.
    """
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    script = (
        "import sys, anyio, json;"
        f"sys.path.insert(0, {str(repo)!r});"
        "from googleads_mcp.server import mcp;"
        "print(json.dumps(sorted(t.name for t in anyio.run(mcp.list_tools))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "GOOGLE_ADS_MCP_READ_ONLY": "1"},
        capture_output=True, text=True, check=True,
    )
    names = frozenset(json.loads(out.stdout))

    assert not (names & APPLY_TOOLS), (
        f"read-only mode still publishes write tools: {sorted(names & APPLY_TOOLS)}. "
        f"A client with no per-tool allowlist could call them."
    )
    assert names == READ_TOOLS | PREVIEW_TOOLS, (
        f"read-only mode should publish exactly the reads and previews; "
        f"differs by {sorted(names ^ (READ_TOOLS | PREVIEW_TOOLS))}"
    )


def test_there_is_no_passthrough_tool(tools):
    """An unregistered operation must be unreachable, not merely undocumented."""
    for name in tools:
        assert not any(
            word in name.lower()
            for word in ("mutate", "execute", "query", "raw", "request", "gaql")
        ), name


def test_no_tool_can_remove_an_entity(tools):
    """REMOVED is irreversible in Google Ads; pausing is the exposed path."""
    for name in tools:
        assert not any(
            word in name.lower() for word in ("remove", "delete", "destroy")
        ), name


def test_no_tool_creates_a_campaign(tools):
    """Creation carries media and a legal declaration a human answers for."""
    for name in tools:
        assert "create" not in name.lower(), name


def test_every_tool_has_a_description(tools):
    for name, tool in tools.items():
        assert (tool.description or "").strip(), name


def test_the_server_instructions_state_the_workflow():
    text = server.INSTRUCTIONS
    assert "preview" in text and "apply" in text
    assert "decimal string" in text.lower()
    assert "never pass micros" in text.lower()
