"""revert_change answers to the same customer and campaign scope as an apply.

The scope can be narrowed after a change was made (or the ledger can hold a
change from another session), so a revert re-checks it against the ENTRY's
customer and campaign rather than trusting that the original preview did.
"""

from __future__ import annotations

import pytest

from googleads_mcp import ledger
from googleads_mcp.changes.base import entry_campaigns
from googleads_mcp.changes.targeting import CampaignLocations, CampaignNetworks
from kindkit import CUSTOMER, limits, preview_and_apply, rig, row


def campaign(client, *, search_partners=False):
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Example campaign", "campaign.status": "ENABLED",
        "campaign.resource_name": f"customers/{CUSTOMER}/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
        "campaign.network_settings.target_search_network": search_partners,
    })])


def networks_entry():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignNetworks())
    _, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=True)
    campaign(client, search_partners=True)
    return engine, client, result


def test_a_revert_outside_the_campaign_scope_is_refused():
    engine, client, result = networks_entry()
    sent = len(client.applied)
    engine.ctx.state.limits = limits(allowed_campaign_ids=("12",))
    with pytest.raises(Exception, match="campaign_in_scope"):
        engine.revert(result.entry_id)
    assert len(client.applied) == sent


def test_a_revert_outside_the_customer_scope_is_refused():
    engine, client, result = networks_entry()
    sent = len(client.applied)
    engine.ctx.state.limits = limits(allowed_customer_ids=("9998887777",))
    with pytest.raises(Exception, match="customer_in_scope"):
        engine.revert(result.entry_id)
    assert len(client.applied) == sent


def test_a_revert_inside_the_scope_still_goes_through():
    engine, client, result = networks_entry()
    engine.ctx.state.limits = limits(allowed_campaign_ids=("11",),
                                     allowed_customer_ids=(CUSTOMER,))
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.network_settings.target_search_network is False


def test_a_batch_revert_outside_the_campaign_scope_is_refused():
    engine, client, _ = rig()
    campaign(client)
    client.answer("campaign_criterion.type = 'LOCATION'", [row(**{
        "campaign_criterion.criterion_id": 1000,
        "campaign_criterion.location.geo_target_constant": "geoTargetConstants/1000",
    })])
    kind = engine.add(CampaignLocations())
    _, result = preview_and_apply(engine, kind, campaign_id="11", add=["3000", "4000"])
    client.answer("campaign_criterion.resource_name =", [row(**{
        "campaign_criterion.resource_name": "x", "campaign_criterion.status": "ENABLED"})])
    sent = len(client.applied)
    engine.ctx.state.limits = limits(allowed_campaign_ids=("12",))
    with pytest.raises(Exception, match="campaign_in_scope"):
        engine.revert(result.batch_id)
    assert len(client.applied) == sent


def test_an_entry_whose_campaign_cannot_be_told_is_refused_under_a_campaign_scope():
    engine, client, _ = rig()
    engine.add(CampaignNetworks())
    ledger.write_intent(
        entry_id="old1", tool="apply_campaign_networks", customer_id=CUSTOMER,
        entity_type="campaign", entity_id="", entity_name="?", field_name="networks",
        before="display_network=off, search_partners=off",
        after="display_network=off, search_partners=on", projected_delta="0",
    )
    ledger.write_outcome(entry_id="old1", ok=True)
    engine.ctx.state.limits = limits(allowed_campaign_ids=("11",))
    with pytest.raises(Exception, match="campaign_in_scope"):
        engine.revert("old1")
    assert client.applied == []


def test_an_entry_for_another_in_scope_customer_is_refused_as_not_the_same_customer():
    """Both accounts are allowed, but the inverse goes to the write client's
    customer: undoing the other account's change here would hit the wrong one."""
    engine, client, _ = rig()
    engine.add(CampaignNetworks())
    ledger.write_intent(
        entry_id="other1", tool="apply_campaign_networks", customer_id="9998887777",
        entity_type="campaign", entity_id="11", entity_name="Example campaign",
        field_name="networks", before="display_network=off, search_partners=off",
        after="display_network=off, search_partners=on", projected_delta="29.00",
        detail={"scope_campaigns": ["11"],
                "before": {"search_partners": False, "display_network": False},
                "after": {"search_partners": True, "display_network": False}},
    )
    ledger.write_outcome(entry_id="other1", ok=True)
    engine.ctx.state.limits = limits(allowed_customer_ids=(CUSTOMER, "9998887777"))
    with pytest.raises(Exception, match="same_customer"):
        engine.revert("other1")
    assert client.applied == []


def test_a_revert_of_a_revert_is_held_to_the_campaign_scope():
    engine, client, result = networks_entry()
    undo = engine.revert(result.entry_id)
    campaign(client, search_partners=False)
    assert entry_campaigns(ledger.find_entry(undo.entry_id)) == ["11"]
    sent = len(client.applied)
    engine.ctx.state.limits = limits(allowed_campaign_ids=("12",))
    with pytest.raises(Exception, match="campaign_in_scope"):
        engine.revert(undo.entry_id)
    assert len(client.applied) == sent
    engine.ctx.state.limits = limits(allowed_campaign_ids=("11",))
    engine.revert(undo.entry_id)
    assert len(client.applied) == sent + 1
