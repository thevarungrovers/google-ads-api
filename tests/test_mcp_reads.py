"""Read tools, through the real read client over a fake transport."""

from __future__ import annotations

import pytest

from googleads_mcp import readkit, tools_read
from kindkit import ReadAccount, read_tools, row


def ad_group_rows(n: int):
    return [
        row(**{
            "campaign.id": 1, "campaign.name": "brand", "ad_group.id": i,
            "ad_group.name": f"g{i}", "ad_group.status": "ENABLED",
            "metrics.cost_micros": (n - i) * 1_000_000, "metrics.clicks": 10,
            "metrics.impressions": 100,
        })
        for i in range(n)
    ]


def test_a_report_never_silently_cuts_rows(monkeypatch):
    account = ReadAccount()
    account.answer("FROM ad_group", ad_group_rows(12))
    tools = read_tools(tools_read, monkeypatch, account)

    out = tools["run_report"](report="ad_groups", limit=5)

    assert out["row_count"] == 5
    assert out["truncated"] is True
    assert out["total_rows_available"] == 12
    # Totals are over all twelve rows, not the five returned: 12+11+...+1.
    assert float(out["totals"]["metrics.cost"]) == pytest.approx(78.0)
    assert "LIMIT" not in out["gaql"]


def test_a_report_is_one_row_per_entity_unless_asked_for_days(monkeypatch):
    account = ReadAccount()
    tools = read_tools(tools_read, monkeypatch, account)

    tools["run_report"](report="campaigns")
    tools["run_report"](report="campaigns", segment_by_date=True)

    aggregated, daily = account.queries[-2:]
    assert "segments.date BETWEEN" in aggregated
    assert not aggregated.startswith("SELECT segments.date")
    assert daily.startswith("SELECT segments.date")


def test_the_scan_cap_is_reported_rather_than_hidden(monkeypatch):
    account = ReadAccount()
    account.answer("FROM ad_group", ad_group_rows(8))
    monkeypatch.setattr(readkit, "SCAN_CAP", 3)
    tools = read_tools(tools_read, monkeypatch, account)
    out = readkit.fetch(tools_read.ReadOnlyGoogleAdsClient.from_env(),
                        tools_read.get_report("ad_groups").build(), 10, scan_cap=3)
    assert out["total_rows_available"] == "3+" and out["truncated"] is True


def test_an_ad_group_filter_on_a_report_without_ad_groups_is_refused(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    tools = read_tools(tools_read, monkeypatch, ReadAccount())
    with pytest.raises(ToolError, match="no ad group"):
        tools["run_report"](report="devices", ad_group_id="5")


# -- filtering by who named it -----------------------------------------------

NAMES = ["AB-max-spring", "AB-cli-launch", "CD-bot-promo", "Hand made", "AB-bot-other"]


def campaign_account():
    account = ReadAccount()
    account.answer("FROM campaign", [
        row(**{"campaign.id": i + 1, "campaign.name": name, "campaign.status": "ENABLED",
               "campaign_budget.id": 9, "campaign_budget.amount_micros": 1_000_000,
               "customer.currency_code": "CAD"})
        for i, name in enumerate(NAMES)
    ])
    return account


def find(monkeypatch, identity=None, **filters):
    from googleads_reporting.naming import Identity

    account = campaign_account()
    tools = read_tools(tools_read, monkeypatch, account,
                       identity=identity or Identity.from_values("AB", "max", source=".env"))
    out = tools["find_campaigns"](**filters)
    return [r["name"] for r in out], out, account


def test_find_campaigns_rows_say_who_named_them(monkeypatch):
    _, out, _ = find(monkeypatch)
    by_name = {r["name"]: r for r in out}
    wanted = {"origin": "agent", "owner": "AB", "agent": "max", "base": "spring",
              "ledger_created": False}
    assert {k: by_name["AB-max-spring"][k] for k in wanted} == wanted
    assert by_name["AB-cli-launch"]["origin"] == "cli"
    assert by_name["Hand made"]["origin"] == "manual" and by_name["Hand made"]["base"] is None


@pytest.mark.parametrize("filters, expected", [
    ({"origin": "manual"}, ["Hand made"]),
    ({"origin": "cli"}, ["AB-cli-launch"]),
    ({"agent_name": "bot"}, ["CD-bot-promo", "AB-bot-other"]),
    ({"owner": "AB"}, ["AB-max-spring", "AB-cli-launch", "AB-bot-other"]),
    ({"owner": "AB", "agent_name": "max"}, ["AB-max-spring"]),
    ({"mine": True}, ["AB-max-spring"]),
])
def test_find_campaigns_filters_on_the_name_prefix(monkeypatch, filters, expected):
    names, _, _ = find(monkeypatch, **filters)
    assert names == expected


def test_mine_narrows_in_gaql_before_the_exact_local_match(monkeypatch):
    _, _, account = find(monkeypatch, mine=True)
    assert 'campaign.name LIKE "AB-max-%"' in account.queries[-1]


@pytest.mark.parametrize("owner, agent, expected", [
    ("AB", "max", ["AB-max-spring"]),
    # initials only: "AB-" does not claim AB-max-/AB-bot-/AB-cli-, which are
    # other identities' full forms, and none here is a plain "AB-<base>"
    ("AB", "", []),
    ("", "bot", []),  # agent only: its names start "bot-", and none here do
])
def test_mine_follows_whichever_parts_are_configured(monkeypatch, owner, agent, expected):
    from googleads_reporting.naming import Identity

    names, _, _ = find(monkeypatch, Identity.from_values(owner, agent, source=".env"),
                       mine=True)
    assert names == expected


def test_mine_without_an_identity_is_refused(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from googleads_reporting.naming import Identity

    with pytest.raises(ToolError, match="needs a naming identity"):
        find(monkeypatch, Identity(), mine=True)


@pytest.mark.parametrize("filters", [{"agent_name": "x' OR 1=1"}, {"owner": "ab\""}])
def test_a_malformed_filter_is_refused_before_any_gaql(monkeypatch, filters):
    from mcp.server.mcpserver.exceptions import ToolError

    account = campaign_account()
    tools = read_tools(tools_read, monkeypatch, account)
    with pytest.raises(ToolError):
        tools["find_campaigns"](**filters)
    assert account.queries == []


def test_find_ad_groups_marks_what_our_ledger_created(monkeypatch):
    from googleads_mcp import ledger

    ledger.write_intent(
        entry_id="mk", tool="apply_create_ad_group", customer_id="1112223333",
        entity_type="ad group", entity_id="11:new:x", entity_name="x", field_name="created",
        before="(none)", after="PAUSED", projected_delta="0", detail={"identity": {}},
    )
    ledger.write_outcome(entry_id="mk", ok=True, resource_name="customers/1112223333/adGroups/7")
    account = ReadAccount()
    account.answer("FROM ad_group", [
        row(**{"ad_group.id": gid, "ad_group.name": name, "ad_group.status": "PAUSED",
               "campaign.id": 11, "campaign.name": "c", "customer.currency_code": "CAD"})
        for gid, name in ((7, "AB-max-General"), (8, "Hand made"))
    ])
    tools = read_tools(tools_read, monkeypatch, account)
    out = {r["ad_group_id"]: r for r in tools["find_ad_groups"]()}
    assert out["7"]["ledger_created"] is True and out["8"]["ledger_created"] is False
    assert [r["name"] for r in tools["find_ad_groups"](origin="manual")] == ["Hand made"]
