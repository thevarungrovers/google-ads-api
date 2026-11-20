"""Per-entity performance reports: the questions an account manager asks daily.

Declared WITHOUT ``segments.date``: one row per entity over the window, which
is what "which ad groups are wasting money" needs. Daily rows are opt-in --
``Report.build(segment_by_date=True)`` prepends the date -- because a 30-day
report of 200 search terms is 200 rows aggregated and up to 6,000 daily, and
an agent reading the daily version runs out of context before it reaches the
interesting rows.

GAQL lets ``segments.date`` sit in WHERE without being selected, which is what
makes the date window still apply to the aggregated form.
"""

from __future__ import annotations

from . import Report

#: The usual performance columns. Money columns are ruled in fields.py.
_METRICS = (
    "metrics.impressions",
    "metrics.clicks",
    "metrics.ctr",
    "metrics.average_cpc",
    "metrics.cost_micros",
    "metrics.conversions",
    "metrics.conversions_value",
    "metrics.cost_per_conversion",
)

AD_GROUPS = Report(
    name="ad_groups",
    description="Ad group performance, one row per ad group.",
    resource="ad_group",
    select=(
        "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
        "ad_group.status", "ad_group.cpc_bid_micros", *_METRICS,
    ),
    order_by=("metrics.cost_micros DESC",),
)

ADS = Report(
    name="ads",
    description="Ad performance with approval status, one row per ad.",
    resource="ad_group_ad",
    select=(
        "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
        "ad_group_ad.ad.id", "ad_group_ad.ad.type", "ad_group_ad.status",
        "ad_group_ad.policy_summary.approval_status", *_METRICS,
    ),
    order_by=("metrics.cost_micros DESC",),
)

SEARCH_TERMS = Report(
    name="search_terms",
    description=(
        "What people actually typed, with the spend it caused -- the input to "
        "a negative-keyword sweep. status says whether it is already a keyword "
        "or excluded."
    ),
    resource="search_term_view",
    select=(
        "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
        "search_term_view.search_term", "search_term_view.status", *_METRICS,
    ),
    order_by=("metrics.cost_micros DESC",),
)

LOCATIONS = Report(
    name="locations",
    description=(
        "Performance by country and by whether the location was the user's "
        "physical location or their area of interest."
    ),
    resource="geographic_view",
    select=(
        "campaign.id", "campaign.name", "geographic_view.country_criterion_id",
        "geographic_view.location_type", *_METRICS,
    ),
    order_by=("metrics.cost_micros DESC",),
)

DEVICES = Report(
    name="devices",
    description="Campaign performance split by device.",
    resource="campaign",
    select=("campaign.id", "campaign.name", "segments.device", *_METRICS),
    order_by=("campaign.name", "segments.device"),
)

HOUR_OF_DAY = Report(
    name="hour_of_day",
    description=(
        "Campaign performance by day of week and hour (account time zone), "
        "the input to an ad schedule."
    ),
    resource="campaign",
    select=(
        "campaign.id", "campaign.name", "segments.day_of_week", "segments.hour",
        *_METRICS,
    ),
    order_by=("campaign.name", "segments.day_of_week", "segments.hour"),
)

AUDIENCES = Report(
    name="audiences",
    description="Performance of audience segments attached to ad groups.",
    resource="ad_group_audience_view",
    select=(
        "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
        "ad_group_criterion.criterion_id", "ad_group_criterion.display_name",
        *_METRICS,
    ),
    order_by=("metrics.cost_micros DESC",),
)

ASSETS = Report(
    name="assets",
    description="Performance of assets (sitelinks, callouts, ...) linked to campaigns.",
    resource="campaign_asset",
    select=(
        "campaign.id", "campaign.name", "asset.id", "asset.name", "asset.type",
        "campaign_asset.field_type", "campaign_asset.status",
        "metrics.impressions", "metrics.clicks", "metrics.cost_micros",
        "metrics.conversions",
    ),
    order_by=("metrics.impressions DESC",),
)

CONVERSIONS_BY_ACTION = Report(
    name="conversions_by_action",
    description=(
        "Conversions per conversion action -- which goal the numbers are "
        "actually counting. Cost cannot be selected alongside this segment."
    ),
    resource="campaign",
    select=(
        "campaign.id", "campaign.name", "segments.conversion_action_name",
        "segments.conversion_action_category", "metrics.conversions",
        "metrics.conversions_value", "metrics.all_conversions",
    ),
    order_by=("campaign.name", "metrics.conversions DESC"),
)
