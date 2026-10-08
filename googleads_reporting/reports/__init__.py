"""Report definitions.

A report is declarative: a resource, a SELECT list and a default ordering. It
holds no client and makes no request, so a report can be inspected, diffed and
schema-checked offline -- ``tests/test_reports.py`` resolves every field path
against the real ``GoogleAdsRow`` descriptor, which catches a typo without a
single API call.

Add a report by defining one in a module here and listing it in
:data:`REGISTRY`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fields import money_column_name, unknown_money_fields
from ..query import Query, QueryError, during


@dataclass(frozen=True)
class Report:
    """A named, read-only report.

    ``select`` order is the output column order.
    """

    name: str
    description: str
    resource: str
    select: tuple[str, ...]
    order_by: tuple[str, ...] = ()
    base_where: tuple[str, ...] = ()
    #: False for resources with no ``segments.date`` (account listings, say).
    supports_date_range: bool = True
    default_range: str = "LAST_30_DAYS"

    def __post_init__(self) -> None:
        if not self.select:
            raise QueryError(f"Report {self.name!r} selects no fields.")
        # Fail at import time rather than emitting a column that is silently
        # off by a factor of a million.
        unruled = unknown_money_fields(self.select)
        if unruled:
            raise QueryError(
                f"Report {self.name!r} selects monetary field(s) with no micros "
                f"ruling in googleads_reporting.fields: {', '.join(unruled)}. "
                "Add them to MICROS_FLOAT_METRICS or CURRENCY_UNIT_METRICS."
            )

    @property
    def columns(self) -> tuple[str, ...]:
        """Output column names: like ``select``, but with ``_micros`` dropped.

        A converted column must not keep a name that says micros, or the next
        consumer multiplies it back up.
        """
        return tuple(money_column_name(path) for path in self.select)

    def build(
        self,
        *,
        date_condition: str | None = None,
        extra_where: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> Query:
        """Assemble this report's :class:`~googleads_reporting.query.Query`."""
        where = list(self.base_where)

        if self.supports_date_range:
            where.append(date_condition or during(self.default_range))
        elif date_condition:
            raise QueryError(
                f"Report {self.name!r} has no date segment, so a date range "
                "cannot be applied to it."
            )

        where.extend(extra_where)

        return Query(
            select=self.select,
            from_resource=self.resource,
            where=tuple(where),
            order_by=self.order_by,
            limit=limit,
        )


from .accounts import ACCOUNTS  # noqa: E402  (import after Report is defined)
from .campaigns import CAMPAIGNS  # noqa: E402
from .keywords import KEYWORDS  # noqa: E402

REGISTRY: dict[str, Report] = {
    report.name: report for report in (ACCOUNTS, CAMPAIGNS, KEYWORDS)
}


def report_names() -> list[str]:
    return sorted(REGISTRY)


def get_report(name: str) -> Report:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown report {name!r}. Available: {', '.join(report_names())}."
        ) from None


__all__ = ["Report", "REGISTRY", "get_report", "report_names"]
