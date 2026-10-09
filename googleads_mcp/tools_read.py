"""Read tools. Safe to allowlist permanently -- none of these can change anything.

They route through :class:`ReadOnlyGoogleAdsClient`, whose allowlist makes a
write unreachable regardless of what is asked for here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from googleads_reporting.client import ReadOnlyGoogleAdsClient
from googleads_reporting.export import summarise, to_dataframe
from googleads_reporting.query import Query, between, contains, during, last_n_days
from googleads_reporting.reports import REGISTRY, get_report, report_names

def refusals_reach_the_caller(fn):
    """Re-raise a deliberate refusal as ToolError so its message survives.

    The MCP runtime carries a ToolError's text through to the caller but
    replaces every other exception with a bare "Error executing tool <name>".
    An unknown report name should say which names exist, not nothing.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, LookupError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=True
)

_CAMPAIGN_FIELDS = (
    "campaign.id",
    "campaign.name",
    "campaign.status",
    "campaign.advertising_channel_type",
    "campaign_budget.id",
    "campaign_budget.amount_micros",
    "customer.currency_code",
)


def _money(value: Any) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def register(mcp, state) -> None:
    """Attach every read tool to ``mcp``."""

    client = ReadOnlyGoogleAdsClient.from_env()
    state.read_client = client
    state.default_customer_id = client.settings.customer_id or ""

    @mcp.tool(title="List the accounts these credentials can reach", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_accounts() -> dict[str, Any]:
        """Accounts reachable directly, plus the one this server defaults to.

        The default is the only account writes may touch unless guardrails.toml
        widens the scope.
        """
        return {
            "default_customer_id": state.default_customer_id,
            "directly_accessible": client.list_accessible_customers(),
            "writes_allowed_on": list(state.limits.allowed_customer_ids)
            or [state.default_customer_id],
        }

    @mcp.tool(title="Find campaigns by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_campaigns(
        name_contains: Annotated[
            str | None,
            Field(description="Case-insensitive substring of the campaign name"),
        ] = None,
        status: Annotated[
            str | None, Field(description="ENABLED, PAUSED or REMOVED")
        ] = None,
        limit: Annotated[int, Field(description="Max rows", ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Campaigns with their ids, status, channel and daily budget.

        Start here. Campaign ids are not visible in the Google Ads UI, so every
        write tool needs one from this.
        """
        where = []
        if name_contains:
            where.append(contains("campaign.name", name_contains))
        if status:
            where.append(f"campaign.status = '{status.upper()}'")
        rows = client.rows(
            Query(
                select=_CAMPAIGN_FIELDS,
                from_resource="campaign",
                where=tuple(where),
                order_by=("campaign.name",),
                limit=limit,
            )
        )
        return [
            {
                "campaign_id": str(r["campaign.id"]),
                "name": r["campaign.name"],
                "status": r["campaign.status"],
                "channel": r["campaign.advertising_channel_type"],
                "budget_id": str(r["campaign_budget.id"]),
                "daily_budget": _money(r["campaign_budget.amount_micros"]),
                "currency": r["customer.currency_code"],
            }
            for r in rows
        ]

    @mcp.tool(title="Find ad groups by name", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def find_ad_groups(
        name_contains: Annotated[str | None, Field(description="Substring")] = None,
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Ad groups with their ids, status, bid and parent campaign.

        Ad group names are unique only WITHIN a campaign, so a bare name match
        across the account is routinely ambiguous -- the campaign is included
        so duplicates can be told apart.
        """
        where = []
        if name_contains:
            where.append(contains("ad_group.name", name_contains))
        if campaign_id:
            where.append(f"campaign.id = {int(campaign_id)}")
        rows = client.rows(
            Query(
                select=(
                    "ad_group.id", "ad_group.name", "ad_group.status",
                    "ad_group.cpc_bid_micros", "campaign.id", "campaign.name",
                    "customer.currency_code",
                ),
                from_resource="ad_group",
                where=tuple(where),
                order_by=("ad_group.name",),
                limit=limit,
            )
        )
        return [
            {
                "ad_group_id": str(r["ad_group.id"]),
                "name": r["ad_group.name"],
                "status": r["ad_group.status"],
                "cpc_bid": _money(r["ad_group.cpc_bid_micros"]),
                "campaign_id": str(r["campaign.id"]),
                "campaign_name": r["campaign.name"],
                "currency": r["customer.currency_code"],
            }
            for r in rows
        ]

    @mcp.tool(title="Run a performance report", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def run_report(
        report: Annotated[
            str, Field(description=f"One of: {', '.join(report_names())}")
        ],
        days: Annotated[
            int | None,
            Field(description="Window of N days ending YESTERDAY", ge=1, le=365),
        ] = 30,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        campaign_id: Annotated[str | None, Field(description="Restrict to one campaign")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """Campaign, keyword or account performance.

        Today is excluded from `days` on purpose: today's metrics are partial
        all day and drag every average down. Cost is already converted out of
        micros, so `metrics.cost` is in the account's currency.
        """
        definition = get_report(report)
        condition = None
        if definition.supports_date_range:
            if start_date and end_date:
                condition = between(start_date, end_date)
            elif days:
                condition = last_n_days(days)
        extra = (f"campaign.id = {int(campaign_id)}",) if campaign_id else ()
        query = definition.build(
            date_condition=condition, extra_where=extra, limit=limit
        )
        rows = client.rows(query)
        frame = to_dataframe(rows, definition.select)
        return {
            "report": report,
            "gaql": query.to_gaql(),
            "row_count": len(frame),
            "totals": {k: str(v) for k, v in summarise(frame).items()},
            "rows": frame.to_dict(orient="records"),
        }

    @mcp.tool(title="Show the write guardrails and session budget", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def get_guardrails() -> dict[str, Any]:
        """The bounds every apply_* is checked against, and what is left.

        Call this before proposing a change, so you propose something that can
        actually be applied rather than something that will be refused.
        """
        from googleads_mcp.guardrails import writes_disabled

        return {
            "limits": state.limits.describe(),
            "session": state.counters.snapshot(),
            "writes_disabled": writes_disabled(),
            "pending_previews": state.previews.pending_count,
        }

    @mcp.tool(title="List changes made in this account by the server", annotations=READ_ONLY)
    @refusals_reach_the_caller
    def list_my_changes(
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> list[dict[str, Any]]:
        """Every change this server has applied, newest last, with entry ids.

        Pass an entry_id to revert_change to undo one.
        """
        from googleads_mcp import ledger

        reverted = ledger.reverted_entry_ids()
        rows = ledger.entries()[-limit:]
        return [
            {
                "entry_id": e.get("entry_id"),
                "at": e.get("at"),
                "tool": e.get("tool"),
                "entity": f"{e.get('entity_type')} {e.get('entity_name')}",
                "entity_id": e.get("entity_id"),
                "field": e.get("field"),
                "before": e.get("before"),
                "after": e.get("after"),
                "applied": e.get("applied", False),
                "reverted": e.get("entry_id") in reverted,
                "error": e.get("error"),
            }
            for e in rows
        ]
