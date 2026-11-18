"""Reverting a NARROWING change widens delivery again, so it is not free.

A kind whose apply projects 0 because it can only narrow (a network off, a
tighter target, a modifier cut, a first location/language/schedule target, a
negative, an audience in targeting mode) used to revert at 0 too: the default
revert projection is the inverse of the original's, and -0 is 0. Undoing one
of these puts back delivery the account had, bounded by the campaign's unused
budget -- 29.00 in this rig (30.00 budget, 1.00/day spent).
"""

from __future__ import annotations

from decimal import Decimal

from googleads_mcp.changes.bidding import BiddingStrategy
from googleads_mcp.changes.keywords import (
    AddNegativeKeywords,
    AddToSharedNegativeList,
    LinkSharedNegativeList,
)
from googleads_mcp.changes.targeting import (
    AdSchedule,
    AudienceTargeting,
    CampaignLanguages,
    CampaignLocations,
    CampaignNetworks,
    DeviceBidModifier,
)
from kindkit import RAW, preview_and_apply, rig, row

HEADROOM = Decimal("29.00")


def campaign(client, *, search_partners=False, strategy=None, cpa=None):
    fields = {
        "campaign.id": 11, "campaign.name": "Example campaign", "campaign.status": "ENABLED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
        "campaign.network_settings.target_search_network": search_partners,
    }
    if strategy:
        fields["campaign.bidding_strategy_type"] = strategy
    if cpa:
        fields["campaign.maximize_conversions.target_cpa_micros"] = cpa
    client.answer("FROM campaign\n", [row(**fields)])


def created_present(client, resource):
    """A created criterion/link reads back as still there, so the revert is not stale."""
    status = {"campaign_shared_set": "ENABLED"}.get(resource)
    fields = {f"{resource}.resource_name": "x"}
    if resource in ("campaign_criterion", "ad_group_criterion", "campaign_shared_set"):
        fields[f"{resource}.status"] = status or "ENABLED"
    client.answer(f"{resource}.resource_name =", [row(**fields)])


def reverted(engine, entry_id) -> Decimal:
    return Decimal(engine.revert(entry_id).projected_daily_spend_delta)


def test_reverting_a_network_switched_off_projects_headroom():
    engine, client, _ = rig()
    campaign(client, search_partners=True)
    kind = engine.add(CampaignNetworks())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=False)
    assert Decimal(preview.projected_daily_spend_delta) == 0
    campaign(client, search_partners=False)
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_a_tighter_target_projects_headroom():
    engine, client, _ = rig()
    campaign(client, strategy="MAXIMIZE_CONVERSIONS", cpa=20_000_000)
    kind = engine.add(BiddingStrategy())
    preview, result = preview_and_apply(engine, kind, campaign_id="11",
                                        strategy="MAXIMIZE_CONVERSIONS", target_cpa="18.00")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    campaign(client, strategy="MAXIMIZE_CONVERSIONS", cpa=18_000_000)
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_a_modifier_cut_projects_headroom():
    engine, client, _ = rig()
    campaign(client)

    def device(modifier):
        client.answer("campaign_criterion.type = 'DEVICE'", [row(**{
            "campaign_criterion.criterion_id": 30001, "campaign_criterion.bid_modifier": modifier,
            "campaign_criterion.device.type_": "DESKTOP",
        })])

    device(0.9)
    kind = engine.add(DeviceBidModifier())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                        bid_modifier_pct="-30")
    assert Decimal(preview.projected_daily_spend_delta) == 0
    device(0.7)
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_a_first_location_target_batch_projects_headroom_once():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignLocations())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", add=["1000"],
                                        exclude=["2000"])
    assert Decimal(preview.projected_daily_spend_delta) == 0
    created_present(client, "campaign_criterion")
    # Two rows, one campaign: the campaign's headroom once, not twice.
    assert reverted(engine, result.batch_id) == HEADROOM


def test_reverting_one_exclusion_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    client.answer("campaign_criterion.type = 'LOCATION'", [row(**{
        "campaign_criterion.criterion_id": 1000,
        "campaign_criterion.location.geo_target_constant": "geoTargetConstants/1000",
    })])
    kind = engine.add(CampaignLocations())
    _, result = preview_and_apply(engine, kind, campaign_id="11", exclude=["2000"])
    created_present(client, "campaign_criterion")
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_a_first_language_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignLanguages())
    _, result = preview_and_apply(engine, kind, campaign_id="11", language_ids=["1000"])
    created_present(client, "campaign_criterion")
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_a_first_schedule_window_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AdSchedule())
    _, result = preview_and_apply(engine, kind, campaign_id="11", windows=[
        {"day": "MONDAY", "start_hour": 8, "end_hour": 20}])
    created_present(client, "campaign_criterion")
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_a_negative_keyword_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AddNegativeKeywords())
    _, result = preview_and_apply(engine, kind, level="campaign", target_id="11",
                                  keywords=["free"])
    created_present(client, "campaign_criterion")
    assert reverted(engine, result.entry_id) == HEADROOM


