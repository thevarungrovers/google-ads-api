"""Row extraction and unit conversion.

Two jobs: pull a dotted GAQL field path out of a ``GoogleAdsRow``, and convert
money out of micros.

**The micros rule is not derivable from the schema.** Two traps:

1. ``metrics.cost_micros`` is an ``int64`` and obviously micros from its name.
   But ``metrics.average_cpc``, ``metrics.average_cpm``, ``metrics.average_cost``
   and ``metrics.cost_per_conversion`` are declared ``double`` and are *also*
   micros -- an average CPC of $1.50 comes back as ``1500000.0``. Nothing in the
   name or the type says so, and the generated docstrings do not mention units.
2. Conversion *values* go the other way. ``metrics.conversions_value`` and
   ``metrics.all_conversions_value`` are already in account currency and must
   **not** be divided, even though they are money. So is anything ending
   ``_per_cost``, which is a ratio.

Dividing the wrong column is silent: no exception, no warning, just a number
that is wrong by a factor of a million. So the set of micros fields is explicit
and listed below rather than inferred from a name pattern alone, and
:func:`unknown_money_fields` exists to flag a selected field this module has no
ruling on.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

MICROS = 1_000_000

#: Decimal places kept after dividing by a million. Six is exact for an integer
#: micros value and leaves no float noise at the sub-cent level.
MONEY_PRECISION = 6

#: Fields whose names end in this suffix are always micros. Schema-derivable.
MICROS_SUFFIX = "_micros"

#: Money fields that are micros DESPITE not saying so in the name. Every entry
#: here was checked against the v25 reference; do not add one on a hunch.
MICROS_FLOAT_METRICS = frozenset(
    {
        "average_cost",
        "average_cpc",
        "average_cpe",
        "average_cpm",
        "cost_per_all_conversions",
        "cost_per_conversion",
        "cost_per_current_model_attributed_conversion",
        "active_view_cpm",
        "benchmark_average_max_cpc",
        # Note: there is no bare "average_cpv" in v25 -- only this one.
        "trueview_average_cpv",
    }
)

#: Money fields that are already in account currency. Listed explicitly so a
#: future maintainer does not "fix" them into the set above.
CURRENCY_UNIT_METRICS = frozenset(
    {
        "conversions_value",
        "all_conversions_value",
        "value_per_conversion",
        "value_per_all_conversions",
        "current_model_attributed_conversions_value",
        "platform_comparable_conversions_value",
        # Both this and cross_device_conversions_value_micros exist. The
        # suffixed one is micros, this one is not -- the clearest illustration
        # of why the rule cannot be a name pattern alone.
        "cross_device_conversions_value",
    }
)

#: Suffixes that mark a ratio or a statistic, never a currency amount.
_NON_MONEY_SUFFIXES = (
    "_per_cost",
    "_percentage",
    "_p_value",
    "_winner_score",
    "_change_point_estimate",
    "_margin_of_error",
    "_p90_lower_bound",
    "_p90_upper_bound",
)


def leaf_name(path: str) -> str:
    """``'metrics.average_cpc'`` -> ``'average_cpc'``."""
    return path.rsplit(".", 1)[-1]


def is_micros_field(path: str) -> bool:
    """Whether the GAQL field at ``path`` is denominated in micros.

    >>> is_micros_field("metrics.cost_micros")
    True
    >>> is_micros_field("metrics.average_cpc")
    True
    >>> is_micros_field("metrics.conversions_value")
    False
    >>> is_micros_field("metrics.conversions_value_per_cost")
    False
    >>> is_micros_field("metrics.cost_micros_p_value")
    False
    """
    name = leaf_name(path)

    # A statistic derived from a micros field is not itself micros, so this
    # check has to come before the suffix rule: cost_micros_p_value ends in
    # neither _micros nor nothing useful.
    if name.endswith(_NON_MONEY_SUFFIXES):
        return False
    if name in CURRENCY_UNIT_METRICS:
        return False
    if name.endswith(MICROS_SUFFIX):
        return True
    return name in MICROS_FLOAT_METRICS


def from_micros(value: Any) -> Any:
    """Convert a micros amount to currency units, rounded to 6 decimals.

    ``None`` passes through, so an absent metric stays absent rather than
    becoming ``0.0`` -- "no data" and "zero spend" are different answers.

    >>> from_micros(1_500_000)
    1.5
    >>> from_micros(1234567)
    1.234567
    >>> from_micros(None) is None
    True
    """
    if value is None:
        return None
    return round(value / MICROS, MONEY_PRECISION)


def unknown_money_fields(paths: Iterable[str]) -> tuple[str, ...]:
    """Selected fields that look monetary but have no ruling in this module.

    Used by the report layer to fail loudly rather than emit a column that is
    silently off by a million. A field is "unruled" if its name suggests money
    but it is in neither the micros nor the currency-unit set.
    """
    suspicious = ("cost", "cpc", "cpm", "cpv", "cpe", "price", "revenue", "profit")
    unknown = []
    for path in paths:
        name = leaf_name(path)
        if name.endswith(_NON_MONEY_SUFFIXES):
            continue
        if not any(token in name for token in suspicious):
            continue
        if name.endswith(MICROS_SUFFIX):
            continue
        if name in MICROS_FLOAT_METRICS or name in CURRENCY_UNIT_METRICS:
            continue
        unknown.append(path)
    return tuple(unknown)


def coerce(value: Any) -> Any:
    """Turn a proto-plus value into something CSV and JSON can hold.

    proto-plus enums become their ``.name``; repeated fields become lists;
    dates and datetimes become ISO strings. Everything else is returned as-is.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # proto-plus enums are IntEnum subclasses, so the isinstance(int) check
    # above would swallow them -- hence .name is tested first, below.
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)) or (
        hasattr(value, "__iter__") and not isinstance(value, (bytes, str))
    ):
        return [coerce(item) for item in value]
    return value


