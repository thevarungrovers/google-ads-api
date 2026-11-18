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


def test_reverting_a_networks_revert_puts_the_original_change_back():
    """The inverse row's structured before/after are swapped like its strings,
    so undoing the undo turns Search Partners back ON rather than re-sending OFF."""
    engine, client, kind, settle = networks_rig(False)
    _, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=True)
    settle("search_partners=on")
    undo = engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.network_settings.target_search_network is False
    row_ = ledger.find_entry(undo.entry_id)
    assert row_["detail"]["before"]["search_partners"] is True
    assert row_["detail"]["after"]["search_partners"] is False
    settle(row_["after"])
    engine.revert(undo.entry_id)
    assert client.applied[-1]["operations"][0].update.network_settings.target_search_network is True


def test_reverting_a_bidding_strategy_revert_puts_the_original_target_back():
    from googleads_mcp.changes.bidding import BiddingStrategy

    engine, client, _ = rig()

    def settle(cpa_micros):
        client.answer("FROM campaign\n", [row(**{
            "campaign.id": 11, "campaign.name": "Example campaign", "campaign.status": "ENABLED",
            "campaign.resource_name": f"customers/{CUSTOMER}/campaigns/11",
            "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
            "campaign.bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "campaign.maximize_conversions.target_cpa_micros": cpa_micros,
        })])

    settle(20_000_000)
    kind = engine.add(BiddingStrategy())
    _, result = preview_and_apply(engine, kind, campaign_id="11",
                                  strategy="MAXIMIZE_CONVERSIONS", target_cpa="18.00")
    settle(18_000_000)
    undo = engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.maximize_conversions.target_cpa_micros \
        == 20_000_000
    settle(20_000_000)
    engine.revert(undo.entry_id)
    assert client.applied[-1]["operations"][0].update.maximize_conversions.target_cpa_micros \
        == 18_000_000


# -- mixed changes: one change that both narrows AND widens ----------------
#
# A change can turn one thing off and another on. It is recorded as a
# narrowing (its undo widens), but it also projected headroom of its own, and
# re-applying it puts that widening back. Re-applying is free only when the
# change was a PURE narrowing -- and those were recorded at 0 anyway.


def mixed_networks_rig(**over):
    engine, client, _ = rig(**over)

    def settle(after):
        client.answer("FROM campaign\n", [row(**{
            "campaign.id": 11, "campaign.name": "Example campaign", "campaign.status": "ENABLED",
            "campaign.resource_name": f"customers/{CUSTOMER}/campaigns/11",
            "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
            "campaign.network_settings.target_search_network": "search_partners=on" in after,
            "campaign.network_settings.target_content_network": "display_network=on" in after,
        })])

    settle("display_network=off, search_partners=on")
    kind = engine.add(CampaignNetworks())
    return engine, client, kind, settle


def test_mixed_networks_reverted_five_deep_charges_every_reapplication():
    """Search Partners OFF + Display ON: the change widens (29.00) and narrows.
    Every step of the chain either re-opens Search Partners or re-enables
    Display, so none of them is free."""
    engine, client, kind, settle = mixed_networks_rig()
    preview, result = preview_and_apply(engine, kind, campaign_id="11",
                                        search_partners=False, display_network=True)
    assert Decimal(preview.projected_daily_spend_delta) == HEADROOM
    settle("display_network=on, search_partners=off")
    assert chain(engine, result.entry_id, settle, 4) == [HEADROOM] * 4


def test_a_reapplied_mixed_networks_change_answers_to_the_per_change_limit():
    engine, client, kind, settle = mixed_networks_rig()
    _, result = preview_and_apply(engine, kind, campaign_id="11",
                                  search_partners=False, display_network=True)
    settle("display_network=on, search_partners=off")
    undo = engine.revert(result.entry_id)
    settle(ledger.find_entry(undo.entry_id)["after"])
    engine.ctx.state.limits = limits(max_projected_delta_per_day=Decimal("10"))
    sent = len(client.applied)
    with pytest.raises(Exception, match="projected_delta_per_day"):
        engine.revert(undo.entry_id)
    assert len(client.applied) == sent