def shared_list(client, attached):
    client.answer("FROM shared_set\n", [row(**{
        "shared_set.id": 8, "shared_set.name": "safety",
        "shared_set.type_": "NEGATIVE_KEYWORDS", "shared_set.status": "ENABLED",
    })])
    client.answer("FROM campaign_shared_set", [row(**{
        "campaign.id": int(cid), "campaign.name": f"c{cid}",
    }) for cid in attached])


def test_reverting_a_shared_negative_projects_every_attached_campaigns_headroom():
    engine, client, _ = rig()
    campaign(client)
    shared_list(client, attached=("11", "12"))
    kind = engine.add(AddToSharedNegativeList())
    _, result = preview_and_apply(engine, kind, shared_set_id="8", keywords=["free"])
    client.answer("shared_criterion.resource_name =", [row(**{
        "shared_criterion.resource_name": "x"})])
    # Both campaigns read the same 29.00 headroom in this rig.
    assert reverted(engine, result.entry_id) == 2 * HEADROOM


def test_reverting_a_linked_negative_list_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    shared_list(client, attached=())
    kind = engine.add(LinkSharedNegativeList())
    _, result = preview_and_apply(engine, kind, shared_set_id="8", campaign_id="11")
    created_present(client, "campaign_shared_set")
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_an_audience_in_targeting_mode_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    client.answer("FROM ad_group\n", [row(**{
        "ad_group.id": 22, "ad_group.name": "General",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
        "campaign.id": 11, "campaign.name": "Example campaign",
    })])
    restriction = RAW.get_type("TargetRestriction")
    restriction.targeting_dimension = RAW.enums.TargetingDimensionEnum.AUDIENCE
    restriction.bid_only = False
    client.answer("ad_group.targeting_setting", [row(**{
        "ad_group.targeting_setting.target_restrictions": [restriction],
    })])
    kind = engine.add(AudienceTargeting())
    _, result = preview_and_apply(engine, kind, ad_group_id="22", user_list_ids=["44"],
                                  mode="targeting")
    created_present(client, "ad_group_criterion")
    assert reverted(engine, result.entry_id) == HEADROOM


def test_reverting_an_observation_audience_stays_free():
    engine, client, _ = rig()
    campaign(client)
    client.answer("FROM ad_group\n", [row(**{
        "ad_group.id": 22, "ad_group.name": "General",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
        "campaign.id": 11, "campaign.name": "Example campaign",
    })])
    kind = engine.add(AudienceTargeting())
    _, result = preview_and_apply(engine, kind, ad_group_id="22", user_list_ids=["44"])
    created_present(client, "ad_group_criterion")
    assert reverted(engine, result.entry_id) == 0


def test_the_revert_row_of_a_narrowing_does_not_count_as_narrowing_again():
    """Undoing the undo re-narrows: free, as the original was."""
    engine, client, _ = rig()
    campaign(client, search_partners=True)
    kind = engine.add(CampaignNetworks())
    _, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=False)
    campaign(client, search_partners=False)
    undo = engine.revert(result.entry_id)
    from googleads_mcp import ledger

    assert "narrowing" not in (ledger.find_entry(undo.entry_id).get("detail") or {})


def legacy_negative_row(entry_id="legacy1", *, tool="apply_add_negative_keywords", detail=None):
    """A negative-keyword row as written before the engine recorded ``narrowing``."""
    from googleads_mcp import ledger
    from kindkit import CUSTOMER

    ledger.write_intent(
        entry_id=entry_id, tool=tool, customer_id=CUSTOMER,
        entity_type="negative keyword", entity_id="11:free", entity_name="free",
        field_name="created", before="(none)", after="present", projected_delta="0",
        detail=detail if detail is not None else {
            "level": "campaign", "target_id": "11", "text": "free", "match_type": "PHRASE",
            "resource": "campaign_criterion", "scope_campaigns": ["11"]},
    )
    ledger.write_outcome(entry_id=entry_id, ok=True,
                         resource_name="customers/1112223333/campaignCriteria/11~1")


def test_a_legacy_negative_row_without_the_narrowing_key_still_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AddNegativeKeywords())
    assert ("negative keyword", "created") in kind.ledger_keys
    legacy_negative_row()
    created_present(client, "campaign_criterion")
    assert reverted(engine, "legacy1") == HEADROOM


def test_a_legacy_revert_row_of_a_negative_does_not_fall_back_to_narrowing():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AddNegativeKeywords())
    legacy_negative_row("legacy2", tool="revert_change")
    from googleads_mcp import ledger

    assert kind.narrowed_for(ledger.find_entry("legacy2")) is None
