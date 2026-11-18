"""Campaign run dates, against the real v25 proto.

The old code assigned ``campaign.start_date``, a field the pinned API version
no longer has, so every campaign created with dates raised while the request
was still being built. These tests build the real message, which is the only
way that kind of mistake shows up offline.
"""

from __future__ import annotations

import pytest

from googleads_reporting.write.builder import build
from googleads_reporting.write.campaigns import plan_create
from googleads_reporting.write.client import MutationError
from googleads_reporting.write.spec import AdGroupSpec, CampaignSpec, ResponsiveSearchAdSpec
from kindkit import FakeWriteClient


def _created_campaign(plan):
    return next(op.create for op in plan.operations if hasattr(op, "create"))


def test_plan_create_sets_start_and_end_date_time():
    client = FakeWriteClient()
    plan = plan_create(
        client,
        name="Example Co -- Search",
        budget_resource_name="customers/1234567890/campaignBudgets/1",
        start_date="2026-12-01",
        end_date="2026-12-31",
    )
    campaign = _created_campaign(plan)
    assert campaign.start_date_time == "2026-12-01 00:00:00"
    assert campaign.end_date_time == "2026-12-31 23:59:59"


def test_plan_create_without_dates_leaves_them_unset():
    plan = plan_create(
        FakeWriteClient(),
        name="Example Co -- Search",
        budget_resource_name="customers/1234567890/campaignBudgets/1",
    )
    campaign = _created_campaign(plan)
    assert campaign.start_date_time == ""
    assert campaign.end_date_time == ""


def test_a_malformed_date_is_refused_with_the_format():
    # A MutationError, which manage.py turns into a clean "error: ..." line; a
    # bare ValueError escaped it as a traceback.
    with pytest.raises(MutationError, match="YYYY-MM-DD"):
        plan_create(
            FakeWriteClient(),
            name="Example Co -- Search",
            budget_resource_name="customers/1234567890/campaignBudgets/1",
            start_date="12/01/2026",
        )


def test_builder_sets_the_same_fields():
    spec = CampaignSpec(
        name="Example Co -- Search",
        channel="SEARCH",
        budget_amount=10.0,
        start_date="2026-12-01",
        end_date="2026-12-31",
        ad_groups=[AdGroupSpec(
            name="Example ad group",
            ads=[ResponsiveSearchAdSpec(
                headlines=["a", "b", "c"], descriptions=["x", "y"],
                final_url="https://example.com",
            )],
        )],
    )
    built = build(FakeWriteClient(), spec)
    campaigns = [
        op.campaign_operation.create
        for op in built.operations
        if op.campaign_operation.create.name
    ]
    assert campaigns[0].start_date_time == "2026-12-01 00:00:00"
    assert campaigns[0].end_date_time == "2026-12-31 23:59:59"


def _spec(**dates):
    return CampaignSpec(
        name="Example Co -- Search",
        channel="SEARCH",
        budget_amount=10.0,
        ad_groups=[AdGroupSpec(
            name="Example ad group",
            ads=[ResponsiveSearchAdSpec(
                headlines=["a", "b", "c"], descriptions=["x", "y"],
                final_url="https://example.com",
            )],
        )],
        **dates,
    )


def test_the_built_plan_shows_the_run_dates_the_human_confirms():
    built = build(FakeWriteClient(), _spec(start_date="2026-12-01", end_date="2026-12-31"))
    shown = {c.field: c.after for c in built.plan.changes}
    assert shown.get("start_date") == "2026-12-01"
    assert shown.get("end_date") == "2026-12-31"


def test_a_malformed_date_in_a_whole_build_is_a_mutation_error():
    with pytest.raises(MutationError, match="end_date must be YYYY-MM-DD"):
        build(FakeWriteClient(), _spec(end_date="31/12/2026"))
