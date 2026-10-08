"""Field extraction and micros tests.

These build *real* proto-plus messages rather than hand-rolled stubs. A stub
cannot discover that `metrics.average_cpc` is a `double`, that an enum arrives
as an `IntEnum` subclass, or that a field name was misspelled -- it only ever
confirms the assumption it was written from. Real messages catch all three
offline, with no credentials and no network.
"""

import pytest
from google.ads.googleads.v25.common.types.metrics import Metrics
from google.ads.googleads.v25.enums.types.advertising_channel_type import (
    AdvertisingChannelTypeEnum,
)
from google.ads.googleads.v25.enums.types.campaign_status import CampaignStatusEnum
from google.ads.googleads.v25.resources.types.campaign import Campaign
from google.ads.googleads.v25.services.types.google_ads_service import GoogleAdsRow

from googleads_reporting import fields
from googleads_reporting.fields import (
    FieldPathError,
    from_micros,
    get_field,
    is_micros_field,
    money_column_name,
    row_to_dict,
    unknown_money_fields,
)


@pytest.fixture
def row():
    return GoogleAdsRow(
        campaign=Campaign(
            id=1234,
            name="Example — Brand — EN",
            status=CampaignStatusEnum.CampaignStatus.ENABLED,
            advertising_channel_type=(
                AdvertisingChannelTypeEnum.AdvertisingChannelType.SEARCH
            ),
        ),
        metrics=Metrics(
            impressions=10_000,
            clicks=250,
            ctr=0.025,
            cost_micros=1_500_000,
            average_cpc=1_234_567.0,
            conversions=12.0,
            conversions_value=480.25,
            cost_per_conversion=125_000_000.0,
        ),
    )


# --------------------------------------------------------------------------
# is_micros_field
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "metrics.cost_micros",
        "metrics.all_revenue_micros",
        "metrics.gross_profit_micros",
        "campaign_budget.amount_micros",
        # double-typed but still micros -- the whole point of the explicit set
        "metrics.average_cpc",
        "metrics.average_cpm",
        "metrics.average_cost",
        "metrics.cost_per_conversion",
        "metrics.cost_per_all_conversions",
    ],
)
def test_micros_fields_are_recognised(path):
    assert is_micros_field(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # Already in account currency -- dividing these is the inverse bug.
        "metrics.conversions_value",
        "metrics.all_conversions_value",
        "metrics.value_per_conversion",
        # Ratios and statistics that merely contain "cost".
        "metrics.conversions_value_per_cost",
        "metrics.all_conversions_value_per_cost",
        "metrics.cost_micros_p_value",
        "metrics.cost_micros_margin_of_error",
        "metrics.cost_per_conversion_p_value",
        "metrics.hotel_price_difference_percentage",
        # Plainly not money.
        "metrics.impressions",
        "metrics.clicks",
        "metrics.ctr",
        "campaign.name",
        "segments.date",
    ],
)
def test_non_micros_fields_are_left_alone(path):
    assert is_micros_field(path) is False


def test_statistic_derived_from_a_micros_field_is_not_micros():
    """cost_micros_p_value ends in neither pattern; the suffix guard must win."""
    assert is_micros_field("metrics.cost_micros") is True
    assert is_micros_field("metrics.cost_micros_p_value") is False
    assert is_micros_field("metrics.cost_micros_change_point_estimate") is False


def test_the_cross_device_pair_is_ruled_both_ways():
    """Both names exist in v25; only the suffixed one is micros."""
    assert is_micros_field("metrics.cross_device_conversions_value_micros") is True
    assert is_micros_field("metrics.cross_device_conversions_value") is False


def test_the_two_rulings_never_overlap():
    assert not (fields.MICROS_FLOAT_METRICS & fields.CURRENCY_UNIT_METRICS)


def test_every_declared_metric_name_exists_in_the_v25_schema():
    """Guards against a typo in either curated set.

    A misspelled entry would silently never match, so the field it was meant to
    cover would go through unconverted.
    """
    declared = fields.MICROS_FLOAT_METRICS | fields.CURRENCY_UNIT_METRICS
    schema = set(Metrics.pb(Metrics()).DESCRIPTOR.fields_by_name)
    assert declared <= schema, sorted(declared - schema)


