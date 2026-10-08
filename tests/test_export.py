import pandas as pd
import pytest

from googleads_reporting.export import (
    output_path,
    summarise,
    to_dataframe,
    write_csv,
)
from googleads_reporting.reports import get_report

COLUMNS = (
    "segments.date",
    "metrics.impressions",
    "metrics.clicks",
    "metrics.cost_micros",
    "metrics.conversions",
    "metrics.conversions_value",
)

ROWS = [
    {
        "segments.date": "2026-10-01",
        "metrics.impressions": 1000,
        "metrics.clicks": 10,
        "metrics.cost_micros": 1.5,
        "metrics.conversions": 2.0,
        "metrics.conversions_value": 30.0,
    },
    {
        "segments.date": "2026-10-02",
        "metrics.impressions": 3000,
        "metrics.clicks": 30,
        "metrics.cost_micros": 3.0,
        "metrics.conversions": 1.0,
        "metrics.conversions_value": 10.0,
    },
]


def test_micros_columns_are_renamed_in_the_output():
    frame = to_dataframe(ROWS, COLUMNS)
    assert "metrics.cost" in frame.columns
    assert "metrics.cost_micros" not in frame.columns


def test_column_order_follows_the_select():
    frame = to_dataframe(ROWS, COLUMNS)
    assert list(frame.columns) == [
        "segments.date",
        "metrics.impressions",
        "metrics.clicks",
        "metrics.cost",
        "metrics.conversions",
        "metrics.conversions_value",
    ]


def test_an_empty_result_keeps_its_header():
    """A day with no delivery is a valid answer; the columns should survive it."""
    frame = to_dataframe([], COLUMNS)
    assert frame.empty
    assert list(frame.columns) == [
        "segments.date",
        "metrics.impressions",
        "metrics.clicks",
        "metrics.cost",
        "metrics.conversions",
        "metrics.conversions_value",
    ]


@pytest.mark.parametrize("report_name", ["accounts", "campaigns", "keywords"])
def test_every_report_shapes_into_its_declared_columns(report_name):
    report = get_report(report_name)
    frame = to_dataframe([], report.select)
    assert list(frame.columns) == list(report.columns)


# --------------------------------------------------------------------------
# summarise
# --------------------------------------------------------------------------


def test_totals_are_summed():
    totals = summarise(to_dataframe(ROWS, COLUMNS))
    assert totals["metrics.impressions"] == 4000
    assert totals["metrics.clicks"] == 40
    assert totals["metrics.cost"] == 4.5
    assert totals["metrics.conversions"] == 3.0
    assert totals["rows"] == 2


def test_ratios_are_recomputed_from_the_sums_not_averaged():
    """Averaging per-row ratios weights a 10-click day like a 30-click one."""
    totals = summarise(to_dataframe(ROWS, COLUMNS))

    # 4.50 total cost / 40 total clicks
    assert totals["average_cpc"] == 0.1125
    # Averaging the two rows' own CPCs would give (0.15 + 0.10) / 2 = 0.125.
    assert totals["average_cpc"] != 0.125

    assert totals["ctr"] == 0.01  # 40 / 4000
    assert totals["cost_per_conversion"] == 1.5  # 4.50 / 3
    assert totals["roas"] == round(40.0 / 4.5, 6)


def test_ratios_are_omitted_rather_than_dividing_by_zero():
    zeroed = [dict(row, **{
        "metrics.impressions": 0,
        "metrics.clicks": 0,
        "metrics.cost_micros": 0.0,
        "metrics.conversions": 0.0,
    }) for row in ROWS]
    totals = summarise(to_dataframe(zeroed, COLUMNS))
    for key in ("ctr", "average_cpc", "cost_per_conversion", "roas"):
        assert key not in totals
    assert totals["rows"] == 2


def test_summarise_ignores_columns_a_report_does_not_have():
    frame = to_dataframe(
        [{"customer_client.id": 1234567890}], ("customer_client.id",)
    )
    assert summarise(frame) == {"rows": 1.0}


def test_summarise_survives_a_non_numeric_cell():
    frame = pd.DataFrame(
        {"metrics.clicks": [1, "n/a", 3], "metrics.impressions": [10, 20, 30]}
    )
    totals = summarise(frame)
    assert totals["metrics.clicks"] == 4.0
    assert totals["metrics.impressions"] == 60.0


# --------------------------------------------------------------------------
# File output
# --------------------------------------------------------------------------


def test_output_path_creates_the_directory_and_names_the_account(tmp_path):
    path = output_path(tmp_path / "nested" / "out", "campaigns", "1234567890")
    assert path.parent.is_dir()
    assert path.name.startswith("campaigns_1234567890_")
    assert path.suffix == ".csv"


def test_two_accounts_do_not_collide(tmp_path):
    first = output_path(tmp_path, "campaigns", "1234567890")
    second = output_path(tmp_path, "campaigns", "0987654321")
    assert first != second


def test_write_csv_round_trips_without_an_index_column(tmp_path):
    frame = to_dataframe(ROWS, COLUMNS)
    path = write_csv(frame, tmp_path / "out" / "report.csv")

    assert path.exists()
    reloaded = pd.read_csv(path)
    assert list(reloaded.columns) == list(frame.columns)
    assert len(reloaded) == 2
    assert reloaded["metrics.cost"].tolist() == [1.5, 3.0]


def test_write_csv_handles_non_ascii_campaign_names(tmp_path):
    frame = to_dataframe(
        [{"campaign.name": "Lufa — Paniers d'été", "metrics.clicks": 1}],
        ("campaign.name", "metrics.clicks"),
    )
    path = write_csv(frame, tmp_path / "accents.csv")
    assert pd.read_csv(path)["campaign.name"][0] == "Lufa — Paniers d'été"
