"""Planning-layer tests. Nothing here touches the API."""

import pytest

from googleads_reporting.write.plan import FieldChange, PlannedChange, micros


@pytest.mark.parametrize(
    "amount, expected",
    [(1.0, 1_000_000), (0.0, 0), (12.34, 12_340_000), (0.01, 10_000),
     (1000.0, 1_000_000_000), (0.5, 500_000)],
)
def test_micros_converts(amount, expected):
    assert micros(amount) == expected


@pytest.mark.parametrize("amount", [2.01, 2.03, 2.05, 2.07, 2.09, 8.29])
def test_micros_rounds_rather_than_truncating(amount):
    """int(2.01 * 1e6) is 2009999 -- a silently wrong bid."""
    assert micros(amount) == round(amount * 1_000_000)
    assert micros(amount) != int(amount * 1_000_000)


def test_micros_round_trips_through_from_micros():
    from googleads_reporting.fields import from_micros

    for amount in (0.01, 1.0, 2.01, 12.34, 999.99):
        assert from_micros(micros(amount)) == amount


def test_field_change_renders_a_diff():
    assert FieldChange("status", "ENABLED", "PAUSED").render() == (
        "status: 'ENABLED' -> 'PAUSED'"
    )


def test_field_change_with_no_before_reads_as_a_value_not_a_diff():
    """A create has no 'before'; rendering '-> x' from None would be noise."""
    assert FieldChange("name", None, "New campaign").render() == (
        "name: 'New campaign'"
    )


def test_planned_change_renders_label_changes_and_warnings():
    plan = PlannedChange(
        action="update", entity="campaign", label="2026 - En - CPM - Desktop",
        service="CampaignService", method="mutate_campaigns",
        changes=[FieldChange("status", "ENABLED", "PAUSED")],
        warnings=["this campaign is currently delivering"],
    )
    rendered = plan.render()
    assert "UPDATE campaign: 2026 - En - CPM - Desktop" in rendered
    assert "status: 'ENABLED' -> 'PAUSED'" in rendered
    assert "! this campaign is currently delivering" in rendered


def test_describe_is_json_safe_for_the_audit_log():
    import json

    plan = PlannedChange(
        action="update", entity="campaign", label="x",
        service="CampaignService", method="mutate_campaigns",
        changes=[FieldChange("budget", 1.0, 2.0)],
    )
    # Must survive json.dumps without a custom encoder.
    assert json.loads(json.dumps(plan.describe()))[0]["changes"][0]["after"] == 2.0
