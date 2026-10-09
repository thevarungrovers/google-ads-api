"""Write tools: a ``preview_*`` and an ``apply_*`` for each change.

SEPARATE TOOLS, NOT A ``dry_run`` PARAMETER. This is the most consequential
choice here. Claude Code's permission rules key on the tool NAME, so separate
names let you permanently allowlist every ``preview_*`` while never allowlisting
an ``apply_*``. With a flag, one "always allow" clicked during a harmless
preview would silently authorise every future real write -- exactly the failure
this is guarding against.

Every ``apply_*`` demands the token its own preview minted. That buys three
things at once: a readable preview always sits directly above the approval
prompt, an apply cannot be called cold, and the recorded ``before`` is
re-checked against the entity's CURRENT value, so a change someone made in the
Google Ads UI in between is caught rather than silently overwritten.

``destructiveHint`` is set only where the change moves money or stops delivery.
Marking a reversible pause destructive would train someone to click through the
loud prompts, which is how loud prompts stop working.

NOT EXPOSED, deliberately:
  - setting any status to REMOVED. It is irreversible in Google Ads, and PAUSED
    achieves the same outcome reversibly.
  - creating or deleting campaigns, ad groups and ads. Creation is a
    multi-entity atomic request with media and a legal declaration attached;
    it belongs in `scripts/manage.py campaign new`, where a human answers for
    it, not in a tool an agent can reach.
  - budget changes on a SHARED budget, which would silently move every campaign
    using it.
  - anything touching billing or account access -- those services are not on
    the write client's allowlist at all.

Enforcement is structural: there is no generic ``mutate(resource, body)``
passthrough, so an unregistered operation is unreachable rather than merely
undocumented. ``tests/test_mcp_surface.py`` pins the registered tool names.
"""

from __future__ import annotations

import functools
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from googleads_mcp import ledger
from googleads_mcp.guardrails import (
    Check,
    any_failed,
    check_campaign_in_scope,
    check_ceiling,
    check_count,
    check_currency,
    check_customer_in_scope,
    check_pct_increase,
    check_projected_delta,
    check_writes_enabled,
    render_checks,
)
from googleads_mcp.previews import (
    ApplyResult,
    ChangePreview,
    PreviewExpired,
    PreviewStale,
    pct_change,
    project_bid_change,
    project_budget_change,
    project_pause,
)
from googleads_reporting.fields import from_micros
from googleads_reporting.query import Query
from googleads_reporting.write import MutatingGoogleAdsClient, adgroups, budgets, campaigns
from googleads_reporting.write.client import MutationError
from googleads_reporting.write.plan import execute

#: A preview contacts the API but changes nothing, so it is read-only.
#: Exceptions that mean "the tool refused on purpose", as opposed to "the tool
#: crashed". The distinction is load-bearing: the MCP runtime carries a
#: ToolError's message through to the caller as
#: "Error executing tool <name>: <message>", but replaces every other
#: exception with a bare "Error executing tool <name>" and keeps the detail
#: server-side. Raising ValueError for a guardrail refusal therefore tells the
#: agent nothing at all -- it cannot see which bound it broke, so it cannot
#: propose something smaller, and will most likely try the same call again.
DELIBERATE_REFUSALS = (
    ValueError,
    LookupError,
    PreviewExpired,
    PreviewStale,
    MutationError,
)


