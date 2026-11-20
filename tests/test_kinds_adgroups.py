"""Ad group status and bid kinds, against real v25 protos."""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp.changes.adgroups import AdGroupCpcBid, AdGroupStatus
from kindkit import failed, preview_and_apply, rig, row


def account(client, *, ad_group_status="PAUSED", campaign_status="ENABLED",
            strategy="MANUAL_CPC", bid=1_000_000):
    client.answer("FROM ad_group", [row(**{
        "ad_group.id": 22, "ad_group.name": "shoes", "ad_group.status": ad_group_status,
        "ad_group.cpc_bid_micros": bid, "ad_group.resource_name": "customers/1/adGroups/22",
        "campaign.id": 11, "campaign.name": "brand",
    })])
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "brand", "campaign.status": campaign_status,
        "campaign.resource_name": "customers/1/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
    })])
    client.answer("campaign.bidding_strategy_type", [row(**{
        "campaign.id": 11, "campaign.bidding_strategy_type": strategy,
    })])


def test_enabling_an_ad_group_with_no_spend_projects_the_campaign_budget():
    """The old projection was 7-day spend / 7: zero for a paused ad group."""
    engine, client, read = rig()
    account(client)
    read.answer("metrics.cost_micros", [])  # spent nothing -- it was paused
    kind = engine.add(AdGroupStatus())

    preview = engine.preview(kind, {"ad_group_id": "22", "new_status": "ENABLED"})

    assert Decimal(preview.projected_daily_spend_delta) == Decimal("30.00")
    assert "campaign's daily budget" in preview.note


def test_enabling_inside_a_paused_campaign_adds_nothing_yet():
    engine, client, _ = rig()
    account(client, campaign_status="PAUSED")
    kind = engine.add(AdGroupStatus())
    preview = engine.preview(kind, {"ad_group_id": "22", "new_status": "ENABLED"})
    assert Decimal(preview.projected_daily_spend_delta) == 0
    assert preview.campaign_status == "PAUSED"


def test_pausing_projects_zero_and_round_trips_through_revert():
    engine, client, _ = rig()
    account(client, ad_group_status="ENABLED")
    kind = engine.add(AdGroupStatus())
    preview, result = preview_and_apply(engine, kind, ad_group_id="22", new_status="PAUSED")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    op = client.applied[-1]["operations"][0]
    assert op.update.status.name == "PAUSED"
    assert "status" in op.update_mask.paths

    account(client, ad_group_status="PAUSED")
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.status.name == "ENABLED"


def test_a_bid_under_automated_bidding_warns_that_it_is_ignored():
    engine, client, _ = rig()
    account(client, strategy="MAXIMIZE_CONVERSIONS")
    kind = engine.add(AdGroupCpcBid())
    preview = engine.preview(kind, {"ad_group_id": "22", "new_cpc_bid": "1.20"})

    assert preview.blocked is False, "informational, not blocking"
    assert any("IGNORES ad group max CPC" in w for w in preview.warnings)
    assert any("bid_is_effective" in c and "NO" in c for c in preview.checks)
    assert Decimal(preview.projected_daily_spend_delta) == 0


def test_a_bid_under_manual_cpc_is_effective_and_sent_in_micros():
    engine, client, _ = rig()
    account(client)
    kind = engine.add(AdGroupCpcBid())
    preview, _ = preview_and_apply(engine, kind, ad_group_id="22", new_cpc_bid="1.20")
    assert any("bid_is_effective" in c and "yes" in c for c in preview.checks)
    assert preview.before == "1.00 CAD" and preview.after == "1.20 CAD"
    assert client.applied[-1]["operations"][0].update.cpc_bid_micros == 1_200_000


def test_a_bid_increase_over_the_limit_is_blocked():
    engine, client, _ = rig()
    account(client)
    kind = engine.add(AdGroupCpcBid())
    preview = engine.preview(kind, {"ad_group_id": "22", "new_cpc_bid": "3.00"})
    assert failed(preview, "max_bid_increase_pct")


def test_the_scope_check_compares_the_customer_the_write_targets():
    """It used to compare the default customer with itself."""
    from kindkit import limits

    engine, client, _ = rig()
    account(client)
    engine.ctx.state.limits = limits(allowed_customer_ids=("9998887777",))
    kind = engine.add(AdGroupStatus())
    preview = engine.preview(kind, {"ad_group_id": "22", "new_status": "PAUSED"})
    assert failed(preview, "customer_in_scope")
    assert preview.customer_id == client.resolve_customer_id()

    # With no allowlist, only the server's own default account is in scope --
    # and a write client pointed elsewhere is caught.
    engine.ctx.state.limits = limits()
    engine.ctx.state.default_customer_id = "9998887777"
    preview = engine.preview(kind, {"ad_group_id": "22", "new_status": "PAUSED"})
    assert failed(preview, "customer_in_scope")


