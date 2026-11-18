"""State-of-the-account read tools: field paths offline, shapes through the rig."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from googleads_mcp import tools_lookup
from kindkit import ReadAccount, read_tools, row
from test_reports import resolve_path

SELECTS = [
    name for name in dir(tools_lookup)
    if name.isupper() and isinstance(getattr(tools_lookup, name), tuple)
]


@pytest.mark.parametrize("name", SELECTS)
def test_every_field_the_lookups_select_exists_in_v25(name):
    problems = [p for p in (resolve_path(f) for f in getattr(tools_lookup, name)) if p]
    assert problems == [], problems


def test_get_campaign_puts_the_budget_question_first(monkeypatch):
    account = ReadAccount()
    account.answer("FROM campaign\nWHERE campaign.id = 7", [row(**{
        "campaign.id": 7, "campaign.name": "brand", "campaign.status": "ENABLED",
        "campaign.primary_status_reasons": ["BUDGET_CONSTRAINED"],
        "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
        "campaign.maximize_conversions.target_cpa_micros": 25_000_000,
        "campaign_budget.id": 3, "campaign_budget.amount_micros": 40_000_000,
        "campaign_budget.explicitly_shared": False, "campaign_budget.reference_count": 1,
        "customer.currency_code": "CAD",
    })])
    account.answer("FROM campaign_criterion", [row(**{
        "campaign_criterion.criterion_id": 2124, "campaign_criterion.type": "LOCATION",
        "campaign_criterion.location.geo_target_constant": "geoTargetConstants/2124",
    })])
    account.answer("FROM geo_target_constant", [row(**{
        "geo_target_constant.resource_name": "geoTargetConstants/2124",
        "geo_target_constant.canonical_name": "Canada",
    })])
    tools = read_tools(tools_lookup, monkeypatch, account)

    out = tools["get_campaign"](campaign_id="7")

    assert out["limited_by_budget"] is True
    assert out["budget"] == {
        "budget_id": "3", "name": "", "daily_amount": "40.00", "shared": False,
        "campaigns_using_it": 1,
    }
    assert out["bidding"]["target_cpa"] == "25.00"
    assert out["locations"][0]["name"] == "Canada"
    assert set(out["metrics"]) == {"last_7_days", "last_30_days"}


def test_get_campaign_not_found_says_so(monkeypatch):
    tools = read_tools(tools_lookup, monkeypatch, ReadAccount())
    with pytest.raises(ToolError, match="^NOT_FOUND: No campaign with id 9"):
        tools["get_campaign"](campaign_id="9")


def test_list_ads_returns_plain_text_headlines(monkeypatch):
    from kindkit import RAW

    def asset(text):
        a = RAW.get_type("AdTextAsset")
        a.text = text
        return a

    account = ReadAccount()
    account.answer("FROM ad_group_ad", [row(**{
        "ad_group_ad.ad.id": 5, "ad_group_ad.status": "ENABLED",
        "ad_group_ad.ad.responsive_search_ad.headlines": [asset("Fresh food"), asset("Local")],
        "ad_group_ad.ad.final_urls": ["https://example.com"],
        "ad_group_ad.policy_summary.approval_status": "APPROVED",
    })])
    tools = read_tools(tools_lookup, monkeypatch, account)
    ads = tools["list_ads"](ad_group_id="1")
    assert ads[0]["headlines"] == ["Fresh food", "Local"]
    assert ads[0]["final_urls"] == ["https://example.com"]
    assert "ad_group_ad.status != 'REMOVED'" in account.queries[-1]


def test_list_keywords_never_returns_negatives_or_removed(monkeypatch):
    account = ReadAccount()
    tools = read_tools(tools_lookup, monkeypatch, account)
    tools["list_keywords"](ad_group_id="4", status="PAUSED")
    gaql = account.queries[-1]
    assert "ad_group_criterion.negative = FALSE" in gaql
    assert "ad_group_criterion.status != 'REMOVED'" in gaql
    assert "ad_group_criterion.status = 'PAUSED'" in gaql
