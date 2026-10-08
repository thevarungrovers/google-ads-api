"""Daily keyword performance.

``keyword_view`` covers positive keywords on Search and Shopping campaigns.
Negative keywords do not appear here -- they have no metrics -- and Performance
Max campaigns contribute no rows, since they have no keywords at all.
"""

from __future__ import annotations

from . import Report

KEYWORDS = Report(
    name="keywords",
    description="Daily keyword performance for Search/Shopping campaigns.",
    resource="keyword_view",
    select=(
        "segments.date",
        "campaign.id",
        "campaign.name",
        "ad_group.id",
        "ad_group.name",
        "ad_group_criterion.criterion_id",
        "ad_group_criterion.keyword.text",
        "ad_group_criterion.keyword.match_type",
        "ad_group_criterion.status",
        "metrics.impressions",
        "metrics.clicks",
        "metrics.ctr",
        "metrics.average_cpc",
        "metrics.cost_micros",
        "metrics.conversions",
        "metrics.conversions_value",
    ),
    order_by=("segments.date", "metrics.cost_micros DESC"),
)