# --------------------------------------------------------------------------
# Re-enabling counts against the DAILY total, not the per-change limit
# --------------------------------------------------------------------------


def _big_campaign(client, *, status="PAUSED"):
    """A campaign whose 100/day budget is over the per-change 50 limit."""
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Brand - Search - EN", "campaign.status": status,
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 100_000_000,
    })])


def _spent_today(amount: str) -> None:
    from googleads_mcp import ledger

    entry = ledger.new_entry_id()
    ledger.write_intent(
        entry_id=entry, tool="apply_x", customer_id="1", entity_type="campaign",
        entity_id="99", entity_name="other", field_name="daily_budget", before="a",
        after="b", projected_delta=amount,
    )
    ledger.write_outcome(entry_id=entry, ok=True)


def test_re_enabling_a_100_a_day_campaign_with_nothing_used_passes():
    from googleads_mcp.changes.campaigns import CampaignStatus

    engine, client, _ = rig(max_projected_delta_per_day_total=Decimal("150"))
    _big_campaign(client)
    kind = engine.add(CampaignStatus())

    preview = engine.preview(kind, {"campaign_id": "11", "new_status": "ENABLED"})

    assert preview.blocked is False, preview.checks
    assert "SKIP projected_delta_per_day (enable: counted against the daily total)" in preview.checks
    assert any("uses 100.00 of today's remaining 150.00" in c for c in preview.checks)
    assert any("resumes spend up to the daily budget" in w and "remaining 150.00" in w
               for w in preview.warnings)
    engine.apply(kind, preview.preview_token)


def test_re_enabling_with_60_already_used_under_150_is_refused_with_both_figures():
    from googleads_mcp.changes.campaigns import CampaignStatus
    from googleads_mcp.previews import PreviewExpired

    engine, client, _ = rig(max_projected_delta_per_day_total=Decimal("150"),
                            max_projected_session_delta=Decimal("500"))
    _big_campaign(client)
    kind = engine.add(CampaignStatus())
    preview = engine.preview(kind, {"campaign_id": "11", "new_status": "ENABLED"})
    _spent_today("60")  # another process used 60 of the day in between

    with pytest.raises(PreviewExpired, match="uses 100.00 of today's remaining 90.00"):
        engine.apply(kind, preview.preview_token)
    assert client.applied == []

    blocked = engine.preview(kind, {"campaign_id": "11", "new_status": "ENABLED"})
    assert failed(blocked, "max_projected_delta_per_day_total")


def test_a_budget_increase_still_needs_the_per_change_limit():
    from googleads_mcp.changes.campaigns import CampaignDailyBudget

    engine, client, _ = rig(max_daily_budget=Decimal("500"), max_budget_change_pct=500.0)
    _big_campaign(client, status="ENABLED")
    client.answer("FROM campaign_budget", [row(**{
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 100_000_000,
        "campaign_budget.explicitly_shared": False,
    })])
    kind = engine.add(CampaignDailyBudget())
    preview = engine.preview(kind, {"campaign_id": "11", "new_daily_budget": "160.00"})
    assert failed(preview, "projected_delta_per_day")


def test_reverting_a_pause_projects_the_budget_it_restores():
    from googleads_mcp.changes.campaigns import CampaignStatus
    from googleads_mcp.previews import PreviewExpired

    engine, client, _ = rig(max_projected_delta_per_day_total=Decimal("150"),
                            max_projected_session_delta=Decimal("500"))
    _big_campaign(client, status="ENABLED")
    kind = engine.add(CampaignStatus())
    _, paused = preview_and_apply(engine, kind, campaign_id="11", new_status="PAUSED")
    _big_campaign(client, status="PAUSED")
    _spent_today("60")

    with pytest.raises(PreviewExpired, match="remaining 90.00"):
        engine.revert(paused.entry_id)


def test_reverting_a_budget_cut_projects_the_inverse_of_the_cut():
    from googleads_mcp.changes.campaigns import CampaignDailyBudget

    engine, client, _ = rig()
    _big_campaign(client, status="ENABLED")
    budget = lambda micros: client.answer("FROM campaign_budget", [row(**{  # noqa: E731
        "campaign_budget.id": 5, "campaign_budget.amount_micros": micros,
        "campaign_budget.explicitly_shared": False,
        "campaign_budget.resource_name": "customers/1112223333/campaignBudgets/5",
    })])
    budget(100_000_000)
    kind = engine.add(CampaignDailyBudget())
    _, cut = preview_and_apply(engine, kind, campaign_id="11", new_daily_budget="80.00")
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Brand - Search - EN", "campaign.status": "ENABLED",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 80_000_000,
    })])
    budget(80_000_000)

    result = engine.revert(cut.entry_id)
    assert result.projected_daily_spend_delta == "20.00"
