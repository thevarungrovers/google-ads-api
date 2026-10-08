"""Daily campaign performance.

One row per campaign per day. ``campaign_budget.amount_micros`` is the budget
as configured, not spend -- spend is ``metrics.cost_micros``.
"""

from __future__ import annotations

from . import Report

CAMPAIGNS = Report(
    name="campaigns",
    description="Daily campaign performance: cost, clicks, conversions.",
    resource="campaign",
    select=(
        "segments.date",
        "campaign.id",
        "campaign.name",
        "campaign.status",
        "campaign.advertising_channel_type",
        "campaign_budget.amount_micros",
        "metrics.impressions",
        "metrics.clicks",
        "metrics.ctr",
        "metrics.average_cpc",
        "metrics.cost_micros",
        "metrics.conversions",
        "metrics.conversions_value",
        "metrics.cost_per_conversion",
    ),
    order_by=("segments.date", "campaign.name"),
)
