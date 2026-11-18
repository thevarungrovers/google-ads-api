"""Recommendations, change history, run_gaql and reconcile_ledger."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from googleads_mcp import tools_audit
from kindkit import ReadAccount, read_tools, row
from test_reports import resolve_path


@pytest.mark.parametrize("name", ["RECOMMENDATION_FIELDS", "CHANGE_EVENT_FIELDS"])
def test_every_field_exists_in_v25(name):
    problems = [p for p in (resolve_path(f) for f in getattr(tools_audit, name)) if p]
    assert problems == [], problems


@pytest.mark.parametrize(
    "query",
    ["UPDATE campaign SET status = 'PAUSED'", "SELECT campaign.id FROM campaign; DELETE x",
     "mutate campaigns"],
)
def test_run_gaql_refuses_anything_but_a_select(monkeypatch, query):
    account = ReadAccount()
    tools = read_tools(tools_audit, monkeypatch, account)
    with pytest.raises(ToolError, match="Only SELECT|chaining"):
        tools["run_gaql"](query=query)
    assert account.queries == [], "nothing may reach Google"


def test_run_gaql_bounds_the_rows_and_says_when_it_cut(monkeypatch):
    account = ReadAccount()
    account.answer("FROM campaign", [row(**{"campaign.id": i}) for i in range(4)])
    tools = read_tools(tools_audit, monkeypatch, account)

    out = tools["run_gaql"](query="SELECT campaign.id FROM campaign", limit=3)

    assert account.queries[-1].endswith("LIMIT 4")
    assert out["row_count"] == 3 and out["truncated"] is True
    assert out["rows"][0] == {"campaign.id": 0}


def test_run_gaql_flags_money_it_has_no_ruling_on(monkeypatch):
    tools = read_tools(tools_audit, monkeypatch, ReadAccount())
    out = tools["run_gaql"](query="SELECT metrics.average_target_cpa_micros, "
                            "metrics.cost_per_unknown_thing FROM campaign")
    assert "metrics.cost_per_unknown_thing" in out["unruled_money_fields"]


def test_recent_changes_always_sends_the_limit_change_event_requires(monkeypatch):
    account = ReadAccount()
    tools = read_tools(tools_audit, monkeypatch, account)
    tools["list_recent_changes"](days=3)
    gaql = account.queries[-1]
    assert "FROM change_event" in gaql and "LIMIT 200" in gaql
    assert "change_event.change_date_time >=" in gaql
    assert "change_event.change_date_time <=" in gaql


def test_recommendations_carry_their_id_and_budget_numbers(monkeypatch):
    account = ReadAccount()
    account.answer("FROM recommendation", [row(**{
        "recommendation.resource_name": "customers/1/recommendations/ABC",
        "recommendation.type": "CAMPAIGN_BUDGET",
        "recommendation.campaign": "customers/1/campaigns/7",
        "recommendation.campaign_budget_recommendation.current_budget_amount_micros":
            10_000_000,
        "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros":
            14_000_000,
    })])
    tools = read_tools(tools_audit, monkeypatch, account)
    out = tools["list_recommendations"](types=["campaign_budget"])
    assert out[0]["recommendation_id"] == "ABC" and out[0]["campaign_id"] == "7"
    assert out[0]["budget"] == {"current": "10.00", "recommended": "14.00"}
    assert "recommendation.type IN ('CAMPAIGN_BUDGET')" in account.queries[-1]


def test_recent_changes_stay_inside_googles_30_day_window(monkeypatch):
    import datetime as dt

    account = ReadAccount()
    tools = read_tools(tools_audit, monkeypatch, account)
    tools["list_recent_changes"](days=30)
    gaql = account.queries[-1]
    today = dt.date.today()
    # change_event is refused outside the last 30 days; 29 leaves a margin for
    # the account's time zone being behind this machine's.
    assert f">= '{(today - dt.timedelta(days=29)).isoformat()}'" in gaql
    # Tomorrow, so an account whose time zone is ahead does not lose today.
    assert f"<= '{(today + dt.timedelta(days=1)).isoformat()}'" in gaql