def test_a_pure_narrowing_still_reapplies_for_free():
    engine, client, kind, settle = mixed_networks_rig()
    _, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=False)
    settle("display_network=off, search_partners=off")
    engine.ctx.state.limits = limits(max_projected_delta_per_day=Decimal("10"))
    # The undo restores a state the account had (exempt); the redo re-narrows
    # (0, so it passes the per-change limit it is now held to).
    assert chain(engine, result.entry_id, settle, 2) == [HEADROOM, 0]


def created_criterion_present(client):
    client.answer("campaign_criterion.resource_name =", [row(**{
        "campaign_criterion.resource_name": "x", "campaign_criterion.status": "ENABLED"})])


def test_first_schedule_windows_with_a_raised_modifier_reapply_at_headroom():
    """The first windows narrow (the campaign served around the clock) but a
    +20% modifier widens inside them: re-applying that is not free."""
    from googleads_mcp.changes.targeting import AdSchedule

    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AdSchedule())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", windows=[
        {"day": "MONDAY", "start_hour": 8, "end_hour": 20, "bid_modifier_pct": "+20"}])
    assert Decimal(preview.projected_daily_spend_delta) == HEADROOM
    created_criterion_present(client)
    undo = engine.revert(result.entry_id)
    assert Decimal(undo.projected_daily_spend_delta) == HEADROOM
    row_ = ledger.find_entry(undo.entry_id)
    assert kind.revert_projection(engine.ctx, row_)[0] == HEADROOM
    assert kind.reapplies(row_)
    # Re-creating it through revert is refused outright (a second REMOVE
    # otherwise); the projection above is what a re-application would cost.
    client.answer("campaign_criterion.resource_name =", [])
    sent = len(client.applied)
    with pytest.raises(Exception, match="already removed"):
        engine.revert(undo.entry_id)
    assert len(client.applied) == sent


def test_location_add_and_exclude_reapply_at_headroom():
    """Adding a target to a campaign that has one widens; the exclusion in the
    same request narrows. Re-applying the pair re-widens."""
    from googleads_mcp.changes.targeting import CampaignLocations

    engine, client, _ = rig()
    campaign(client)
    client.answer("campaign_criterion.type = 'LOCATION'", [row(**{
        "campaign_criterion.criterion_id": 1000,
        "campaign_criterion.location.geo_target_constant": "geoTargetConstants/1000",
    })])
    kind = engine.add(CampaignLocations())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", add=["3000"],
                                        exclude=["2000"])
    assert Decimal(preview.projected_daily_spend_delta) == HEADROOM
    created_criterion_present(client)
    undo = engine.revert(result.batch_id)
    rows = [ledger.find_entry(e) for e in undo.entry_ids]
    assert all(kind.revert_projection(engine.ctx, r)[0] == HEADROOM for r in rows)
    assert all(kind.reapplies(r) for r in rows)
    client.answer("campaign_criterion.resource_name =", [])
    sent = len(client.applied)
    with pytest.raises(Exception, match="already removed"):
        engine.revert(undo.batch_id)
    assert len(client.applied) == sent


# -- parity: only every other step of a chain re-applies the change ------------
#
# A chain alternates between the state before the original change (A) and the
# one after it (B). A revert that lands on B re-applies the change and answers
# to the per-change limit; one that lands on A only restores what the account
# had -- a plain undo, exempt from that limit, though still counted in the
# session and daily totals. Carrying ``undoes`` is not enough to tell them
# apart: every row past the first carries it.

TIGHT = Decimal("10")


