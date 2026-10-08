import pytest

from googleads_reporting.customer_id import (
    CustomerIdError,
    format_customer_id,
    normalize_customer_id,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1234567890", "1234567890"),
        ("123-456-7890", "1234567890"),
        ("  123-456-7890  ", "1234567890"),
        ("123 456 7890", "1234567890"),
        ("123_456_7890", "1234567890"),
        (1234567890, "1234567890"),
        ("customers/1234567890", "1234567890"),
        ("customers/1234567890/campaigns/55", "1234567890"),
        # Leading zeros are significant and must survive.
        ("012-345-6789", "0123456789"),
        # Non-breaking space and zero-width space from rich-text paste.
        ("123 456 7890", "1234567890"),
        ("123-456-7890​", "1234567890"),
    ],
)
def test_normalizes_accepted_forms(raw, expected):
    assert normalize_customer_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        True,  # bool is an int subclass; must not slip through as 1
        "12345",  # too short
        "12345678901",  # too long
        "123-456-789",  # 9 digits
        "abcdefghij",
        "123-456-789x",
        1234.0,
        ["1234567890"],
    ],
)
def test_rejects_bad_values(raw):
    with pytest.raises(CustomerIdError):
        normalize_customer_id(raw)


def test_length_error_names_the_actual_length():
    with pytest.raises(CustomerIdError, match="got 5 in"):
        normalize_customer_id("12345")


def test_format_round_trips():
    assert format_customer_id("1234567890") == "123-456-7890"
    assert format_customer_id("123-456-7890") == "123-456-7890"
    assert normalize_customer_id(format_customer_id("0123456789")) == "0123456789"
