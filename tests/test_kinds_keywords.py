"""Keywords, negatives and shared lists, against real v25 protos."""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp import ledger
from googleads_mcp.changes.keywords import (
    AddKeywords,
    AddNegativeKeywords,
    AddToSharedNegativeList,
    KeywordCpcBid,
    KeywordStatus,
    LinkSharedNegativeList,
)
from googleads_mcp.previews import PreviewStale
from kindkit import failed, preview_and_apply, rig, row


def ad_group(client, *, status="ENABLED"):
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "brand", "campaign.status": "ENABLED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
    })])
    client.answer("campaign.bidding_strategy_type", [row(**{
        "campaign.id": 11, "campaign.bidding_strategy_type": "MANUAL_CPC",
    })])
    client.answer("FROM ad_group\n", [row(**{
        "ad_group.id": 22, "ad_group.name": "shoes", "ad_group.status": status,
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
        "campaign.id": 11, "campaign.name": "brand",
    })])


def keywords_present(client, *pairs, negative=False):
    flag = "TRUE" if negative else "FALSE"
    client.answer(f"ad_group_criterion.negative = {flag}", [row(**{
        "ad_group_criterion.keyword.text": text,
        "ad_group_criterion.keyword.match_type": match,
    }) for text, match in pairs])


# --------------------------------------------------------------------------
# add_keywords
# --------------------------------------------------------------------------


def test_keywords_are_created_paused_in_one_request_one_row_each():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddKeywords())

    preview, result = preview_and_apply(
        engine, kind, ad_group_id="22", keywords=["running shoes", "trail shoes"],
        match_type="EXACT",
    )

    assert preview.before == "(none)" and "PAUSED" in preview.after
    assert Decimal(preview.projected_daily_spend_delta) == 0
    [sent] = client.applied
    assert sent["method"] == "mutate_ad_group_criteria"
    ops = sent["operations"]
    assert [op.create.keyword.text for op in ops] == ["running shoes", "trail shoes"]
    assert {op.create.status.name for op in ops} == {"PAUSED"}
    assert {op.create.keyword.match_type.name for op in ops} == {"EXACT"}
    rows = ledger.batch_entries(result.batch_id)
    assert [r["resource_name"] for r in rows] == result.resource_names
    assert result.session["applies_used"] == 1


def test_enabled_keywords_project_the_campaigns_unused_budget():
    engine, client, read = rig()
    ad_group(client)
    kind = engine.add(AddKeywords())
    preview = engine.preview(kind, {"ad_group_id": "22", "keywords": ["shoes"], "enabled": True})
    # 30.00 budget - 7.00 / 7 days average spend.
    assert Decimal(preview.projected_daily_spend_delta) == Decimal("29.00")
    assert any("serving immediately" in w for w in preview.warnings)


def test_a_keyword_that_already_exists_blocks_the_preview():
    engine, client, _ = rig()
    ad_group(client)
    keywords_present(client, ("running shoes", "PHRASE"))
    kind = engine.add(AddKeywords())
    preview = engine.preview(kind, {"ad_group_id": "22", "keywords": ["Running Shoes"]})
    assert failed(preview, "not_already_present")


def test_a_keyword_bid_over_the_ceiling_blocks_the_preview():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddKeywords())
    preview = engine.preview(kind, {"ad_group_id": "22", "keywords": ["x"], "cpc_bid": "9.00"})
    assert failed(preview, "max_cpc_bid")


def test_too_many_keywords_in_one_call_is_blocked():
    engine, client, _ = rig(max_entities_per_call=2)
    ad_group(client)
    kind = engine.add(AddKeywords())
    preview = engine.preview(kind, {"ad_group_id": "22", "keywords": ["a", "b", "c"]})
    assert failed(preview, "entities_per_call")


def test_a_keyword_added_by_someone_else_since_the_preview_is_stale():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddKeywords())
    preview = engine.preview(kind, {"ad_group_id": "22", "keywords": ["shoes"]})
    keywords_present(client, ("shoes", "PHRASE"))
    with pytest.raises(PreviewStale):
        engine.apply(kind, preview.preview_token)
    assert client.applied == []


