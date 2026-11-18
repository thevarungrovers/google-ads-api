"""Ads and assets: create an RSA, pause/enable an ad, "edit" an RSA (create
the new one and pause the old one in one request), and attach sitelinks,
callouts and structured snippets.

Nothing here removes an ad. A created ad starts PAUSED, and the undo for a
created ad is to leave it paused (or pause it with preview_ad_status). The
undo for a replacement pauses the new ad and restores the old one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from googleads_mcp.changes.base import NOTHING, ChangeKind, LedgerItem, Proposal
from googleads_mcp.changes.campaigns import wanted_status
from googleads_mcp.changes.common import CreatesRemovable, campaign_headroom, one, resource_status
from googleads_mcp.guardrails import Check
from googleads_reporting.write import adgroups, ads, assets, campaigns

Headlines = Annotated[list[str], Field(description="3-15 headlines, 30 chars each",
                                       min_length=3, max_length=15)]
Descriptions = Annotated[list[str], Field(description="2-4 descriptions, 90 chars each",
                                          min_length=2, max_length=4)]

_AD_FIELDS = (
    "ad_group_ad.ad.id", "ad_group_ad.ad.type", "ad_group_ad.status",
    "ad_group_ad.resource_name", "ad_group_ad.ad.final_urls",
    "ad_group_ad.ad.responsive_search_ad.headlines",
    "ad_group_ad.ad.responsive_search_ad.descriptions",
    "ad_group_ad.ad.responsive_search_ad.path1", "ad_group_ad.ad.responsive_search_ad.path2",
    "ad_group.id", "ad_group.name", "ad_group.resource_name",
    "campaign.id", "campaign.name",
)


def _ad(ctx, ad_group_id: str, ad_id: str):
    found = one(ctx, _AD_FIELDS, "ad_group_ad", [
        f"ad_group.id = {int(ad_group_id)}", f"ad_group_ad.ad.id = {int(ad_id)}",
        "ad_group_ad.status != 'REMOVED'",
    ])
    if found is None:
        raise LookupError(f"No ad {ad_id} in ad group {ad_group_id}. See list_ads.")
    return found


def _texts(repeated) -> list[str]:
    return [item.text for item in repeated]


class CreateResponsiveSearchAd(ChangeKind):
    name = "create_responsive_search_ad"
    preview_title = "Preview creating a responsive search ad (created PAUSED)"
    apply_title = "Apply creating a responsive search ad (PAUSED; counts as 3 applies)"
    ledger_keys = (("ad", "created"),)
    weight = 3
    revertible = False

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        headlines: Headlines,
        descriptions: Descriptions,
        final_url: Annotated[str, Field(description="Absolute https URL")],
        path1: Annotated[str | None, Field(description="Display path, 15 chars")] = None,
        path2: Annotated[str | None, Field(description="Display path, 15 chars")] = None,
    ) -> Proposal:
        """Show what creating a new responsive search ad would do. Writes
        nothing. The ad is created PAUSED and goes live only through a
        separate preview_ad_status; Google reviews it in the meantime."""
        ads.validate_rsa_text(headlines, descriptions, final_url)
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        return Proposal(
            entity_type="ad", entity_id=f"{ad_group_id}:new:{headlines[0]}",
            entity_name=f"RSA {headlines[0]!r}", path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="created", before=NOTHING, after="PAUSED", checks=[],
            projected=Decimal("0"), campaign_id=str(row.campaign.id),
            note=("Created PAUSED: no spend until enabled. Headlines: "
                  + " | ".join(headlines) + ". Descriptions: " + " | ".join(descriptions)),
            payload={"ad_group_resource": row.ad_group.resource_name, "headlines": list(headlines),
                     "descriptions": list(descriptions), "final_url": final_url,
                     "path1": path1, "path2": path2},
        )

    def current(self, ctx, payload):
        return NOTHING

    def build(self, ctx, payload):
        return ads.plan_create_responsive_search_ad(
            ctx.client, ad_group_resource_name=payload["ad_group_resource"],
            headlines=payload["headlines"], descriptions=payload["descriptions"],
            final_url=payload["final_url"], path1=payload["path1"], path2=payload["path2"],
            status="PAUSED",
        )

    def current_for_entry(self, ctx, entry):
        if not entry.get("resource_name"):
            return None
        return resource_status(ctx, "ad_group_ad", entry["resource_name"], "status")

    def inverse(self, ctx, entry):
        raise ValueError(
            "A created ad is never removed here. It was created PAUSED; if it has been "
            "enabled since, pause it with preview_ad_status. Removing it is a job for "
            "the Google Ads UI."
        )


class AdStatus(ChangeKind):
    name = "ad_status"
    preview_title = "Preview pausing or enabling an ad"
    apply_title = "Apply an ad status change (AFFECTS DELIVERY)"
    ledger_keys = (("ad", "status"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From list_ads")],
        ad_id: Annotated[str, Field(description="From list_ads")],
        new_status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling one ad would do. Writes nothing.
        Enabling is bounded by the campaign's unused daily budget and counted
        against the daily total."""
        wanted = wanted_status(new_status)
        row = _ad(ctx, ad_group_id, ad_id)
        before = row.ad_group_ad.status.name
        if wanted == "PAUSED":
            projected, note = Decimal("0"), "Pausing cannot increase spend."
        else:
            projected, note = campaign_headroom(ctx, str(row.campaign.id))
        return Proposal(
            entity_type="ad", entity_id=f"{ad_group_id}~{ad_id}",
            entity_name=f"ad {ad_id} ({row.ad_group_ad.ad.type_.name})",
            path=f"{row.campaign.name} > {row.ad_group.name}", field_name="status",
            before=before, after=wanted, checks=[], projected=projected,
            campaign_id=str(row.campaign.id), note=note, is_enable=wanted == "ENABLED",
            warnings=[f"already {wanted}"] if before == wanted else [],
            payload={"ad_group_id": str(ad_group_id), "ad_id": str(ad_id)},
        )

    def current(self, ctx, payload):
        return _ad(ctx, payload["ad_group_id"], payload["ad_id"]).ad_group_ad.status.name

    def build(self, ctx, payload):
        return ads.plan_set_status(ctx.client, ad_id=payload["ad_id"], status=payload["after"])

    def current_for_entry(self, ctx, entry):
        return _ad(ctx, *entry["entity_id"].split("~")).ad_group_ad.status.name

    def revert_projection(self, ctx, entry):
        if entry["before"] != "ENABLED":
            return Decimal("0"), False
        row = _ad(ctx, *entry["entity_id"].split("~"))
        return campaign_headroom(ctx, str(row.campaign.id))[0], True

    def inverse(self, ctx, entry):
        return ads.plan_set_status(ctx.client, ad_id=entry["entity_id"].split("~")[1],
                                   status=entry["before"])


