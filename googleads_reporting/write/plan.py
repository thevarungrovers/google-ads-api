"""Planned changes: what a mutation would do, before it does it.

A :class:`PlannedChange` carries both halves of a mutation -- the protos the
API needs, and a human-readable diff. Keeping them together is what lets the
confirmation prompt describe exactly the operations that will be sent, rather
than a separately-written summary that can drift from them.

Nothing here touches the API. :func:`execute` does, and only with ``apply=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from google.api_core import protobuf_helpers

from .client import MutatingGoogleAdsClient, MutationResult


@dataclass(frozen=True)
class FieldChange:
    """One field moving from ``before`` to ``after``."""

    field: str
    before: Any
    after: Any

    def render(self) -> str:
        if self.before is None:
            return f"{self.field}: {self.after!r}"
        return f"{self.field}: {self.before!r} -> {self.after!r}"


@dataclass
class PlannedChange:
    """A mutation that has been built but not sent."""

    action: str
    entity: str
    label: str
    service: str
    method: str
    operations: list[Any] = field(default_factory=list)
    changes: list[FieldChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"{self.action.upper()} {self.entity}: {self.label}"]
        lines.extend(f"    {change.render()}" for change in self.changes)
        lines.extend(f"    ! {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def describe(self) -> list[dict[str, Any]]:
        """A JSON-safe rendering for the audit log."""
        return [
            {
                "action": self.action,
                "entity": self.entity,
                "label": self.label,
                "changes": [
                    {"field": c.field, "before": c.before, "after": c.after}
                    for c in self.changes
                ],
            }
        ]


def execute(
    client: MutatingGoogleAdsClient,
    plan: PlannedChange,
    *,
    apply: bool = False,
    customer_id: str | None = None,
) -> MutationResult:
    """Send ``plan``. Validates server-side unless ``apply`` is True."""
    return client.mutate(
        service_name=plan.service,
        method=plan.method,
        operations=plan.operations,
        customer_id=customer_id,
        apply=apply,
        describe=plan.describe(),
    )


def set_update_mask(client: MutatingGoogleAdsClient, operation: Any) -> None:
    """Populate ``update_mask`` from the fields actually set on ``update``.

    Google Ads requires an explicit mask on every update: without it the
    request is rejected, and with the WRONG one it silently clears every field
    the mask names but the payload does not set. Deriving it from the populated
    message is the only version that cannot disagree with the payload.
    """
    client.raw.copy_from(
        operation.update_mask,
        protobuf_helpers.field_mask(None, operation.update._pb),
    )


def micros(amount: float) -> int:
    """Currency units -> micros, for sending money to the API.

    The inverse of :func:`googleads_reporting.fields.from_micros`. Rounded, not
    truncated: ``2.01 * 1_000_000`` is ``2009999.9999999998`` in binary floating
    point, so ``int()`` would send 2009999 micros -- a bid one ten-thousandth
    of a cent under what was asked for, on 151 of the first 10,000 cent values.
    Small, silent, and wrong in the direction of "why does this not match what
    I typed".
    """
    return int(round(amount * 1_000_000))
