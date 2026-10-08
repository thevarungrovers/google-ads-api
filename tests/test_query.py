from datetime import date

import pytest

from googleads_reporting.query import (
    Query,
    QueryError,
    assert_select_only,
    between,
    during,
    in_list,
    last_n_days,
    parse_select,
    quote_literal,
)


# --------------------------------------------------------------------------
# Query assembly
# --------------------------------------------------------------------------


def test_minimal_query():
    gaql = Query(select=("campaign.id",), from_resource="campaign").to_gaql()
    assert gaql == "SELECT campaign.id\nFROM campaign"


def test_full_query_renders_clauses_in_order():
    gaql = Query(
        select=("segments.date", "campaign.name", "metrics.cost_micros"),
        from_resource="campaign",
        where=("segments.date DURING LAST_7_DAYS", "campaign.status = 'ENABLED'"),
        order_by=("segments.date", "metrics.cost_micros DESC"),
        limit=50,
    ).to_gaql()

    assert gaql.splitlines() == [
        "SELECT segments.date, campaign.name, metrics.cost_micros",
        "FROM campaign",
        "WHERE segments.date DURING LAST_7_DAYS",
        "  AND campaign.status = 'ENABLED'",
        "ORDER BY segments.date, metrics.cost_micros DESC",
        "LIMIT 50",
    ]


def test_with_where_appends_without_mutating_the_original():
    base = Query(select=("campaign.id",), from_resource="campaign")
    extended = base.with_where("campaign.status = 'ENABLED'")
    assert base.where == ()
    assert extended.where == ("campaign.status = 'ENABLED'",)
    assert extended.select == base.select


@pytest.mark.parametrize(
    "path",
    ["Campaign.Id", "campaign", "metrics..clicks", "1campaign.id", "campaign.id ", ""],
)
def test_rejects_malformed_field_paths(path):
    with pytest.raises(QueryError):
        Query(select=(path,), from_resource="campaign")


def test_rejects_empty_select():
    with pytest.raises(QueryError, match="at least one field"):
        Query(select=(), from_resource="campaign")


def test_rejects_bad_resource_and_limit():
    with pytest.raises(QueryError, match="resource name"):
        Query(select=("campaign.id",), from_resource="Campaign")
    with pytest.raises(QueryError, match="LIMIT must be positive"):
        Query(select=("campaign.id",), from_resource="campaign", limit=0)


# --------------------------------------------------------------------------
# Read-only enforcement at the query layer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gaql",
    [
        "UPDATE campaign SET status = 'PAUSED'",
        "DELETE FROM campaign",
        "INSERT INTO campaign VALUES (1)",
        "DROP TABLE campaign",
        "  update campaign set x = 1",
        "",
        "   ",
    ],
)
def test_non_select_statements_are_refused(gaql):
    with pytest.raises(QueryError):
        assert_select_only(gaql)


def test_a_write_keyword_smuggled_into_a_select_is_refused():
    with pytest.raises(QueryError, match="DELETE"):
        assert_select_only("SELECT campaign.id FROM campaign; DELETE FROM campaign")


def test_statement_chaining_is_refused():
    with pytest.raises(QueryError, match="chaining"):
        assert_select_only("SELECT campaign.id FROM campaign; SELECT 1 FROM campaign")


def test_a_trailing_semicolon_alone_is_tolerated():
    assert assert_select_only("SELECT campaign.id FROM campaign;")


def test_a_field_merely_containing_a_keyword_is_not_refused():
    """'update' inside an identifier must not trip the word-boundary check."""
    assert assert_select_only(
        "SELECT campaign.id, change_event.change_date_time FROM change_event"
    )


# --------------------------------------------------------------------------
# parse_select
# --------------------------------------------------------------------------


def test_parse_select_preserves_order():
    assert parse_select(
        "SELECT campaign.name, segments.date, metrics.clicks FROM campaign"
    ) == ("campaign.name", "segments.date", "metrics.clicks")


