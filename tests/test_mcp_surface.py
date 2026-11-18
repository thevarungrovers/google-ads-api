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
    # state of the account, for deciding what to change
    "get_campaign",
    "get_ad_group",
    "list_keywords",
    "list_negative_keywords",
    "list_shared_negative_lists",
    "list_ads",
    "list_assets",
    "list_labels",
    "list_audiences",
    "list_conversion_actions",
    "find_locations",
    "find_languages",
    # what changed, what Google suggests, and the read-only escape hatch
    "list_recommendations",
    "list_recent_changes",
    "reconcile_ledger",
    "run_gaql",
})

PREVIEW_TOOLS = frozenset({
    "preview_campaign_status",
    "preview_campaign_daily_budget",
    "preview_ad_group_status",
    "preview_ad_group_cpc_bid",
    # keywords and search terms
    "preview_add_keywords",
    "preview_keyword_status",
    "preview_keyword_cpc_bid",
    "preview_add_negative_keywords",
    "preview_add_to_shared_negative_list",
    "preview_link_shared_negative_list",
    # bidding and budgets
    "preview_bidding_strategy",
    "preview_shared_budget_amount",
    # targeting
    "preview_campaign_locations",
    "preview_campaign_languages",
    "preview_ad_schedule",
    "preview_device_bid_modifier",
    "preview_location_bid_modifier",
    "preview_audience_targeting",
    "preview_campaign_networks",
    # ads and assets
    "preview_create_responsive_search_ad",
    "preview_ad_status",
    "preview_replace_responsive_search_ad",
    "preview_add_sitelinks",
    "preview_add_callouts",
    "preview_add_structured_snippets",
    # creation and naming
    "preview_create_campaign",
    "preview_create_ad_group",
    "preview_rename_campaign",
    "preview_rename_ad_group",
    # recommendations
    "preview_recommendation",
})

APPLY_TOOLS = frozenset({
    "apply_campaign_status",
    "apply_campaign_daily_budget",
    "apply_ad_group_status",
    "apply_ad_group_cpc_bid",
    # keywords and search terms
    "apply_add_keywords",
    "apply_keyword_status",
    "apply_keyword_cpc_bid",
    "apply_add_negative_keywords",
    "apply_add_to_shared_negative_list",
    "apply_link_shared_negative_list",
    # bidding and budgets
    "apply_bidding_strategy",
    "apply_shared_budget_amount",
    # targeting
    "apply_campaign_locations",
    "apply_campaign_languages",
    "apply_ad_schedule",
    "apply_device_bid_modifier",
    "apply_location_bid_modifier",
    "apply_audience_targeting",
    "apply_campaign_networks",
    # ads and assets
    "apply_create_responsive_search_ad",
    "apply_ad_status",
    "apply_replace_responsive_search_ad",
    "apply_add_sitelinks",
    "apply_add_callouts",
    "apply_add_structured_snippets",
    # creation and naming
    "apply_create_campaign",
    "apply_create_ad_group",
    "apply_rename_campaign",
    "apply_rename_ad_group",
    # recommendations
    "apply_recommendation",
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


#: Applies that cannot move money or stop delivery, and so are NOT marked
#: destructive -- crying wolf on a rename trains people to click through the
#: loud prompts. Pinned by name: anything not listed here must be destructive.
NON_DESTRUCTIVE_APPLIES = frozenset({
    "revert_change",
    "apply_rename_campaign",
    "apply_rename_ad_group",
})


def test_money_moving_applies_are_marked_destructive(tools):
    for name in APPLY_TOOLS - NON_DESTRUCTIVE_APPLIES:
        assert tools[name].annotations.destructive_hint is True, name


def test_the_non_destructive_applies_really_are(tools):
    for name in NON_DESTRUCTIVE_APPLIES:
        assert tools[name].annotations.destructive_hint is False, name


def test_revert_is_not_marked_destructive(tools):
    """It restores a previous value. Crying wolf trains people to click through."""
    assert tools["revert_change"].annotations.destructive_hint is False


def test_every_apply_takes_only_a_preview_token(tools):
    """An apply must not accept the values directly -- that would bypass preview."""
    for name in APPLY_TOOLS - {"revert_change"}:
        properties = (tools[name].input_schema or {}).get("properties", {})
        assert set(properties) == {"preview_token"}, (name, sorted(properties))


def test_installer_tool_list_matches_the_server(tools):
    """install.py hardcodes the tool names. Keep that list honest.

    It has to hardcode them: it is stdlib-only on purpose, so someone can run
    it before the venv exists, which means it cannot import the server to ask.
    The cost of that choice is a second copy of the tool set that can drift --
    and drifting the wrong way would silently allowlist a write tool.
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_installer", Path(__file__).resolve().parent.parent / "install.py"
    )
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)

    assert frozenset(install.SAFE_TOOLS) == READ_TOOLS | PREVIEW_TOOLS, sorted(
        frozenset(install.SAFE_TOOLS) ^ (READ_TOOLS | PREVIEW_TOOLS)
    )
    assert frozenset(install.WRITE_TOOLS) == APPLY_TOOLS, sorted(
        frozenset(install.WRITE_TOOLS) ^ APPLY_TOOLS
    )
    assert not (frozenset(install.SAFE_TOOLS) & APPLY_TOOLS), (
        "install.py would allowlist a tool that changes the account"
    )
    assert frozenset(install.SAFE_TOOLS) | frozenset(install.WRITE_TOOLS) == set(tools), (
        "install.py does not account for every registered tool"
    )


def test_installer_never_allowlists_a_write_tool():
    """The generated Claude Code rules, checked as strings.

    A wildcard or a bare `mcp__google-ads` entry would approve the writes too.
    Assert on what the installer produces, not on what it intends.
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_installer2", Path(__file__).resolve().parent.parent / "install.py"
    )
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)

    rules = [f"mcp__{install.SERVER_NAME}__{t}" for t in install.SAFE_TOOLS]
    assert len(rules) == len(set(rules))
    for rule in rules:
        assert "*" not in rule, f"{rule} is a wildcard; support varies by version"
        assert rule.rsplit("__", 1)[-1] not in APPLY_TOOLS, f"{rule} pre-approves a write"
    assert f"mcp__{install.SERVER_NAME}" not in rules


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