def test_reverting_an_added_keyword_removes_exactly_that_criterion():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddKeywords())
    _, result = preview_and_apply(engine, kind, ad_group_id="22", keywords=["a", "b"])
    created = result.resource_names
    client.answer("ad_group_criterion.resource_name =", [row(**{
        "ad_group_criterion.status": "PAUSED",
    })])

    engine.revert(result.batch_id)

    [_, removal] = client.applied
    assert removal["method"] == "mutate_ad_group_criteria"
    assert [op.remove for op in removal["operations"]] == created


def test_a_revert_refuses_a_keyword_someone_enabled_since():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddKeywords())
    _, result = preview_and_apply(engine, kind, ad_group_id="22", keywords=["a"])
    client.answer("ad_group_criterion.resource_name =", [row(**{
        "ad_group_criterion.status": "ENABLED",
    })])
    with pytest.raises(PreviewStale):
        engine.revert(result.entry_id)


# --------------------------------------------------------------------------
# keyword status and bid
# --------------------------------------------------------------------------


def keyword(client, *, status="PAUSED", own_bid=0, effective=1_000_000):
    client.answer("ad_group_criterion.criterion_id = 77", [row(**{
        "ad_group_criterion.criterion_id": 77,
        "ad_group_criterion.keyword.text": "shoes",
        "ad_group_criterion.keyword.match_type": "PHRASE",
        "ad_group_criterion.status": status,
        "ad_group_criterion.cpc_bid_micros": own_bid,
        "ad_group_criterion.effective_cpc_bid_micros": effective,
        "ad_group.id": 22, "ad_group.name": "shoes", "campaign.id": 11,
        "campaign.name": "brand",
    })])


def test_enabling_a_keyword_round_trips_through_revert():
    engine, client, _ = rig()
    ad_group(client)
    keyword(client)
    kind = engine.add(KeywordStatus())
    preview, result = preview_and_apply(
        engine, kind, ad_group_id="22", criterion_id="77", new_status="ENABLED"
    )
    assert (preview.before, preview.after) == ("PAUSED", "ENABLED")
    op = client.applied[-1]["operations"][0]
    assert op.update.resource_name == "customers/1112223333/adGroupCriteria/22~77"
    assert "status" in op.update_mask.paths

    keyword(client, status="ENABLED")
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.status.name == "PAUSED"


def test_a_first_keyword_bid_reverts_by_clearing_back_to_the_ad_group_default():
    engine, client, _ = rig()
    ad_group(client)
    keyword(client)  # no bid of its own: inherits 1.00
    kind = engine.add(KeywordCpcBid())
    preview, result = preview_and_apply(
        engine, kind, ad_group_id="22", criterion_id="77", new_cpc_bid="1.40"
    )
    assert preview.before == "(ad group default)" and preview.after == "1.40 CAD"
    assert client.applied[-1]["operations"][0].update.cpc_bid_micros == 1_400_000

    keyword(client, own_bid=1_400_000, effective=1_400_000)
    engine.revert(result.entry_id)
    op = client.applied[-1]["operations"][0]
    assert list(op.update_mask.paths) == ["cpc_bid_micros"]
    assert op.update.cpc_bid_micros == 0


def test_the_bid_increase_limit_is_measured_from_the_inherited_bid():
    """A keyword with no own bid must not dodge max_bid_increase_pct."""
    engine, client, _ = rig()
    ad_group(client)
    keyword(client)  # effective 1.00
    kind = engine.add(KeywordCpcBid())
    preview = engine.preview(kind, {"ad_group_id": "22", "criterion_id": "77",
                                    "new_cpc_bid": "2.00"})
    assert failed(preview, "max_bid_increase_pct")


# --------------------------------------------------------------------------
# negatives and shared lists
# --------------------------------------------------------------------------