def _coerce_scalar(value: Any) -> Any:
    """Like :func:`coerce` but checks ``.name`` before the int fast path."""
    if value is None or isinstance(value, (bool, str, float)):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, int):
        return value
    return coerce(value)


class FieldPathError(AttributeError):
    """Raised when a field path does not exist on the row."""


_MISSING = object()


def get_field(row: Any, path: str) -> Any:
    """Read the dotted GAQL ``path`` off ``row`` and coerce the result.

    ``get_field(row, "metrics.cost_micros")`` is ``row.metrics.cost_micros``.
    Money fields are converted out of micros here, so callers never see a raw
    micros amount.
    """
    current: Any = row
    for part in path.split("."):
        current = getattr(current, part, _MISSING)
        if current is _MISSING:
            raise FieldPathError(
                f"{path!r} is not a field on this row (stopped at {part!r})."
            )

    value = _coerce_scalar(current)
    if is_micros_field(path) and isinstance(value, (int, float)) and not isinstance(
        value, bool
    ):
        return from_micros(value)
    return value


def row_to_dict(row: Any, paths: Iterable[str]) -> dict[str, Any]:
    """Flatten ``row`` into ``{field_path: value}`` for the given paths.

    Column order follows ``paths``, i.e. the order of the GAQL SELECT clause.
    """
    return {path: get_field(row, path) for path in paths}


def money_column_name(path: str) -> str:
    """The output column name for a micros field.

    ``metrics.cost_micros`` reports as ``metrics.cost`` once divided, because
    leaving ``_micros`` on a column that is no longer micros is how the wrong
    number gets multiplied back up downstream.

    >>> money_column_name("metrics.cost_micros")
    'metrics.cost'
    >>> money_column_name("metrics.average_cpc")
    'metrics.average_cpc'
    """
    if leaf_name(path).endswith(MICROS_SUFFIX):
        return path[: -len(MICROS_SUFFIX)]
    return path
