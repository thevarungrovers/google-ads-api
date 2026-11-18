"""Campaign targeting: locations, languages, ad schedule, device and location
bid modifiers, networks, and audience segments on ad groups.

Every addition is a NEW criterion (reverted by removing exactly that one, via
criteria.plan_remove_created). Every modifier or network change is an UPDATE
of a value that already exists (reverted by setting the old value back).
Nothing here removes a criterion somebody else created.

Bid modifiers are multipliers on the wire -- 1.2 is +20%, 0.8 is -20%, 0 is
"never show here" -- and percentages everywhere else, because "+20%" is what a
person approves.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from .client import MutatingGoogleAdsClient, MutationError
from .criteria import AD_GROUP_CRITERION, CAMPAIGN_CRITERION, ad_group_path, campaign_path, customer
from .plan import FieldChange, PlannedChange

DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
DEVICES = ("MOBILE", "DESKTOP", "TABLET")


def modifier_from_pct(pct: Decimal) -> float:
    """+20 -> 1.2. -100 -> 0.0 (exclude)."""
    return float((Decimal("100") + pct) / Decimal("100"))


def pct_from_modifier(modifier: float | None) -> Decimal:
    """1.2 -> +20. An unset modifier (0 on the wire for 'not set') is 0%.

    Google reports an absent modifier as 0.0, which is ALSO how "-100%" is
    written. Criteria returned with no modifier are treated as unset (0%);
    this module never writes 0.0 itself unless asked for -100.
    """
    if not modifier:
        return Decimal("0")
    return (Decimal(str(modifier)) * 100 - 100).quantize(Decimal("1"))


def render_pct(pct: Decimal) -> str:
    return f"{pct:+.0f}%"


def criterion_spec_label(spec: dict[str, Any]) -> str:
    kind = spec["kind"]
    if kind == "location":
        return f"{'exclude' if spec.get('negative') else 'target'} location {spec['geo_target_id']}"
    if kind == "language":
        return f"language {spec['language_id']}"
    if kind == "schedule":
        label = f"{spec['day']} {spec['start_hour']:02d}:00-{spec['end_hour']:02d}:00"
        if spec.get("bid_modifier_pct") is not None:
            label += f" {render_pct(Decimal(str(spec['bid_modifier_pct'])))}"
        return label
    if kind == "device":
        return f"device {spec['device']} {render_pct(Decimal(str(spec['bid_modifier_pct'])))}"
    raise MutationError(f"unknown criterion kind {kind!r}")


def plan_add_campaign_criteria(
    client: MutatingGoogleAdsClient, *, campaign_id: str, specs: Sequence[dict[str, Any]]
) -> PlannedChange:
    """New campaign criteria, one operation per spec, in spec order.

    A spec is a plain dict so it can ride in a preview token:
      {"kind": "location", "geo_target_id": "1000", "negative": False}
      {"kind": "language", "language_id": "1000"}
      {"kind": "schedule", "day": "MONDAY", "start_hour": 8, "end_hour": 20,
       "bid_modifier_pct": "10"}
      {"kind": "device", "device": "MOBILE", "bid_modifier_pct": "-20"}
    """
    service, method = CAMPAIGN_CRITERION
    operations = []
    for spec in specs:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path(client, campaign_id)
        kind = spec["kind"]
        if kind == "location":
            criterion.location.geo_target_constant = f"geoTargetConstants/{int(spec['geo_target_id'])}"
            if spec.get("negative"):
                criterion.negative = True
        elif kind == "language":
            criterion.language.language_constant = f"languageConstants/{int(spec['language_id'])}"
        elif kind == "schedule":
            if spec["day"] not in DAYS:
                raise MutationError(f"day must be one of {', '.join(DAYS)}")
            if not 0 <= int(spec["start_hour"]) < int(spec["end_hour"]) <= 24:
                raise MutationError("a schedule window needs 0 <= start_hour < end_hour <= 24")
            criterion.ad_schedule.day_of_week = getattr(client.enums.DayOfWeekEnum, spec["day"])
            criterion.ad_schedule.start_hour = int(spec["start_hour"])
            criterion.ad_schedule.end_hour = int(spec["end_hour"])
            criterion.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
            criterion.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
        elif kind == "device":
            if spec["device"] not in DEVICES:
                raise MutationError(f"device must be one of {', '.join(DEVICES)}")
            criterion.device.type_ = getattr(client.enums.DeviceEnum, spec["device"])
        else:
            raise MutationError(f"unknown criterion kind {kind!r}")
        if spec.get("bid_modifier_pct") is not None:
            criterion.bid_modifier = modifier_from_pct(Decimal(str(spec["bid_modifier_pct"])))
        operations.append(operation)
    return PlannedChange(
        action="create", entity="campaign criterion", label=f"{len(specs)} on campaign {campaign_id}",
        service=service, method=method, operations=operations,
        changes=[FieldChange(criterion_spec_label(s), None, "added") for s in specs],
    )


def plan_set_campaign_criterion_modifier(
    client: MutatingGoogleAdsClient, *, campaign_id: str, criterion_id: str, pct: Decimal
) -> PlannedChange:
    service, method = CAMPAIGN_CRITERION
    operation = client.get_type("CampaignCriterionOperation")
    operation.update.resource_name = (
        f"customers/{customer(client)}/campaignCriteria/{int(campaign_id)}~{int(criterion_id)}"
    )
    operation.update.bid_modifier = modifier_from_pct(pct)
    # Written by hand: 0.0 (a -100% modifier) is the proto default and would
    # drop out of a derived mask, silently sending no change at all.
    operation.update_mask.paths.append("bid_modifier")
    return PlannedChange(
        action="update", entity="campaign criterion", label=f"{campaign_id}~{criterion_id}",
        service=service, method=method, operations=[operation],
        changes=[FieldChange("bid_modifier", None, render_pct(pct))],
    )


def plan_set_networks(
    client: MutatingGoogleAdsClient, *, campaign_id: str, search_partners: bool | None,
    display_network: bool | None,
) -> PlannedChange:
    if search_partners is None and display_network is None:
        raise MutationError("Nothing to change: pass search_partners and/or display_network.")
    operation = client.get_type("CampaignOperation")
    operation.update.resource_name = campaign_path(client, campaign_id)
    changes = []
    # Explicit paths: False is the proto default, so turning a network OFF
    # would vanish from a mask derived from the payload.
    if search_partners is not None:
        operation.update.network_settings.target_search_network = search_partners
        operation.update_mask.paths.append("network_settings.target_search_network")
        changes.append(FieldChange("search_partners", None, search_partners))
    if display_network is not None:
        operation.update.network_settings.target_content_network = display_network
        operation.update_mask.paths.append("network_settings.target_content_network")
        changes.append(FieldChange("display_network", None, display_network))
    return PlannedChange(
        action="update", entity="campaign", label=f"id {campaign_id}",
        service="CampaignService", method="mutate_campaigns", operations=[operation],
        changes=changes,
    )


def plan_add_ad_group_audiences(
    client: MutatingGoogleAdsClient, *, ad_group_id: str, user_list_ids: Sequence[str]
) -> PlannedChange:
    service, method = AD_GROUP_CRITERION
    operations = []
    for user_list_id in user_list_ids:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = ad_group_path(client, ad_group_id)
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.user_list.user_list = f"customers/{customer(client)}/userLists/{int(user_list_id)}"
        operations.append(operation)
    return PlannedChange(
        action="create", entity="audience", label=f"{len(operations)} on ad group {ad_group_id}",
        service=service, method=method, operations=operations,
        changes=[FieldChange(f"user list {u}", None, "added") for u in user_list_ids],
    )
