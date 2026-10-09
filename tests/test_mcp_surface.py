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
