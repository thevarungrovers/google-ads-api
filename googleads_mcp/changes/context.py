"""What every change kind is handed: the clients, the limits, and the helpers.

One object rather than a dozen closures, so a kind is a plain class that can be
tested with a fake context, and so the engine and the kinds agree on where the
write client, the counters and the daily budget live.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from googleads_mcp.guardrails import Check
from googleads_reporting.query import Query, last_n_days


class WriteContext:
    def __init__(self, state, client, execute: Callable[..., Any]) -> None:
        self.state = state
        self.client = client
        # `execute` is handed in rather than imported so that it is resolved
        # through tools_write at CALL time: the write-path tests replace
        # tools_write.execute with a recorder, and a reference captured here at
        # import would keep calling the real one.
        self._execute = execute

    # -- state -----------------------------------------------------------

    @property
    def read(self):
        return self.state.read_client

    @property
    def limits(self):
        return self.state.limits

    @property
    def counters(self):
        return self.state.counters

    @property
    def previews(self):
        return self.state.previews

    @property
    def default_customer(self) -> str:
        return self.state.default_customer_id

    def target_customer(self) -> str:
        """The customer a write will actually be sent to."""
        return self.default_customer

    # -- sending ---------------------------------------------------------

    def send(self, plan, *, apply: bool):
        return self._execute(self.client, plan, apply=apply)

    def validate(self, plan) -> Check:  # pragma: no cover - replaced in Phase 1
        return Check("google_validate_only", True, "not run")

    # -- caps (the daily half arrives with the persistent cap) -------------

    def daily_would_exceed(self, projected: Decimal, *, weight: int = 1) -> str | None:
        return None

    def cap_checks(self, projected: Decimal, weight: int) -> list[Check]:
        return []

    def daily_snapshot(self) -> dict[str, Any] | None:
        return None

    # -- reads kinds share -------------------------------------------------

    def metrics_7d(self, where: str) -> dict[str, Any]:
        """Last 7 whole days for one entity, for context in the preview."""
        rows = self.read.rows(
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

    def currency_of(self) -> str:
        rows = self.read.rows(
            Query(select=("customer.currency_code",), from_resource="customer")
        )
        return rows[0]["customer.currency_code"] if rows else ""
