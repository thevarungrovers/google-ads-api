"""Bidding strategy and shared budget kinds."""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp.changes.bidding import BiddingStrategy, SharedBudgetAmount
from kindkit import failed, limits, preview_and_apply, rig, row


def campaign(client, *, strategy="MAXIMIZE_CONVERSIONS", cpa=20_000_000, portfolio=""):
    fields = {
        "campaign.id": 11, "campaign.name": "brand", "campaign.status": "ENABLED",
        "campaign.bidding_strategy_type": strategy,
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
    }
    if strategy == "MAXIMIZE_CONVERSIONS":
        fields["campaign.maximize_conversions.target_cpa_micros"] = cpa
    if portfolio:
        fields["campaign.bidding_strategy"] = portfolio
    client.answer("FROM campaign\n", [row(**fields)])


def test_switching_strategy_warns_about_learning_and_projects_headroom():
    engine, client, _ = rig()
    campaign(client, strategy="MANUAL_CPC")
    kind = engine.add(BiddingStrategy())
    preview, _ = preview_and_apply(engine, kind, campaign_id="11",
                                   strategy="MAXIMIZE_CONVERSIONS", target_cpa="25.00")
    assert preview.before == "MANUAL_CPC"
    assert preview.after == "MAXIMIZE_CONVERSIONS (target CPA 25.00 CAD)"
    assert any("learning period" in w for w in preview.warnings)
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    op = client.applied[-1]["operations"][0]
    assert list(op.update_mask.paths) == ["maximize_conversions.target_cpa_micros"]
    assert op.update.maximize_conversions.target_cpa_micros == 25_000_000


def test_a_target_move_beyond_the_limit_is_blocked_in_either_direction():
    engine, client, _ = rig()
    campaign(client)  # target CPA 20.00
    kind = engine.add(BiddingStrategy())
    for target in ("30.00", "10.00"):
        preview = engine.preview(kind, {"campaign_id": "11", "strategy": "MAXIMIZE_CONVERSIONS",
                                        "target_cpa": target})
        assert failed(preview, "max_target_change_pct"), target
    ok = engine.preview(kind, {"campaign_id": "11", "strategy": "MAXIMIZE_CONVERSIONS",
                               "target_cpa": "18.00"})
    assert not ok.blocked
    assert Decimal(ok.projected_daily_spend_delta) == 0, "a lower target only restricts"


def test_a_target_cpa_over_the_ceiling_and_a_portfolio_are_blocked():
    engine, client, _ = rig()
    campaign(client, strategy="MANUAL_CPC")
    kind = engine.add(BiddingStrategy())
    preview = engine.preview(kind, {"campaign_id": "11", "strategy": "MAXIMIZE_CONVERSIONS",
                                    "target_cpa": "150.00"})
    assert failed(preview, "max_target_cpa")
    campaign(client, portfolio="customers/1112223333/biddingStrategies/4")
    preview = engine.preview(kind, {"campaign_id": "11", "strategy": "MANUAL_CPC"})
    assert failed(preview, "not_portfolio")


def test_a_target_for_the_wrong_strategy_is_refused():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(BiddingStrategy())
    with pytest.raises(ValueError, match="target_roas does not apply"):
        engine.preview(kind, {"campaign_id": "11", "strategy": "MAXIMIZE_CONVERSIONS",
                              "target_roas": "4"})


def test_a_strategy_switch_reverts_to_the_old_strategy_and_target():
    engine, client, _ = rig()
    campaign(client)  # MAXIMIZE_CONVERSIONS @ 20.00
    kind = engine.add(BiddingStrategy())
    _, result = preview_and_apply(engine, kind, campaign_id="11", strategy="MANUAL_CPC")
    campaign(client, strategy="MANUAL_CPC")
    engine.revert(result.entry_id)
    op = client.applied[-1]["operations"][0]
    assert op.update.maximize_conversions.target_cpa_micros == 20_000_000


def shared_budget(client, *, shared=True, amount=40_000_000, fed=((11, "brand"), (12, "generic"))):
    client.answer("FROM campaign_budget", [row(**{
        "campaign_budget.id": 5, "campaign_budget.name": "pool",
        "campaign_budget.amount_micros": amount, "campaign_budget.explicitly_shared": shared,
        "campaign_budget.resource_name": "customers/1112223333/campaignBudgets/5",
    })])
    client.answer("FROM campaign\nWHERE campaign_budget.id = 5", [row(**{
        "campaign.id": cid, "campaign.name": name, "campaign.status": "ENABLED",
    }) for cid, name in fed])


def test_a_shared_budget_lists_every_campaign_and_projects_the_full_change():
    engine, client, _ = rig()
    shared_budget(client)
    kind = engine.add(SharedBudgetAmount())
    preview, result = preview_and_apply(engine, kind, budget_id="5", new_daily_budget="50.00")
    assert any("brand (ENABLED), generic (ENABLED)" in w for w in preview.warnings)
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("10.00")
    assert client.applied[-1]["operations"][0].update.amount_micros == 50_000_000

    shared_budget(client, amount=50_000_000)
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.amount_micros == 40_000_000


def test_a_shared_budget_feeding_an_out_of_scope_campaign_is_blocked():
    engine, client, _ = rig(allowed_campaign_ids=("11",))
    shared_budget(client)
    kind = engine.add(SharedBudgetAmount())
    preview = engine.preview(kind, {"budget_id": "5", "new_daily_budget": "45.00"})
    assert failed(preview, "campaign_in_scope")


def test_an_unshared_budget_is_sent_to_the_campaign_tool():
    engine, client, _ = rig()
    shared_budget(client, shared=False)
    kind = engine.add(SharedBudgetAmount())
    with pytest.raises(ValueError, match="preview_campaign_daily_budget"):
        engine.preview(kind, {"budget_id": "5", "new_daily_budget": "45.00"})