class ReplaceResponsiveSearchAd(ChangeKind):
    name = "replace_responsive_search_ad"
    preview_title = "Preview editing an RSA (new ad + pause the old, one request)"
    apply_title = "Apply an RSA replacement (AFFECTS DELIVERY; counts as 3 applies)"
    ledger_keys = (("ad", "replacement"),)
    weight = 3

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From list_ads")],
        ad_id: Annotated[str, Field(description="The RSA to replace, from list_ads")],
        headlines: Headlines,
        descriptions: Descriptions,
        final_url: Annotated[str | None, Field(description="Default: the old ad's")] = None,
        path1: Annotated[str | None, Field(description="Default: the old ad's")] = None,
        path2: Annotated[str | None, Field(description="Default: the old ad's")] = None,
    ) -> Proposal:
        """Show what editing an RSA's text would do. Writes nothing.

        Google cannot edit an ad's text, so this creates a new RSA with the old
        one's status and pauses the old one, in a single all-or-nothing
        request. The new ad goes through policy review from scratch and its
        stats start at zero. Reverting pauses the new ad and restores the old.
        """
        row = _ad(ctx, ad_group_id, ad_id)
        if row.ad_group_ad.ad.type_.name != "RESPONSIVE_SEARCH_AD":
            raise ValueError(f"Ad {ad_id} is a {row.ad_group_ad.ad.type_.name}, not an RSA.")
        rsa = row.ad_group_ad.ad.responsive_search_ad
        url = final_url or (row.ad_group_ad.ad.final_urls[0] if row.ad_group_ad.ad.final_urls else "")
        ads.validate_rsa_text(headlines, descriptions, url)
        old_status = row.ad_group_ad.status.name
        before = f"ad {ad_id} {old_status}"
        after = f"new ad {old_status}; ad {ad_id} PAUSED"
        old_text = " | ".join(_texts(rsa.headlines))
        return Proposal(
            entity_type="ad", entity_id=f"{ad_group_id}~{ad_id}",
            entity_name=f"RSA {ad_id} -> {headlines[0]!r}",
            path=f"{row.campaign.name} > {row.ad_group.name}", field_name="replacement",
            before=before, after=after,
            checks=[Check("is_a_change", list(headlines) != _texts(rsa.headlines)
                          or list(descriptions) != _texts(rsa.descriptions)
                          or url != (row.ad_group_ad.ad.final_urls[0] if row.ad_group_ad.ad.final_urls else ""),
                          "text or URL differs from the old ad")],
            projected=Decimal("0"), campaign_id=str(row.campaign.id),
            warnings=["the new ad restarts policy review and its stats start at zero"],
            note=(f"Old headlines: {old_text}. New headlines: {' | '.join(headlines)}. "
                  "A swap of one serving ad for another: projected delta 0."),
            items=[LedgerItem("ad", f"{ad_group_id}~{ad_id}", f"RSA {ad_id}", "replacement",
                              before, after,
                              {"old_ad_resource": row.ad_group_ad.resource_name,
                               "old_status": old_status, "ad_id": str(ad_id),
                               "ad_group_id": str(ad_group_id)})],
            payload={"ad_group_id": str(ad_group_id), "ad_id": str(ad_id),
                     "ad_group_resource": row.ad_group.resource_name,
                     "old_ad_resource": row.ad_group_ad.resource_name, "new_status": old_status,
                     "headlines": list(headlines), "descriptions": list(descriptions),
                     "final_url": url, "path1": path1 or rsa.path1 or None,
                     "path2": path2 or rsa.path2 or None},
        )

    def current(self, ctx, payload):
        return f"ad {payload['ad_id']} {_ad(ctx, payload['ad_group_id'], payload['ad_id']).ad_group_ad.status.name}"

    def build(self, ctx, payload):
        return ads.plan_replace_responsive_search_ad(
            ctx.client, ad_group_resource_name=payload["ad_group_resource"],
            old_ad_resource_name=payload["old_ad_resource"], new_status=payload["new_status"],
            headlines=payload["headlines"], descriptions=payload["descriptions"],
            final_url=payload["final_url"], path1=payload["path1"], path2=payload["path2"],
        )

    def resource_names_for(self, payload, names):
        return list(names[:1])  # [new ad, old ad] -> the new one

    def current_for_entry(self, ctx, entry):
        detail = entry.get("detail") or {}
        if not entry.get("resource_name"):
            return None
        new = resource_status(ctx, "ad_group_ad", entry["resource_name"], "status")
        old = resource_status(ctx, "ad_group_ad", detail["old_ad_resource"], "status")
        return f"new ad {new}; ad {detail['ad_id']} {old}"

    def inverse(self, ctx, entry):
        detail = entry.get("detail") or {}
        if not entry.get("resource_name"):
            raise ValueError("No resource name recorded for the new ad; cannot undo.")
        return ads.plan_undo_replacement(
            ctx.client, new_ad_resource_name=entry["resource_name"],
            old_ad_resource_name=detail["old_ad_resource"], old_status=detail["old_status"],
        )


