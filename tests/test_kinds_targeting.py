"""Targeting kinds: locations, languages, schedule, modifiers, networks, audiences."""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp.changes.targeting import (
    AdSchedule,
    AudienceTargeting,
    CampaignLanguages,
    CampaignLocations,
    CampaignNetworks,
    DeviceBidModifier,
    LocationBidModifier,
    parse_pct,
)
from kindkit import RAW, failed, preview_and_apply, rig, row


def campaign(client, *, search_partners=False, display=False):
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Brand - Search - EN", "campaign.status": "ENABLED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
        "campaign.network_settings.target_search_network": search_partners,
        "campaign.network_settings.target_content_network": display,
    })])


def criteria_rows(client, kind, rows):
    client.answer(f"campaign_criterion.type = '{kind}'", rows)


def test_a_first_location_target_is_flagged_as_narrowing():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignLocations())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", add=["1000"],
                                        exclude=["2000"])
    assert any("serves EVERYWHERE" in w for w in preview.warnings)
    assert Decimal(preview.projected_daily_spend_delta) == 0
    ops = client.applied[-1]["operations"]
    assert [op.create.location.geo_target_constant for op in ops] == [
        "geoTargetConstants/1000", "geoTargetConstants/2000"]
    assert [op.create.negative for op in ops] == [False, True]
    assert len(result.entry_ids) == 2


def test_another_location_widens_and_a_duplicate_blocks():
    engine, client, _ = rig()
    campaign(client)
    criteria_rows(client, "LOCATION", [row(**{
        "campaign_criterion.criterion_id": 1000,
        "campaign_criterion.location.geo_target_constant": "geoTargetConstants/1000",
    })])
    kind = engine.add(CampaignLocations())
    preview = engine.preview(kind, {"campaign_id": "11", "add": ["3000"]})
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    preview = engine.preview(kind, {"campaign_id": "11", "add": ["1000"]})
    assert failed(preview, "not_already_present")


def test_languages_are_language_constants():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignLanguages())
    preview, _ = preview_and_apply(engine, kind, campaign_id="11", language_ids=["1000"])
    assert any("ALL languages" in w for w in preview.warnings)
    op = client.applied[-1]["operations"][0]
    assert op.create.language.language_constant == "languageConstants/1000"


def test_schedule_windows_carry_their_modifier_and_are_bounded():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(AdSchedule())
    preview, _ = preview_and_apply(engine, kind, campaign_id="11", windows=[
        {"day": "MONDAY", "start_hour": 8, "end_hour": 20, "bid_modifier_pct": "+10"},
    ])
    op = client.applied[-1]["operations"][0]
    assert op.create.ad_schedule.day_of_week.name == "MONDAY"
    assert (op.create.ad_schedule.start_hour, op.create.ad_schedule.end_hour) == (8, 20)
    assert op.create.bid_modifier == pytest.approx(1.1)

    big = engine.preview(kind, {"campaign_id": "11", "windows": [
        {"day": "TUESDAY", "start_hour": 8, "end_hour": 20, "bid_modifier_pct": "+80"}]})
    assert failed(big, "max_bid_modifier_pct")


def test_a_percent_is_never_a_multiplier():
    assert parse_pct("+20") == Decimal("20")
    with pytest.raises(ValueError, match="between -100 and"):
        parse_pct("-120")
    with pytest.raises(ValueError, match="signed percentage"):
        parse_pct("lots")


def test_a_new_device_modifier_creates_and_reverts_by_removal():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(DeviceBidModifier())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", device="MOBILE",
                                        bid_modifier_pct="-20")
    assert (preview.before, preview.after) == ("(none)", "-20%")
    op = client.applied[-1]["operations"][0]
    assert op.create.device.type_.name == "MOBILE"
    assert op.create.bid_modifier == pytest.approx(0.8)

    criteria_rows(client, "DEVICE", [row(**{
        "campaign_criterion.criterion_id": 30001, "campaign_criterion.bid_modifier": 0.8,
        "campaign_criterion.device.type_": "MOBILE",
    })])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].remove == result.resource_name


