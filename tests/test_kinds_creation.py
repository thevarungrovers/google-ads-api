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
    """Our ad group: the prefix is kept, and the revert restores the exact old name."""
    engine, client, _ = named_rig("AB", "max")
    client.answer("ad_group.id = 22", [row(**{
        "ad_group.id": 22, "ad_group.name": "AB-max-General", "campaign.id": 11,
        "campaign.name": "Example Co - Search - EN",
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
    })])
    kind = engine.add(RenameAdGroup())
    preview, result = preview_and_apply(engine, kind, ad_group_id="22", new_name="Generic")
    assert preview.naming["rule"] == "kept_prefix"
    assert client.applied[-1]["operations"][0].update.name == "AB-max-Generic"
    client.answer("ad_group.id = 22", [row(**{
        "ad_group.id": 22, "ad_group.name": "AB-max-Generic", "campaign.id": 11,
        "ad_group.resource_name": "customers/1112223333/adGroups/22",
    })])
    engine.revert(result.entry_id)
    assert client.applied[-1]["operations"][0].update.name == "AB-max-General"


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


# -- renames: only our own entities ----------------------------------------


def campaign_named(client, name, campaign_id=11):
    client.answer(f"campaign.id = {campaign_id}", [row(**{
        "campaign.id": campaign_id, "campaign.name": name, "campaign.status": "ENABLED",
        "campaign.resource_name": f"customers/1112223333/campaigns/{campaign_id}",
    })])


def ledger_created_campaign(campaign_id=11, groups=(), group_ids=()):
    """A create_campaign row as the engine writes it: the names it showed, and
    every resource the request created (the ad groups by id)."""
    from googleads_mcp import ledger

    ledger.write_intent(
        entry_id=f"made{campaign_id}", tool="apply_create_campaign", customer_id="1112223333",
        entity_type="campaign", entity_id="new:x", entity_name="x", field_name="created",
        before="(none)", after="PAUSED", projected_delta="0",
        detail={"created_names": [["ad_group", g] for g in groups]},
    )
    made = [f"customers/1112223333/campaigns/{campaign_id}",
            *(f"customers/1112223333/adGroups/{g}" for g in group_ids)]
    ledger.write_outcome(entry_id=f"made{campaign_id}", ok=True, resource_name=made[0],
                         created_resources=made)


@pytest.mark.parametrize("owner, agent, prefix", IDENTITIES)
def test_renaming_our_campaign_keeps_the_prefix(owner, agent, prefix):
    engine, client, _ = named_rig(owner, agent)
    campaign_named(client, f"{prefix}old")
    if not prefix:
        ledger_created_campaign()  # with no prefix, "ours" is the ledger's word
    kind = engine.add(RenameCampaign())
    preview, _ = preview_and_apply(engine, kind, campaign_id="11", new_name="spring")
    assert preview.after == f"{prefix}spring"
    assert client.applied[-1]["operations"][0].update.name == f"{prefix}spring"


@pytest.mark.parametrize("owner, agent, prefix", IDENTITIES)
def test_renaming_a_manual_campaign_is_refused(owner, agent, prefix):
    engine, client, _ = named_rig(owner, agent)
    campaign_named(client, "Hand made")
    kind = engine.add(RenameCampaign())
    preview = engine.preview(kind, {"campaign_id": "11", "new_name": "spring"})
    assert failed(preview, "own_entity")
    assert any("manual entity" in line for line in preview.checks)


@pytest.mark.parametrize("owner, agent, prefix", IDENTITIES)
def test_renaming_another_agents_campaign_is_refused(owner, agent, prefix):
    engine, client, _ = named_rig(owner, agent)
    campaign_named(client, "CD-bot-launch")
    kind = engine.add(RenameCampaign())
    preview = engine.preview(kind, {"campaign_id": "11", "new_name": "spring"})
    assert failed(preview, "own_entity")
    assert any("another agent" in line for line in preview.checks)


def test_removing_our_prefix_is_not_possible_and_forging_another_is_refused():
    engine, client, _ = named_rig("AB", "max")
    campaign_named(client, "AB-max-old")
    kind = engine.add(RenameCampaign())
    # A bare base keeps the prefix; there is no way to ask for it gone.
    assert engine.preview(kind, {"campaign_id": "11", "new_name": "new"}).after == "AB-max-new"
    with pytest.raises(ValueError, match="another agent's prefix"):
        engine.preview(kind, {"campaign_id": "11", "new_name": "CD-bot-new"})


