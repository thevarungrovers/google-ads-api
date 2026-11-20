"""Ads and assets kinds."""

from __future__ import annotations

from decimal import Decimal

import pytest

from googleads_mcp.changes.ads import (
    AdStatus,
    AddCallouts,
    AddSitelinks,
    AddStructuredSnippets,
    CreateResponsiveSearchAd,
    ReplaceResponsiveSearchAd,
)
from googleads_reporting.write.client import MutationError
from kindkit import RAW, failed, preview_and_apply, rig, row

HEADLINES = ["Fresh local produce", "Delivered weekly", "Order online"]
DESCRIPTIONS = ["Seasonal food from nearby farms.", "Pick a box, skip any week."]


def text(t):
    asset = RAW.get_type("AdTextAsset")
    asset.text = t
    return asset


def account(client, *, ad_status="ENABLED", ad_type="RESPONSIVE_SEARCH_AD"):
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Brand - Search - EN", "campaign.status": "ENABLED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
        "campaign_budget.id": 5, "campaign_budget.amount_micros": 30_000_000,
    })])
    client.answer("FROM ad_group\n", [row(**{
        "ad_group.id": 22, "ad_group.name": "General",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
        "campaign.id": 11, "campaign.name": "Brand - Search - EN",
    })])
    client.answer("FROM ad_group_ad\n", [row(**{
        "ad_group_ad.ad.id": 5, "ad_group_ad.ad.type_": ad_type,
        "ad_group_ad.status": ad_status,
        "ad_group_ad.resource_name": "customers/1112223333/adGroupAds/22~5",
        "ad_group_ad.ad.final_urls": ["https://example.com"],
        "ad_group_ad.ad.responsive_search_ad.headlines": [text("Old one"), text("Old two"),
                                                          text("Old three")],
        "ad_group_ad.ad.responsive_search_ad.descriptions": [text("Old a."), text("Old b.")],
        "ad_group.id": 22, "ad_group.name": "General",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
        "campaign.id": 11, "campaign.name": "Brand - Search - EN",
    })])


def test_a_new_rsa_is_created_paused_and_costs_three_applies():
    engine, client, _ = rig()
    account(client)
    kind = engine.add(CreateResponsiveSearchAd())
    preview, result = preview_and_apply(
        engine, kind, ad_group_id="22", headlines=HEADLINES, descriptions=DESCRIPTIONS,
        final_url="https://example.com/boxes",
    )
    assert preview.apply_weight == 3 and preview.after == "PAUSED"
    op = client.applied[-1]["operations"][0]
    assert op.create.status.name == "PAUSED"
    assert [h.text for h in op.create.ad.responsive_search_ad.headlines] == HEADLINES
    assert result.session["applies_used"] == 3
    with pytest.raises(ValueError, match="never removed"):
        engine.revert(result.entry_id)


def test_an_over_long_headline_is_refused_by_name():
    engine, client, _ = rig()
    account(client)
    kind = engine.add(CreateResponsiveSearchAd())
    with pytest.raises(MutationError, match="headline 1 is 31 chars"):
        engine.preview(kind, {"ad_group_id": "22", "headlines": ["x" * 31, "b", "c"],
                              "descriptions": DESCRIPTIONS, "final_url": "https://example.com"})


def test_a_creation_needs_room_for_three_applies():
    from googleads_mcp.previews import PreviewExpired

    engine, client, _ = rig(max_applies_per_session=2)
    account(client)
    kind = engine.add(CreateResponsiveSearchAd())
    preview = engine.preview(kind, {"ad_group_id": "22", "headlines": HEADLINES,
                                    "descriptions": DESCRIPTIONS,
                                    "final_url": "https://example.com"})
    with pytest.raises(PreviewExpired, match="costs 3"):
        engine.apply(kind, preview.preview_token)


