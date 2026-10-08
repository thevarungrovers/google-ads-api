"""Turning report rows into DataFrames and files.

Kept separate from the client so the fetch scripts stay thin, and so output
formatting can be tested without a client at all.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from .fields import money_column_name


def to_dataframe(
    rows: Iterable[dict[str, Any]], columns: Sequence[str]
) -> pd.DataFrame:
    """Build a DataFrame with exactly ``columns``, in order.

    ``columns`` are GAQL field paths as selected. They are renamed to their
    output names here (``metrics.cost_micros`` -> ``metrics.cost``), because the
    values have already been divided and a column still called ``_micros``
    invites the next consumer to divide again.

    An empty result still yields a correctly-typed empty frame rather than a
    frame with no columns -- a day with no delivery is a valid answer, and the
    header should survive it.
    """
    selected = list(columns)
    frame = pd.DataFrame(list(rows), columns=selected)
    return frame.rename(
        columns={path: money_column_name(path) for path in selected}
    )


def output_path(
    output_dir: Path, report_name: str, customer_id: str, *, suffix: str = "csv"
) -> Path:
    """A timestamped path under ``output_dir``, creating the directory.

    The customer ID and a UTC timestamp are in the filename so exports from
    different accounts or runs never overwrite each other.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_dir / f"{report_name}_{customer_id}_{stamp}.{suffix}"


def write_csv(frame: pd.DataFrame, path: Path) -> Path:
    """Write ``frame`` to ``path`` as UTF-8 CSV with no index column."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    return path


#: Columns worth totalling in a summary line. Averages and ratios are excluded
#: because summing a CTR or an average CPC is meaningless -- they have to be
#: recomputed from the totals, which :func:`summarise` does.
SUMMABLE = (
    "metrics.impressions",
    "metrics.clicks",
    "metrics.cost",
    "metrics.conversions",
    "metrics.conversions_value",
)


def summarise(frame: pd.DataFrame) -> dict[str, float]:
    """Totals for a report, with ratios recomputed rather than averaged.

    Averaging a column of CTRs or average-CPCs weights every row equally,
    which is wrong whenever rows differ in volume. Derived figures here come
    from the summed numerator and denominator.
    """
    totals: dict[str, float] = {}
    for column in SUMMABLE:
        if column in frame.columns:
            totals[column] = float(pd.to_numeric(frame[column], errors="coerce").sum())

    impressions = totals.get("metrics.impressions", 0.0)
    clicks = totals.get("metrics.clicks", 0.0)
    cost = totals.get("metrics.cost", 0.0)
    conversions = totals.get("metrics.conversions", 0.0)
    value = totals.get("metrics.conversions_value", 0.0)

    if impressions:
        totals["ctr"] = round(clicks / impressions, 6)
    if clicks:
        totals["average_cpc"] = round(cost / clicks, 6)
    if conversions:
        totals["cost_per_conversion"] = round(cost / conversions, 6)
    if cost:
        totals["roas"] = round(value / cost, 6)

    totals["rows"] = float(len(frame))
    return totals