def test_campaign_negatives_project_zero_and_say_they_are_immediate():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddNegativeKeywords())
    preview, result = preview_and_apply(
        engine, kind, level="campaign", target_id="11", keywords=["free", "jobs"],
    )
    assert Decimal(preview.projected_daily_spend_delta) == 0
    assert "take effect the moment" in preview.note
    [sent] = client.applied
    assert sent["method"] == "mutate_campaign_criteria"
    assert all(op.create.negative for op in sent["operations"])
    assert sent["operations"][0].create.campaign == "customers/1112223333/campaigns/11"

    client.answer("campaign_criterion.resource_name =", [row(**{
        "campaign_criterion.resource_name": result.resource_names[0],
    })])
    engine.revert(result.entry_ids[0])
    assert client.applied[-1]["operations"][0].remove == result.resource_names[0]


def test_ad_group_negatives_are_negative_ad_group_criteria():
    engine, client, _ = rig()
    ad_group(client)
    kind = engine.add(AddNegativeKeywords())
    preview_and_apply(engine, kind, level="ad_group", target_id="22", keywords=["cheap"],
                      match_type="EXACT")
    op = client.applied[-1]["operations"][0]
    assert op.create.negative is True
    assert op.create.ad_group == "customers/1112223333/adGroups/22"


def shared_list(client, attached=(("11", "brand"),)):
    client.answer("FROM shared_set\n", [row(**{
        "shared_set.id": 8, "shared_set.name": "brand safety",
        "shared_set.type_": "NEGATIVE_KEYWORDS", "shared_set.status": "ENABLED",
    })])
    client.answer("FROM campaign_shared_set", [row(**{
        "campaign.id": int(cid), "campaign.name": name,
    }) for cid, name in attached])


def test_a_shared_list_change_names_every_campaign_it_reaches():
    from kindkit import limits

    engine, client, _ = rig()
    shared_list(client, attached=(("11", "brand"), ("12", "generic")))
    kind = engine.add(AddToSharedNegativeList())
    preview = engine.preview(kind, {"shared_set_id": "8", "keywords": ["free"]})
    assert any("brand, generic" in w for w in preview.warnings)

    # And every one of them must be inside the campaign scope.
    engine.ctx.state.limits = limits(allowed_campaign_ids=("11",))
    preview = engine.preview(kind, {"shared_set_id": "8", "keywords": ["free"]})
    assert failed(preview, "campaign_in_scope")


def test_linking_a_list_twice_is_blocked_and_the_link_reverts():
    engine, client, _ = rig()
    ad_group(client)
    shared_list(client, attached=())
    kind = engine.add(LinkSharedNegativeList())
    _, result = preview_and_apply(engine, kind, shared_set_id="8", campaign_id="11")
    sent = client.applied[-1]
    assert sent["method"] == "mutate_campaign_shared_sets"
    assert sent["operations"][0].create.shared_set == "customers/1112223333/sharedSets/8"

    client.answer("campaign_shared_set.status = 'ENABLED'", [row(**{
        "campaign_shared_set.status": "ENABLED",
    })])
    preview = engine.preview(kind, {"shared_set_id": "8", "campaign_id": "11"})
    assert failed(preview, "not_already_attached")

    client.answer("campaign_shared_set.resource_name =", [row(**{
        "campaign_shared_set.status": "ENABLED",
    })])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].remove == result.resource_name


def test_removal_refuses_anything_that_is_not_a_criterion_or_link():
    from googleads_reporting.write import criteria
    from googleads_reporting.write.client import MutationError
    from kindkit import FakeWriteClient

    client = FakeWriteClient()
    for name in ("customers/1/campaigns/11", "customers/1/adGroups/22",
                 "customers/1/adGroupAds/22~5", "customers/1/campaignBudgets/3"):
        with pytest.raises(MutationError, match="only criteria"):
            criteria.plan_remove_created(client, resource_names=[name])
    with pytest.raises(MutationError, match="one kind per request"):
        criteria.plan_remove_created(client, resource_names=[
            "customers/1/adGroupCriteria/1~2", "customers/1/campaignCriteria/1~2",
        ])
