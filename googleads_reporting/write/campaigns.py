"""Campaign mutations."""

from __future__ import annotations

from ..query import Query
from .client import MutatingGoogleAdsClient, MutationError
from .lookup import Candidate, NotFound, resolve, search
from .plan import FieldChange, PlannedChange, set_update_mask

SERVICE = "CampaignService"
METHOD = "mutate_campaigns"

SETTABLE_STATUSES = ("ENABLED", "PAUSED", "REMOVED")

_FIELDS = (
    "campaign.id", "campaign.name", "campaign.status",
    "campaign.advertising_channel_type", "campaign.resource_name",
    "campaign_budget.id", "campaign_budget.amount_micros",
)


def find(
    client: MutatingGoogleAdsClient,
    *,
    campaign_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
):
    """Fetch one campaign by id or exact name. Raises if not exactly one."""
    if (campaign_id is None) == (name is None):
        raise MutationError("Give exactly one of campaign_id or name.")

    if campaign_id is not None:
        rows = client.query(
            Query(
                select=_FIELDS,
                from_resource="campaign",
                where=(f"campaign.id = {int(campaign_id)}",),
            ).to_gaql(),
            customer_id=customer_id,
        )
        if not rows:
            raise NotFound(f"No campaign with id {campaign_id}.")
        return rows[0]

    return resolve(
        client, entity="campaign", id_field="campaign.id",
        name_field="campaign.name", resource="campaign", select=_FIELDS,
        needle=name, customer_id=customer_id, to_candidate=as_candidate,
    )


def as_candidate(row) -> Candidate:
    return Candidate(
        id=str(row.campaign.id),
        name=row.campaign.name,
        status=row.campaign.status.name,
        context=row.campaign.advertising_channel_type.name,
    )


def search_campaigns(client, needle=None, *, customer_id=None, limit=50):
    """Campaigns whose name contains ``needle``. Read-only."""
    return search(
        client, resource="campaign", select=_FIELDS,
        name_field="campaign.name", needle=needle,
        customer_id=customer_id, limit=limit,
    )


def plan_set_status(
    client: MutatingGoogleAdsClient,
    *,
    status: str,
    campaign_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
) -> PlannedChange:
    """Pause, enable or remove a campaign."""
    wanted = status.upper()
    if wanted not in SETTABLE_STATUSES:
        raise MutationError(
            f"{status!r} is not a settable campaign status. "
            f"Choose one of: {', '.join(SETTABLE_STATUSES)}."
        )

    row = find(client, campaign_id=campaign_id, name=name, customer_id=customer_id)
    before = row.campaign.status.name

    if before == wanted:
        raise MutationError(
            f"Campaign {row.campaign.name!r} is already {wanted}; nothing to do."
        )

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = row.campaign.resource_name
    campaign.status = getattr(client.enums.CampaignStatusEnum, wanted)
    set_update_mask(client, operation)

    warnings = []
    if wanted == "REMOVED":
        warnings.append(
            "REMOVED is permanent in Google Ads -- a removed campaign cannot be "
            "re-enabled, only recreated. Use PAUSED if you may want it back."
        )

    return PlannedChange(
        action="update", entity="campaign",
        label=f"{row.campaign.name} (id {row.campaign.id})",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("status", before, wanted)],
        warnings=warnings,
    )


def plan_rename(
    client: MutatingGoogleAdsClient,
    *,
    new_name: str,
    campaign_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
) -> PlannedChange:
    row = find(client, campaign_id=campaign_id, name=name, customer_id=customer_id)
    if row.campaign.name == new_name:
        raise MutationError(f"Campaign is already named {new_name!r}.")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = row.campaign.resource_name
    campaign.name = new_name
    set_update_mask(client, operation)

    return PlannedChange(
        action="update", entity="campaign", label=f"id {row.campaign.id}",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("name", row.campaign.name, new_name)],
    )


def plan_set_budget(
    client: MutatingGoogleAdsClient,
    *,
    amount: float,
    campaign_id: str | None = None,
    name: str | None = None,
    customer_id: str | None = None,
    override_guardrail: bool = False,
) -> PlannedChange:
    """Set the daily budget of the campaign's budget, found via the campaign.

    In the Google Ads data model a budget is a separate entity that a campaign
    POINTS AT -- it is not a field on the campaign. People do not think that
    way: they think "this campaign's budget". So this resolves the campaign,
    follows it to its budget, and changes that, which means nobody has to go
    hunting for a budget id the UI never shows them.

    The indirection is real, though, not just plumbing: one budget can be
    shared by several campaigns, and changing it then changes all of them. That
    is why the plan warns instead of hiding it.
    """
    from . import budgets

    row = find(client, campaign_id=campaign_id, name=name, customer_id=customer_id)
    plan = budgets.plan_set_amount(
        client,
        str(row.campaign_budget.id),
        amount=amount,
        customer_id=customer_id,
        override_guardrail=override_guardrail,
    )
    # Relabel in the terms the request was made in.
    plan.label = (
        f"{row.campaign.name} (campaign {row.campaign.id}, "
        f"budget {row.campaign_budget.id})"
    )
    return plan


def plan_create(
    client: MutatingGoogleAdsClient,
    *,
    name: str,
    budget_resource_name: str,
    channel: str = "SEARCH",
    status: str = "PAUSED",
    start_date: str | None = None,
    end_date: str | None = None,
) -> PlannedChange:
    """A new campaign.

    Defaults to PAUSED on purpose: a campaign created ENABLED begins spending
    the moment it is accepted, before anyone has checked its targeting, bids or
    ads. Starting paused makes going live a separate, deliberate act.
    """
    operation = client.get_type("CampaignOperation")
    campaign = operation.create
    campaign.name = name
    campaign.status = getattr(client.enums.CampaignStatusEnum, status.upper())
    campaign.advertising_channel_type = getattr(
        client.enums.AdvertisingChannelTypeEnum, channel.upper()
    )
    campaign.campaign_budget = budget_resource_name
    # Manual CPC with no bid set is the least opinionated starting point; a
    # smart bidding strategy needs conversion history this campaign has none of.
    campaign.manual_cpc.enhanced_cpc_enabled = False
    if start_date:
        campaign.start_date = start_date.replace("-", "")
    if end_date:
        campaign.end_date = end_date.replace("-", "")

    changes = [
        FieldChange("name", None, name),
        FieldChange("status", None, status.upper()),
        FieldChange("channel", None, channel.upper()),
        FieldChange("budget", None, budget_resource_name),
    ]
    if start_date:
        changes.append(FieldChange("start_date", None, start_date))
    if end_date:
        changes.append(FieldChange("end_date", None, end_date))

    warnings = []
    if status.upper() == "ENABLED":
        warnings.append(
            "creating this campaign ENABLED means it can begin spending "
            "immediately, before its targeting and ads have been reviewed"
        )

    return PlannedChange(
        action="create", entity="campaign", label=name,
        service=SERVICE, method=METHOD, operations=[operation],
        changes=changes, warnings=warnings,
    )
