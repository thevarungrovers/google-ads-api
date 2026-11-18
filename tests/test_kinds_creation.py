"""Creating campaigns and ad groups, and renaming them."""

from __future__ import annotations

import pytest

from googleads_mcp.changes.creation import CreateAdGroup, CreateCampaign, RenameAdGroup, RenameCampaign
from googleads_reporting.write.client import MutationError
from kindkit import failed, preview_and_apply, rig, row

AD = {"type": "responsive_search",
      "headlines": ["Example Co boxes", "Delivered weekly", "Order online"],
      "descriptions": ["Fresh food, delivered.", "Skip any week."],
      "final_url": "https://example.com"}


def args(**over):
    base = dict(name="Example Co - Search - EN", channel="SEARCH", daily_budget="20.00",
                contains_eu_political_advertising=False,
                ad_groups=[{"name": "General", "cpc_bid": "0.80",
                            "keywords": ["example boxes"], "ads": [AD]}])
    base.update(over)
    return base


def test_a_campaign_is_built_paused_in_one_atomic_request():
    engine, client, _ = rig()
    kind = engine.add(CreateCampaign())
    preview, result = preview_and_apply(engine, kind, **args())

    assert preview.after == "PAUSED" and preview.apply_weight == 3
    assert any("does NOT contain EU political advertising" in w for w in preview.warnings)
    [validate, sent] = client.sent
    assert validate["apply"] is False and sent["apply"] is True
    assert sent["method"] == "mutate (atomic)"
    ops = sent["operations"]
    campaign = next(op.campaign_operation.create for op in ops if "campaign_operation" in op)
    assert campaign.status.name == "PAUSED"
    assert campaign.contains_eu_political_advertising.name == (
        "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING")
    group = next(op.ad_group_operation.create for op in ops if "ad_group_operation" in op)
    assert group.status.name == "PAUSED"
    ad = next(op.ad_group_ad_operation.create for op in ops if "ad_group_ad_operation" in op)
    assert ad.status.name == "PAUSED"
    assert "/campaigns/" in result.resource_name
    assert result.session["applies_used"] == 3

    with pytest.raises(ValueError, match="never removed"):
        engine.revert(result.entry_id)


def test_the_eu_declaration_has_no_default():
    import inspect

    parameter = inspect.signature(CreateCampaign().propose).parameters[
        "contains_eu_political_advertising"]
    assert parameter.default is inspect.Parameter.empty


def test_a_campaign_name_already_taken_blocks():
    engine, client, _ = rig()
    client.answer('campaign.name = "Example Co - Search - EN"', [row(**{"campaign.id": 9})])
    kind = engine.add(CreateCampaign())
    assert failed(engine.preview(kind, args()), "name_is_free")


def test_a_budget_over_the_ceiling_and_video_are_refused():
    engine, client, _ = rig()
    kind = engine.add(CreateCampaign())
    assert failed(engine.preview(kind, args(daily_budget="500.00")), "max_daily_budget")
    with pytest.raises(MutationError, match="needs a SEARCH campaign, not DISPLAY"):
        engine.preview(kind, args(channel="DISPLAY"))


def test_an_ad_group_is_created_paused():
    engine, client, _ = rig()
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Example Co - Search - EN",
        "campaign.status": "PAUSED", "campaign.resource_name": "customers/1112223333/campaigns/11",
    })])
    kind = engine.add(CreateAdGroup())
    _, result = preview_and_apply(engine, kind, campaign_id="11", name="Gifts", cpc_bid="0.70")
    op = client.applied[-1]["operations"][0]
    assert op.create.status.name == "PAUSED"
    assert op.create.campaign == "customers/1112223333/campaigns/11"
    assert op.create.cpc_bid_micros == 700_000


def test_renaming_round_trips():
    engine, client, _ = rig()
    client.answer("ad_group.id = 22", [row(**{
        "ad_group.id": 22, "ad_group.name": "General", "campaign.id": 11,
        "campaign.name": "Example Co - Search - EN",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
    })])
    kind = engine.add(RenameAdGroup())
    _, result = preview_and_apply(engine, kind, ad_group_id="22", new_name="Generic")
    assert client.applied[-1]["operations"][0].update.name == "Generic"
    client.answer("ad_group.id = 22", [row(**{
        "ad_group.id": 22, "ad_group.name": "Generic", "campaign.id": 11,
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
    })])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.name == "General"