def refusals_reach_the_caller(fn):
    """Re-raise a deliberate refusal as ToolError so its message survives."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except DELIBERATE_REFUSALS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


PREVIEW_SAFE = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=True
)

#: destructive_hint is set only where the change moves money or stops delivery.
#: Marking a reversible action destructive would train someone to click through
#: the loud prompts, which is how loud prompts stop working.
APPLY_SPENDS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

#: Reverting restores a previous value; it is a write, but not a new risk.
APPLY_REVERSIBLE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

SETTABLE_STATUSES = ("ENABLED", "PAUSED")


def money(micros_value: Any) -> Decimal:
    """A raw micros field from a proto row, as currency units.

    *** THIS IS NOT OPTIONAL. *** `ReadOnlyGoogleAdsClient.rows()` returns
    flattened dicts with money ALREADY converted, but `.query()` -- which
    campaigns.find() and adgroups.find() use -- returns the raw proto, where a
    $1.00 budget is the integer 1000000. Reading one of those straight into a
    preview renders "1000000.00 CAD" as the before-value in the text a human is
    about to approve, and turns every derived number (the delta, the percentage
    change) into nonsense in the same breath.
    """
    return Decimal(str(from_micros(int(micros_value or 0))))


def parse_decimal(raw: str, name: str) -> Decimal:
    """Money arrives as a decimal string in major units, never as micros.

    The argument is rendered verbatim in the prompt a human approves, so "2.50"
    has to mean two fifty. Accepting a float here would also let 0.1 + 0.2
    arrive as 0.30000000000000004 in the approval text.
    """
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(
            f"{name} must be a decimal string in the account's currency, "
            f'e.g. "2.50" -- got {raw!r}. Never pass micros.'
        ) from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative, got {value}.")
    if value > Decimal("1000"):
        # 2500000 is what you get typing $2.50 as micros. It is a valid Decimal
        # and a catastrophic budget, so it is caught before the ceiling check,
        # where the message can actually say what went wrong.
        raise ValueError(
            f"{name} of {value} looks like micros, not currency. "
            f"If you meant {value / 1_000_000}, pass that."
        )
    return value


def register(mcp, state) -> None:  # noqa: C901 - a flat list of tool definitions
    """Attach every write tool to ``mcp``."""

    write_client = MutatingGoogleAdsClient.from_env()
    state.write_client = write_client
    read = state.read_client
    default_customer = state.default_customer_id

    # -- shared helpers ---------------------------------------------------

    def metrics_7d(where: str) -> dict[str, Any]:
        """Last 7 whole days for one entity, for context in the preview."""
        from googleads_reporting.query import last_n_days

        rows = read.rows(
            Query(
                select=("metrics.cost_micros", "metrics.clicks", "metrics.impressions",
                        "metrics.conversions"),
                from_resource="campaign" if where.startswith("campaign") else "ad_group",
                where=(where, last_n_days(7)),
            )
        )
        return {
            "cost": f"{sum(Decimal(str(r['metrics.cost_micros'] or 0)) for r in rows):.2f}",
            "clicks": sum(int(r["metrics.clicks"] or 0) for r in rows),
            "impressions": sum(int(r["metrics.impressions"] or 0) for r in rows),
            "conversions": f"{sum(Decimal(str(r['metrics.conversions'] or 0)) for r in rows):.1f}",
        }

    def currency_of() -> str:
        rows = read.rows(
            Query(select=("customer.currency_code",), from_resource="customer")
        )
        return rows[0]["customer.currency_code"] if rows else ""

    def build_preview(
        *,
        tool: str,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        path: str,
        field_name: str,
        before: str,
        after: str,
        checks: list[Check],
        projected: Decimal,
        payload: dict[str, Any],
        campaign_status: str | None = None,
        metrics: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        note: str | None = None,
    ) -> ChangePreview:
        blocked = any_failed(checks)
        token = state.previews.mint(
            tool=tool, payload=payload, projected_delta=projected, blocked=blocked
        )
        return ChangePreview(
            tool=tool,
            preview_token=token,
            expires_in_seconds=600,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_name=entity_name,
            path=path,
            field_name=field_name,
            before=before,
            after=after,
            customer_id=default_customer,
            campaign_status=campaign_status,
            metrics_7d=metrics,
            projected_daily_spend_delta=f"{projected:.2f}",
            checks=render_checks(checks),
            blocked=blocked,
            warnings=warnings or [],
            note=note,
        )

    def commit(
        pending, *, tool: str, mutate, entity: dict[str, Any], current_before: str
    ) -> ApplyResult:
        """Session check, staleness check, mutate, ledger. In that order."""
        payload = pending.payload
        if payload["before"] != current_before:
            raise PreviewStale(
                f"{entity['entity_name']} has changed since the preview: it was "
                f"{payload['before']!r} then and is {current_before!r} now. "
                "Someone edited it in between. Take a fresh preview."
            )
        blocked = state.counters.would_exceed(pending.projected_delta)
        if blocked:
            raise PreviewExpired(blocked)

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id=entry_id,
            tool=tool,
            customer_id=default_customer,
            entity_type=entity["entity_type"],
            entity_id=str(entity["entity_id"]),
            entity_name=entity["entity_name"],
            field_name=entity["field_name"],
            before=payload["before"],
            after=payload["after"],
            projected_delta=str(pending.projected_delta),
        )
        try:
            result = mutate()
        except Exception as exc:
            ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
            raise
        resource = result.resource_names[0] if result.resource_names else None
        ledger.write_outcome(entry_id=entry_id, ok=True, resource_name=resource)
        state.counters.record(pending.projected_delta)

        return ApplyResult(
            applied=True,
            entry_id=entry_id,
            entity_type=entity["entity_type"],
            entity_id=str(entity["entity_id"]),
            entity_name=entity["entity_name"],
            field_name=entity["field_name"],
            before=payload["before"],
            after=payload["after"],
            resource_name=resource,
            session=state.counters.snapshot(),
        )

    # -- campaign status --------------------------------------------------

    @mcp.tool(title="Preview pausing or enabling a campaign", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_campaign_status(
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or enabling a campaign would do. Writes nothing."""
        wanted = new_status.upper()
        if wanted not in SETTABLE_STATUSES:
            raise ValueError(
                f"new_status must be ENABLED or PAUSED, got {new_status!r}. "
                "REMOVED is not available here: it is irreversible in Google "
                "Ads, and PAUSED achieves the same thing reversibly."
            )
        row = campaigns.find(write_client, campaign_id=campaign_id)
        before = row.campaign.status.name
        metrics = metrics_7d(f"campaign.id = {int(campaign_id)}")

        projected = (
            project_pause(Decimal(metrics["cost"]))
            if wanted == "PAUSED"
            # Re-enabling can resume spend up to the daily budget.
            else money(row.campaign_budget.amount_micros)
        )
        checks = [
            check_writes_enabled(),
            check_customer_in_scope(state.limits, default_customer, default_customer),
            check_campaign_in_scope(state.limits, str(row.campaign.id)),
            check_projected_delta(state.limits, projected),
        ]
        warnings = []
        if before == wanted:
            warnings.append(f"already {wanted}; applying would be a no-op")
        if wanted == "ENABLED":
            warnings.append(
                "enabling resumes delivery immediately, up to the daily budget"
            )

        return build_preview(
            tool="apply_campaign_status",
            entity_type="campaign",
            entity_id=str(row.campaign.id),
            entity_name=row.campaign.name,
            path=row.campaign.name,
            field_name="status",
            before=before,
            after=wanted,
            checks=checks,
            projected=projected,
            metrics=metrics,
            campaign_status=before,
            warnings=warnings,
            payload={
                "campaign_id": str(row.campaign.id),
                "before": before,
                "after": wanted,
                "name": row.campaign.name,
            },
            note=(
                "Pausing cannot increase spend, so its projected delta is 0. "
                "Enabling is bounded by the daily budget, not by past spend."
            ),
        )

    @mcp.tool(
        title="Apply a campaign status change (AFFECTS DELIVERY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_campaign_status(
        preview_token: Annotated[str, Field(description="From preview_campaign_status")],
    ) -> ApplyResult:
        """Pause or enable the campaign the preview named."""
        pending = state.previews.take(preview_token, "apply_campaign_status")
        payload = pending.payload
        row = campaigns.find(write_client, campaign_id=payload["campaign_id"])
        return commit(
            pending,
            tool="apply_campaign_status",
            current_before=row.campaign.status.name,
            entity={
                "entity_type": "campaign",
                "entity_id": payload["campaign_id"],
                "entity_name": payload["name"],
                "field_name": "status",
            },
            mutate=lambda: execute(
                write_client,
                campaigns.plan_set_status(
                    write_client,
                    campaign_id=payload["campaign_id"],
                    status=payload["after"],
                ),
                apply=True,
            ),
        )

    # -- campaign daily budget --------------------------------------------

    @mcp.tool(title="Preview a campaign daily budget change", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_campaign_daily_budget(
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_daily_budget: Annotated[
            str, Field(description='Decimal string in account currency, e.g. "25.00"')
        ],
    ) -> ChangePreview:
        """Show what changing a campaign's daily budget would do. Writes nothing.

        The delta is EXACT, not an estimate: a daily budget is the cap itself
        moving, not a guess about behaviour.
        """
        after = parse_decimal(new_daily_budget, "new_daily_budget")
        row = campaigns.find(write_client, campaign_id=campaign_id)
        before = money(row.campaign_budget.amount_micros)
        currency = currency_of()
        metrics = metrics_7d(f"campaign.id = {int(campaign_id)}")

        budget_row = budgets._budget_row(write_client, str(row.campaign_budget.id))
        shared = bool(budget_row.explicitly_shared)

        projected = project_budget_change(before, after)
        checks = [
            check_writes_enabled(),
            check_customer_in_scope(state.limits, default_customer, default_customer),
            check_campaign_in_scope(state.limits, str(row.campaign.id)),
            check_currency(state.limits, currency),
            check_ceiling("max_daily_budget", after, state.limits.max_daily_budget, currency),
            check_pct_increase(
                "max_budget_change_pct", before, after, state.limits.max_budget_change_pct
            ),
            check_projected_delta(state.limits, max(projected, Decimal("0"))),
            Check(
                "budget_not_shared",
                not shared,
                "exclusive to this campaign" if not shared
                else "SHARED -- would change every campaign using it",
            ),
        ]
        warnings = []
        if after < before:
            warnings.append(
                "Lowering a daily budget can stop delivery part-way through "
                "today if the campaign has already spent more than the new cap."
            )

        return build_preview(
            tool="apply_campaign_daily_budget",
            entity_type="campaign",
            entity_id=str(row.campaign.id),
            entity_name=row.campaign.name,
            path=f"{row.campaign.name} > budget {row.campaign_budget.id}",
            field_name="daily_budget",
            before=f"{before:.2f} {currency}".strip(),
            after=f"{after:.2f} {currency}".strip(),
            checks=checks,
            projected=projected,
            metrics=metrics,
            campaign_status=row.campaign.status.name,
            warnings=warnings,
            payload={
                "campaign_id": str(row.campaign.id),
                "budget_id": str(row.campaign_budget.id),
                "before": f"{before:.2f} {currency}".strip(),
                "after": f"{after:.2f} {currency}".strip(),
                "after_amount": str(after),
                "name": row.campaign.name,
            },
            note=(
                f"{pct_change(before, after) or 'n/a'} change. Compare the delta "
                "with the last 7 days' spend: if the campaign never reached the "
                "old cap, raising it may change nothing."
            ),
        )

    @mcp.tool(
        title="Apply a campaign daily budget change (SPENDS MONEY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_campaign_daily_budget(
        preview_token: Annotated[
            str, Field(description="From preview_campaign_daily_budget")
        ],
    ) -> ApplyResult:
        """Change the daily budget the preview named."""
        pending = state.previews.take(preview_token, "apply_campaign_daily_budget")
        payload = pending.payload
        row = campaigns.find(write_client, campaign_id=payload["campaign_id"])
        current = money(row.campaign_budget.amount_micros)
        currency = currency_of()
        return commit(
            pending,
            tool="apply_campaign_daily_budget",
            current_before=f"{current:.2f} {currency}".strip(),
            entity={
                "entity_type": "campaign",
                "entity_id": payload["campaign_id"],
                "entity_name": payload["name"],
                "field_name": "daily_budget",
            },
            mutate=lambda: execute(
                write_client,
                campaigns.plan_set_budget(
                    write_client,
                    campaign_id=payload["campaign_id"],
                    amount=float(payload["after_amount"]),
                ),
                apply=True,
            ),
        )

    # -- ad group status ---------------------------------------------------

    @mcp.tool(title="Preview pausing or enabling an ad group", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_ad_group_status(
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        new_status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or enabling an ad group would do. Writes nothing."""
        wanted = new_status.upper()
        if wanted not in SETTABLE_STATUSES:
            raise ValueError("new_status must be ENABLED or PAUSED. REMOVED is irreversible.")
        row = adgroups.find(write_client, ad_group_id=ad_group_id)
        before = row.ad_group.status.name
        metrics = metrics_7d(f"ad_group.id = {int(ad_group_id)}")
        projected = (
            project_pause(Decimal(metrics["cost"]))
            if wanted == "PAUSED"
            else Decimal(metrics["cost"]) / 7
        )
        checks = [
            check_writes_enabled(),
            check_customer_in_scope(state.limits, default_customer, default_customer),
            check_campaign_in_scope(state.limits, str(row.campaign.id)),
            check_projected_delta(state.limits, projected),
        ]
        return build_preview(
            tool="apply_ad_group_status",
            entity_type="ad group",
            entity_id=str(row.ad_group.id),
            entity_name=row.ad_group.name,
            path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="status",
            before=before,
            after=wanted,
            checks=checks,
            projected=projected,
            metrics=metrics,
            warnings=["already " + wanted] if before == wanted else [],
            payload={
                "ad_group_id": str(row.ad_group.id),
                "before": before,
                "after": wanted,
                "name": row.ad_group.name,
            },
        )

    @mcp.tool(
        title="Apply an ad group status change (AFFECTS DELIVERY)",
        annotations=APPLY_SPENDS,
    )
    @refusals_reach_the_caller
    def apply_ad_group_status(
        preview_token: Annotated[str, Field(description="From preview_ad_group_status")],
    ) -> ApplyResult:
        """Pause or enable the ad group the preview named."""
        pending = state.previews.take(preview_token, "apply_ad_group_status")
        payload = pending.payload
        row = adgroups.find(write_client, ad_group_id=payload["ad_group_id"])
        return commit(
            pending,
            tool="apply_ad_group_status",
            current_before=row.ad_group.status.name,
            entity={
                "entity_type": "ad group",
                "entity_id": payload["ad_group_id"],
                "entity_name": payload["name"],
                "field_name": "status",
            },
            mutate=lambda: execute(
                write_client,
                adgroups.plan_set_status(
                    write_client,
                    ad_group_id=payload["ad_group_id"],
                    status=payload["after"],
                ),
                apply=True,
            ),
        )

    # -- ad group bid ------------------------------------------------------

    @mcp.tool(title="Preview an ad group CPC bid change", annotations=PREVIEW_SAFE)
    @refusals_reach_the_caller
    def preview_ad_group_cpc_bid(
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        new_cpc_bid: Annotated[
            str, Field(description='Decimal string, e.g. "0.75". Never micros.')
        ],
    ) -> ChangePreview:
        """Show what changing an ad group's max CPC would do. Writes nothing."""
        after = parse_decimal(new_cpc_bid, "new_cpc_bid")
        row = adgroups.find(write_client, ad_group_id=ad_group_id)
        before = money(row.ad_group.cpc_bid_micros)
        currency = currency_of()
        metrics = metrics_7d(f"ad_group.id = {int(ad_group_id)}")
        projected = project_bid_change(before, after, int(metrics["clicks"]))

        checks = [
            check_writes_enabled(),
            check_customer_in_scope(state.limits, default_customer, default_customer),
            check_campaign_in_scope(state.limits, str(row.campaign.id)),
            check_currency(state.limits, currency),
            check_ceiling("max_cpc_bid", after, state.limits.max_cpc_bid, currency),
            check_pct_increase(
                "max_bid_increase_pct", before, after, state.limits.max_bid_increase_pct
            ),
            check_projected_delta(state.limits, max(projected, Decimal("0"))),
        ]
        return build_preview(
            tool="apply_ad_group_cpc_bid",
            entity_type="ad group",
            entity_id=str(row.ad_group.id),
            entity_name=row.ad_group.name,
            path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="cpc_bid",
            before=f"{before:.2f} {currency}".strip(),
            after=f"{after:.2f} {currency}".strip(),
            checks=checks,
            projected=projected,
            metrics=metrics,
            payload={
                "ad_group_id": str(row.ad_group.id),
                "before": f"{before:.2f} {currency}".strip(),
                "after": f"{after:.2f} {currency}".strip(),
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

    @mcp.tool(
        title="Apply an ad group CPC bid change (SPENDS MONEY)", annotations=APPLY_SPENDS
    )
    @refusals_reach_the_caller
    def apply_ad_group_cpc_bid(
        preview_token: Annotated[str, Field(description="From preview_ad_group_cpc_bid")],
    ) -> ApplyResult:
        """Change the bid the preview named."""
        pending = state.previews.take(preview_token, "apply_ad_group_cpc_bid")
        payload = pending.payload
        row = adgroups.find(write_client, ad_group_id=payload["ad_group_id"])
        current = money(row.ad_group.cpc_bid_micros)
        currency = currency_of()
        return commit(
            pending,
            tool="apply_ad_group_cpc_bid",
            current_before=f"{current:.2f} {currency}".strip(),
            entity={
                "entity_type": "ad group",
                "entity_id": payload["ad_group_id"],
                "entity_name": payload["name"],
                "field_name": "cpc_bid",
            },
            mutate=lambda: execute(
                write_client,
                adgroups.plan_set_cpc_bid(
                    write_client,
                    ad_group_id=payload["ad_group_id"],
                    amount=float(payload["after_amount"]),
                ),
                apply=True,
            ),
        )

    # -- revert ------------------------------------------------------------

    @mcp.tool(title="Revert a change by its ledger entry id", annotations=APPLY_REVERSIBLE)
    @refusals_reach_the_caller
    def revert_change(
        entry_id: Annotated[str, Field(description="From list_my_changes")],
    ) -> ApplyResult:
        """Put a value back to what it was before an earlier change.

        Reverting is itself a change and is itself recorded, so the ledger
        stays a complete history rather than a list of things that are still
        true.
        """
        entry = ledger.find_entry(entry_id)
        if entry is None:
            raise ValueError(f"No ledger entry {entry_id!r}. See list_my_changes.")
        if not entry.get("applied"):
            raise ValueError(f"Entry {entry_id} was never applied; nothing to revert.")
        if entry_id in ledger.reverted_entry_ids():
            raise ValueError(f"Entry {entry_id} has already been reverted.")

        field_name = entry["field"]
        before = entry["before"]
        new_entry = ledger.new_entry_id()
        ledger.write_intent(
            entry_id=new_entry,
            tool="revert_change",
            customer_id=default_customer,
            entity_type=entry["entity_type"],
            entity_id=entry["entity_id"],
            entity_name=entry["entity_name"],
            field_name=field_name,
            before=entry["after"],
            after=before,
            projected_delta="0",
        )
        try:
            if field_name == "status" and entry["entity_type"] == "campaign":
                plan = campaigns.plan_set_status(
                    write_client, campaign_id=entry["entity_id"], status=before
                )
            elif field_name == "status":
                plan = adgroups.plan_set_status(
                    write_client, ad_group_id=entry["entity_id"], status=before
                )
            elif field_name == "daily_budget":
                plan = campaigns.plan_set_budget(
                    write_client,
                    campaign_id=entry["entity_id"],
                    amount=float(before.split()[0]),
                )
            elif field_name == "cpc_bid":
                plan = adgroups.plan_set_cpc_bid(
                    write_client,
                    ad_group_id=entry["entity_id"],
                    amount=float(before.split()[0]),
                )
            else:
                raise ValueError(f"Cannot revert field {field_name!r} automatically.")
            result = execute(write_client, plan, apply=True)
        except Exception as exc:
            ledger.write_outcome(entry_id=new_entry, ok=False, error=str(exc))
            raise
        resource = result.resource_names[0] if result.resource_names else None
        ledger.write_outcome(
            entry_id=new_entry, ok=True, resource_name=resource, reverts=entry_id
        )
        state.counters.record(Decimal("0"))

        return ApplyResult(
            applied=True,
            entry_id=new_entry,
            entity_type=entry["entity_type"],
            entity_id=entry["entity_id"],
            entity_name=entry["entity_name"],
            field_name=field_name,
            before=entry["after"],
            after=before,
            resource_name=resource,
            session=state.counters.snapshot(),
        )
