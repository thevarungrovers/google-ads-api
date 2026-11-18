"""Ad group status and ad group CPC bid."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from googleads_mcp.changes.base import (
    ChangeKind,
    Proposal,
    amount_of,
    money,
    parse_decimal,
    with_currency,
)
from googleads_mcp.changes.campaigns import wanted_status
from googleads_mcp.guardrails import check_ceiling, check_currency, check_pct_increase
from googleads_mcp.previews import project_bid_change, project_pause
from googleads_reporting.write import adgroups


class AdGroupStatus(ChangeKind):
    name = "ad_group_status"
    preview_title = "Preview pausing or enabling an ad group"
    apply_title = "Apply an ad group status change (AFFECTS DELIVERY)"
    ledger_keys = (("ad group", "status"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        new_status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling an ad group would do. Writes nothing."""
        wanted = wanted_status(new_status)
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        before = row.ad_group.status.name
        metrics = ctx.metrics_7d(f"ad_group.id = {int(ad_group_id)}")
        projected = (
            project_pause(Decimal(metrics["cost"]))
            if wanted == "PAUSED"
            else Decimal(metrics["cost"]) / 7
        )
        return Proposal(
            entity_type="ad group",
            entity_id=str(row.ad_group.id),
            entity_name=row.ad_group.name,
            path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="status",
            before=before,
            after=wanted,
            checks=[],
            projected=projected,
            metrics=metrics,
            campaign_id=str(row.campaign.id),
            warnings=["already " + wanted] if before == wanted else [],
            payload={"ad_group_id": str(row.ad_group.id), "name": row.ad_group.name},
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return adgroups.find(ctx.client, ad_group_id=payload["ad_group_id"]).ad_group.status.name

    def build(self, ctx, payload: dict[str, Any]):
        return adgroups.plan_set_status(
            ctx.client, ad_group_id=payload["ad_group_id"], status=payload["after"]
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return adgroups.find(ctx.client, ad_group_id=entry["entity_id"]).ad_group.status.name

    def inverse(self, ctx, entry: dict[str, Any]):
        return adgroups.plan_set_status(
            ctx.client, ad_group_id=entry["entity_id"], status=entry["before"]
        )


class AdGroupCpcBid(ChangeKind):
    name = "ad_group_cpc_bid"
    preview_title = "Preview an ad group CPC bid change"
    apply_title = "Apply an ad group CPC bid change (SPENDS MONEY)"
    ledger_keys = (("ad group", "cpc_bid"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        new_cpc_bid: Annotated[
            str, Field(description='Decimal string, e.g. "0.75". Never micros.')
        ],
    ) -> Proposal:
        """Show what changing an ad group's max CPC would do. Writes nothing."""
        after = parse_decimal(new_cpc_bid, "new_cpc_bid")
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        before = money(row.ad_group.cpc_bid_micros)
        currency = ctx.currency_of()
        metrics = ctx.metrics_7d(f"ad_group.id = {int(ad_group_id)}")
        projected = project_bid_change(before, after, int(metrics["clicks"]))
        limits = ctx.limits
        checks = [
            check_currency(limits, currency),
            check_ceiling("max_cpc_bid", after, limits.max_cpc_bid, currency),
            check_pct_increase(
                "max_bid_increase_pct", before, after, limits.max_bid_increase_pct
            ),
        ]
        return Proposal(
            entity_type="ad group",
            entity_id=str(row.ad_group.id),
            entity_name=row.ad_group.name,
            path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="cpc_bid",
            before=with_currency(before, currency),
            after=with_currency(after, currency),
            checks=checks,
            projected=projected,
            metrics=metrics,
            campaign_id=str(row.campaign.id),
            payload={
                "ad_group_id": str(row.ad_group.id),
                "after_amount": str(after),
                "name": row.ad_group.name,
            },
            note=(
                "projected_daily_spend_delta assumes the same click volume at "
                "the new bid. That overstates it when the bid falls and "
                "understates it when a higher bid wins more auctions -- it is a "
                "bound to reason with, not a forecast."
            ),
        )

    def _rendered(self, ctx, ad_group_id: str) -> str:
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        return with_currency(money(row.ad_group.cpc_bid_micros), ctx.currency_of())

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self._rendered(ctx, payload["ad_group_id"])

    def build(self, ctx, payload: dict[str, Any]):
        return adgroups.plan_set_cpc_bid(
            ctx.client, ad_group_id=payload["ad_group_id"],
            amount=float(payload["after_amount"]),
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return self._rendered(ctx, entry["entity_id"])

    def inverse(self, ctx, entry: dict[str, Any]):
        return adgroups.plan_set_cpc_bid(
            ctx.client, ad_group_id=entry["entity_id"],
            amount=float(amount_of(entry["before"])),
        )
