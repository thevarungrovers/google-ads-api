"""The general low-risk tool: a closed allowlist that can never hold money."""

from __future__ import annotations

import pytest

from googleads_mcp.changes.low_risk import ALLOWLIST, LowRiskChange
from googleads_reporting.fields import is_micros_field, unknown_money_fields
from kindkit import failed, preview_and_apply, rig, row

#: Pinned exactly. Adding a pair means editing this set, in review.
EXPECTED = {
    ("label", "create"),
    ("campaign", "attach_label"), ("campaign", "detach_label"),
    ("ad_group", "attach_label"), ("ad_group", "detach_label"),
    ("ad", "attach_label"), ("ad", "detach_label"),
    ("keyword", "attach_label"), ("keyword", "detach_label"),
    ("campaign", "final_url_suffix"), ("campaign", "tracking_url_template"),
    ("ad_group", "final_url_suffix"), ("ad_group", "tracking_url_template"),
    ("recommendation", "dismiss"),
}

#: Words that mark a field as money, delivery or targeting. None may appear.
FORBIDDEN = (
    "bid", "budget", "cpc", "cpm", "cpa", "cpv", "roas", "amount", "micros", "cost",
    "spend", "status", "network", "location", "geo", "language", "schedule", "device",
    "audience", "targeting", "negative", "keyword_text", "match", "strategy", "target",
    "enable", "pause", "remove", "delete",
)


def test_the_allowlist_is_exactly_what_was_chosen():
    assert set(ALLOWLIST) == EXPECTED


@pytest.mark.parametrize(("resource", "field"), sorted(EXPECTED))
def test_no_allowlisted_field_is_money_status_or_targeting(resource, field):
    lowered = field.lower()
    assert not any(word in lowered for word in FORBIDDEN), field
    path = f"{resource}.{field}"
    assert not is_micros_field(path), path
    assert unknown_money_fields([path]) == (), path


def test_the_forbidden_list_would_catch_a_money_field():
    """Negative control: a check that matches nothing proves nothing."""
    for field in ("cpc_bid_micros", "amount_micros", "status", "target_cpa_micros"):
        assert any(word in field for word in FORBIDDEN), field


def test_anything_else_is_refused_before_any_read():
    engine, client, _ = rig()
    kind = engine.add(LowRiskChange())
    with pytest.raises(ValueError, match="not a low-risk edit"):
        engine.preview(kind, {"resource": "campaign", "id": "11", "field": "status",
                              "value": "PAUSED"})
    assert client.queries == []


def test_a_tracking_template_is_set_and_reverts_to_the_old_one():
    engine, client, _ = rig()
    client.answer("campaign.tracking_url_template", [row(**{
        "campaign.tracking_url_template": "{lpurl}?src=old"})])
    kind = engine.add(LowRiskChange())
    preview, result = preview_and_apply(engine, kind, resource="campaign", id="11",
                                        field="tracking_url_template", value="{lpurl}?src=new")
    assert (preview.before, preview.after) == ("{lpurl}?src=old", "{lpurl}?src=new")
    op = client.applied[-1]["operations"][0]
    assert list(op.update_mask.paths) == ["tracking_url_template"]

    client.answer("campaign.tracking_url_template", [row(**{
        "campaign.tracking_url_template": "{lpurl}?src=new"})])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.tracking_url_template == "{lpurl}?src=old"


def test_clearing_a_suffix_sends_the_empty_value_with_its_mask():
    engine, client, _ = rig()
    client.answer("ad_group.final_url_suffix", [row(**{"ad_group.final_url_suffix": "a=1"})])
    kind = engine.add(LowRiskChange())
    preview_and_apply(engine, kind, resource="ad_group", id="22", field="final_url_suffix",
                      value="")
    op = client.applied[-1]["operations"][0]
    assert op.update.final_url_suffix == "" and list(op.update_mask.paths) == ["final_url_suffix"]


def test_a_malformed_suffix_is_refused():
    engine, client, _ = rig()
    kind = engine.add(LowRiskChange())
    with pytest.raises(ValueError, match="AFTER"):
        engine.preview(kind, {"resource": "campaign", "id": "11",
                              "field": "final_url_suffix", "value": "?a=1"})


def test_attaching_a_label_reverts_by_detaching_it():
    engine, client, _ = rig()
    kind = engine.add(LowRiskChange())
    preview, result = preview_and_apply(engine, kind, resource="keyword", id="22~77",
                                        field="attach_label", value="9")
    op = client.applied[-1]["operations"][0]
    assert op.create.ad_group_criterion == "customers/1112223333/adGroupCriteria/22~77"
    assert op.create.label == "customers/1112223333/labels/9"

    client.answer("FROM ad_group_criterion_label", [row(**{
        "ad_group_criterion_label.resource_name": "x"})])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].remove == (
        "customers/1112223333/adGroupCriterionLabels/22~77~9")


def test_attaching_a_label_already_there_is_blocked():
    engine, client, _ = rig()
    client.answer("FROM campaign_label", [row(**{"campaign_label.resource_name": "x"})])
    kind = engine.add(LowRiskChange())
    preview = engine.preview(kind, {"resource": "campaign", "id": "11",
                                    "field": "attach_label", "value": "9"})
    assert failed(preview, "is_a_change")


def test_dismissing_is_not_validated_and_cannot_be_reverted():
    engine, client, _ = rig()
    kind = engine.add(LowRiskChange())
    preview, result = preview_and_apply(engine, kind, resource="recommendation", id="R1",
                                        field="dismiss")
    assert not any("google_validate_only" in c for c in preview.checks)
    assert [s["apply"] for s in client.sent] == [True]
    assert client.sent[-1]["method"] == "dismiss_recommendation"
    with pytest.raises(ValueError, match="un-dismiss"):
        engine.revert(result.entry_id)