def walk(engine, client, entry_id, settle, depth, *, reapplying, batch=False):
    """Revert ``depth`` deep under a per-change limit of TIGHT. Steps listed in
    ``reapplying`` (1-based) must be refused under it, sending nothing, and
    then go through once it is lifted; every other step must pass under it."""
    out = []
    for step in range(1, depth + 1):
        engine.ctx.state.limits = limits(max_projected_delta_per_day=TIGHT)
        if step in reapplying:
            sent = len(client.applied)
            with pytest.raises(Exception, match="projected_delta_per_day"):
                engine.revert(entry_id)
            assert len(client.applied) == sent, f"step {step} sent something"
            engine.ctx.state.limits = limits()
        result = engine.revert(entry_id)
        out.append(Decimal(result.projected_daily_spend_delta))
        rows = [ledger.find_entry(e) for e in (result.entry_ids or [result.entry_id])]
        settle(rows[0]["after"])
        entry_id = result.batch_id if batch else result.entry_id
    return out


def test_a_cut_reverted_five_deep_holds_only_reapplications_to_the_per_change_limit():
    """The reviewer's probe: revert 3 restores the pre-cut modifier (29.00) --
    a plain undo -- and was refused against 10 because its row carried undoes."""
    engine, client, kind, settle = modifier_rig(0.9)
    _, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                  bid_modifier_pct="-30")
    settle("-30%")
    assert walk(engine, client, result.entry_id, settle, 5, reapplying=()) == \
        [HEADROOM, 0, HEADROOM, 0, HEADROOM]


def test_a_raise_reverted_five_deep_holds_every_reapplication_to_the_per_change_limit():
    engine, client, kind, settle = modifier_rig(0.7)
    _, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                  bid_modifier_pct="-10")
    settle("-10%")
    assert walk(engine, client, result.entry_id, settle, 5, reapplying=(2, 4)) == \
        [0, HEADROOM, 0, HEADROOM, 0]


def test_a_mixed_change_reverted_five_deep_holds_every_reapplication_to_the_limit():
    # Six steps at 29.00 each: room in the session total for all of them.
    engine, client, kind, settle = mixed_networks_rig(max_projected_session_delta=Decimal("500"))
    _, result = preview_and_apply(engine, kind, campaign_id="11",
                                  search_partners=False, display_network=True)
    settle("display_network=on, search_partners=off")
    assert walk(engine, client, result.entry_id, settle, 5, reapplying=(2, 4)) == \
        [HEADROOM] * 5


def test_an_exempt_undo_still_counts_in_the_session_total():
    engine, client, kind, settle = modifier_rig(0.9)
    _, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                  bid_modifier_pct="-30")
    settle("-30%")
    last = None
    entry_id = result.entry_id
    for _ in range(2):
        last = engine.revert(entry_id)
        settle(ledger.find_entry(last.entry_id)["after"])
        entry_id = last.entry_id
    counters = engine.ctx.counters
    counters._limits = limits(max_projected_delta_per_day=TIGHT,
                              max_projected_session_delta=counters.projected_delta + TIGHT)
    engine.ctx.state.limits = counters._limits
    sent = len(client.applied)
    with pytest.raises(Exception, match="session"):
        engine.revert(entry_id)  # step 3: exempt from the per-change limit only
    assert len(client.applied) == sent


class TwoDeviceModifier(DeviceBidModifier):
    """Test-only: one modifier on DESKTOP and MOBILE as ONE batch, so a chain
    of reverts runs on the batch path with a kind that is not a creation."""

    name = "two_device_modifier"

    def propose(self, ctx, campaign_id: str, bid_modifier_pct: str):
        parts = [DeviceBidModifier.propose(self, ctx, campaign_id=campaign_id, device=d,
                                           bid_modifier_pct=bid_modifier_pct)
                 for d in ("DESKTOP", "MOBILE")]
        out = parts[0]
        out.items = [p.items[0] for p in parts]
        out.before = "; ".join(p.before for p in parts)
        out.batch = True
        out.payload = {"parts": [p.payload for p in parts]}
        return out

    def current(self, ctx, payload):
        return "; ".join(DeviceBidModifier.current(self, ctx, p) for p in payload["parts"])

    def build(self, ctx, payload):
        from googleads_mcp.changes.engine import merge_plans

        return merge_plans([DeviceBidModifier.build(self, ctx, p) for p in payload["parts"]])