#: The one tool allowed to name a query: a READ passthrough. Deliberately listed
#: by name -- any other tool matching the words below is still a failure.
READ_PASSTHROUGH = frozenset({"run_gaql"})


def test_there_is_no_passthrough_tool(tools):
    """An unregistered operation must be unreachable, not merely undocumented."""
    for name in set(tools) - READ_PASSTHROUGH:
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


#: Every tool allowed to CREATE something, by name. Creation is allowed now
#: (agent-parity), but only through these, each created PAUSED where Google
#: has a status, and each costing 3 applies against the caps.
CREATION_TOOLS = frozenset({
    "preview_create_responsive_search_ad",
    "apply_create_responsive_search_ad",
    "preview_create_campaign",
    "apply_create_campaign",
    "preview_create_ad_group",
    "apply_create_ad_group",
})


def test_only_the_pinned_tools_create_anything(tools):
    for name in tools:
        if "create" in name.lower():
            assert name in CREATION_TOOLS, f"{name} creates but is not pinned"


def test_creation_previews_say_what_is_created_paused(tools):
    for name in CREATION_TOOLS:
        if name.startswith("preview_"):
            assert "PAUSED" in (tools[name].title or "") + (tools[name].description or ""), name


def test_creation_applies_are_destructive_and_say_they_cost_three(tools):
    for name in CREATION_TOOLS:
        if name.startswith("apply_"):
            assert tools[name].annotations.destructive_hint is True, name
            assert "3 applies" in (tools[name].title or ""), name


def test_every_tool_has_a_description(tools):
    for name, tool in tools.items():
        assert (tool.description or "").strip(), name


def test_the_server_instructions_state_the_workflow():
    text = server.INSTRUCTIONS
    assert "preview" in text and "apply" in text
    assert "decimal string" in text.lower()
    assert "never pass micros" in text.lower()


def test_find_campaigns_status_is_an_enum_not_free_text(tools):
    """The value is interpolated into GAQL; a free string was injectable."""
    schema = tools["find_campaigns"].input_schema["properties"]["status"]
    options = schema.get("anyOf", [schema])
    enums = [o.get("enum") for o in options if o.get("enum")]
    assert enums == [["ENABLED", "PAUSED", "REMOVED"]], schema


def test_the_read_passthrough_cannot_write(tools):
    """run_gaql is read-only by construction, and annotated as such."""
    assert tools["run_gaql"].annotations.read_only_hint is True
    assert tools["run_gaql"].annotations.destructive_hint is False
    assert READ_PASSTHROUGH <= READ_TOOLS
