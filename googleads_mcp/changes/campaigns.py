"""Campaign status and campaign daily budget."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from googleads_mcp.changes.base import (
    SETTABLE_STATUSES,
    ChangeKind,
    Proposal,
    amount_of,
    money,
    parse_decimal,
    with_currency,
)
from googleads_mcp.guardrails import Check, check_ceiling, check_currency, check_pct_increase
from googleads_mcp.previews import pct_change, project_budget_change, project_pause
from googleads_reporting.write import budgets, campaigns


def wanted_status(raw: str) -> str:
    wanted = raw.upper()
    if wanted not in SETTABLE_STATUSES:
        raise ValueError(
            f"new_status must be ENABLED or PAUSED, got {raw!r}. "
            "REMOVED is not available here: it is irreversible in Google "
            "Ads, and PAUSED achieves the same thing reversibly."
        )
    return wanted


class CampaignStatus(ChangeKind):
    name = "campaign_status"
    preview_title = "Preview pausing or enabling a campaign"
    apply_title = "Apply a campaign status change (AFFECTS DELIVERY)"
    ledger_keys = (("campaign", "status"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling a campaign would do. Writes nothing."""
        wanted = wanted_status(new_status)
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        before = row.campaign.status.name
        metrics = ctx.metrics_7d(f"campaign.id = {int(campaign_id)}")

        projected = (
            project_pause(Decimal(metrics["cost"]))
            if wanted == "PAUSED"
            # Re-enabling can resume spend up to the daily budget.
            else money(row.campaign_budget.amount_micros)
        )
        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        if wanted == "ENABLED":
            warnings.append(
                "enabling resumes delivery immediately, up to the daily budget"
            )
        return Proposal(
            entity_type="campaign",
            entity_id=str(row.campaign.id),
            entity_name=row.campaign.name,
            path=row.campaign.name,
            field_name="status",
            before=before,
            after=wanted,
            checks=[],
            projected=projected,
            metrics=metrics,
            campaign_status=before,
            campaign_id=str(row.campaign.id),
            warnings=warnings,
            payload={"campaign_id": str(row.campaign.id), "name": row.campaign.name},
            note=(
                "Pausing cannot increase spend, so its projected delta is 0. "
                "Enabling is bounded by the daily budget, not by past spend."
            ),
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return campaigns.find(ctx.client, campaign_id=payload["campaign_id"]).campaign.status.name

    def build(self, ctx, payload: dict[str, Any]):
        return campaigns.plan_set_status(
            ctx.client, campaign_id=payload["campaign_id"], status=payload["after"]
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return campaigns.find(ctx.client, campaign_id=entry["entity_id"]).campaign.status.name

    def inverse(self, ctx, entry: dict[str, Any]):
        return campaigns.plan_set_status(
            ctx.client, campaign_id=entry["entity_id"], status=entry["before"]
        )


class CampaignDailyBudget(ChangeKind):
    name = "campaign_daily_budget"
    preview_title = "Preview a campaign daily budget change"
    apply_title = "Apply a campaign daily budget change (SPENDS MONEY)"
    ledger_keys = (("campaign", "daily_budget"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "25.00"')
        ],
    ) -> Proposal:
        """Show what changing a campaign's daily budget would do. Writes nothing.

        The delta is EXACT, not an estimate: a daily budget is the cap itself
        moving, not a guess about behaviour.
        """
        after = parse_decimal(new_daily_budget, "new_daily_budget")
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        before = money(row.campaign_budget.amount_micros)
        currency = ctx.currency_of()
        metrics = ctx.metrics_7d(f"campaign.id = {int(campaign_id)}")

        budget_row = budgets._budget_row(ctx.client, str(row.campaign_budget.id))
        shared = bool(budget_row.explicitly_shared)

        projected = project_budget_change(before, after)
        limits = ctx.limits
        checks = [
            check_currency(limits, currency),
            check_ceiling("max_daily_budget", after, limits.max_daily_budget, currency),
            check_pct_increase(
                "max_budget_change_pct", before, after, limits.max_budget_change_pct
            ),
            Check(
                "budget_not_shared",
                not shared,
                "exclusive to this campaign" if not shared
                else "SHARED -- would change every campaign using it; use "
                "preview_shared_budget_amount, which lists them",
            ),
        ]
        warnings = []
        if after < before:
            warnings.append(
                "Lowering a daily budget can stop delivery part-way through "
                "today if the campaign has already spent more than the new cap."
            )
        return Proposal(
            entity_type="campaign",
            entity_id=str(row.campaign.id),
            entity_name=row.campaign.name,
            path=f"{row.campaign.name} > budget {row.campaign_budget.id}",
            field_name="daily_budget",
            before=with_currency(before, currency),
            after=with_currency(after, currency),
            checks=checks,
            projected=projected,
            metrics=metrics,
            campaign_status=row.campaign.status.name,
            campaign_id=str(row.campaign.id),
            warnings=warnings,
            payload={
                "campaign_id": str(row.campaign.id),
                "budget_id": str(row.campaign_budget.id),
                "after_amount": str(after),
                "name": row.campaign.name,
            },
            note=(
                f"{pct_change(before, after) or 'n/a'} change. Compare the delta "
                "with the last 7 days' spend: if the campaign never reached the "
                "old cap, raising it may change nothing."
            ),
        )

    def _rendered(self, ctx, campaign_id: str) -> str:
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        return with_currency(money(row.campaign_budget.amount_micros), ctx.currency_of())

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self._rendered(ctx, payload["campaign_id"])

    def build(self, ctx, payload: dict[str, Any]):
        return campaigns.plan_set_budget(
            ctx.client, campaign_id=payload["campaign_id"],
            amount=float(payload["after_amount"]),
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return self._rendered(ctx, entry["entity_id"])

    def inverse(self, ctx, entry: dict[str, Any]):
        return campaigns.plan_set_budget(
            ctx.client, campaign_id=entry["entity_id"],
            amount=float(amount_of(entry["before"])),
        )