def test_an_existing_device_modifier_updates_and_reverts_by_value():
    engine, client, _ = rig()
    campaign(client)
    criteria_rows(client, "DEVICE", [row(**{
        "campaign_criterion.criterion_id": 30001, "campaign_criterion.bid_modifier": 0.9,
        "campaign_criterion.device.type_": "DESKTOP",
    })])
    kind = engine.add(DeviceBidModifier())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", device="DESKTOP",
                                        bid_modifier_pct="+15")
    assert preview.before == "-10%"
    op = client.applied[-1]["operations"][0]
    assert op.update.resource_name.endswith("campaignCriteria/11~30001")
    assert list(op.update_mask.paths) == ["bid_modifier"]

    criteria_rows(client, "DEVICE", [row(**{
        "campaign_criterion.criterion_id": 30001, "campaign_criterion.bid_modifier": 1.15,
        "campaign_criterion.device.type_": "DESKTOP",
    })])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.bid_modifier == pytest.approx(0.9)


def test_a_location_modifier_refuses_an_excluded_location():
    engine, client, _ = rig()
    campaign(client)
    client.answer("campaign_criterion.criterion_id = 1000", [row(**{
        "campaign_criterion.criterion_id": 1000, "campaign_criterion.type_": "LOCATION",
        "campaign_criterion.negative": True, "campaign.name": "Brand - Search - EN",
    })])
    kind = engine.add(LocationBidModifier())
    with pytest.raises(LookupError, match="No targeted location"):
        engine.preview(kind, {"campaign_id": "11", "criterion_id": "1000",
                              "bid_modifier_pct": "+10"})


def test_turning_search_partners_on_widens_and_reverts_off():
    engine, client, _ = rig()
    campaign(client)
    kind = engine.add(CampaignNetworks())
    preview, result = preview_and_apply(engine, kind, campaign_id="11", search_partners=True)
    assert preview.before == "display_network=off, search_partners=off"
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    op = client.applied[-1]["operations"][0]
    assert op.update.network_settings.target_search_network is True

    campaign(client, search_partners=True)
    engine.revert(result.entry_id)
    op = client.applied[-1]["operations"][0]
    assert op.update.network_settings.target_search_network is False
    assert "network_settings.target_search_network" in op.update_mask.paths


def ad_group(client, *, bid_only=True):
    client.answer("FROM ad_group\n", [row(**{
        "ad_group.id": 22, "ad_group.name": "General",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
        "campaign.id": 11, "campaign.name": "Brand - Search - EN",
    })])
    restriction = RAW.get_type("TargetRestriction")
    restriction.targeting_dimension = RAW.enums.TargetingDimensionEnum.AUDIENCE
    restriction.bid_only = bid_only
    client.answer("ad_group.targeting_setting", [row(**{
        "ad_group.targeting_setting.target_restrictions": [restriction],
    })])


def test_audiences_are_added_only_in_the_ad_groups_own_mode():
    engine, client, _ = rig()
    ad_group(client, bid_only=True)
    kind = engine.add(AudienceTargeting())
    _, result = preview_and_apply(engine, kind, ad_group_id="22", user_list_ids=["44"])
    op = client.applied[-1]["operations"][0]
    assert op.create.user_list.user_list == "customers/1112223333/userLists/44"

    preview = engine.preview(kind, {"ad_group_id": "22", "user_list_ids": ["45"],
                                    "mode": "targeting"})
    assert failed(preview, "mode_matches")


# ---------------------------------------------------------------------------
# A bid_modifier of 0.0 is -100%: EXCLUDED, not "no modifier"
# ---------------------------------------------------------------------------


def test_pct_from_modifier_tells_unset_from_excluded():
    from googleads_reporting.write.targeting import modifier_of, pct_from_modifier

    unset = row(**{"campaign_criterion.criterion_id": 1}).campaign_criterion
    excluded = row(**{"campaign_criterion.criterion_id": 1,
                      "campaign_criterion.bid_modifier": 0.0}).campaign_criterion
    assert modifier_of(unset) is None
    assert pct_from_modifier(modifier_of(unset)) == Decimal("0")
    assert modifier_of(excluded) == 0.0
    assert pct_from_modifier(modifier_of(excluded)) == Decimal("-100")


