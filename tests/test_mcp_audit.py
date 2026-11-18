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


# -- reconcile_ledger: the naming section ------------------------------------


def _created(entry_id, entity_type, resource, *, identity=True, names=(), also=()):
    """``also``: the other resources the request created (nested ad groups)."""
    from googleads_mcp import ledger

    detail = {"created_names": [list(n) for n in names]}
    if identity:
        detail["identity"] = {"owner": "AB", "agent": "max", "source": ".env"}
    ledger.write_intent(
        entry_id=entry_id, tool="apply_x", customer_id="1112223333", entity_type=entity_type,
        entity_id="new", entity_name="x", field_name="created", before="(none)",
        after="PAUSED", projected_delta="0", detail=detail,
    )
    ledger.write_outcome(entry_id=entry_id, ok=True, resource_name=resource,
                         created_resources=[resource, *also] if also else None)


def _names_state(identity, campaigns=(), ad_groups=(), labels=()):
    from types import SimpleNamespace

    from kindkit import FakeRead

    read = FakeRead()
    read.answer("FROM campaign\n", [{"campaign.id": i, "campaign.name": n} for i, n in campaigns])
    read.answer("FROM ad_group\n", [{"ad_group.id": i, "ad_group.name": n, "campaign.id": c}
                                    for i, n, c in ad_groups])
    read.answer("FROM label\n", [{"label.id": i, "label.name": n} for i, n in labels])
    return SimpleNamespace(identity=identity, read_client=read)


def test_reconcile_reports_lost_and_unrecorded_prefixes():
    from googleads_mcp.tools_write import naming_reconcile
    from googleads_reporting.naming import Identity

    _created("c1", "campaign", "customers/1112223333/campaigns/1")
    _created("c2", "campaign", "customers/1112223333/campaigns/2", identity=False)
    _created("c3", "campaign", "customers/1112223333/campaigns/3",
             names=[("ad_group", "AB-max-General")],
             also=["customers/1112223333/adGroups/30"])
    state = _names_state(
        Identity.from_values("AB", "max", source=".env"),
        campaigns=[(1, "renamed in the UI"), (2, "old style"), (3, "AB-max-launch"),
                   (4, "AB-max-forged"), (5, "CD-bot-x"), (6, "CD-bot-y"), (7, "Hand made")],
        ad_groups=[(30, "AB-max-General", 3)],
    )
    report = naming_reconcile(state)
    assert report["checked"] is True
    lost = {r["entity_id"]: r["pre_feature"] for r in report["prefix_lost"]}
    assert lost == {"1": False, "2": True}
    assert [r["entity_id"] for r in report["prefix_without_ledger"]] == ["4"]
    assert report["other_identities"] == {"CD-bot-": 2}


def test_reconcile_skips_names_when_no_prefix_is_configured():
    from googleads_mcp.tools_write import naming_reconcile
    from googleads_reporting.naming import Identity

    state = _names_state(Identity(), campaigns=[(1, "x")])
    report = naming_reconcile(state)
    assert report["checked"] is False and "no naming identity" in report["reason"]
    assert state.read_client.queries == []


def test_reconcile_under_initials_only_does_not_file_other_identities_as_ours():
    from googleads_mcp.tools_write import naming_reconcile
    from googleads_reporting.naming import Identity

    state = _names_state(
        Identity.from_values("AB", "", source=".env"),
        campaigns=[(1, "AB-launch"), (2, "AB-bot-x"), (3, "AB-cli-y")],
    )
    report = naming_reconcile(state)
    assert [r["entity_id"] for r in report["prefix_without_ledger"]] == ["1"]
    assert report["other_identities"] == {"AB-bot-": 1, "AB-cli-": 1}


def test_a_label_created_through_the_low_risk_kind_counts_as_ledger_created():
    """Label rows are written with field "create" (the low-risk kind's own
    field name), not "created": the index must read both."""
    from googleads_mcp import ledger
    from googleads_mcp.tools_write import naming_reconcile
    from googleads_reporting.naming import Identity

    ledger.write_intent(
        entry_id="l1", tool="apply_low_risk_change", customer_id="1112223333",
        entity_type="label", entity_id="new:AB-max-promo", entity_name="label 'AB-max-promo'",
        field_name="create", before="(none)", after="AB-max-promo", projected_delta="0",
        detail={"label_id": None, "created_names": [["label", "AB-max-promo"]],
                "identity": {"owner": "AB", "agent": "max", "source": ".env"}},
    )
    ledger.write_outcome(entry_id="l1", ok=True, resource_name="customers/1112223333/labels/9")
    assert "9" in ledger.created_index()["label"]
    state = _names_state(Identity.from_values("AB", "max", source=".env"),
                         labels=[(9, "AB-max-promo")])
    report = naming_reconcile(state)
    assert report["prefix_without_ledger"] == [] and report["prefix_lost"] == []