def two_device_rig(start):
    engine, client, _ = rig()
    campaign(client)
    multipliers = {"-10%": 0.9, "-30%": 0.7, "-50%": 0.5}

    def devices(modifier):
        client.answer("campaign_criterion.type = 'DEVICE'", [row(**{
            "campaign_criterion.criterion_id": 30001 + i,
            "campaign_criterion.bid_modifier": modifier,
            "campaign_criterion.device.type_": name,
        }) for i, name in enumerate(("DESKTOP", "MOBILE"))])

    devices(start)
    kind = engine.add(TwoDeviceModifier())
    return engine, client, kind, lambda after: devices(multipliers[after])


def test_a_batch_cut_reverted_five_deep_holds_only_reapplications_to_the_limit():
    engine, client, kind, settle = two_device_rig(0.9)
    _, result = preview_and_apply(engine, kind, campaign_id="11", bid_modifier_pct="-30")
    assert result.batch_id
    settle("-30%")
    assert walk(engine, client, result.batch_id, settle, 5, reapplying=(), batch=True) == \
        [HEADROOM, 0, HEADROOM, 0, HEADROOM]


def test_a_batch_raise_reverted_five_deep_holds_every_reapplication_to_the_limit():
    engine, client, kind, settle = two_device_rig(0.7)
    _, result = preview_and_apply(engine, kind, campaign_id="11", bid_modifier_pct="-10")
    settle("-10%")
    assert walk(engine, client, result.batch_id, settle, 5, reapplying=(2, 4), batch=True) == \
        [0, HEADROOM, 0, HEADROOM, 0]


def test_a_revert_row_without_the_parity_flag_is_read_off_the_ledger():
    """Rows written before ``forward`` was recorded: the row they undid says."""
    import copy

    engine, client, kind, settle = modifier_rig(0.9)
    _, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                  bid_modifier_pct="-30")
    settle("-30%")
    undo = engine.revert(result.entry_id)
    settle(ledger.find_entry(undo.entry_id)["after"])
    redo = engine.revert(undo.entry_id)

    def legacy(entry_id):
        entry = copy.deepcopy(ledger.find_entry(entry_id))
        del entry["detail"]["undoes"]["forward"]
        return entry

    assert kind.reapplies(legacy(undo.entry_id))       # back to the cut
    assert not kind.reapplies(legacy(redo.entry_id))   # back to before it
    orphan = legacy(redo.entry_id)
    orphan["detail"]["undoes"]["entry_id"] = "gone"
    # The undone row is gone: held to the limit, since it would put a widening back.
    assert kind.reapplies(orphan)


# -- a created criterion: its removal is not undone by a second REMOVE ---------


def test_reverting_the_removal_of_a_created_criterion_is_refused():
    """Reverting the revert used to send plan_remove_created for the same,
    already removed, resource: a second REMOVE."""
    from googleads_mcp.changes.targeting import AdSchedule

    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AdSchedule())
    _, result = preview_and_apply(engine, kind, campaign_id="11", windows=[
        {"day": "MONDAY", "start_hour": 8, "end_hour": 20}])
    created_criterion_present(client)
    undo = engine.revert(result.entry_id)
    client.answer("campaign_criterion.resource_name =", [])
    sent = len(client.applied)
    with pytest.raises(ValueError, match="already removed; add it again with preview_ad_schedule"):
        engine.revert(undo.entry_id)
    assert len(client.applied) == sent
    with pytest.raises(ValueError, match="already removed"):
        kind.inverse(engine.ctx, ledger.find_entry(undo.entry_id))


def test_reverting_the_batch_removal_of_created_criteria_is_refused():
    from googleads_mcp.changes.targeting import CampaignLocations

    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignLocations())
    _, result = preview_and_apply(engine, kind, campaign_id="11", add=["3000", "4000"])
    created_criterion_present(client)
    undo = engine.revert(result.batch_id)
    assert undo.batch_id
    client.answer("campaign_criterion.resource_name =", [])
    sent = len(client.applied)
    with pytest.raises(ValueError, match="already removed; add it again with preview_campaign_locations"):
        engine.revert(undo.batch_id)
    assert len(client.applied) == sent
