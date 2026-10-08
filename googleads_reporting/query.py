"""GAQL query construction.

GAQL has no mutating form -- the grammar only admits ``SELECT`` -- so building
queries here keeps the reporting path read-only by construction rather than
by convention.
:meth:`Query.to_gaql` emits nothing else, and :func:`assert_select_only` is the
belt-and-braces check for a query string that arrived from somewhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Sequence


class QueryError(ValueError):
    """Raised when a query cannot be built or is not a plain SELECT."""


_FIELD_PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

#: Statements that must never appear. GAQL does not support them, but a string
#: assembled from user input could still carry one, and the API would reject it
#: in a way that reads like a syntax error rather than a blocked write.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MUTATE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def assert_select_only(gaql: str) -> str:
    """Return ``gaql`` unchanged, or raise if it is not a bare SELECT.

    >>> assert_select_only("SELECT campaign.id FROM campaign")
    'SELECT campaign.id FROM campaign'
    >>> assert_select_only("UPDATE campaign SET x = 1")
    Traceback (most recent call last):
    ...
    googleads_reporting.query.QueryError: Only SELECT queries are allowed...
    """
    stripped = gaql.strip()
    if not stripped:
        raise QueryError("Query is empty.")
    if not stripped[:6].upper() == "SELECT":
        raise QueryError(
            f"Only SELECT queries are allowed here; got {stripped[:40]!r}."
        )
    found = _FORBIDDEN.search(stripped)
    if found:
        raise QueryError(
            f"Only SELECT queries are allowed here; found "
            f"{found.group(0).upper()!r} in the query."
        )
    if ";" in stripped.rstrip(";"):
        raise QueryError("Query contains ';'; statement chaining is not allowed.")
    return gaql


def parse_select(gaql: str) -> tuple[str, ...]:
    """Return the field paths in a query's SELECT clause, in order.

    Used to drive column order, so the output columns match the query the
    report declared rather than whatever order the API happens to populate.

    >>> parse_select("SELECT campaign.id, metrics.clicks FROM campaign")
    ('campaign.id', 'metrics.clicks')
    """
    assert_select_only(gaql)
    match = re.search(r"\bSELECT\b(.*?)\bFROM\b", gaql, re.IGNORECASE | re.DOTALL)
    if not match:
        raise QueryError("Query has no FROM clause.")
    paths = [part.strip() for part in match.group(1).split(",")]
    return tuple(path for path in paths if path)


def _validate_field_path(path: str) -> str:
    if not _FIELD_PATH.match(path):
        raise QueryError(
            f"{path!r} is not a valid GAQL field path "
            "(expected lowercase dotted form such as 'metrics.cost_micros')."
        )
    return path


@dataclass(frozen=True)
class Query:
    """A GAQL SELECT, assembled from parts.

    >>> print(Query(
    ...     select=("campaign.id", "metrics.clicks"),
    ...     from_resource="campaign",
    ...     where=("segments.date DURING LAST_7_DAYS",),
    ...     order_by=("metrics.clicks DESC",),
    ...     limit=10,
    ... ).to_gaql())
    SELECT campaign.id, metrics.clicks
    FROM campaign
    WHERE segments.date DURING LAST_7_DAYS
    ORDER BY metrics.clicks DESC
    LIMIT 10
    """

    select: tuple[str, ...]
    from_resource: str
    where: tuple[str, ...] = ()
    order_by: tuple[str, ...] = ()
    limit: int | None = None

    def __post_init__(self) -> None:
        if not self.select:
            raise QueryError("A query must select at least one field.")
        for path in self.select:
            _validate_field_path(path)
        if not re.match(r"^[a-z][a-z0-9_]*$", self.from_resource):
            raise QueryError(
                f"{self.from_resource!r} is not a valid resource name."
            )
        if self.limit is not None and self.limit <= 0:
            raise QueryError(f"LIMIT must be positive, got {self.limit}.")

    def to_gaql(self) -> str:
        lines = [
            "SELECT " + ", ".join(self.select),
            f"FROM {self.from_resource}",
        ]
        if self.where:
            lines.append("WHERE " + "\n  AND ".join(self.where))
        if self.order_by:
            lines.append("ORDER BY " + ", ".join(self.order_by))
        if self.limit is not None:
            lines.append(f"LIMIT {self.limit}")
        return assert_select_only("\n".join(lines))

    def with_where(self, *conditions: str) -> "Query":
        """Return a copy with extra WHERE conditions appended."""
        from dataclasses import replace

        return replace(self, where=self.where + tuple(conditions))


# ---------------------------------------------------------------------------
# Date ranges
# ---------------------------------------------------------------------------

#: GAQL's own relative ranges. Preferred over computing dates locally, because
#: they are evaluated in the ACCOUNT's time zone -- this machine's clock and the
#: account's may well disagree about which day "yesterday" is.
PRESET_RANGES = (
    "TODAY",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "THIS_WEEK_SUN_TODAY",
    "THIS_WEEK_MON_TODAY",
    "LAST_WEEK_SUN_SAT",
    "LAST_WEEK_MON_SUN",
    "THIS_MONTH",
    "LAST_MONTH",
    "LAST_BUSINESS_WEEK",
    "ALL_TIME",
)


def during(preset: str) -> str:
    """A ``segments.date DURING <preset>`` condition.

    >>> during("LAST_30_DAYS")
    'segments.date DURING LAST_30_DAYS'
    """
    name = preset.strip().upper()
    if name not in PRESET_RANGES:
        raise QueryError(
            f"{preset!r} is not a GAQL date preset. "
            f"Choose one of: {', '.join(PRESET_RANGES)}."
        )
    return f"segments.date DURING {name}"


def between(start: date | str, end: date | str) -> str:
    """A ``segments.date BETWEEN 'start' AND 'end'`` condition.

    >>> between("2026-09-01", "2026-09-30")
    "segments.date BETWEEN '2026-09-01' AND '2026-09-30'"
    """
    start_iso = _as_iso(start, "start")
    end_iso = _as_iso(end, "end")
    if start_iso > end_iso:
        raise QueryError(
            f"start {start_iso} is after end {end_iso}."
        )
    return f"segments.date BETWEEN '{start_iso}' AND '{end_iso}'"


def last_n_days(days: int, *, today: date | None = None) -> str:
    """A ``BETWEEN`` condition covering the ``days`` days ending yesterday.

    Yesterday, not today: today's metrics are partial all day, and a partial
    day silently drags every average down.

    >>> last_n_days(7, today=date(2026, 10, 8))
    "segments.date BETWEEN '2026-10-01' AND '2026-10-07'"
    """
    if days <= 0:
        raise QueryError(f"days must be positive, got {days}.")
    anchor = today or date.today()
    end = anchor - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return between(start, end)


def _as_iso(value: date | str, label: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise QueryError(
            f"{label} must be a date or an ISO YYYY-MM-DD string, got {value!r}."
        ) from exc


def quote_literal(value: str) -> str:
    """Quote a string for a GAQL literal, rejecting anything that could escape.

    GAQL has no parameter binding, so any value interpolated into a condition
    goes through here. Rather than attempting to escape quotes, this refuses
    them: no report in this project needs a quote inside a literal, and
    refusing is the one option with no bypass.

    >>> quote_literal("ENABLED")
    "'ENABLED'"
    """
    if any(ch in value for ch in "'\"\\\n\r"):
        raise QueryError(
            f"Refusing to quote a literal containing a quote, backslash or "
            f"newline: {value!r}."
        )
    return f"'{value}'"


def quote_double(value: str) -> str:
    """Quote a GAQL literal using DOUBLE quotes.

    The single-quoted form in :func:`quote_literal` cannot carry an apostrophe,
    which rules out perfectly ordinary campaign names -- "Paniers d'été", "Dad's
    Day". GAQL accepts double-quoted literals too, so an apostrophe needs no
    escaping at all inside one.

    What is still refused is a double quote, a backslash or a newline: those
    could close the literal. As in :func:`quote_literal`, refusing beats
    escaping, because refusal has no bypass.

    >>> print(quote_double("Paniers d'été"))
    "Paniers d'été"
    >>> print(quote_double("Brand - EN"))
    "Brand - EN"
    """
    if any(ch in value for ch in '"\\\n\r'):
        raise QueryError(
            f"Refusing to quote a literal containing a double quote, backslash "
            f"or newline: {value!r}."
        )
    return f'"{value}"'


def contains(field_path: str, needle: str) -> str:
    """A case-insensitive substring match: ``field LIKE "%needle%"``.

    GAQL's ``LIKE`` is case-insensitive -- verified against the live API, where
    ``'%en - cpm%'`` returned the same rows as ``'%En - CPM%'``. That makes it
    the right operator for "find me the campaign I half-remember the name of",
    which is how people actually reach a campaign: names are visible in the UI
    and IDs are not.

    ``%`` and ``_`` keep their wildcard meaning, so a literal percent in the
    search text will behave as a wildcard.

    >>> contains("campaign.name", "CPM")
    'campaign.name LIKE "%CPM%"'
    """
    _validate_field_path(field_path)
    if not needle.strip():
        raise QueryError("Search text is empty.")
    return f"{field_path} LIKE {quote_double('%' + needle + '%')}"


def in_list(field_path: str, values: Iterable[str]) -> str:
    """A ``field IN ('a', 'b')`` condition.

    >>> in_list("campaign.status", ["ENABLED", "PAUSED"])
    "campaign.status IN ('ENABLED', 'PAUSED')"
    """
    _validate_field_path(field_path)
    quoted = [quote_literal(value) for value in values]
    if not quoted:
        raise QueryError(f"IN list for {field_path} is empty.")
    return f"{field_path} IN ({', '.join(quoted)})"
