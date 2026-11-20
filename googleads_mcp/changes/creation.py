"""Creating campaigns and ad groups, and renaming them.

Creation is allowed, on three conditions that are the point of this module:

  * everything is created PAUSED -- campaign, ad groups and ads -- so going
    live is always a separate, previewed enable that counts against the daily
    total with the campaign's full budget;
  * each creation costs 3 applies against the session and daily caps, so an
    agent cannot build out an account in a loop;
  * nothing created is ever removed here. revert_change refuses a creation and
    says why; a paused campaign spends nothing, and removing one is a job for
    a person in the Google Ads UI.

create_campaign reuses spec.CampaignSpec and builder.build/execute_build: the
budget, campaign, ad groups, keywords and ads go in ONE atomic request wired
by temp ids, so a failure anywhere creates nothing. The EU political
advertising declaration is a REQUIRED argument with no default, and it is
rendered in the preview a human approves: it is a legal declaration, and one
made silently on someone's behalf is not a declaration.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Discriminator, Field, Tag

from googleads_mcp.changes.base import (
    APPLY_REVERSIBLE,
    NOTHING,
    ChangeKind,
    Proposal,
    parse_decimal,
    with_currency,
)
from googleads_mcp.changes.common import one, resource_status
from googleads_mcp.guardrails import Check, check_ceiling, check_currency
from googleads_reporting.query import quote_double
from googleads_reporting.write import adgroups, builder, campaigns
from googleads_reporting.write.spec import (
    AdGroupSpec,
    CampaignSpec,
    ResponsiveDisplayAdSpec,
    ResponsiveSearchAdSpec,
)

NEVER_REMOVED = (
    "A created {what} is never removed here. It was created PAUSED and spends "
    "nothing while paused; leave it paused, or remove it in the Google Ads UI."
)


class SearchAd(BaseModel):
    type: Literal["responsive_search"] = "responsive_search"
    headlines: list[str] = Field(min_length=3, max_length=15)
    descriptions: list[str] = Field(min_length=2, max_length=4)
    final_url: str
    path1: str | None = None
    path2: str | None = None


class DisplayAd(BaseModel):
    type: Literal["responsive_display"] = "responsive_display"
    business_name: str
    long_headline: str
    headlines: list[str] = Field(min_length=1, max_length=5)
    descriptions: list[str] = Field(min_length=1, max_length=5)
    final_url: str
    marketing_images: list[str] = Field(description="Local paths, 1.91:1")
    square_marketing_images: list[str] = Field(description="Local paths, 1:1")
    logo_images: list[str] = []
    square_logo_images: list[str] = []


def _ad_type(value: Any) -> str:
    """The ad's `type`, defaulting to responsive_search as SearchAd declares.

    A plain discriminator demands the tag, so an ad that left `type` out --
    as the schema's default invites -- was refused with "Unable to extract
    tag" (live, from a create_campaign preview).
    """
    if isinstance(value, dict):
        return value.get("type") or "responsive_search"
    return getattr(value, "type", "responsive_search")


class NewAdGroup(BaseModel):
    name: str
    cpc_bid: str | None = Field(default=None, description='Decimal string, e.g. "0.80"')
    keywords: list[str] = Field(default=[], description="PHRASE match, Search only")
    ads: list[Annotated[Union[Annotated[SearchAd, Tag("responsive_search")],
                              Annotated[DisplayAd, Tag("responsive_display")]],
                        Discriminator(_ad_type)]] = Field(min_length=1)


def _spec_ad(ad: dict[str, Any]):
    if ad["type"] == "responsive_search":
        return ResponsiveSearchAdSpec(
            headlines=ad["headlines"], descriptions=ad["descriptions"],
            final_url=ad["final_url"], path1=ad.get("path1"), path2=ad.get("path2"),
        )
    return ResponsiveDisplayAdSpec(
        business_name=ad["business_name"], long_headline=ad["long_headline"],
        headlines=ad["headlines"], descriptions=ad["descriptions"], final_url=ad["final_url"],
        marketing_images=[Path(p) for p in ad["marketing_images"]],
        square_marketing_images=[Path(p) for p in ad["square_marketing_images"]],
        logo_images=[Path(p) for p in ad.get("logo_images", [])],
        square_logo_images=[Path(p) for p in ad.get("square_logo_images", [])],
    )


def build_spec(payload: dict[str, Any]) -> CampaignSpec:
    """The CampaignSpec a payload describes -- ALWAYS paused, at every level."""
    return CampaignSpec(
        name=payload["name"],
        channel=payload["channel"],
        budget_amount=float(payload["daily_budget"]),
        budget_name=f"{payload['name']} budget",
        status="PAUSED",
        contains_eu_political_advertising=payload["contains_eu_political_advertising"],
        ad_groups=[
            AdGroupSpec(
                name=g["name"],
                cpc_bid=float(g["cpc_bid"]) if g.get("cpc_bid") else None,
                keywords=list(g.get("keywords") or []),
                ads=[_spec_ad(a) for a in g["ads"]],
                status="PAUSED",
            )
            for g in payload["ad_groups"]
        ],
    )


def _campaign_named(ctx, name: str) -> str:
    found = one(ctx, ("campaign.id", "campaign.status"), "campaign",
                [f"campaign.name = {quote_double(name)}", "campaign.status != 'REMOVED'"])
    return NOTHING if found is None else f"exists as campaign {found.campaign.id}"


class CreateCampaign(ChangeKind):
    name = "create_campaign"
    preview_title = "Preview creating a whole campaign (created PAUSED)"
    apply_title = "Apply creating a campaign (PAUSED; counts as 3 applies)"
    ledger_keys = (("campaign", "created"),)
    weight = 3
    revertible = False
    creates_named = ("campaign", "ad_group")

    def propose(
        self,
        ctx,
        name: Annotated[str, Field(description="The BASE campaign name; the server adds "
                                   "the configured OWNER-agent- prefix. Must not exist yet")],
        channel: Annotated[Literal["SEARCH", "DISPLAY"], Field(description="Video is refused by the API")],
        daily_budget: Annotated[str, Field(description='Decimal string, e.g. "20.00"')],
        contains_eu_political_advertising: Annotated[
            bool,
            Field(description="REQUIRED legal declaration: does this campaign contain EU "
                  "political advertising? Ask the human; never assume."),
        ],
        ad_groups: Annotated[list[NewAdGroup], Field(min_length=1)],
    ) -> Proposal:
        """Show what creating a campaign -- budget, ad groups, keywords and ads
        in one all-or-nothing request -- would do. Writes nothing.

        Everything is created PAUSED. Going live is a separate
        preview_campaign_status, which projects the full budget against the
        daily total. The EU political advertising declaration is required and
        shown in the preview: ask the human for it, never default it.
        """
        budget = parse_decimal(daily_budget, "daily_budget")
        groups = [(g if isinstance(g, NewAdGroup) else NewAdGroup.model_validate(g)).model_dump()
                  for g in ad_groups]
        # Every name the request sends is stamped here, once: the campaign's
        # (and so its budget's, "<name> budget") and each nested ad group's.
        stamped = ctx.name_for_create("campaign", name)
        names = [stamped]
        for group in groups:
            group_name = ctx.name_for_create("ad_group", group["name"])
            names.append(group_name)
            group["name"] = group_name.final
            if group.get("cpc_bid"):
                group["cpc_bid"] = str(parse_decimal(group["cpc_bid"], f"{group['name']} cpc_bid"))
        payload = {
            "name": stamped.final, "channel": channel, "daily_budget": str(budget),
            "contains_eu_political_advertising": bool(contains_eu_political_advertising),
            "ad_groups": groups,
        }
        build_spec(payload).validate()  # every problem at once, before any request
        currency = ctx.currency_of()
        existing = _campaign_named(ctx, payload["name"])
        checks = [
            check_currency(ctx.limits, currency),
            check_ceiling("max_daily_budget", budget, ctx.limits.max_daily_budget, currency),
            Check("name_is_free", existing == NOTHING,
                  "no campaign has this name" if existing == NOTHING else existing),
            *[check_ceiling(f"max_cpc_bid ({g['name']})", Decimal(g["cpc_bid"]),
                            ctx.limits.max_cpc_bid, currency)
              for g in groups if g.get("cpc_bid")],
        ]
        declaration = ("CONTAINS EU political advertising" if contains_eu_political_advertising
                       else "does NOT contain EU political advertising")
        summary = "; ".join(
            f"{g['name']}: {len(g['ads'])} ad(s), {len(g.get('keywords') or [])} keyword(s)"
            for g in groups
        )
        return Proposal(
            entity_type="campaign", entity_id=f"new:{payload['name']}",
            entity_name=payload["name"], path=payload["name"], field_name="created",
            before=NOTHING, after="PAUSED", checks=checks, projected=Decimal("0"),
            warnings=[f"legal declaration: this campaign {declaration}"],
            note=(f"{channel} campaign, budget {with_currency(budget, currency)}/day, all "
                  f"PAUSED. Ad groups: {summary}. Enabling it later projects the full "
                  "budget against the daily total."),
            payload=payload, names=names,
            created_names=[("campaign", payload["name"]),
                           *(("ad_group", g["name"]) for g in groups)],
        )

    def current(self, ctx, payload):
        return _campaign_named(ctx, payload["name"])

    def build(self, ctx, payload):
        built = builder.build(ctx.client, build_spec(payload))
        return built.plan

    def resource_names_for(self, payload, names):
        campaigns_made = [n for n in names if "/campaigns/" in n]
        return campaigns_made[:1]

    def current_for_entry(self, ctx, entry):
        if not entry.get("resource_name"):
            return None
        return resource_status(ctx, "campaign", entry["resource_name"], "status")

    def inverse(self, ctx, entry):
        raise ValueError(NEVER_REMOVED.format(what="campaign"))


class CreateAdGroup(ChangeKind):
    name = "create_ad_group"
    preview_title = "Preview creating an ad group (created PAUSED)"
    apply_title = "Apply creating an ad group (PAUSED; counts as 3 applies)"
    ledger_keys = (("ad group", "created"),)
    weight = 3
    revertible = False
    creates_named = ("ad_group",)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        name: Annotated[str, Field(description="The BASE name; the server adds the "
                                   "configured OWNER-agent- prefix. Unique within the campaign")],
        cpc_bid: Annotated[str | None, Field(description='Decimal string, e.g. "0.80"')] = None,
    ) -> Proposal:
        """Show what creating an empty ad group would do. Writes nothing.
        Created PAUSED; add keywords and ads, then enable it."""
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        bid = parse_decimal(cpc_bid, "cpc_bid") if cpc_bid else None
        stamped = ctx.name_for_create("ad_group", name)
        name = stamped.final
        existing = self._named(ctx, campaign_id, name)
        checks = [Check("name_is_free", existing == NOTHING,
                        "free in this campaign" if existing == NOTHING else existing)]
        if bid is not None:
            currency = ctx.currency_of()
            checks += [check_currency(ctx.limits, currency),
                       check_ceiling("max_cpc_bid", bid, ctx.limits.max_cpc_bid, currency)]
        return Proposal(
            entity_type="ad group", entity_id=f"{campaign_id}:new:{name}", entity_name=name,
            path=f"{row.campaign.name} > {name}", field_name="created", before=NOTHING,
            after="PAUSED", checks=checks, projected=Decimal("0"),
            campaign_id=str(row.campaign.id), campaign_status=row.campaign.status.name,
            note="Created PAUSED: no spend until enabled with preview_ad_group_status.",
            payload={"campaign_id": str(campaign_id), "campaign_resource": row.campaign.resource_name,
                     "name": name, "cpc_bid": None if bid is None else str(bid)},
            names=[stamped], created_names=[("ad_group", name)],
        )

    @staticmethod
    def _named(ctx, campaign_id: str, name: str) -> str:
        found = one(ctx, ("ad_group.id",), "ad_group", [
            f"campaign.id = {int(campaign_id)}", f"ad_group.name = {quote_double(name)}",
            "ad_group.status != 'REMOVED'",
        ])
        return NOTHING if found is None else f"exists as ad group {found.ad_group.id}"

    def current(self, ctx, payload):
        return self._named(ctx, payload["campaign_id"], payload["name"])

    def build(self, ctx, payload):
        return adgroups.plan_create(
            ctx.client, name=payload["name"], campaign_resource_name=payload["campaign_resource"],
            cpc_bid=None if payload["cpc_bid"] is None else float(payload["cpc_bid"]),
            status="PAUSED",
        )

    def current_for_entry(self, ctx, entry):
        if not entry.get("resource_name"):
            return None
        return resource_status(ctx, "ad_group", entry["resource_name"], "status")

    def inverse(self, ctx, entry):
        raise ValueError(NEVER_REMOVED.format(what="ad group"))


class RenameCampaign(ChangeKind):
    name = "rename_campaign"
    preview_title = "Preview renaming a campaign"
    apply_title = "Apply renaming a campaign"
    apply_annotations = APPLY_REVERSIBLE
    ledger_keys = (("campaign", "name"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        new_name: Annotated[str, Field(description="The new BASE name; the prefix is kept")],
    ) -> Proposal:
        """Show what renaming a campaign would do. Writes nothing. A name
        changes nothing about delivery; reports keyed on the old name will
        stop matching it.

        Only the agent's OWN campaigns can be renamed: one whose name carries
        this server's prefix, or that the ledger shows it created. The prefix
        is kept (or put back); only the base changes."""
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        stamped = ctx.name_for_rename("campaign", row.campaign.name, new_name,
                                      entity_id=str(row.campaign.id))
        new_name = stamped.final
        taken = _campaign_named(ctx, new_name)
        return Proposal(
            entity_type="campaign", entity_id=str(row.campaign.id), entity_name=row.campaign.name,
            path=row.campaign.name, field_name="name", before=row.campaign.name, after=new_name,
            checks=[Check("name_is_free", taken == NOTHING,
                          "no campaign has this name" if taken == NOTHING else taken)],
            projected=Decimal("0"), campaign_id=str(row.campaign.id),
            payload={"campaign_id": str(row.campaign.id)}, names=[stamped],
        )

    def current(self, ctx, payload):
        return campaigns.find(ctx.client, campaign_id=payload["campaign_id"]).campaign.name

    def build(self, ctx, payload):
        return campaigns.plan_rename(ctx.client, campaign_id=payload["campaign_id"],
                                     new_name=payload["after"])

    def current_for_entry(self, ctx, entry):
        return campaigns.find(ctx.client, campaign_id=entry["entity_id"]).campaign.name

    def inverse(self, ctx, entry):
        return campaigns.plan_rename(ctx.client, campaign_id=entry["entity_id"],
                                     new_name=entry["before"])


class RenameAdGroup(ChangeKind):
    name = "rename_ad_group"
    preview_title = "Preview renaming an ad group"
    apply_title = "Apply renaming an ad group"
    apply_annotations = APPLY_REVERSIBLE
    ledger_keys = (("ad group", "name"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        new_name: Annotated[str, Field(description="The new BASE name, unique within its "
                                       "campaign; the prefix is kept")],
    ) -> Proposal:
        """Show what renaming an ad group would do. Writes nothing.

        Only the agent's OWN ad groups can be renamed: one whose name carries
        this server's prefix, or that the ledger shows it created. The prefix
        is kept (or put back); only the base changes."""
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        stamped = ctx.name_for_rename("ad_group", row.ad_group.name, new_name,
                                      entity_id=str(row.ad_group.id))
        new_name = stamped.final
        taken = CreateAdGroup._named(ctx, str(row.campaign.id), new_name)
        return Proposal(
            entity_type="ad group", entity_id=str(row.ad_group.id), entity_name=row.ad_group.name,
            path=f"{row.campaign.name} > {row.ad_group.name}", field_name="name",
            before=row.ad_group.name, after=new_name,
            checks=[Check("name_is_free", taken == NOTHING,
                          "free in this campaign" if taken == NOTHING else taken)],
            projected=Decimal("0"), campaign_id=str(row.campaign.id),
            payload={"ad_group_id": str(row.ad_group.id)}, names=[stamped],
        )

    def current(self, ctx, payload):
        return adgroups.find(ctx.client, ad_group_id=payload["ad_group_id"]).ad_group.name

    def build(self, ctx, payload):
        return adgroups.plan_rename(ctx.client, ad_group_id=payload["ad_group_id"],
                                    new_name=payload["after"])

    def current_for_entry(self, ctx, entry):
        return adgroups.find(ctx.client, ad_group_id=entry["entity_id"]).ad_group.name

    def inverse(self, ctx, entry):
        return adgroups.plan_rename(ctx.client, ad_group_id=entry["entity_id"],
                                    new_name=entry["before"])
