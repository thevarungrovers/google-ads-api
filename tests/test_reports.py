"""Report definition tests.

The one that matters most is `test_every_selected_field_exists_in_the_schema`.
It resolves each report's field paths against the real `GoogleAdsRow`
descriptor, so a misspelled or removed field fails here -- offline, with no
credentials -- rather than as an API error on the first live run, or worse, as
a column that quietly comes back empty.
"""

import pytest
from google.ads.googleads.v25.services.types.google_ads_service import GoogleAdsRow

from googleads_reporting import fields
from googleads_reporting.query import QueryError, Query, during, parse_select
from googleads_reporting.reports import (
    REGISTRY,
    Report,
    get_report,
    report_names,
)

ROW_DESCRIPTOR = GoogleAdsRow.pb(GoogleAdsRow()).DESCRIPTOR

ALL_REPORTS = [pytest.param(report, id=name) for name, report in sorted(REGISTRY.items())]


def resolve_path(path: str) -> str:
    """'' if ``path`` exists on GoogleAdsRow, else why it does not."""
    descriptor = ROW_DESCRIPTOR
    for part in path.split("."):
        if descriptor is None:
            return f"{path!r}: {part!r} is a field of a scalar, not a message"
        field = descriptor.fields_by_name.get(part)
        if field is None:
            return f"{path!r}: no field {part!r}"
        descriptor = (
            field.message_type if field.type == field.TYPE_MESSAGE else None
        )
    return ""


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------


def test_the_resolver_rejects_a_field_that_does_not_exist():
    """Negative control: a resolver that returns '' for everything proves nothing."""
    assert resolve_path("campaign.id") == ""
    assert "no field 'nonexistent'" in resolve_path("campaign.nonexistent")
    assert "no field 'nope'" in resolve_path("nope.id")
    assert resolve_path("campaign.id.deeper") != ""


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_every_selected_field_exists_in_the_schema(report: Report):
    problems = [p for p in (resolve_path(path) for path in report.select) if p]
    assert problems == []


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_every_order_by_field_exists_in_the_schema(report: Report):
    problems = []
    for clause in report.order_by:
        path = clause.split()[0]
        direction = clause.split()[1:]
        assert direction in ([], ["ASC"], ["DESC"]), clause
        problem = resolve_path(path)
        if problem:
            problems.append(problem)
    assert problems == []


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_order_by_fields_are_also_selected(report: Report):
    """Ordering by an unselected field is legal GAQL but confusing output."""
    ordered = {clause.split()[0] for clause in report.order_by}
    assert ordered <= set(report.select), ordered - set(report.select)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_registry_is_populated_and_keyed_by_name():
    assert report_names() == ["accounts", "campaigns", "keywords"]
    for name, report in REGISTRY.items():
        assert report.name == name


def test_get_report_error_lists_what_is_available():
    with pytest.raises(KeyError, match="accounts, campaigns, keywords"):
        get_report("campaign")  # singular -- an easy mistake


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_every_report_has_a_description(report: Report):
    assert report.description.strip()


# --------------------------------------------------------------------------
# Query assembly
# --------------------------------------------------------------------------


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_build_produces_a_select_only_query(report: Report):
    gaql = report.build().to_gaql()
    assert gaql.startswith("SELECT ")
    assert parse_select(gaql) == report.select


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_build_applies_the_default_range_only_where_dates_are_supported(
    report: Report,
):
    gaql = report.build().to_gaql()
    if report.supports_date_range:
        assert f"segments.date DURING {report.default_range}" in gaql
    else:
        assert "segments.date" not in gaql


def test_an_explicit_date_condition_replaces_the_default():
    gaql = get_report("campaigns").build(
        date_condition="segments.date BETWEEN '2026-09-01' AND '2026-09-30'"
    ).to_gaql()
    assert "BETWEEN '2026-09-01' AND '2026-09-30'" in gaql
    assert "LAST_30_DAYS" not in gaql


def test_a_date_range_on_a_dateless_report_is_refused():
    """Better a clear error than an API error about an unknown segment."""
    with pytest.raises(QueryError, match="no date segment"):
        get_report("accounts").build(date_condition=during("LAST_7_DAYS"))


def test_extra_where_conditions_are_appended():
    gaql = get_report("campaigns").build(
        extra_where=("campaign.status = 'ENABLED'",)
    ).to_gaql()
    assert "AND campaign.status = 'ENABLED'" in gaql


def test_limit_is_applied():
    assert get_report("campaigns").build(limit=10).to_gaql().endswith("LIMIT 10")


# --------------------------------------------------------------------------
# Money columns
# --------------------------------------------------------------------------


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_no_output_column_still_claims_to_be_micros(report: Report):
    """A converted value under a _micros name gets multiplied back up."""
    assert [column for column in report.columns if column.endswith("_micros")] == []


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_columns_line_up_one_to_one_with_select(report: Report):
    assert len(report.columns) == len(report.select)


def test_micros_columns_are_renamed_and_others_are_not():
    columns = dict(zip(get_report("campaigns").select, get_report("campaigns").columns))
    assert columns["metrics.cost_micros"] == "metrics.cost"
    assert columns["campaign_budget.amount_micros"] == "campaign_budget.amount"
    assert columns["metrics.average_cpc"] == "metrics.average_cpc"
    assert columns["metrics.conversions_value"] == "metrics.conversions_value"


@pytest.mark.parametrize("report", ALL_REPORTS)
def test_no_report_selects_an_unruled_money_field(report: Report):
    assert fields.unknown_money_fields(report.select) == ()


def test_a_report_selecting_an_unruled_money_field_fails_at_definition():
    """The __post_init__ guard, exercised directly."""
    with pytest.raises(QueryError, match="no micros ruling"):
        Report(
            name="bad",
            description="selects a money field nobody has ruled on",
            resource="campaign",
            select=("campaign.id", "metrics.some_future_cost_metric"),
        )


def test_a_report_selecting_nothing_fails_at_definition():
    with pytest.raises(QueryError, match="selects no fields"):
        Report(name="empty", description="x", resource="campaign", select=())
