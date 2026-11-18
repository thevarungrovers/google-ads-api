"""Google recommendations are applied through the typed kind they amount to."""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp.changes.recommendations import ApplyRecommendation
from kindkit import failed, preview_and_apply, rig, row


def recommendation(client, rec_type, **fields):
    client.answer("FROM recommendation", [row(**{
        "recommendation.resource_name": "customers/1112223333/recommendations/R1",
        "recommendation.type_": rec_type,
        "recommendation.campaign": "customers/1112223333/campaigns/11",
        "recommendation.ad_group": "customers/1112223333/adGroups/22",
        **fields,
    })])


def campaign(client, *, budget=20_000_000, shared=False):
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Brand - Search - EN", "campaign.status": "ENABLED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": budget,
    })])
    client.answer("FROM campaign_budget", [row(**{
        "campaign_budget.id": 5, "campaign_budget.amount_micros": budget,
        "campaign_budget.explicitly_shared": shared,
        "campaign_budget.resource_name": "customers/1112223333/campaignBudgets/5",
    })])


def test_a_budget_recommendation_is_held_to_the_budget_bounds():
    engine, client, _ = rig()
    campaign(client)
    recommendation(client, "CAMPAIGN_BUDGET", **{
        "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros":
            60_000_000,  # +200%: over max_budget_change_pct
    })
    kind = engine.add(ApplyRecommendation())
    preview = engine.preview(kind, {"recommendation_id": "R1"})
    assert failed(preview, "max_budget_change_pct")
    assert "applied as preview_campaign_daily_budget" in preview.note


def test_a_budget_recommendation_within_bounds_applies_and_reverts_as_a_budget():
    engine, client, _ = rig()
    campaign(client)
    recommendation(client, "CAMPAIGN_BUDGET", **{
        "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros":
            25_000_000,
    })
    kind = engine.add(ApplyRecommendation())
    from googleads_mcp.changes.campaigns import CampaignDailyBudget

    engine.add(CampaignDailyBudget())  # revert finds the delegate by its ledger key
    preview, result = preview_and_apply(engine, kind, recommendation_id="R1")
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("5.00")
    assert client.applied[-1]["operations"][0].update.amount_micros == 25_000_000
    campaign(client, budget=25_000_000)
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.amount_micros == 20_000_000


def test_a_keyword_recommendation_arrives_paused():
    engine, client, _ = rig()
    client.answer("FROM ad_group\n", [row(**{
        "ad_group.id": 22, "ad_group.name": "General", "ad_group.status": "ENABLED",
        "campaign.id": 11, "campaign.name": "Brand - Search - EN",
    })])
    recommendation(client, "KEYWORD", **{
        "recommendation.keyword_recommendation.keyword.text": "example boxes",
        "recommendation.keyword_recommendation.keyword.match_type": "PHRASE",
    })
    kind = engine.add(ApplyRecommendation())
    preview_and_apply(engine, kind, recommendation_id="R1")
    op = client.applied[-1]["operations"][0]
    assert op.create.keyword.text == "example boxes"
    assert op.create.status.name == "PAUSED"


def test_an_unsupported_type_is_refused_with_the_reason():
    engine, client, _ = rig()
    recommendation(client, "RESPONSIVE_SEARCH_AD_ASSET")
    kind = engine.add(ApplyRecommendation())
    with pytest.raises(ValueError, match="no guarded change kind matches"):
        engine.preview(kind, {"recommendation_id": "R1"})
