"""Reads and building blocks several change kinds share."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from googleads_mcp.changes.base import NOTHING, ChangeKind, money
from googleads_reporting.query import Query, last_n_days, quote_literal
from googleads_reporting.write import campaigns, criteria


def one(ctx, select: tuple[str, ...], resource: str, where: list[str]):
    """The single proto row matching ``where``, or None. Via the WRITE client.

    The write client's query() returns raw protos -- money still in micros --
    which is what staleness checks want: they compare against what the plan
    will send, not against a formatted string.
    """
    rows = ctx.client.query(
        Query(select=select, from_resource=resource, where=tuple(where)).to_gaql()
    )
    return rows[0] if rows else None


def campaign_headroom(ctx, campaign_id: str) -> tuple[Decimal, str]:
    """How much MORE the campaign could spend per day than it does now.

    The honest upper bound for anything that widens delivery inside a campaign
    -- a new keyword, an enabled keyword, a new location: none of them can
    take the campaign past its daily budget, and the last 7 days say how much
    of that budget it already uses. Zero, with a reason, when the campaign is
    not serving at all.
    """
    row = campaigns.find(ctx.client, campaign_id=str(campaign_id))
    status = row.campaign.status.name
    if status != "ENABLED":
        return Decimal("0"), f"the campaign is {status}, so this adds no spend until it is enabled"
    budget = money(row.campaign_budget.amount_micros)
    spent = Decimal(ctx.metrics_7d(f"campaign.id = {int(campaign_id)}")["cost"]) / 7
    headroom = max(budget - spent, Decimal("0"))
    return headroom, (
        f"upper bound: the campaign's unused daily budget ({budget:.2f} budget "
        f"- {spent:.2f}/day average over 7 days)"
    )


def resource_status(ctx, resource: str, resource_name: str, status_field: str | None) -> str:
    """A created resource's status today, or NOTHING if it is gone."""
    select = (f"{resource}.resource_name",) + ((f"{resource}.{status_field}",) if status_field else ())
    found = one(ctx, select, resource,
                [f"{resource}.resource_name = {quote_literal(resource_name)}"])
    if found is None:
        return NOTHING
    if status_field is None:
        return "present"
    value = getattr(getattr(found, resource), status_field)
    return getattr(value, "name", str(value))


class CreatesRemovable(ChangeKind):
    """A kind that CREATES criteria/links with no paused state.

    Its ledger rows have field "created", before NOTHING and after the status
    the thing was created with (or "present"). Reverting one removes exactly
    the resource Google returned for it -- see criteria.plan_remove_created --
    and refuses a row that has no resource name recorded.
    """

    #: GAQL resource and status field used to read a created row back.
    resource: str = ""
    status_field: str | None = None

    def resource_for(self, entry: dict[str, Any]) -> str:
        """The GAQL resource a ledger row's created thing lives in."""
        return self.resource

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        if not entry.get("resource_name"):
            return None
        return resource_status(
            ctx, self.resource_for(entry), entry["resource_name"], self.status_field
        )

    def inverse(self, ctx, entry: dict[str, Any]):
        if not entry.get("resource_name"):
            raise ValueError(
                f"Entry {entry['entry_id']} has no resource name recorded, so it cannot "
                "be traced to exactly one created criterion. Not removing anything."
            )
        return criteria.plan_remove_created(ctx.client, resource_names=[entry["resource_name"]])
