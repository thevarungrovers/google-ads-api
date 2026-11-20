"""What every change kind is handed: the clients, the limits, and the helpers.

One object rather than a dozen closures, so a kind is a plain class that can be
tested with a fake context, and so the engine and the kinds agree on where the
write client, the counters and the daily budget live.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from googleads_mcp.guardrails import Check, DailyUsage
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

    # -- naming ------------------------------------------------------------

    @property
    def identity(self):
        """Whose prefix goes on created names (from .env; see naming.py)."""
        from googleads_reporting.naming import Identity

        return getattr(self.state, "identity", None) or Identity()

    def name_for_create(self, entity_type: str, requested: str):
        """The final name for a new ``entity_type``: a naming.Stamped.

        Refuses (NameRefused, a ValueError) a name that cannot be used at all;
        a name that is only too long, or an identity that is malformed, comes
        back with problems the engine turns into FAIL lines.
        """
        from googleads_reporting import naming

        return naming.stamp(self.identity, requested, entity_type=entity_type)

    def name_for_rename(self, entity_type: str, current: str, requested: str, *,
                        entity_id: str):
        """The final name for renaming an existing entity: a naming.Stamped.

        Only OUR entities may be renamed -- the live name carries our prefix,
        or the ledger shows this server created it. Anything else comes back
        with an ``own_entity`` problem, a FAIL line in the preview.
        """
        from googleads_mcp import ledger
        from googleads_reporting import naming

        owned = naming.owns(self.identity, current) or ledger.created_by_us(
            entity_type, entity_id)
        return naming.rename(self.identity, current, requested, entity_type=entity_type,
                             ledger_owned=owned)

    def target_customer(self) -> str:
        """The customer a write will actually be sent to.

        Asked of the WRITE client, which is what resolves the customer id on
        every mutate -- not read off the server's default. The scope check used
        to be called as check_customer_in_scope(limits, default, default),
        comparing a value with itself; with an empty allowlist that could never
        fail, whichever account the writes were really going to.
        """
        resolve = getattr(self.client, "resolve_customer_id", None)
        if callable(resolve):
            return resolve()
        return self.default_customer

    # -- sending ---------------------------------------------------------

    def send(self, plan, *, apply: bool):
        return self._execute(self.client, plan, apply=apply)

    def validate(self, build: Callable[[], Any]) -> Check:
        """Build the mutation and send it with validate_only=True.

        Google runs every server-side check -- policy, field limits, a bid
        under the minimum, a keyword already present -- and changes nothing.
        A refusal becomes a FAIL line in the preview the human reads, rather
        than an error after they approved it.

        Building happens in here too, because a plan function can refuse on
        its own ("already PAUSED; nothing to do") and that refusal belongs on
        the same line.
        """
        from googleads_reporting.write.client import MutationError

        try:
            self.send(build(), apply=False)
        except (MutationError, ValueError, LookupError) as exc:
            return Check("google_validate_only", False, str(exc).strip())
        return Check("google_validate_only", True, "Google accepted the request (validate_only)")

    # -- caps ------------------------------------------------------------

    @property
    def daily(self) -> DailyUsage:
        # Built per use, not cached: it holds no state, only the limits, and
        # every answer comes from the ledger.
        return DailyUsage(self.limits)

    def daily_would_exceed(self, projected: Decimal, *, weight: int = 1) -> str | None:
        return self.daily.would_exceed(projected, weight)

    def cap_warnings(self, projected: Decimal, weight: int) -> list[str]:
        """The session and daily caps, as warnings on the preview.

        Warnings rather than FAIL checks on purpose: the caps are enforced at
        APPLY time (another process may use up the day in between), and an
        apply refused for the cap says which cap, where a blocked token could
        only say "failed a guardrail".
        """
        warnings = []
        for reason in (
            self.counters.would_exceed(projected, weight=weight),
            self.daily_would_exceed(projected, weight=weight),
        ):
            if reason:
                warnings.append(f"the apply will be REFUSED: {reason}")
        return warnings

    def daily_snapshot(self) -> dict[str, Any] | None:
        return self.daily.snapshot()

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
