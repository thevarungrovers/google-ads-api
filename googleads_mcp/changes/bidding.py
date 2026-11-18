"""Bidding strategy and shared budgets: the two levers that move a whole
campaign's spend -- or several campaigns' -- at once."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from googleads_mcp.changes.base import (
    ChangeKind,
    LedgerItem,
    Proposal,
    amount_of,
    money,
    parse_decimal,
    with_currency,
)
from googleads_mcp.changes.common import campaign_headroom, one
from googleads_mcp.guardrails import (
    Check,
    check_abs_pct_change,
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
    check_pct_increase,
)
from googleads_mcp.previews import pct_change, project_budget_change
from googleads_reporting.query import Query
from googleads_reporting.write import bidding, budgets
from googleads_reporting.write.bidding import Strategy

_CAMPAIGN_BIDDING = (
    "campaign.id", "campaign.name", "campaign.status", "campaign.bidding_strategy_type",
    "campaign.bidding_strategy", "campaign.manual_cpc.enhanced_cpc_enabled",
    "campaign.target_cpa.target_cpa_micros", "campaign.maximize_conversions.target_cpa_micros",
    "campaign.target_roas.target_roas", "campaign.maximize_conversion_value.target_roas",
    "campaign.target_spend.cpc_bid_ceiling_micros",
)


def _campaign(ctx, campaign_id: str):
    found = one(ctx, _CAMPAIGN_BIDDING, "campaign", [f"campaign.id = {int(campaign_id)}"])
    if found is None:
        raise LookupError(f"No campaign with id {campaign_id}.")
    return found


class BiddingStrategy(ChangeKind):
    name = "bidding_strategy"
    preview_title = "Preview a campaign bidding strategy or target change"
    apply_title = "Apply a bidding strategy change (SPENDS MONEY, RESETS LEARNING)"
    ledger_keys = (("campaign", "bidding_strategy"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        strategy: Annotated[
            Literal["MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS",
                    "MAXIMIZE_CONVERSION_VALUE"],
            Field(description="The strategy to bid with"),
        ],
        target_cpa: Annotated[
            str | None, Field(description='MAXIMIZE_CONVERSIONS only, decimal string e.g. "25.00"')
        ] = None,
        target_roas: Annotated[
            str | None,
            Field(description='MAXIMIZE_CONVERSION_VALUE only, a RATIO: "4.0" means 400%'),
        ] = None,
        max_cpc: Annotated[
            str | None, Field(description='MAXIMIZE_CLICKS only: ceiling on any one CPC')
        ] = None,
    ) -> Proposal:
        """Show what switching a campaign's bidding strategy, or moving its
        target CPA / ROAS, would do. Writes nothing.

        A strategy switch restarts Google's learning period (a week or two of
        unstable delivery), so the preview says so. A target may move at most
        max_target_change_pct in one change. Portfolio strategies are refused:
        they are shared and would move every campaign using them.
        """
        row = _campaign(ctx, campaign_id)
        currency = ctx.currency_of()
        before = bidding.strategy_from_row(row.campaign)
        cpa = parse_decimal(target_cpa, "target_cpa") if target_cpa else None
        roas = parse_decimal(target_roas, "target_roas", ceiling=Decimal("100")) if target_roas else None
        ceiling = parse_decimal(max_cpc, "max_cpc") if max_cpc else None
        wrong = [
            label for label, value, owner in (
                ("target_cpa", cpa, "MAXIMIZE_CONVERSIONS"),
                ("target_roas", roas, "MAXIMIZE_CONVERSION_VALUE"),
                ("max_cpc", ceiling, "MAXIMIZE_CLICKS"),
            ) if value is not None and strategy != owner
        ]
        if wrong:
            raise ValueError(f"{', '.join(wrong)} does not apply to {strategy}.")
        after = Strategy(
            strategy,
            target_cpa=float(cpa) if cpa else None,
            target_roas=float(roas) if roas else None,
            max_cpc=float(ceiling) if ceiling else None,
        )
        portfolio = row.campaign.bidding_strategy
        checks = [
            Check("not_portfolio", not portfolio,
                  "standard (campaign-level) strategy" if not portfolio
                  else f"PORTFOLIO strategy {portfolio} -- shared; change it in the UI"),
            check_currency(ctx.limits, currency),
            Check("is_a_change", after != before,
                  "differs from today" if after != before else "identical to today"),
        ]
        if cpa is not None:
            checks.append(check_ceiling("max_target_cpa", cpa, ctx.limits.max_target_cpa, currency))
        if ceiling is not None:
            checks.append(check_ceiling("max_cpc_bid", ceiling, ctx.limits.max_cpc_bid, currency))
        same = before.name == after.name
        warnings = []
        if same:
            old_target = before.target_cpa or before.target_roas
            new_target = after.target_cpa or after.target_roas
            if old_target and new_target:
                checks.append(check_abs_pct_change(
                    "max_target_change_pct", Decimal(str(old_target)), Decimal(str(new_target)),
                    ctx.limits.max_target_change_pct,
                ))
        else:
            warnings.append(
                f"switching {before.name} -> {after.name} restarts Google's learning "
                "period: expect a week or two of unstable delivery and cost"
            )
        # A tighter target can only reduce spend: a LOWER target CPA or a
        # HIGHER target ROAS. Everything else can take the campaign up to its
        # unused budget.
        restricts = same and (
            (after.target_cpa and before.target_cpa and after.target_cpa < before.target_cpa)
            or (after.target_roas and before.target_roas and after.target_roas > before.target_roas)
        )
        if restricts:
            projected, note = Decimal("0"), "A tighter target can only reduce spend."
        else:
            projected, note = campaign_headroom(ctx, campaign_id)
        return Proposal(
            entity_type="campaign", entity_id=str(row.campaign.id),
            entity_name=row.campaign.name, path=row.campaign.name,
            field_name="bidding_strategy", before=before.render(currency),
            after=after.render(currency), checks=checks, projected=projected,
            campaign_id=str(row.campaign.id), campaign_status=row.campaign.status.name,
            warnings=warnings, note=note,
            items=[LedgerItem("campaign", str(row.campaign.id), row.campaign.name,
                              "bidding_strategy", before.render(currency),
                              after.render(currency),
                              {"before": before.as_dict(), "after": after.as_dict()})],
            payload={"campaign_id": str(row.campaign.id), "strategy": after.as_dict(),
                     "before_strategy": before.as_dict()},
        )

    def _rendered(self, ctx, campaign_id: str) -> str:
        return bidding.strategy_from_row(_campaign(ctx, campaign_id).campaign).render(
            ctx.currency_of()
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self._rendered(ctx, payload["campaign_id"])

    def build(self, ctx, payload: dict[str, Any]):
        return bidding.plan_set_strategy(
            ctx.client, campaign_id=payload["campaign_id"],
            strategy=Strategy(**payload["strategy"]),
            before=Strategy(**payload["before_strategy"]),
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return self._rendered(ctx, entry["entity_id"])

    def inverse(self, ctx, entry: dict[str, Any]):
        detail = entry.get("detail") or {}
        if "before" not in detail:
            raise ValueError("This entry carries no structured before-strategy to restore.")
        return bidding.plan_set_strategy(
            ctx.client, campaign_id=entry["entity_id"],
            strategy=Strategy(**detail["before"]), before=Strategy(**detail["after"]),
        )


class SharedBudgetAmount(ChangeKind):
    name = "shared_budget_amount"
    preview_title = "Preview a SHARED budget's daily amount change"
    apply_title = "Apply a shared budget change (SPENDS MONEY ACROSS CAMPAIGNS)"
    ledger_keys = (("budget", "daily_budget"),)

    def propose(
        self,
        ctx,
        budget_id: Annotated[str, Field(description="From get_campaign's budget.budget_id")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "60.00"')
        ],
    ) -> Proposal:
        """Show what changing a SHARED budget would do. Writes nothing.

        A shared budget feeds several campaigns at once; the preview lists
        every one, each must be in the campaign scope, and the projected delta
        is the full change (the campaigns between them can spend all of it).
        For a budget only one campaign uses, use preview_campaign_daily_budget.
        """
        after = parse_decimal(new_daily_budget, "new_daily_budget")
        budget = budgets._budget_row(ctx.client, budget_id)
        if not budget.explicitly_shared:
            raise ValueError(
                f"Budget {budget_id} is not shared. Use preview_campaign_daily_budget "
                "with the campaign it belongs to."
            )
        before = money(budget.amount_micros)
        currency = ctx.currency_of()
        fed = self._campaigns(ctx, budget_id)
        checks = [
            check_currency(ctx.limits, currency),
            check_ceiling("max_daily_budget", after, ctx.limits.max_daily_budget, currency),
            check_pct_increase("max_budget_change_pct", before, after,
                               ctx.limits.max_budget_change_pct),
            *[check_campaign_in_scope(ctx.limits, cid) for cid, _, _ in fed],
        ]
        names = ", ".join(f"{n} ({s})" for _, n, s in fed) or "no campaigns"
        return Proposal(
            entity_type="budget", entity_id=str(budget_id), entity_name=budget.name,
            path=f"shared budget {budget.name} -> {names}", field_name="daily_budget",
            before=with_currency(before, currency), after=with_currency(after, currency),
            checks=checks, projected=project_budget_change(before, after),
            warnings=[f"changes the budget of EVERY campaign using it: {names}"],
            note=f"{pct_change(before, after) or 'n/a'} change, shared by {len(fed)} campaign(s).",
            payload={"budget_id": str(budget_id), "after_amount": str(after)},
        )

    @staticmethod
    def _campaigns(ctx, budget_id: str) -> list[tuple[str, str, str]]:
        rows = ctx.client.query(Query(
            select=("campaign.id", "campaign.name", "campaign.status"),
            from_resource="campaign",
            where=(f"campaign_budget.id = {int(budget_id)}", "campaign.status != 'REMOVED'"),
        ).to_gaql())
        return [(str(r.campaign.id), r.campaign.name, r.campaign.status.name) for r in rows]

    def _rendered(self, ctx, budget_id: str) -> str:
        return with_currency(money(budgets._budget_row(ctx.client, budget_id).amount_micros),
                             ctx.currency_of())

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self._rendered(ctx, payload["budget_id"])

    def build(self, ctx, payload: dict[str, Any]):
        return budgets.plan_set_amount(
            ctx.client, payload["budget_id"], amount=float(payload["after_amount"])
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return self._rendered(ctx, entry["entity_id"])

    def inverse(self, ctx, entry: dict[str, Any]):
        return budgets.plan_set_amount(
            ctx.client, entry["entity_id"], amount=float(amount_of(entry["before"]))
        )
