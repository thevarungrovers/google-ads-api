"""Ad group mutations."""

from __future__ import annotations

from ..query import Query
from .client import MutatingGoogleAdsClient, MutationError
from .lookup import Candidate, NotFound, resolve, search
from .plan import FieldChange, PlannedChange, micros, set_update_mask

SERVICE = "AdGroupService"
METHOD = "mutate_ad_groups"

SETTABLE_STATUSES = ("ENABLED", "PAUSED", "REMOVED")

_FIELDS = (
    "ad_group.id", "ad_group.name", "ad_group.status",
    "ad_group.cpc_bid_micros", "ad_group.resource_name",
    "campaign.id", "campaign.name",
)


def find(
    client: MutatingGoogleAdsClient,
    *,
    ad_group_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
):
    if (ad_group_id is None) == (name is None):
        raise MutationError("Give exactly one of ad_group_id or name.")
    if ad_group_id is not None:
        rows = client.query(
            Query(
                select=_FIELDS,
                from_resource="ad_group",
                where=(f"ad_group.id = {int(ad_group_id)}",),
            ).to_gaql(),
            customer_id=customer_id,
        )
        if not rows:
            raise NotFound(f"No ad group with id {ad_group_id}.")
        return rows[0]

    # Ad group names are only unique WITHIN a campaign, so an account-wide name
    # match is routinely ambiguous -- the candidate list names the campaign so
    # the duplicates can actually be told apart.
    return resolve(
        client, entity="ad group", id_field="ad_group.id",
        name_field="ad_group.name", resource="ad_group", select=_FIELDS,
        needle=name, customer_id=customer_id, to_candidate=as_candidate,
    )


def as_candidate(row) -> Candidate:
    return Candidate(
        id=str(row.ad_group.id),
        name=row.ad_group.name,
        status=row.ad_group.status.name,
        context=f"in {row.campaign.name}",
    )


def search_ad_groups(client, needle=None, *, customer_id=None, limit=50):
    """Ad groups whose name contains ``needle``. Read-only."""
    return search(
        client, resource="ad_group", select=_FIELDS,
        name_field="ad_group.name", needle=needle,
        customer_id=customer_id, limit=limit,
    )


def plan_set_status(
    client: MutatingGoogleAdsClient,
    *,
    status: str,
    ad_group_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
) -> PlannedChange:
    wanted = status.upper()
    if wanted not in SETTABLE_STATUSES:
        raise MutationError(
            f"{status!r} is not a settable ad group status. "
            f"Choose one of: {', '.join(SETTABLE_STATUSES)}."
        )
    row = find(client, ad_group_id=ad_group_id, name=name, customer_id=customer_id)
    before = row.ad_group.status.name
    if before == wanted:
        raise MutationError(
            f"Ad group {row.ad_group.name!r} is already {wanted}; nothing to do."
        )

    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = row.ad_group.resource_name
    ad_group.status = getattr(client.enums.AdGroupStatusEnum, wanted)
    set_update_mask(client, operation)

    warnings = []
    if wanted == "REMOVED":
        warnings.append("REMOVED is permanent; use PAUSED if you may want it back.")

    return PlannedChange(
        action="update", entity="ad group",
        label=f"{row.ad_group.name} (id {row.ad_group.id}, in {row.campaign.name})",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("status", before, wanted)],
        warnings=warnings,
    )


def plan_set_cpc_bid(
    client: MutatingGoogleAdsClient,
    *,
    amount: float,
    ad_group_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
) -> PlannedChange:
    """Set the ad group's default max CPC, in account currency."""
    row = find(client, ad_group_id=ad_group_id, name=name, customer_id=customer_id)
    before = (row.ad_group.cpc_bid_micros or 0) / 1_000_000

    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = row.ad_group.resource_name
    ad_group.cpc_bid_micros = micros(amount)
    set_update_mask(client, operation)

    warnings = []
    if before and amount > before * 10:
        warnings.append(
            f"that is {amount / before:,.0f}x the current bid; check it is not "
            "micros entered by mistake"
        )

    return PlannedChange(
        action="update", entity="ad group",
        label=f"{row.ad_group.name} (id {row.ad_group.id})",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("cpc_bid", round(before, 2), round(amount, 2))],
        warnings=warnings,
    )


def plan_create(
    client: MutatingGoogleAdsClient,
    *,
    name: str,
    campaign_resource_name: str,
    cpc_bid: float | None = None,
    status: str = "PAUSED",
) -> PlannedChange:
    """A new ad group. Paused by default, for the same reason campaigns are."""
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create
    ad_group.name = name
    ad_group.campaign = campaign_resource_name
    ad_group.status = getattr(client.enums.AdGroupStatusEnum, status.upper())
    changes = [
        FieldChange("name", None, name),
        FieldChange("campaign", None, campaign_resource_name),
        FieldChange("status", None, status.upper()),
    ]
    if cpc_bid is not None:
        ad_group.cpc_bid_micros = micros(cpc_bid)
        changes.append(FieldChange("cpc_bid", None, round(cpc_bid, 2)))

    return PlannedChange(
        action="create", entity="ad group", label=name,
        service=SERVICE, method=METHOD, operations=[operation], changes=changes,
    )