def test_parse_select_handles_multiline_and_odd_spacing():
    assert parse_select(
        "SELECT\n  campaign.id,\n  metrics.clicks\nFROM campaign\nLIMIT 1"
    ) == ("campaign.id", "metrics.clicks")


def test_parse_select_round_trips_a_built_query():
    query = Query(
        select=("segments.date", "campaign.name", "metrics.cost_micros"),
        from_resource="campaign",
        where=("segments.date DURING LAST_30_DAYS",),
    )
    assert parse_select(query.to_gaql()) == query.select


def test_parse_select_needs_a_from_clause():
    with pytest.raises(QueryError, match="FROM"):
        parse_select("SELECT campaign.id")


# --------------------------------------------------------------------------
# Date ranges
# --------------------------------------------------------------------------


def test_during_accepts_presets_case_insensitively():
    assert during("last_30_days") == "segments.date DURING LAST_30_DAYS"
    assert during("LAST_30_DAYS") == "segments.date DURING LAST_30_DAYS"


def test_during_rejects_an_invented_preset():
    with pytest.raises(QueryError, match="not a GAQL date preset"):
        during("LAST_45_DAYS")


def test_between_accepts_dates_and_iso_strings():
    expected = "segments.date BETWEEN '2026-09-01' AND '2026-09-30'"
    assert between("2026-09-01", "2026-09-30") == expected
    assert between(date(2026, 9, 1), date(2026, 9, 30)) == expected


def test_between_rejects_a_reversed_range():
    with pytest.raises(QueryError, match="is after end"):
        between("2026-09-30", "2026-09-01")


@pytest.mark.parametrize("bad", ["01/09/2026", "2026-13-01", "yesterday", ""])
def test_between_rejects_non_iso_dates(bad):
    with pytest.raises(QueryError, match="ISO YYYY-MM-DD"):
        between(bad, "2026-09-30")


def test_last_n_days_ends_yesterday_not_today():
    """Today is partial all day, and a partial day drags every average down."""
    assert last_n_days(7, today=date(2026, 10, 8)) == (
        "segments.date BETWEEN '2026-10-01' AND '2026-10-07'"
    )
    assert last_n_days(1, today=date(2026, 10, 8)) == (
        "segments.date BETWEEN '2026-10-07' AND '2026-10-07'"
    )


def test_last_n_days_spans_a_month_boundary():
    assert last_n_days(3, today=date(2026, 3, 2)) == (
        "segments.date BETWEEN '2026-02-27' AND '2026-03-01'"
    )


def test_last_n_days_rejects_zero_or_negative():
    with pytest.raises(QueryError, match="days must be positive"):
        last_n_days(0)


# --------------------------------------------------------------------------
# Literals
# --------------------------------------------------------------------------


def test_quote_literal_wraps_a_plain_value():
    assert quote_literal("ENABLED") == "'ENABLED'"


@pytest.mark.parametrize(
    "value",
    [
        "O'Brien",
        "ENABLED' OR '1'='1",
        'say "hi"',
        "back\\slash",
        "line\nbreak",
        "carriage\rreturn",
    ],
)
def test_quote_literal_refuses_anything_that_could_escape(value):
    """GAQL has no parameter binding, so refusing beats escaping."""
    with pytest.raises(QueryError, match="Refusing to quote"):
        quote_literal(value)


def test_in_list_builds_a_quoted_list():
    assert in_list("campaign.status", ["ENABLED", "PAUSED"]) == (
        "campaign.status IN ('ENABLED', 'PAUSED')"
    )


def test_in_list_rejects_empty_and_validates_the_field():
    with pytest.raises(QueryError, match="empty"):
        in_list("campaign.status", [])
    with pytest.raises(QueryError, match="valid GAQL field path"):
        in_list("Campaign.Status", ["ENABLED"])


def test_in_list_refuses_an_injected_value():
    with pytest.raises(QueryError, match="Refusing to quote"):
        in_list("campaign.status", ["ENABLED') OR TRUE OR ('"])