def test_replacing_an_rsa_creates_and_pauses_in_one_request_and_reverts():
    engine, client, _ = rig()
    account(client)
    kind = engine.add(ReplaceResponsiveSearchAd())
    preview, result = preview_and_apply(engine, kind, ad_group_id="22", ad_id="5",
                                        headlines=HEADLINES, descriptions=DESCRIPTIONS)
    assert preview.before == "ad 5 ENABLED"
    assert preview.after == "new ad ENABLED; ad 5 PAUSED"
    assert "Old headlines: Old one" in preview.note
    [sent] = client.applied
    create, pause = sent["operations"]
    assert create.create.status.name == "ENABLED"
    assert create.create.ad.final_urls[0] == "https://example.com"  # inherited
    assert pause.update.resource_name.endswith("adGroupAds/22~5")
    assert pause.update.status.name == "PAUSED"

    client.answer("ad_group_ad.resource_name = '" + result.resource_name, [row(**{
        "ad_group_ad.status": "ENABLED"})])
    client.answer("adGroupAds/22~5'", [row(**{"ad_group_ad.status": "PAUSED"})])
    engine.revert(result.entry_id)
    new_paused, old_back = client.applied[-1]["operations"]
    assert new_paused.update.resource_name == result.resource_name
    assert new_paused.update.status.name == "PAUSED"
    assert old_back.update.status.name == "ENABLED"


def test_only_an_rsa_can_be_replaced():
    engine, client, _ = rig()
    account(client, ad_type="EXPANDED_TEXT_AD")
    kind = engine.add(ReplaceResponsiveSearchAd())
    with pytest.raises(ValueError, match="not an RSA"):
        engine.preview(kind, {"ad_group_id": "22", "ad_id": "5", "headlines": HEADLINES,
                              "descriptions": DESCRIPTIONS})


def test_enabling_an_ad_is_an_enable():
    engine, client, _ = rig()
    account(client, ad_status="PAUSED")
    client.answer("ad_group_ad.ad.id = 5\n", [row(**{
        "ad_group_ad.ad.id": 5, "ad_group_ad.status": "PAUSED",
        "ad_group_ad.resource_name": "customers/1112223333/adGroupAds/22~5",
    })])
    kind = engine.add(AdStatus())
    preview = engine.preview(kind, {"ad_group_id": "22", "ad_id": "5", "new_status": "ENABLED"})
    assert any(c.startswith("SKIP projected_delta_per_day") for c in preview.checks)


def test_sitelinks_go_in_one_atomic_request_and_revert_unlinks():
    engine, client, _ = rig()
    account(client)
    kind = engine.add(AddSitelinks())
    _, result = preview_and_apply(engine, kind, level="campaign", target_id="11", sitelinks=[
        {"link_text": "Weekly boxes", "final_url": "https://example.com/boxes"},
        {"link_text": "About us", "final_url": "https://example.com/about",
         "description1": "Who we are", "description2": "And how we work"},
    ])
    [sent] = client.applied
    assert sent["method"] == "mutate (atomic)"
    asset, link = sent["operations"][:2]
    assert asset.asset_operation.create.sitelink_asset.link_text == "Weekly boxes"
    assert link.campaign_asset_operation.create.asset == asset.asset_operation.create.resource_name
    assert link.campaign_asset_operation.create.field_type.name == "SITELINK"
    links = [n for n in result.resource_names if "/campaignAssets/" in n]
    assert result.entry_ids and len(links) == 2

    client.answer("campaign_asset.resource_name =", [row(**{"campaign_asset.status": "ENABLED"})])
    engine.revert(result.batch_id)
    assert [op.remove for op in client.applied[-1]["operations"]] == links


def test_asset_text_limits_are_checked_locally():
    engine, client, _ = rig()
    account(client)
    with pytest.raises(MutationError, match="callout 1 is 26 chars"):
        engine.preview(engine.add(AddCallouts()), {"level": "campaign", "target_id": "11",
                                                   "callouts": ["x" * 26]})
    with pytest.raises(MutationError, match="header must be one of"):
        engine.preview(engine.add(AddStructuredSnippets()), {
            "level": "ad_group", "target_id": "22", "header": "Flavours",
            "values": ["a", "b", "c"]})
