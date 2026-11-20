"""Switching a campaign's bidding strategy, and moving its target.

Four standard strategies, all set on the campaign itself (portfolio strategies
are deliberately out of scope -- they are shared objects, like shared budgets,
and a change to one moves every campaign using it):

    MANUAL_CPC                 ad group / keyword bids are used as written
    MAXIMIZE_CLICKS            Google bids; optional ceiling on any one CPC
    MAXIMIZE_CONVERSIONS       optional target CPA
    MAXIMIZE_CONVERSION_VALUE  optional target ROAS (a ratio: 4.0 means 400%)

The update mask is written BY HAND, naming the strategy's leaf field. Deriving
it from the populated message (set_update_mask) does not work here: a strategy
with no target set is an empty submessage, which protobuf does not consider
populated, so the derived mask would name nothing and Google would change
nothing -- or, worse, accept a mask naming the wrong oneof member.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .client import MutatingGoogleAdsClient, MutationError
from .criteria import campaign_path
from .plan import FieldChange, PlannedChange, micros

SERVICE, METHOD = "CampaignService", "mutate_campaigns"

STRATEGIES = ("MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE")

#: bidding_strategy_type as Google reports it -> the name used here.
FROM_REPORTED = {
    "MANUAL_CPC": "MANUAL_CPC",
    "TARGET_SPEND": "MAXIMIZE_CLICKS",
    "MAXIMIZE_CONVERSIONS": "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE": "MAXIMIZE_CONVERSION_VALUE",
    # The legacy standalone forms read back as these; switching AWAY from one
    # is supported, and reverting to one re-expresses it as the modern form.
    "TARGET_CPA": "MAXIMIZE_CONVERSIONS",
    "TARGET_ROAS": "MAXIMIZE_CONVERSION_VALUE",
}


@dataclass(frozen=True)
class Strategy:
    """One strategy and its optional target, in currency units / a ratio."""

    name: str
    target_cpa: float | None = None
    target_roas: float | None = None
    max_cpc: float | None = None

    def render(self, currency: str = "") -> str:
        unit = f" {currency}" if currency else ""
        if self.target_cpa:
            return f"{self.name} (target CPA {self.target_cpa:.2f}{unit})"
        if self.target_roas:
            return f"{self.name} (target ROAS {self.target_roas:g})"
        if self.max_cpc:
            return f"{self.name} (max CPC {self.max_cpc:.2f}{unit})"
        return self.name

    def as_dict(self) -> dict:
        return asdict(self)


def strategy_from_row(campaign) -> Strategy:
    """What a campaign proto row is bidding with today."""
    reported = campaign.bidding_strategy_type.name
    name = FROM_REPORTED.get(reported)
    if name is None:
        raise MutationError(
            f"The campaign bids with {reported}, which this tool does not switch from."
        )

    def m(value: int) -> float | None:
        return round(value / 1_000_000, 2) if value else None

    if name == "MAXIMIZE_CONVERSIONS":
        return Strategy(name, target_cpa=m(campaign.maximize_conversions.target_cpa_micros
                                           or campaign.target_cpa.target_cpa_micros))
    if name == "MAXIMIZE_CONVERSION_VALUE":
        return Strategy(name, target_roas=(campaign.maximize_conversion_value.target_roas
                                           or campaign.target_roas.target_roas or None))
    if name == "MAXIMIZE_CLICKS":
        return Strategy(name, max_cpc=m(campaign.target_spend.cpc_bid_ceiling_micros))
    return Strategy(name)


def plan_set_strategy(
    client: MutatingGoogleAdsClient, *, campaign_id: str, strategy: Strategy,
    before: Strategy | None = None,
) -> PlannedChange:
    if strategy.name not in STRATEGIES:
        raise MutationError(f"strategy must be one of {', '.join(STRATEGIES)}.")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = campaign_path(client, campaign_id)
    if strategy.name == "MANUAL_CPC":
        # Enhanced CPC is being retired by Google; always off here.
        campaign.manual_cpc.enhanced_cpc_enabled = False
        path = "manual_cpc.enhanced_cpc_enabled"
    elif strategy.name == "MAXIMIZE_CLICKS":
        campaign.target_spend.cpc_bid_ceiling_micros = micros(strategy.max_cpc or 0)
        path = "target_spend.cpc_bid_ceiling_micros"
    elif strategy.name == "MAXIMIZE_CONVERSIONS":
        campaign.maximize_conversions.target_cpa_micros = micros(strategy.target_cpa or 0)
        path = "maximize_conversions.target_cpa_micros"
    else:
        campaign.maximize_conversion_value.target_roas = float(strategy.target_roas or 0)
        path = "maximize_conversion_value.target_roas"
    operation.update_mask.paths.append(path)
    return PlannedChange(
        action="update", entity="campaign", label=f"id {campaign_id}",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("bidding_strategy",
                             before.render() if before else None, strategy.render())],
        warnings=[] if before and before.name == strategy.name else [
            "a strategy change restarts Google's learning period: expect a week or "
            "two of unstable performance"
        ],
    )