def test_a_ledger_owned_campaign_that_lost_its_prefix_gets_it_back():
    engine, client, _ = named_rig("AB", "max")
    campaign_named(client, "renamed in the UI")
    ledger_created_campaign()
    kind = engine.add(RenameCampaign())
    preview = engine.preview(kind, {"campaign_id": "11", "new_name": "spring"})
    assert not preview.blocked, preview.checks
    assert preview.after == "AB-max-spring" and preview.naming["rule"] == "restored_prefix"
    assert any("restoring the prefix" in w for w in preview.warnings)


@pytest.mark.parametrize("owner, agent, prefix", IDENTITIES)
def test_an_ad_group_built_inside_our_campaign_is_ours(owner, agent, prefix):
    engine, client, _ = named_rig(owner, agent)
    client.answer("ad_group.id = 22", [row(**{
        "ad_group.id": 22, "ad_group.name": f"{prefix}General", "campaign.id": 11,
        "campaign.name": "x", "ad_group.resource_name": "customers/1112223333/adGroups/22",
    })])
    ledger_created_campaign(groups=[f"{prefix}General"], group_ids=[22])
    kind = engine.add(RenameAdGroup())
    preview = engine.preview(kind, {"ad_group_id": "22", "new_name": "Generic"})
    assert not preview.blocked, preview.checks
    assert preview.after == f"{prefix}Generic"


def test_a_manual_ad_group_in_our_campaign_is_not_ours():
    engine, client, _ = named_rig("", "")
    client.answer("ad_group.id = 22", [row(**{
        "ad_group.id": 22, "ad_group.name": "Added by hand", "campaign.id": 11,
        "campaign.name": "x", "ad_group.resource_name": "customers/1112223333/adGroups/22",
    })])
    ledger_created_campaign(groups=["General"])
    kind = engine.add(RenameAdGroup())
    assert failed(engine.preview(kind, {"ad_group_id": "22", "new_name": "x"}), "own_entity")


def test_with_no_identity_a_create_cannot_forge_another_identity():
    engine, client, _ = named_rig("", "")
    kind = engine.add(CreateCampaign())
    with pytest.raises(ValueError, match="CD-bot-"):
        engine.preview(kind, args(name="CD-bot-spring"))


def test_with_no_identity_a_rename_cannot_forge_another_identity():
    engine, client, _ = named_rig("", "")
    campaign_named(client, "old")
    ledger_created_campaign()
    kind = engine.add(RenameCampaign())
    with pytest.raises(ValueError, match="CD-bot-"):
        engine.preview(kind, {"campaign_id": "11", "new_name": "CD-bot-spring"})


def test_a_later_manual_ad_group_with_a_nested_name_is_not_ours():
    """With no prefix configured, our campaign was created with an ad group
    named "General". A person later adds ANOTHER ad group named "General" to
    it (the first was removed or renamed). Only the id proves which one we
    made; the name alone must not make theirs ours."""
    engine, client, _ = named_rig("", "")
    client.answer("ad_group.id = 23", [row(**{
        "ad_group.id": 23, "ad_group.name": "General", "campaign.id": 11,
        "campaign.name": "x", "ad_group.resource_name": "customers/1112223333/adGroups/23",
    })])
    ledger_created_campaign(groups=["General"], group_ids=[22])
    kind = engine.add(RenameAdGroup())
    assert failed(engine.preview(kind, {"ad_group_id": "23", "new_name": "x"}), "own_entity")


def test_creating_a_campaign_records_every_entity_it_created_by_id():
    from googleads_mcp import ledger

    engine, client, _ = named_rig("", "")
    kind = engine.add(CreateCampaign())
    _, result = preview_and_apply(engine, kind, **args(
        name="spring", ad_groups=[{"name": "General", "ads": [AD]}]))
    index = ledger.created_index()
    made = ledger.find_entry(result.entry_id)["detail"]["created_resources"]
    group_ids = {n.rsplit("/", 1)[1] for n in made if "/adGroups/" in n}
    assert len(group_ids) == 1 and group_ids <= index["ad_group"]