def test_a_campaign_rename_to_a_taken_name_blocks():
    engine, client, _ = rig()
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Old", "campaign.status": "ENABLED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
    })])
    client.answer('campaign.name = "Taken"', [row(**{"campaign.id": 12})])
    kind = engine.add(RenameCampaign())
    assert failed(engine.preview(kind, {"campaign_id": "11", "new_name": "Taken"}), "name_is_free")


# -- naming: what the request SENDS carries the prefix --------------------

IDENTITIES = [("AB", "max", "AB-max-"), ("AB", "", "AB-"), ("", "max", "max-"), ("", "", "")]


def named_rig(owner, agent):
    from googleads_reporting.naming import Identity

    engine, client, read = rig()
    engine.ctx.state.identity = Identity.from_values(owner, agent, source=".env")
    return engine, client, read


def sent_names(client):
    ops = client.applied[-1]["operations"]
    campaign = next(op.campaign_operation.create for op in ops if "campaign_operation" in op)
    budget = next(op.campaign_budget_operation.create for op in ops
                  if "campaign_budget_operation" in op)
    groups = [op.ad_group_operation.create.name for op in ops if "ad_group_operation" in op]
    return campaign.name, budget.name, groups


@pytest.mark.parametrize("owner, agent, prefix", IDENTITIES)
def test_a_campaign_and_every_nested_ad_group_are_sent_prefixed(owner, agent, prefix):
    engine, client, _ = named_rig(owner, agent)
    kind = engine.add(CreateCampaign())
    two_groups = [{"name": "General", "ads": [AD]}, {"name": "Gifts", "ads": [AD]}]
    preview, _ = preview_and_apply(engine, kind, **args(name="spring_promo",
                                                          ad_groups=two_groups))
    final = f"{prefix}spring_promo"
    assert preview.entity_name == final and preview.path == final
    assert preview.naming["final"] == final
    assert [b["final"] for b in preview.naming.get("also", [])] == [
        f"{prefix}General", f"{prefix}Gifts"]
    assert sent_names(client) == (final, f"{final} budget",
                                  [f"{prefix}General", f"{prefix}Gifts"])


def test_our_own_prefix_is_not_doubled_and_another_is_refused():
    engine, client, _ = named_rig("AB", "max")
    kind = engine.add(CreateCampaign())
    preview = engine.preview(kind, args(name="AB-max-spring"))
    assert preview.naming["final"] == "AB-max-spring" and preview.naming["rule"] == "already_ours"
    with pytest.raises(ValueError, match="another agent's prefix"):
        engine.preview(kind, args(name="CD-bot-spring"))


def test_name_is_free_checks_the_final_name():
    engine, client, _ = named_rig("AB", "max")
    client.answer('campaign.name = "AB-max-spring"', [row(**{"campaign.id": 9})])
    kind = engine.add(CreateCampaign())
    assert failed(engine.preview(kind, args(name="spring")), "name_is_free")


def test_an_over_length_campaign_name_is_refused_with_the_room_left():
    engine, client, _ = named_rig("AB", "max")
    kind = engine.add(CreateCampaign())
    preview = engine.preview(kind, args(name="x" * 242))
    assert failed(preview, "name_length")
    assert any("at most 241 bytes" in line for line in preview.checks)


@pytest.mark.parametrize("owner, agent, prefix", IDENTITIES)
def test_an_ad_group_is_sent_prefixed(owner, agent, prefix):
    engine, client, _ = named_rig(owner, agent)
    client.answer("FROM campaign\n", [row(**{
        "campaign.id": 11, "campaign.name": "Example campaign", "campaign.status": "PAUSED",
        "campaign.resource_name": "customers/1112223333/campaigns/11",
    })])
    kind = engine.add(CreateAdGroup())
    preview, _ = preview_and_apply(engine, kind, campaign_id="11", name="Gifts")
    assert preview.naming["final"] == f"{prefix}Gifts"
    assert client.applied[-1]["operations"][0].create.name == f"{prefix}Gifts"
