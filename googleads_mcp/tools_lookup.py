"""Read tools for the current STATE of an account: what exists and how it is set.

run_report answers "what happened"; these answer "what is there", which every
write needs first -- the ids (the Google Ads UI shows almost none), the current
bid or strategy, which campaigns a shared budget feeds, which locations are
targeted. Safe to allowlist permanently, like everything in tools_read: they go
through ReadOnlyGoogleAdsClient, which cannot write.

The SELECT lists are module constants so tests/test_mcp_lookup.py can check
every field path against the v25 schema offline.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from googleads_mcp.errors import refusals_reach_the_caller
from googleads_mcp.tools_read import (
    READ_ONLY,
    AgentName,
    Mine,
    NameOrigin,
    Owner,
    capped,
    naming_fields,
    naming_filter,
)
from googleads_reporting.query import Query, contains, in_list, last_n_days, quote_literal

CAMPAIGN_DETAIL = (
    "campaign.id", "campaign.name", "campaign.status", "campaign.serving_status",
    "campaign.primary_status", "campaign.primary_status_reasons",
    "campaign.advertising_channel_type", "campaign.advertising_channel_sub_type",
    "campaign.bidding_strategy_type", "campaign.bidding_strategy",
    "campaign.manual_cpc.enhanced_cpc_enabled",
    "campaign.target_cpa.target_cpa_micros",
    "campaign.maximize_conversions.target_cpa_micros",
    "campaign.target_roas.target_roas",
    "campaign.maximize_conversion_value.target_roas",
    "campaign.target_spend.cpc_bid_ceiling_micros",
    "campaign.network_settings.target_google_search",
    "campaign.network_settings.target_search_network",
    "campaign.network_settings.target_content_network",
    "campaign.network_settings.target_partner_search_network",
    "campaign.start_date_time", "campaign.end_date_time",
    "campaign.final_url_suffix", "campaign.tracking_url_template",
    "campaign_budget.id", "campaign_budget.name", "campaign_budget.amount_micros",
    "campaign_budget.explicitly_shared", "campaign_budget.reference_count",
    "customer.currency_code",
)

METRIC_FIELDS = (
    "metrics.impressions", "metrics.clicks", "metrics.cost_micros",
    "metrics.conversions", "metrics.conversions_value",
)

CAMPAIGN_IMPRESSION_SHARE = (
    "metrics.search_impression_share",
    "metrics.search_budget_lost_impression_share",
)

CAMPAIGN_CRITERIA = (
    "campaign_criterion.criterion_id", "campaign_criterion.type",
    "campaign_criterion.negative", "campaign_criterion.status",
    "campaign_criterion.bid_modifier",
    "campaign_criterion.location.geo_target_constant",
    "campaign_criterion.language.language_constant",
    "campaign_criterion.ad_schedule.day_of_week",
    "campaign_criterion.ad_schedule.start_hour",
    "campaign_criterion.ad_schedule.start_minute",
    "campaign_criterion.ad_schedule.end_hour",
    "campaign_criterion.ad_schedule.end_minute",
    "campaign_criterion.device.type",
    "campaign_criterion.display_name",
)

AD_GROUP_DETAIL = (
    "ad_group.id", "ad_group.name", "ad_group.status", "ad_group.type",
    "ad_group.cpc_bid_micros", "ad_group.target_cpa_micros",
    "ad_group.effective_target_cpa_micros", "ad_group.targeting_setting.target_restrictions",
    "ad_group.final_url_suffix", "ad_group.tracking_url_template",
    "campaign.id", "campaign.name", "campaign.status", "campaign.bidding_strategy_type",
    "customer.currency_code",
)

KEYWORD_FIELDS = (
    "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
    "ad_group_criterion.criterion_id", "ad_group_criterion.keyword.text",
    "ad_group_criterion.keyword.match_type", "ad_group_criterion.status",
    "ad_group_criterion.cpc_bid_micros", "ad_group_criterion.effective_cpc_bid_micros",
    "ad_group_criterion.quality_info.quality_score",
    "ad_group_criterion.approval_status", "ad_group_criterion.system_serving_status",
)

CAMPAIGN_NEGATIVE_FIELDS = (
    "campaign.id", "campaign.name", "campaign_criterion.criterion_id",
    "campaign_criterion.keyword.text", "campaign_criterion.keyword.match_type",
)

AD_GROUP_NEGATIVE_FIELDS = (
    "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
    "ad_group_criterion.criterion_id", "ad_group_criterion.keyword.text",
    "ad_group_criterion.keyword.match_type",
)

SHARED_SET_FIELDS = (
    "shared_set.id", "shared_set.name", "shared_set.type", "shared_set.status",
    "shared_set.member_count",
)

CAMPAIGN_SHARED_SET_FIELDS = (
    "campaign.id", "campaign.name", "shared_set.id", "campaign_shared_set.status",
)

SHARED_CRITERION_FIELDS = (
    "shared_set.id", "shared_criterion.criterion_id", "shared_criterion.keyword.text",
    "shared_criterion.keyword.match_type",
)

AD_FIELDS = (
    "campaign.id", "campaign.name", "ad_group.id", "ad_group.name",
    "ad_group_ad.ad.id", "ad_group_ad.ad.type", "ad_group_ad.status",
    "ad_group_ad.ad.final_urls",
    "ad_group_ad.ad.responsive_search_ad.headlines",
    "ad_group_ad.ad.responsive_search_ad.descriptions",
    "ad_group_ad.ad.responsive_search_ad.path1",
    "ad_group_ad.ad.responsive_search_ad.path2",
    # Legacy text ads, still serving on old accounts (selectable in v25).
    "ad_group_ad.ad.expanded_text_ad.headline_part1",
    "ad_group_ad.ad.expanded_text_ad.headline_part2",
    "ad_group_ad.ad.expanded_text_ad.headline_part3",
    "ad_group_ad.ad.expanded_text_ad.description",
    "ad_group_ad.ad.expanded_text_ad.description2",
    "ad_group_ad.ad.expanded_text_ad.path1",
    "ad_group_ad.ad.expanded_text_ad.path2",
    "ad_group_ad.ad.text_ad.headline",
    "ad_group_ad.ad.text_ad.description1",
    "ad_group_ad.ad.text_ad.description2",
    "ad_group_ad.policy_summary.approval_status",
    "ad_group_ad.policy_summary.review_status",
    "ad_group_ad.policy_summary.policy_topic_entries",
)

ASSET_FIELDS = (
    "asset.id", "asset.name", "asset.type", "asset.final_urls",
    "asset.sitelink_asset.link_text", "asset.callout_asset.callout_text",
    "asset.structured_snippet_asset.header", "asset.structured_snippet_asset.values",
    "asset.text_asset.text",
)

LABEL_FIELDS = ("label.id", "label.name", "label.status", "label.text_label.description")

USER_LIST_FIELDS = (
    "user_list.id", "user_list.name", "user_list.type", "user_list.size_for_search",
    "user_list.membership_status",
)

AUDIENCE_FIELDS = ("audience.id", "audience.name", "audience.status", "audience.description")

CONVERSION_ACTION_FIELDS = (
    "conversion_action.id", "conversion_action.name", "conversion_action.status",
    "conversion_action.category", "conversion_action.type",
    "conversion_action.primary_for_goal",
)

GEO_FIELDS = (
    "geo_target_constant.id", "geo_target_constant.name",
    "geo_target_constant.canonical_name", "geo_target_constant.country_code",
    "geo_target_constant.target_type", "geo_target_constant.status",
)

LANGUAGE_FIELDS = (
    "language_constant.id", "language_constant.name", "language_constant.code",
    "language_constant.targetable",
)


def money(value: Any) -> str | None:
    """A value rows() already converted out of micros, as a 2dp string."""
    if value is None:
        return None
    return f"{Decimal(str(value)):.2f}"


#: Ad types whose copy ad_copy reads; any other is said to be no text ad.
TEXT_AD_TYPES = frozenset({"RESPONSIVE_SEARCH_AD", "EXPANDED_TEXT_AD", "TEXT_AD"})


def ad_copy(r: dict[str, Any]) -> dict[str, Any]:
    """Headlines, descriptions and paths of an ad, whatever its text-ad type.

    A legacy text ad keeps its copy in other fields than a responsive search
    ad; reading only the RSA ones listed an old account's ads with no copy at
    all, as if they were blank. Other types (image, shopping, video) carry no
    ad text, and say so.
    """
    kind = r["ad_group_ad.ad.type"]
    if kind == "EXPANDED_TEXT_AD":
        p = "ad_group_ad.ad.expanded_text_ad."
        return {"headlines": [t for t in (r[p + "headline_part1"], r[p + "headline_part2"],
                                          r[p + "headline_part3"]) if t],
                "descriptions": [t for t in (r[p + "description"], r[p + "description2"]) if t],
                "path1": r[p + "path1"] or None, "path2": r[p + "path2"] or None}
    if kind == "TEXT_AD":
        p = "ad_group_ad.ad.text_ad."
        return {"headlines": [r[p + "headline"]] if r[p + "headline"] else [],
                "descriptions": [t for t in (r[p + "description1"], r[p + "description2"]) if t],
                "path1": None, "path2": None}
    p = "ad_group_ad.ad.responsive_search_ad."
    out = {"headlines": texts(r[p + "headlines"]), "descriptions": texts(r[p + "descriptions"]),
           "path1": r[p + "path1"] or None, "path2": r[p + "path2"] or None}
    if kind not in TEXT_AD_TYPES:
        out["note"] = f"a {kind} carries no ad text; its creative is not listed here"
    return out


def texts(value: Any) -> list[str]:
    """AdTextAsset-style repeated messages (or strings) as plain text."""
    out = []
    for item in value or []:
        out.append(item if isinstance(item, str) else getattr(item, "text", str(item)))
    return out


def topics(value: Any) -> list[str]:
    return [getattr(e, "topic", str(e)) for e in value or []]


def id_condition(field: str, raw: str | None) -> list[str]:
    return [f"{field} = {int(raw)}"] if raw else []


def criteria_with_modifiers(client, query: Query) -> list[dict[str, Any]]:
    """Campaign criteria as flattened dicts, each with ``_modifier`` rendered.

    ``bid_modifier`` is an optional field: unset means "no modifier", while
    0.0 means -100%, EXCLUDED. A flattened row reads 0.0 for both, so the
    modifier is taken off the proto with the presence-aware ``modifier_of``
    before it is flattened: None, "+20%", or "-100% (excluded)".
    """
    from googleads_reporting.fields import row_to_dict
    from googleads_reporting.retry import retry_read
    from googleads_reporting.write.targeting import modifier_of, pct_from_modifier, render_pct

    def collect() -> list[dict[str, Any]]:
        out = []
        for proto in client.search_stream(query):
            flat = row_to_dict(proto, query.select)
            modifier = modifier_of(proto.campaign_criterion)
            flat["_modifier"] = (None if modifier is None
                                 else render_pct(pct_from_modifier(modifier)))
            out.append(flat)
        return out

    return retry_read(collect)


def register(mcp, state) -> None:
    client = state.read_client

    def rows(select, resource, where=(), order_by=(), limit=None):
        return client.rows(
            Query(select=tuple(select), from_resource=resource, where=tuple(where),
                  order_by=tuple(order_by), limit=limit)
        )

    def totals(resource: str, where: list[str], extra: tuple[str, ...] = ()) -> dict[str, Any]:
        """7-day and 30-day totals, both ending yesterday."""
        out = {}
        for days in (7, 30):
            found = rows((*METRIC_FIELDS, *extra), resource, [*where, last_n_days(days)])
            agg: dict[str, Any] = {
                "cost": money(sum(Decimal(str(r["metrics.cost_micros"] or 0)) for r in found)),
                "clicks": sum(int(r["metrics.clicks"] or 0) for r in found),
                "impressions": sum(int(r["metrics.impressions"] or 0) for r in found),
                "conversions": f"{sum(float(r['metrics.conversions'] or 0) for r in found):.1f}",
                "conversions_value": money(
                    sum(Decimal(str(r["metrics.conversions_value"] or 0)) for r in found)
                ),
            }
            for path in extra:
                values = [r[path] for r in found if r.get(path) is not None]
                agg[path.split(".")[-1]] = values[0] if len(values) == 1 else values or None
            out[f"last_{days}_days"] = agg
        return out

    # -- one entity, everything about it -----------------------------------

    @mcp.tool(title="Everything about one campaign", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def get_campaign(
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
    ) -> dict[str, Any]:
        """One campaign in full: serving and primary status with the reasons,
        bidding strategy and its targets, budget (shared? limited by it?),
        networks, locations, languages, ad schedule, device modifiers, and 7-
        and 30-day totals.

        Read this before proposing any change to a campaign. `limited_by_budget`
        is the thing to check before raising a budget: if it is false, the
        campaign is not spending what it has, and more budget changes nothing.
        """
        found = rows(CAMPAIGN_DETAIL, "campaign", [f"campaign.id = {int(campaign_id)}"])
        if not found:
            raise LookupError(f"No campaign with id {campaign_id}.")
        c = found[0]
        where = [f"campaign.id = {int(campaign_id)}"]
        metrics = totals("campaign", where, CAMPAIGN_IMPRESSION_SHARE)

        criteria = criteria_with_modifiers(client, Query(
            select=CAMPAIGN_CRITERIA, from_resource="campaign_criterion",
            where=(*where, "campaign_criterion.status != 'REMOVED'",
                   in_list("campaign_criterion.type",
                           ["LOCATION", "LANGUAGE", "AD_SCHEDULE", "DEVICE", "PROXIMITY"])),
        ))
        geo_names = {}
        geo_resources = sorted({
            r["campaign_criterion.location.geo_target_constant"] for r in criteria
            if r["campaign_criterion.type"] == "LOCATION"
            and r["campaign_criterion.location.geo_target_constant"]
        })
        if geo_resources:
            for g in rows(("geo_target_constant.resource_name",
                           "geo_target_constant.canonical_name"),
                          "geo_target_constant",
                          [in_list("geo_target_constant.resource_name", geo_resources)]):
                geo_names[g["geo_target_constant.resource_name"]] = g[
                    "geo_target_constant.canonical_name"
                ]

        locations, languages, schedule, devices = [], [], [], []
        for r in criteria:
            kind = r["campaign_criterion.type"]
            base = {
                "criterion_id": str(r["campaign_criterion.criterion_id"]),
                "bid_modifier": r["_modifier"],
            }
            if kind == "LOCATION":
                resource = r["campaign_criterion.location.geo_target_constant"]
                locations.append({
                    **base, "geo_target": resource,
                    "name": geo_names.get(resource, r["campaign_criterion.display_name"]),
                    "excluded": bool(r["campaign_criterion.negative"]),
                })
            elif kind == "LANGUAGE":
                languages.append({
                    "criterion_id": base["criterion_id"],
                    "language": r["campaign_criterion.language.language_constant"],
                    "name": r["campaign_criterion.display_name"],
                })
            elif kind == "AD_SCHEDULE":
                schedule.append({
                    **base, "day": r["campaign_criterion.ad_schedule.day_of_week"],
                    "from": f"{r['campaign_criterion.ad_schedule.start_hour']:02d}:"
                            f"{_minute(r['campaign_criterion.ad_schedule.start_minute'])}",
                    "to": f"{r['campaign_criterion.ad_schedule.end_hour']:02d}:"
                          f"{_minute(r['campaign_criterion.ad_schedule.end_minute'])}",
                })
            elif kind == "DEVICE":
                devices.append({**base, "device": r["campaign_criterion.device.type"]})

        reasons = c["campaign.primary_status_reasons"] or []
        lost_to_budget = metrics["last_30_days"].get("search_budget_lost_impression_share")
        shared = bool(c["campaign_budget.explicitly_shared"])
        budget = {
            "budget_id": str(c["campaign_budget.id"]),
            "name": c["campaign_budget.name"],
            "daily_amount": money(c["campaign_budget.amount_micros"]),
            "shared": shared,
            "campaigns_using_it": c["campaign_budget.reference_count"],
        }
        if shared:
            budget["feeds"] = [
                {"campaign_id": str(r["campaign.id"]), "name": r["campaign.name"],
                 "status": r["campaign.status"]}
                for r in rows(("campaign.id", "campaign.name", "campaign.status"),
                              "campaign",
                              [f"campaign_budget.id = {int(c['campaign_budget.id'])}"])
            ]

        from googleads_mcp import ledger

        return {
            "campaign_id": str(c["campaign.id"]),
            "name": c["campaign.name"],
            "naming": naming_fields(state, c["campaign.name"], ledger.created_by_us(
                "campaign", str(c["campaign.id"]))),
            "status": c["campaign.status"],
            "serving_status": c["campaign.serving_status"],
            "primary_status": c["campaign.primary_status"],
            "primary_status_reasons": reasons,
            "channel": c["campaign.advertising_channel_type"],
            "channel_sub_type": c["campaign.advertising_channel_sub_type"],
            "currency": c["customer.currency_code"],
            "bidding": {
                "strategy_type": c["campaign.bidding_strategy_type"],
                "portfolio_strategy": c["campaign.bidding_strategy"] or None,
                "enhanced_cpc": c["campaign.manual_cpc.enhanced_cpc_enabled"],
                "target_cpa": money(
                    c["campaign.target_cpa.target_cpa_micros"]
                    or c["campaign.maximize_conversions.target_cpa_micros"]
                ) if (c["campaign.target_cpa.target_cpa_micros"]
                      or c["campaign.maximize_conversions.target_cpa_micros"]) else None,
                "target_roas": (
                    c["campaign.target_roas.target_roas"]
                    or c["campaign.maximize_conversion_value.target_roas"] or None
                ),
                "max_cpc_ceiling": money(c["campaign.target_spend.cpc_bid_ceiling_micros"])
                if c["campaign.target_spend.cpc_bid_ceiling_micros"] else None,
            },
            "budget": budget,
            "limited_by_budget": "BUDGET_CONSTRAINED" in reasons
            or bool(lost_to_budget and float(lost_to_budget) > 0.05),
            "networks": {
                "google_search": c["campaign.network_settings.target_google_search"],
                "search_partners": c["campaign.network_settings.target_search_network"],
                "display_network": c["campaign.network_settings.target_content_network"],
                "partner_search": c["campaign.network_settings.target_partner_search_network"],
            },
            "start": c["campaign.start_date_time"] or None,
            "end": c["campaign.end_date_time"] or None,
            "final_url_suffix": c["campaign.final_url_suffix"] or None,
            "tracking_url_template": c["campaign.tracking_url_template"] or None,
            "locations": locations,
            "languages": languages,
            "ad_schedule": schedule,
            "device_bid_modifiers": devices,
            "metrics": metrics,
        }

    @mcp.tool(title="Everything about one ad group", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def get_ad_group(
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
    ) -> dict[str, Any]:
        """One ad group in full: bids, targeting mode, how many ads and keywords
        it has by status, its campaign's bidding strategy, and 7- and 30-day
        totals.

        `bid_is_effective` says whether Google actually uses this ad group's
        max CPC: under automated bidding it is stored and ignored.
        """
        where = [f"ad_group.id = {int(ad_group_id)}"]
        found = rows(AD_GROUP_DETAIL, "ad_group", where)
        if not found:
            raise LookupError(f"No ad group with id {ad_group_id}.")
        g = found[0]

        ads: dict[str, int] = {}
        for r in rows(("ad_group_ad.ad.id", "ad_group_ad.status"), "ad_group_ad",
                      [*where, "ad_group_ad.status != 'REMOVED'"]):
            ads[r["ad_group_ad.status"]] = ads.get(r["ad_group_ad.status"], 0) + 1
        keywords: dict[str, int] = {}
        negatives = 0
        for r in rows(("ad_group_criterion.criterion_id", "ad_group_criterion.status",
                       "ad_group_criterion.negative"), "ad_group_criterion",
                      [*where, "ad_group_criterion.type = 'KEYWORD'",
                       "ad_group_criterion.status != 'REMOVED'"]):
            if r["ad_group_criterion.negative"]:
                negatives += 1
            else:
                status = r["ad_group_criterion.status"]
                keywords[status] = keywords.get(status, 0) + 1

        restrictions = []
        for item in g["ad_group.targeting_setting.target_restrictions"] or []:
            dimension = getattr(getattr(item, "targeting_dimension", None), "name", None)
            restrictions.append({
                "dimension": dimension,
                "mode": "observation" if getattr(item, "bid_only", False) else "targeting",
            })
        strategy = g["campaign.bidding_strategy_type"]
        from googleads_mcp import ledger

        return {
            "ad_group_id": str(g["ad_group.id"]),
            "name": g["ad_group.name"],
            "naming": naming_fields(state, g["ad_group.name"], ledger.created_by_us(
                "ad_group", str(g["ad_group.id"]))),
            "status": g["ad_group.status"],
            "type": g["ad_group.type"],
            "campaign": {"campaign_id": str(g["campaign.id"]), "name": g["campaign.name"],
                         "status": g["campaign.status"], "bidding_strategy_type": strategy},
            "currency": g["customer.currency_code"],
            "cpc_bid": money(g["ad_group.cpc_bid_micros"]),
            "bid_is_effective": strategy in ("MANUAL_CPC", "ENHANCED_CPC"),
            "target_cpa": money(g["ad_group.target_cpa_micros"])
            if g["ad_group.target_cpa_micros"] else None,
            "effective_target_cpa": money(g["ad_group.effective_target_cpa_micros"])
            if g["ad_group.effective_target_cpa_micros"] else None,
            "audience_targeting": restrictions,
            "final_url_suffix": g["ad_group.final_url_suffix"] or None,
            "tracking_url_template": g["ad_group.tracking_url_template"] or None,
            "ads_by_status": ads,
            "keywords_by_status": keywords,
            "negative_keywords": negatives,
            "metrics": totals("ad_group", where),
        }

    # -- keywords and negatives ------------------------------------------------

    @mcp.tool(title="List keywords", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_keywords(
        ad_group_id: Annotated[str | None, Field(description="Restrict to one ad group")] = None,
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        status: Annotated[
            Literal["ENABLED", "PAUSED"] | None, Field(description="Default: both")
        ] = None,
        text_contains: Annotated[str | None, Field(description="Substring")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> list[dict[str, Any]]:
        """Positive keywords with ids, match type, status, bid, effective bid,
        quality score and approval. The criterion_id is what the keyword write
        tools take."""
        where = [
            "ad_group_criterion.type = 'KEYWORD'",
            "ad_group_criterion.negative = FALSE",
            "ad_group_criterion.status != 'REMOVED'",
            *id_condition("ad_group.id", ad_group_id),
            *id_condition("campaign.id", campaign_id),
        ]
        if status:
            where.append(f"ad_group_criterion.status = {quote_literal(status)}")
        if text_contains:
            where.append(contains("ad_group_criterion.keyword.text", text_contains))
        return capped([
            {
                "criterion_id": str(r["ad_group_criterion.criterion_id"]),
                "text": r["ad_group_criterion.keyword.text"],
                "match_type": r["ad_group_criterion.keyword.match_type"],
                "status": r["ad_group_criterion.status"],
                "cpc_bid": money(r["ad_group_criterion.cpc_bid_micros"])
                if r["ad_group_criterion.cpc_bid_micros"] else None,
                "effective_cpc_bid": money(r["ad_group_criterion.effective_cpc_bid_micros"]),
                "quality_score": r["ad_group_criterion.quality_info.quality_score"] or None,
                "approval_status": r["ad_group_criterion.approval_status"],
                "serving_status": r["ad_group_criterion.system_serving_status"],
                "ad_group_id": str(r["ad_group.id"]),
                "ad_group_name": r["ad_group.name"],
                "campaign_id": str(r["campaign.id"]),
                "campaign_name": r["campaign.name"],
            }
            for r in rows(KEYWORD_FIELDS, "ad_group_criterion", where,
                          ("ad_group_criterion.keyword.text",), limit + 1)
        ], limit, "keywords")

    @mcp.tool(title="List negative keywords", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_negative_keywords(
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        ad_group_id: Annotated[str | None, Field(description="Restrict to one ad group")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 500,
    ) -> dict[str, Any]:
        """Negative keywords at campaign and ad group level, plus the shared
        negative lists attached to the campaign(s). Read before adding
        negatives, so you do not add one that is already there."""
        campaign_level = [] if ad_group_id else [
            {
                "level": "campaign", "criterion_id": str(r["campaign_criterion.criterion_id"]),
                "text": r["campaign_criterion.keyword.text"],
                "match_type": r["campaign_criterion.keyword.match_type"],
                "campaign_id": str(r["campaign.id"]), "campaign_name": r["campaign.name"],
            }
            for r in rows(CAMPAIGN_NEGATIVE_FIELDS, "campaign_criterion", [
                "campaign_criterion.type = 'KEYWORD'", "campaign_criterion.negative = TRUE",
                "campaign_criterion.status != 'REMOVED'",
                *id_condition("campaign.id", campaign_id),
            ], ("campaign_criterion.keyword.text",), limit + 1)
        ]
        ad_group_level = [
            {
                "level": "ad_group", "criterion_id": str(r["ad_group_criterion.criterion_id"]),
                "text": r["ad_group_criterion.keyword.text"],
                "match_type": r["ad_group_criterion.keyword.match_type"],
                "ad_group_id": str(r["ad_group.id"]), "ad_group_name": r["ad_group.name"],
                "campaign_id": str(r["campaign.id"]), "campaign_name": r["campaign.name"],
            }
            for r in rows(AD_GROUP_NEGATIVE_FIELDS, "ad_group_criterion", [
                "ad_group_criterion.type = 'KEYWORD'", "ad_group_criterion.negative = TRUE",
                "ad_group_criterion.status != 'REMOVED'",
                *id_condition("campaign.id", campaign_id),
                *id_condition("ad_group.id", ad_group_id),
            ], ("ad_group_criterion.keyword.text",), limit + 1)
        ]
        shared = [
            {"shared_set_id": str(r["shared_set.id"]), "campaign_id": str(r["campaign.id"]),
             "campaign_name": r["campaign.name"]}
            for r in rows(CAMPAIGN_SHARED_SET_FIELDS, "campaign_shared_set", [
                "campaign_shared_set.status = 'ENABLED'",
                "shared_set.type = 'NEGATIVE_KEYWORDS'",
                *id_condition("campaign.id", campaign_id),
            ])
        ]
        cut = [name for name, found in (("campaign_level", campaign_level),
                                        ("ad_group_level", ad_group_level)) if len(found) > limit]
        return {
            "campaign_level": campaign_level[:limit],
            "ad_group_level": ad_group_level[:limit],
            "shared_lists_attached": shared,
            **({"truncated": cut, "note": f"more negatives exist than the {limit} shown per "
                                          "level: raise limit or narrow to one campaign"}
               if cut else {}),
        }

    @mcp.tool(title="List shared negative keyword lists", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_shared_negative_lists(
        shared_set_id: Annotated[
            str | None, Field(description="Also list this one list's keywords")
        ] = None,
    ) -> list[dict[str, Any]]:
        """Shared negative keyword lists, with the campaigns each is attached to.
        Pass shared_set_id to include that list's keywords."""
        links: dict[str, list[dict[str, str]]] = {}
        for r in rows(CAMPAIGN_SHARED_SET_FIELDS, "campaign_shared_set", [
            "campaign_shared_set.status = 'ENABLED'", "shared_set.type = 'NEGATIVE_KEYWORDS'",
        ]):
            links.setdefault(str(r["shared_set.id"]), []).append(
                {"campaign_id": str(r["campaign.id"]), "name": r["campaign.name"]}
            )
        out = []
        for r in rows(SHARED_SET_FIELDS, "shared_set", [
            "shared_set.type = 'NEGATIVE_KEYWORDS'", "shared_set.status = 'ENABLED'",
            *id_condition("shared_set.id", shared_set_id),
        ], ("shared_set.name",)):
            entry = {
                "shared_set_id": str(r["shared_set.id"]), "name": r["shared_set.name"],
                "keyword_count": r["shared_set.member_count"],
                "attached_to": links.get(str(r["shared_set.id"]), []),
            }
            if shared_set_id:
                entry["keywords"] = [
                    {"criterion_id": str(k["shared_criterion.criterion_id"]),
                     "text": k["shared_criterion.keyword.text"],
                     "match_type": k["shared_criterion.keyword.match_type"]}
                    for k in rows(SHARED_CRITERION_FIELDS, "shared_criterion",
                                  [f"shared_set.id = {int(shared_set_id)}"])
                ]
            out.append(entry)
        return out

    # -- ads and assets ----------------------------------------------------------

    @mcp.tool(title="List ads with their text and approval status", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_ads(
        ad_group_id: Annotated[str | None, Field(description="Restrict to one ad group")] = None,
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """Ads with headlines, descriptions, final URLs, and approval and policy
        status. Removed ads are left out."""
        if not (ad_group_id or campaign_id):
            raise ValueError("Pass ad_group_id or campaign_id.")
        found = rows(AD_FIELDS, "ad_group_ad", [
            "ad_group_ad.status != 'REMOVED'",
            *id_condition("ad_group.id", ad_group_id),
            *id_condition("campaign.id", campaign_id),
        ], limit=limit + 1)
        return capped([
            {
                "ad_id": str(r["ad_group_ad.ad.id"]), "type": r["ad_group_ad.ad.type"],
                "status": r["ad_group_ad.status"],
                "approval_status": r["ad_group_ad.policy_summary.approval_status"],
                "review_status": r["ad_group_ad.policy_summary.review_status"],
                "policy_topics": topics(r["ad_group_ad.policy_summary.policy_topic_entries"]),
                "final_urls": list(r["ad_group_ad.ad.final_urls"] or []),
                **ad_copy(r),
                "ad_group_id": str(r["ad_group.id"]), "ad_group_name": r["ad_group.name"],
                "campaign_id": str(r["campaign.id"]), "campaign_name": r["campaign.name"],
            }
            for r in found
        ], limit, "ads")

    @mcp.tool(title="List assets (sitelinks, callouts, snippets, ...)", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_assets(
        asset_type: Annotated[
            Literal["SITELINK", "CALLOUT", "STRUCTURED_SNIPPET", "TEXT", "IMAGE",
                    "YOUTUBE_VIDEO"] | None,
            Field(description="Default: every type"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> list[dict[str, Any]]:
        """Assets in the account with their ids and text."""
        where = [f"asset.type = {quote_literal(asset_type)}"] if asset_type else []
        return capped([
            {
                "asset_id": str(r["asset.id"]), "name": r["asset.name"] or None,
                "type": r["asset.type"],
                "text": r["asset.sitelink_asset.link_text"]
                or r["asset.callout_asset.callout_text"]
                or r["asset.text_asset.text"] or r["asset.structured_snippet_asset.header"]
                or None,
                "snippet_values": list(r["asset.structured_snippet_asset.values"] or []) or None,
                "final_urls": list(r["asset.final_urls"] or []),
            }
            for r in rows(ASSET_FIELDS, "asset", where, ("asset.id",), limit + 1)
        ], limit, "assets")

    # -- ids the write tools take ------------------------------------------------

    @mcp.tool(title="List labels", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_labels(
        origin: NameOrigin = "any",
        agent_name: AgentName = None,
        owner: Owner = None,
        mine: Mine = False,
    ) -> list[dict[str, Any]]:
        """Labels in the account, with their ids, who named each (origin /
        owner / agent / base) and whether this server created it. Filter on
        those like find_campaigns."""
        from googleads_mcp import ledger

        naming_where, keep, _ = naming_filter(state, "label.name", origin=origin,
                                              agent_name=agent_name, owner=owner, mine=mine)
        created = ledger.created_index()["label"]
        return [
            {"label_id": str(r["label.id"]), "name": r["label.name"],
             "status": r["label.status"],
             "description": r["label.text_label.description"] or None,
             **naming_fields(state, r["label.name"], str(r["label.id"]) in created)}
            for r in rows(LABEL_FIELDS, "label", ["label.status != 'REMOVED'", *naming_where],
                          ("label.name",))
            if keep(r["label.name"])
        ]

    @mcp.tool(title="List audience segments", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_audiences(
        name_contains: Annotated[str | None, Field(description="Substring")] = None,
    ) -> dict[str, Any]:
        """Remarketing / customer lists (user lists) and combined audiences,
        with the ids audience targeting takes."""
        user_where = [contains("user_list.name", name_contains)] if name_contains else []
        audience_where = [contains("audience.name", name_contains)] if name_contains else []
        return {
            "user_lists": [
                {"user_list_id": str(r["user_list.id"]), "name": r["user_list.name"],
                 "type": r["user_list.type"], "search_size": r["user_list.size_for_search"],
                 "status": r["user_list.membership_status"]}
                for r in rows(USER_LIST_FIELDS, "user_list", user_where, ("user_list.name",))
            ],
            "audiences": [
                {"audience_id": str(r["audience.id"]), "name": r["audience.name"],
                 "status": r["audience.status"], "description": r["audience.description"]}
                for r in rows(AUDIENCE_FIELDS, "audience", audience_where, ("audience.name",))
            ],
        }

    @mcp.tool(title="List conversion actions", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_conversion_actions() -> list[dict[str, Any]]:
        """Conversion actions: which are primary (count towards bidding) and
        which are only observed. Read-only here on purpose: editing one moves
        bidding across the whole account."""
        return [
            {"conversion_action_id": str(r["conversion_action.id"]),
             "name": r["conversion_action.name"], "status": r["conversion_action.status"],
             "category": r["conversion_action.category"],
             "type": r["conversion_action.type"],
             "primary": r["conversion_action.primary_for_goal"]}
            for r in rows(CONVERSION_ACTION_FIELDS, "conversion_action",
                          ["conversion_action.status != 'REMOVED'"],
                          ("conversion_action.name",))
        ]

    @mcp.tool(title="Find location targets by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_locations(
        name_contains: Annotated[str, Field(description='e.g. "Springfield"')],
        country_code: Annotated[str | None, Field(description='e.g. "CA"')] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> list[dict[str, Any]]:
        """Geo target ids for preview_campaign_locations. Names are ambiguous
        ("Springfield" exists in several states) -- check canonical_name before using one."""
        where = [contains("geo_target_constant.name", name_contains),
                 "geo_target_constant.status = 'ENABLED'"]
        if country_code:
            where.append(
                f"geo_target_constant.country_code = {quote_literal(country_code.upper())}"
            )
        return capped([
            {"geo_target_id": str(r["geo_target_constant.id"]),
             "name": r["geo_target_constant.name"],
             "canonical_name": r["geo_target_constant.canonical_name"],
             "country_code": r["geo_target_constant.country_code"],
             "type": r["geo_target_constant.target_type"]}
            for r in rows(GEO_FIELDS, "geo_target_constant", where, limit=limit + 1)
        ], limit, "locations")

    @mcp.tool(title="Find language targets", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_languages(
        name_contains: Annotated[str | None, Field(description='e.g. "Spanish"')] = None,
    ) -> list[dict[str, Any]]:
        """Language ids for preview_campaign_languages."""
        where = ["language_constant.targetable = TRUE"]
        if name_contains:
            where.append(contains("language_constant.name", name_contains))
        return [
            {"language_id": str(r["language_constant.id"]), "name": r["language_constant.name"],
             "code": r["language_constant.code"]}
            for r in rows(LANGUAGE_FIELDS, "language_constant", where, ("language_constant.name",))
        ]


def _minute(value: Any) -> str:
    """MinuteOfHour enum name -> "00" / "15" / "30" / "45"."""
    return {"ZERO": "00", "FIFTEEN": "15", "THIRTY": "30", "FORTY_FIVE": "45"}.get(
        str(value), "00"
    )