# ---------------------------------------------------------------------------
# Linked assets
# ---------------------------------------------------------------------------


Level = Annotated[Literal["campaign", "ad_group"], Field(description="Where to attach them")]
TargetId = Annotated[str, Field(description="The campaign_id or ad_group_id")]


class _AddLinkedAssets(CreatesRemovable):
    """Create assets and link them; revert removes the LINK only."""

    asset_kind = ""
    status_field = "status"

    def resource_for(self, entry):
        return (entry.get("detail") or {}).get("resource", "campaign_asset")

    def _propose(self, ctx, level: str, target_id: str, items: list[dict[str, Any]]) -> Proposal:
        assets.validate(self.asset_kind, items)
        if level == "ad_group":
            row = adgroups.find(ctx.client, ad_group_id=target_id)
            campaign_id, path = str(row.campaign.id), f"{row.campaign.name} > {row.ad_group.name}"
        else:
            row = campaigns.find(ctx.client, campaign_id=target_id)
            campaign_id, path = str(row.campaign.id), row.campaign.name
        resource = "ad_group_asset" if level == "ad_group" else "campaign_asset"
        labels = [assets.label(self.asset_kind, i) for i in items]
        return Proposal(
            entity_type=self.asset_kind, entity_id=str(target_id),
            entity_name=f"{len(items)} {self.asset_kind}(s) on {path}", path=path,
            field_name="created", before=NOTHING, after="; ".join(labels), checks=[],
            projected=Decimal("0"), campaign_id=campaign_id, batch=True,
            items=[LedgerItem(self.asset_kind, f"{level}:{target_id}:{label}", label, "created",
                              NOTHING, "ENABLED", {"resource": resource, "level": level,
                                                   "target_id": str(target_id)})
                   for label in labels],
            note=("Assets are created and linked in one all-or-nothing request. They change "
                  "how ads look, not what is bid, so the projected delta is 0. Reverting "
                  "unlinks them; the assets themselves stay in the library, unused."),
            payload={"level": level, "target_id": str(target_id), "items": items},
        )

    def current(self, ctx, payload):
        return NOTHING

    def build(self, ctx, payload):
        return assets.plan_add_linked_assets(
            ctx.client, kind=self.asset_kind, level=payload["level"],
            target_id=payload["target_id"], items=payload["items"],
        )

    def resource_names_for(self, payload, names):
        # [asset, link, asset, link, ...] -- the LINK is what a revert removes.
        return list(names[1::2])


