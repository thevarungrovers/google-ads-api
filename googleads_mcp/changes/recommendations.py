"""Acting on a Google recommendation -- through the typed kind it amounts to.

Google's own ApplyRecommendation call is NOT used. It would apply whatever the
recommendation says with none of our bounds: a budget recommendation would
skip max_daily_budget and max_budget_change_pct, a keyword one would arrive
ENABLED. Instead each supported type is translated into the typed change it
is -- a budget recommendation becomes preview_campaign_daily_budget with the
recommended amount -- and goes through that kind's checks, ledger and revert.

Types with no matching kind are refused, with the reason. Google retires a
recommendation on its own once the account reflects it.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from googleads_mcp.changes.base import ChangeKind, Proposal, money
from googleads_mcp.changes.bidding import BiddingStrategy, SharedBudgetAmount
from googleads_mcp.changes.campaigns import CampaignDailyBudget
from googleads_mcp.changes.common import one
from googleads_mcp.changes.keywords import AddKeywords
from googleads_mcp.changes.targeting import CampaignNetworks
from googleads_reporting.query import quote_literal
from googleads_reporting.write import budgets, campaigns

#: Recommendation type -> what it becomes. Anything absent is refused.
SUPPORTED = (
    "CAMPAIGN_BUDGET", "KEYWORD", "MAXIMIZE_CONVERSIONS_OPT_IN",
    "MAXIMIZE_CLICKS_OPT_IN", "SEARCH_PARTNERS_OPT_IN", "DISPLAY_EXPANSION_OPT_IN",
)

_DELEGATES: dict[str, ChangeKind] = {
    kind.name: kind for kind in (
        CampaignDailyBudget(), SharedBudgetAmount(), AddKeywords(), BiddingStrategy(),
        CampaignNetworks(),
    )
}

_FIELDS = (
    "recommendation.resource_name", "recommendation.type", "recommendation.campaign",
    "recommendation.ad_group", "recommendation.dismissed",
    "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros",
    "recommendation.keyword_recommendation.keyword.text",
    "recommendation.keyword_recommendation.keyword.match_type",
)


def _tail(resource_name: str) -> str:
    return resource_name.rsplit("/", 1)[-1]


class ApplyRecommendation(ChangeKind):
    name = "recommendation"
    preview_title = "Preview acting on a Google recommendation (through its typed change)"
    apply_title = "Apply a Google recommendation (through its typed change's bounds)"
    # No keys of its own: the ledger rows are the delegate's, so revert_change
    # finds the delegate kind (budget, keyword, ...) directly.
    ledger_keys = ()

    def _route(self, ctx, recommendation_id: str) -> tuple[str, dict[str, Any], str]:
        resource = f"customers/{ctx.target_customer()}/recommendations/{recommendation_id}"
        found = one(ctx, _FIELDS, "recommendation",
                    [f"recommendation.resource_name = {quote_literal(resource)}"])
        if found is None:
            raise LookupError(f"No recommendation {recommendation_id}. See list_recommendations.")
        rec = found.recommendation
        kind = rec.type_.name
        if rec.dismissed:
            raise ValueError(f"Recommendation {recommendation_id} has been dismissed.")
        if kind not in SUPPORTED:
            raise ValueError(
                f"{kind} recommendations are not applied here: no guarded change kind "
                f"matches them. Supported: {', '.join(SUPPORTED)}. Make the change with "
                "the matching preview_* tool, or in the Google Ads UI."
            )
        campaign_id = _tail(rec.campaign) if rec.campaign else None
        if kind == "CAMPAIGN_BUDGET":
            amount = money(rec.campaign_budget_recommendation.recommended_budget_amount_micros)
            row = campaigns.find(ctx.client, campaign_id=campaign_id)
            if budgets._budget_row(ctx.client, str(row.campaign_budget.id)).explicitly_shared:
                return ("shared_budget_amount",
                        {"budget_id": str(row.campaign_budget.id),
                         "new_daily_budget": f"{amount:.2f}"}, kind)
            return ("campaign_daily_budget",
                    {"campaign_id": campaign_id, "new_daily_budget": f"{amount:.2f}"}, kind)
        if kind == "KEYWORD":
            keyword = rec.keyword_recommendation.keyword
            return ("add_keywords",
                    {"ad_group_id": _tail(rec.ad_group), "keywords": [keyword.text],
                     "match_type": keyword.match_type.name}, kind)
        if kind == "MAXIMIZE_CONVERSIONS_OPT_IN":
            return ("bidding_strategy",
                    {"campaign_id": campaign_id, "strategy": "MAXIMIZE_CONVERSIONS"}, kind)
        if kind == "MAXIMIZE_CLICKS_OPT_IN":
            return ("bidding_strategy",
                    {"campaign_id": campaign_id, "strategy": "MAXIMIZE_CLICKS"}, kind)
        if kind == "SEARCH_PARTNERS_OPT_IN":
            return "campaign_networks", {"campaign_id": campaign_id, "search_partners": True}, kind
        return "campaign_networks", {"campaign_id": campaign_id, "display_network": True}, kind

    def propose(
        self,
        ctx,
        recommendation_id: Annotated[str, Field(description="From list_recommendations")],
    ) -> Proposal:
        """Show what acting on a Google recommendation would do. Writes nothing.

        The recommendation is translated into the typed change it amounts to
        (a budget recommendation becomes a daily budget change, a keyword one
        becomes add_keywords -- created PAUSED) and checked against THAT
        change's bounds. Google's own one-click apply is never used: it would
        skip every bound here. Unsupported types are refused with the reason.
        """
        delegate_name, args, rec_type = self._route(ctx, recommendation_id)
        delegate = _DELEGATES[delegate_name]
        proposal = delegate.propose(ctx, **args)
        proposal.payload["_delegate"] = delegate_name
        proposal.payload["_recommendation"] = {"id": recommendation_id, "type": rec_type}
        proposal.note = (
            f"Google recommendation {rec_type}, applied as preview_{delegate_name}"
            f"({', '.join(f'{k}={v!r}' for k, v in args.items())}) with that tool's "
            "bounds. " + (proposal.note or "")
        ).strip()
        return proposal

    def _delegate(self, payload: dict[str, Any]) -> ChangeKind:
        return _DELEGATES[payload["_delegate"]]

    def current(self, ctx, payload):
        return self._delegate(payload).current(ctx, payload)

    def build(self, ctx, payload):
        return self._delegate(payload).build(ctx, payload)

    def resource_names_for(self, payload, names):
        return self._delegate(payload).resource_names_for(payload, names)
