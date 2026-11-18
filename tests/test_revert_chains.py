"""Reverting a revert re-applies the change it undid, and costs what that did.

A revert's ledger row used to record only its OWN projection, and dropped the
``narrowing`` marker. So undoing an undo read "the inverse of 0" and projected
0, even when it put a widening back: a modifier -30% -> -10% (29.00) reverted
(0), then reverted again, raised every bid for free and skipped both the
increase-ratio check and the per-change limit. Every inverse row now records
what undoing IT costs, and a re-application re-runs the original's checks.

29.00 is the rig campaign's unused budget (30.00 budget, 1.00/day spent).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp import ledger
from googleads_mcp.changes.campaigns import CampaignDailyBudget
from googleads_mcp.changes.targeting import CampaignNetworks, DeviceBidModifier
from kindkit import CUSTOMER, limits, preview_and_apply, rig, row

HEADROOM = Decimal("29.00")


def campaign(client, *, budget_micros=30_000_000, search_partners=False):
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Example campaign", "campaign.status": "ENABLED",
        "campaign.resource_name": f"customers/{CUSTOMER}/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": budget_micros,
        "campaign.network_settings.target_search_network": search_partners,
    })])
    client.answer("FROM campaign_budget", [row(**{
        "campaign_budget.id": 5, "campaign_budget.amount_micros": budget_micros,
        "campaign_budget.explicitly_shared": False,
        "campaign_budget.resource_name": f"customers/{CUSTOMER}/campaignBudgets/5",
    })])


def device(client, modifier):
    client.answer("campaign_criterion.type = 'DEVICE'", [row(**{
        "campaign_criterion.criterion_id": 30001, "campaign_criterion.bid_modifier": modifier,
        "campaign_criterion.device.type_": "DESKTOP",
    })])


def chain(engine, entry_id, settle, depth):
    """Revert ``entry_id``, then that revert, ``depth`` times; the projections.

    ``settle(after)`` makes the account read as the last row left it, so each
    revert passes its staleness check.
    """
    out = []
    for _ in range(depth):
        result = engine.revert(entry_id)
        out.append(Decimal(result.projected_daily_spend_delta))
        settle(ledger.find_entry(result.entry_id)["after"])
        entry_id = result.entry_id
    return out


def modifier_rig(start, **over):
    engine, client, _ = rig(**over)
    campaign(client)
    device(client, start)
    kind = engine.add(DeviceBidModifier())
    multipliers = {"-10%": 0.9, "-30%": 0.7, "-50%": 0.5}
    return engine, client, kind, lambda after: device(client, multipliers[after])


def test_a_modifier_cut_reverted_four_deep_alternates_headroom_and_zero():
    engine, client, kind, settle = modifier_rig(0.9)
    preview, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                        bid_modifier_pct="-30")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    settle("-30%")
    # widen, re-narrow, widen AGAIN (was 0.00), re-narrow
    assert chain(engine, result.entry_id, settle, 4) == [HEADROOM, 0, HEADROOM, 0]


def test_a_modifier_raise_reverted_three_deep_charges_the_reapplication():
    engine, client, kind, settle = modifier_rig(0.7)
    preview, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                        bid_modifier_pct="-10")
    assert Decimal(preview.projected_daily_spend_delta) == HEADROOM
    settle("-10%")
    # undo (narrows), redo (widens: was 0.00), undo
    assert chain(engine, result.entry_id, settle, 4) == [0, HEADROOM, 0, HEADROOM]


def test_a_reapplied_modifier_raise_reruns_the_increase_ratio_check():
    engine, client, kind, settle = modifier_rig(0.5, max_bid_modifier_pct=90.0)
    _, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                  bid_modifier_pct="-10")  # 0.5 -> 0.9: +80%
    settle("-10%")
    undo = engine.revert(result.entry_id)
    settle("-50%")
    engine.ctx.state.limits = limits(max_bid_modifier_pct=50.0)
    sent = len(client.applied)
    with pytest.raises(Exception, match="max_bid_modifier_increase_pct"):
        engine.revert(undo.entry_id)
    assert len(client.applied) == sent


def test_a_reapplied_widening_answers_to_the_per_change_limit():
    engine, client, kind, settle = modifier_rig(0.7)
    _, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                  bid_modifier_pct="-10")
    settle("-10%")
    undo = engine.revert(result.entry_id)
    settle("-30%")
    engine.ctx.state.limits = limits(max_projected_delta_per_day=Decimal("10"))
    with pytest.raises(Exception, match="projected_delta_per_day"):
        engine.revert(undo.entry_id)


def budget_rig(**over):
    engine, client, _ = rig(**over)
    campaign(client)
    kind = engine.add(CampaignDailyBudget())

    def settle(after):
        campaign(client, budget_micros=int(Decimal(after.split()[0]) * 1_000_000))

    return engine, client, kind, settle


def test_a_budget_raise_reverted_four_deep_charges_every_reraise():
    engine, client, kind, settle = budget_rig()
    preview, result = preview_and_apply(engine, kind, campaign_id="11", new_daily_budget="40.00")
    assert Decimal(preview.projected_daily_spend_delta) == 10
    settle("40.00 CAD")
    assert chain(engine, result.entry_id, settle, 4) == [0, 10, 0, 10]


def test_a_budget_cut_reverted_three_deep():
    engine, client, kind, settle = budget_rig()
    _, result = preview_and_apply(engine, kind, campaign_id="11", new_daily_budget="20.00")
    settle("20.00 CAD")
    assert chain(engine, result.entry_id, settle, 3) == [10, 0, 10]


def test_a_reapplied_budget_raise_reruns_the_increase_check():
    engine, client, kind, settle = budget_rig()
    _, result = preview_and_apply(engine, kind, campaign_id="11", new_daily_budget="40.00")
    settle("40.00 CAD")
    undo = engine.revert(result.entry_id)
    settle("30.00 CAD")
    engine.ctx.state.limits = limits(max_budget_change_pct=20.0)
    with pytest.raises(Exception, match="max_budget_change_pct"):
        engine.revert(undo.entry_id)


def networks_rig(start):
    engine, client, _ = rig()
    campaign(client, search_partners=start)
    kind = engine.add(CampaignNetworks())

    def settle(after):
        campaign(client, search_partners="search_partners=on" in after)

    return engine, client, kind, settle


def test_networks_switched_off_reverted_four_deep():
    engine, client, kind, settle = networks_rig(True)
    _, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=False)
    settle("search_partners=off")
    assert chain(engine, result.entry_id, settle, 4) == [HEADROOM, 0, HEADROOM, 0]


def test_networks_switched_on_reverted_three_deep():
    engine, client, kind, settle = networks_rig(False)
    preview, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=True)
    assert Decimal(preview.projected_daily_spend_delta) == HEADROOM
    settle("search_partners=on")
    assert chain(engine, result.entry_id, settle, 3) == [0, HEADROOM, 0]