class Sitelink(BaseModel):
    link_text: str = Field(description="25 chars")
    final_url: str = Field(description="Absolute https URL")
    description1: str | None = Field(default=None, description="35 chars; both or neither")
    description2: str | None = Field(default=None, description="35 chars; both or neither")


class AddSitelinks(_AddLinkedAssets):
    name = "add_sitelinks"
    preview_title = "Preview adding sitelinks to a campaign or ad group"
    apply_title = "Apply adding sitelinks"
    ledger_keys = (("sitelink", "created"),)
    asset_kind = "sitelink"

    def propose(
        self, ctx, level: Level, target_id: TargetId,
        sitelinks: Annotated[list[Sitelink], Field(min_length=1)],
    ) -> Proposal:
        """Show what adding sitelinks would do. Writes nothing."""
        items = [(s if isinstance(s, Sitelink) else Sitelink.model_validate(s)).model_dump()
                 for s in sitelinks]
        return self._propose(ctx, level, target_id, items)


class AddCallouts(_AddLinkedAssets):
    name = "add_callouts"
    preview_title = "Preview adding callouts to a campaign or ad group"
    apply_title = "Apply adding callouts"
    ledger_keys = (("callout", "created"),)
    asset_kind = "callout"

    def propose(
        self, ctx, level: Level, target_id: TargetId,
        callouts: Annotated[list[str], Field(min_length=1, description="25 chars each")],
    ) -> Proposal:
        """Show what adding callouts would do. Writes nothing."""
        return self._propose(ctx, level, target_id, [{"text": t} for t in callouts])


class AddStructuredSnippets(_AddLinkedAssets):
    name = "add_structured_snippets"
    preview_title = "Preview adding a structured snippet to a campaign or ad group"
    apply_title = "Apply adding a structured snippet"
    ledger_keys = (("structured_snippet", "created"),)
    asset_kind = "structured_snippet"

    def propose(
        self, ctx, level: Level, target_id: TargetId,
        header: Annotated[str, Field(description=f"One of: {', '.join(assets.SNIPPET_HEADERS)}")],
        values: Annotated[list[str], Field(min_length=3, max_length=10,
                                           description="3-10 values, 25 chars each")],
    ) -> Proposal:
        """Show what adding a structured snippet would do. Writes nothing."""
        return self._propose(ctx, level, target_id, [{"header": header, "values": list(values)}])