def test_an_excluded_device_is_an_unexclusion_and_reverts_to_the_exclusion():
    engine, client, _ = rig()
    campaign(client)
    criteria_rows(client, "DEVICE", [row(**{
        "campaign_criterion.criterion_id": 30002, "campaign_criterion.bid_modifier": 0.0,
        "campaign_criterion.device.type_": "TABLET",
    })])
    kind = engine.add(DeviceBidModifier())
    preview = engine.preview(kind, {"campaign_id": "11", "device": "TABLET",
                                    "bid_modifier_pct": "-50"})
    assert preview.before == "-100% (excluded)"
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    assert any("resumes serving" in w for w in preview.warnings)
    result = engine.apply(kind, preview.preview_token)

    criteria_rows(client, "DEVICE", [row(**{
        "campaign_criterion.criterion_id": 30002, "campaign_criterion.bid_modifier": 0.5,
        "campaign_criterion.device.type_": "TABLET",
    })])
    engine.revert(result.entry_id)
    op = client.applied[-1]["operations"][0]
    assert "bid_modifier" in op.update and op.update.bid_modifier == 0.0
    assert list(op.update_mask.paths) == ["bid_modifier"]


def excluded_location(client, modifier=0.0):
    client.answer("campaign_criterion.criterion_id = 1000", [row(**{
        "campaign_criterion.criterion_id": 1000, "campaign_criterion.type_": "LOCATION",
        "campaign_criterion.negative": False, "campaign_criterion.bid_modifier": modifier,
        "campaign_criterion.location.geo_target_constant": "geoTargetConstants/1000",
        "campaign.name": "Brand - Search - EN",
    })])


def test_an_excluded_location_is_an_unexclusion_and_reverts_to_the_exclusion():
    engine, client, _ = rig()
    campaign(client)
    excluded_location(client)
    kind = engine.add(LocationBidModifier())
    preview = engine.preview(kind, {"campaign_id": "11", "criterion_id": "1000",
                                    "bid_modifier_pct": "-50"})
    assert preview.before == "-100% (excluded)"
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    assert any("resumes serving" in w for w in preview.warnings)
    result = engine.apply(kind, preview.preview_token)

    excluded_location(client, modifier=0.5)
    engine.revert(result.entry_id)
    op = client.applied[-1]["operations"][0]
    assert "bid_modifier" in op.update and op.update.bid_modifier == 0.0


# ---------------------------------------------------------------------------
# A modifier change is judged by how the MULTIPLIER moves, not by its sign
# ---------------------------------------------------------------------------


def device_modifier(client, modifier: float, device: str = "DESKTOP"):
    criteria_rows(client, "DEVICE", [row(**{
        "campaign_criterion.criterion_id": 30001, "campaign_criterion.bid_modifier": modifier,
        "campaign_criterion.device.type_": device,
    })])


def test_a_smaller_cut_is_an_increase_and_projects_headroom():
    engine, client, _ = rig()
    campaign(client)
    device_modifier(client, 0.5)
    kind = engine.add(DeviceBidModifier())
    preview = engine.preview(kind, {"campaign_id": "11", "device": "DESKTOP",
                                    "bid_modifier_pct": "-30"})
    assert preview.before == "-50%"
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")


def test_a_smaller_raise_is_a_cut_and_projects_nothing():
    engine, client, _ = rig()
    campaign(client)
    device_modifier(client, 1.4)
    kind = engine.add(DeviceBidModifier())
    preview = engine.preview(kind, {"campaign_id": "11", "device": "DESKTOP",
                                    "bid_modifier_pct": "+10"})
    assert Decimal(preview.projected_daily_spend_delta) == 0


def test_the_relative_increase_in_multiplier_is_bounded_too():
    # -50% -> 0% doubles the multiplier (0.5 -> 1.0): a +100% increase, even
    # though both values sit well inside +/-50%.
    engine, client, _ = rig()
    campaign(client)
    device_modifier(client, 0.5)
    kind = engine.add(DeviceBidModifier())
    preview = engine.preview(kind, {"campaign_id": "11", "device": "DESKTOP",
                                    "bid_modifier_pct": "0"})
    assert failed(preview, "max_bid_modifier_increase_pct")
    assert not failed(preview, "max_bid_modifier_pct")
    ok = engine.preview(kind, {"campaign_id": "11", "device": "DESKTOP",
                               "bid_modifier_pct": "-30"})
    assert not ok.blocked, ok.checks


def test_a_location_modifier_is_judged_by_the_multiplier_move():
    engine, client, _ = rig()
    campaign(client)
    excluded_location(client, modifier=0.5)
    kind = engine.add(LocationBidModifier())
    preview = engine.preview(kind, {"campaign_id": "11", "criterion_id": "1000",
                                    "bid_modifier_pct": "-10"})
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    assert failed(preview, "max_bid_modifier_increase_pct")
