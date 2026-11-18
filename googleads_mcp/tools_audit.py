"""Read tools for what changed, what Google suggests, and anything else.

  list_recommendations   Google's own suggestions, with estimated impact
  list_recent_changes    Google's change_event history -- who changed what,
                         including people in the UI, which our ledger cannot see
  run_gaql               the read-only escape hatch for any other question

reconcile_ledger lives in tools_write instead: it needs the change kinds to
know how to read each ledger field back.

run_gaql is the ONE tool whose name the surface test lets mention a query. It
is safe for the same reason every read is: it goes through
ReadOnlyGoogleAdsClient, which cannot construct a mutating service or call a
mutate method, and assert_select_only refuses anything that is not a bare
SELECT before a request is built. It is a read passthrough, never a write one.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from googleads_mcp.errors import refusals_reach_the_caller
from googleads_mcp.tools_lookup import money
from googleads_mcp.tools_read import READ_ONLY
from googleads_reporting.fields import row_to_dict, unknown_money_fields
from googleads_reporting.query import Query, assert_select_only, in_list, parse_select
from googleads_reporting.retry import retry_read

RECOMMENDATION_FIELDS = (
    "recommendation.resource_name", "recommendation.type", "recommendation.campaign",
    "recommendation.ad_group", "recommendation.campaign_budget",
    "recommendation.dismissed",
    "recommendation.impact.base_metrics.impressions",
    "recommendation.impact.base_metrics.clicks",
    "recommendation.impact.base_metrics.cost_micros",
    "recommendation.impact.base_metrics.conversions",
    "recommendation.impact.potential_metrics.impressions",
    "recommendation.impact.potential_metrics.clicks",
    "recommendation.impact.potential_metrics.cost_micros",
    "recommendation.impact.potential_metrics.conversions",
    "recommendation.campaign_budget_recommendation.current_budget_amount_micros",
    "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros",
    "recommendation.keyword_recommendation.keyword.text",
    "recommendation.keyword_recommendation.keyword.match_type",
)

CHANGE_EVENT_FIELDS = (
    "change_event.change_date_time", "change_event.user_email",
    "change_event.client_type", "change_event.change_resource_type",
    "change_event.resource_change_operation", "change_event.changed_fields",
    "change_event.change_resource_name", "change_event.campaign",
    "change_event.ad_group",
)

#: change_event refuses a query without a LIMIT, and caps it at 10,000.
CHANGE_EVENT_MAX = 10_000

#: change_event only answers for the last 30 days. One less, so an account
#: whose time zone trails this machine's is not asked for day 31.
CHANGE_EVENT_MAX_DAYS = 29

#: Rows run_gaql will return. Anything bigger belongs in a report export.
GAQL_MAX_ROWS = 1000


def tail_id(resource_name: str | None) -> str | None:
    """customers/1/recommendations/ABC -> ABC."""
    return resource_name.rsplit("/", 1)[-1] if resource_name else None


def register(mcp, state) -> None:
    client = state.read_client

    @mcp.tool(title="List Google's recommendations", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_recommendations(
        types: Annotated[
            list[str] | None,
            Field(description='Recommendation types, e.g. ["CAMPAIGN_BUDGET", "KEYWORD"]. '
                  "Default: all"),
        ] = None,
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """Google's active (not dismissed) recommendations with estimated
        impact. Treat them as suggestions from a party that is paid by the
        click: check each against run_report before acting.

        Act on one with preview_recommendation (only types that map onto
        a guarded change are accepted) or set it aside with
        preview_low_risk_change(resource="recommendation", field="dismiss").
        """
        where = ["recommendation.dismissed = FALSE"]
        if types:
            where.append(in_list("recommendation.type", [t.upper() for t in types]))
        if campaign_id:
            where.append(f"campaign.id = {int(campaign_id)}")
        found = client.rows(Query(
            select=RECOMMENDATION_FIELDS, from_resource="recommendation",
            where=tuple(where), limit=limit,
        ))
        out = []
        for r in found:
            base_cost = r["recommendation.impact.base_metrics.cost_micros"]
            potential_cost = r["recommendation.impact.potential_metrics.cost_micros"]
            entry: dict[str, Any] = {
                "recommendation_id": tail_id(r["recommendation.resource_name"]),
                "resource_name": r["recommendation.resource_name"],
                "type": r["recommendation.type"],
                "campaign_id": tail_id(r["recommendation.campaign"]) or None,
                "ad_group_id": tail_id(r["recommendation.ad_group"]) or None,
                "impact": {
                    "base": {
                        "impressions": r["recommendation.impact.base_metrics.impressions"],
                        "clicks": r["recommendation.impact.base_metrics.clicks"],
                        "cost": money(base_cost),
                        "conversions": r["recommendation.impact.base_metrics.conversions"],
                    },
                    "potential": {
                        "impressions": r["recommendation.impact.potential_metrics.impressions"],
                        "clicks": r["recommendation.impact.potential_metrics.clicks"],
                        "cost": money(potential_cost),
                        "conversions": r["recommendation.impact.potential_metrics.conversions"],
                    },
                },
            }
            recommended = r[
                "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros"
            ]
            if recommended:
                entry["budget"] = {
                    "current": money(r[
                        "recommendation.campaign_budget_recommendation."
                        "current_budget_amount_micros"
                    ]),
                    "recommended": money(recommended),
                }
            keyword = r["recommendation.keyword_recommendation.keyword.text"]
            if keyword:
                entry["keyword"] = {
                    "text": keyword,
                    "match_type": r["recommendation.keyword_recommendation.keyword.match_type"],
                }
            out.append(entry)
        return out

    @mcp.tool(title="List recent changes made in Google Ads by anyone", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_recent_changes(
        days: Annotated[int, Field(description="Look back N days (Google keeps 30; "
                                               "clamped to 29)",
                                   ge=1, le=30)] = 7,
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> list[dict[str, Any]]:
        """Google's change history: who changed what, from where, newest first.

        Covers changes made in the Google Ads UI, the mobile app and other API
        clients -- everything list_my_changes cannot see. Read it before
        reverting anything: a person may have adjusted the same entity since.
        """
        # Both bounds: Google's change_event examples always give a closed
        # range, and an open-ended one has been refused as missing a filter.
        # The lower bound is clamped inside change_event's 30-day window (29
        # days, leaving room for the account's time zone trailing this
        # machine's); the upper bound is tomorrow, so an account whose time
        # zone is AHEAD does not lose the rest of its today.
        today = dt.date.today()
        start = (today - dt.timedelta(days=min(days, CHANGE_EVENT_MAX_DAYS))).isoformat()
        end = (today + dt.timedelta(days=1)).isoformat()
        where = [f"change_event.change_date_time >= '{start}'",
                 f"change_event.change_date_time <= '{end}'"]
        if campaign_id:
            where.append(f"campaign.id = {int(campaign_id)}")
        found = client.rows(Query(
            select=CHANGE_EVENT_FIELDS, from_resource="change_event", where=tuple(where),
            order_by=("change_event.change_date_time DESC",),
            limit=min(limit, CHANGE_EVENT_MAX),
        ))
        return [
            {
                "at": r["change_event.change_date_time"],
                "by": r["change_event.user_email"],
                "via": r["change_event.client_type"],
                "resource_type": r["change_event.change_resource_type"],
                "operation": r["change_event.resource_change_operation"],
                "changed_fields": list(getattr(r["change_event.changed_fields"], "paths", [])),
                "resource": r["change_event.change_resource_name"],
                "campaign": r["change_event.campaign"] or None,
                "ad_group": r["change_event.ad_group"] or None,
            }
            for r in found
        ]

    @mcp.tool(title="Run a read-only GAQL query", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def run_gaql(
        query: Annotated[str, Field(description="A GAQL SELECT. Nothing else is accepted.")],
        limit: Annotated[int, Field(ge=1, le=GAQL_MAX_ROWS)] = 200,
    ) -> dict[str, Any]:
        """The read-only escape hatch: any GAQL SELECT, for a question no other
        tool answers. Goes through the read-only client, so it cannot change
        anything; a non-SELECT is refused before any request is built.

        Money fields are converted out of micros where this project has a
        ruling on them; `unruled_money_fields` lists any selected field that
        looks like money but has none -- do NOT trust those numbers' units.
        Prefer run_report / get_* when they cover the question: their columns
        are vetted.
        """
        gaql = assert_select_only(query.strip().rstrip(";"))
        paths = parse_select(gaql)
        upper = gaql.upper()
        if " LIMIT " not in upper.replace("\n", " "):
            gaql = f"{gaql}\nLIMIT {limit + 1}"

        def run() -> list[dict[str, Any]]:
            return [row_to_dict(row, paths) for row in client.search_stream(gaql)]

        found = retry_read(run)
        return {
            "gaql": gaql,
            "row_count": min(len(found), limit),
            "truncated": len(found) > limit,
            "unruled_money_fields": list(unknown_money_fields(paths)),
            "rows": [
                {k: (str(v) if isinstance(v, Decimal) else _jsonable(v)) for k, v in r.items()}
                for r in found[:limit]
            ],
        }


def _jsonable(value: Any) -> Any:
    """Whatever row_to_dict could not flatten (a nested message) as text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return str(value)
