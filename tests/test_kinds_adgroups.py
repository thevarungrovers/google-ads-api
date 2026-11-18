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