# --------------------------------------------------------------------------
# from_micros
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "micros, expected",
    [
        (1_500_000, 1.5),
        (0, 0.0),
        (1, 0.000001),
        (1_234_567, 1.234567),
        (-2_000_000, -2.0),
        (999_999_999_999, 999999.999999),
    ],
)
def test_from_micros_converts(micros, expected):
    assert from_micros(micros) == expected


def test_from_micros_keeps_none_distinct_from_zero():
    """An absent metric is not zero spend; collapsing them loses a real answer."""
    assert from_micros(None) is None
    assert from_micros(0) == 0.0


# --------------------------------------------------------------------------
# get_field / row_to_dict
# --------------------------------------------------------------------------


def test_enums_come_back_as_names_not_integers(row):
    assert get_field(row, "campaign.status") == "ENABLED"
    assert get_field(row, "campaign.advertising_channel_type") == "SEARCH"
    # The trap: proto-plus enums subclass int, so an isinstance(int) fast path
    # would return 2 here and the CSV would be full of magic numbers.
    assert not isinstance(get_field(row, "campaign.status"), int)


def test_money_is_converted_and_non_money_is_not(row):
    assert get_field(row, "metrics.cost_micros") == 1.5
    assert get_field(row, "metrics.average_cpc") == 1.234567
    assert get_field(row, "metrics.cost_per_conversion") == 125.0
    # Already in currency units.
    assert get_field(row, "metrics.conversions_value") == 480.25
    # Counts and ratios untouched.
    assert get_field(row, "metrics.impressions") == 10_000
    assert get_field(row, "metrics.clicks") == 250
    assert get_field(row, "metrics.ctr") == 0.025


def test_plain_fields_pass_through(row):
    assert get_field(row, "campaign.id") == 1234
    assert get_field(row, "campaign.name") == "Example — Brand — EN"


def test_unknown_path_raises_rather_than_returning_none(row):
    with pytest.raises(FieldPathError, match="campaign.nonexistent"):
        get_field(row, "campaign.nonexistent")
    with pytest.raises(FieldPathError, match="stopped at 'nope'"):
        get_field(row, "nope.at.all")


def test_row_to_dict_preserves_select_order(row):
    paths = [
        "campaign.name",
        "campaign.id",
        "metrics.cost_micros",
        "metrics.impressions",
    ]
    result = row_to_dict(row, paths)
    assert list(result) == paths
    assert result["metrics.cost_micros"] == 1.5


def test_row_to_dict_on_an_empty_row_yields_proto_defaults():
    """An account with no delivery returns rows of zeros, not an error."""
    result = row_to_dict(
        GoogleAdsRow(), ["metrics.cost_micros", "metrics.clicks", "campaign.name"]
    )
    assert result == {
        "metrics.cost_micros": 0.0,
        "metrics.clicks": 0,
        "campaign.name": "",
    }


# --------------------------------------------------------------------------
# column naming and the unruled-field guard
# --------------------------------------------------------------------------


def test_micros_suffix_is_dropped_once_converted():
    assert money_column_name("metrics.cost_micros") == "metrics.cost"
    assert money_column_name("campaign_budget.amount_micros") == (
        "campaign_budget.amount"
    )


def test_non_suffixed_columns_keep_their_name():
    assert money_column_name("metrics.average_cpc") == "metrics.average_cpc"
    assert money_column_name("metrics.clicks") == "metrics.clicks"


def test_unknown_money_fields_flags_an_unruled_currency_field():
    assert unknown_money_fields(["metrics.some_new_cost_metric"]) == (
        "metrics.some_new_cost_metric",
    )


def test_unknown_money_fields_stays_quiet_on_ruled_and_plain_fields():
    assert unknown_money_fields(
        [
            "metrics.cost_micros",
            "metrics.average_cpc",
            "metrics.conversions_value",
            "metrics.conversions_value_per_cost",
            "metrics.impressions",
            "campaign.name",
            "segments.date",
        ]
    ) == ()
