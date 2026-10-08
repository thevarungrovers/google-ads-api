"""Turning a :class:`~.spec.CampaignSpec` into one atomic request.

Everything a new campaign needs -- budget, campaign, ad groups, image and video
assets, ads, keywords -- goes in a single ``GoogleAdsService.mutate`` call,
wired together with **temp resource names**: negative ids that an operation
later in the same request can reference before the entity exists.

    budget   -1   customers/X/campaignBudgets/-1
    campaign -2   references campaignBudgets/-1
    ad group -3   references campaigns/-2
    asset  -100   customers/X/assets/-100
    ad            references adGroups/-3 and assets/-100

The alternative -- one call per entity -- cannot be undone halfway. A failure
at the ad step would leave a campaign and an ad group already created, with a
budget attached, for someone to find and clean up later. Here, a failure
anywhere means nothing was created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import MutatingGoogleAdsClient
from .media import (
    LANDSCAPE,
    LOGO,
    SQUARE,
    SQUARE_LOGO,
    ImageFile,
    check,
    load_image,
)
from .plan import FieldChange, PlannedChange, micros
from .spec import (
    AdGroupSpec,
    CampaignSpec,
    ResponsiveDisplayAdSpec,
    ResponsiveSearchAdSpec,
    VideoResponsiveAdSpec,
)


class _TempIds:
    """Hands out the negative ids that link operations within one request."""

    def __init__(self) -> None:
        self._next = -1

    def take(self) -> int:
        value = self._next
        self._next -= 1
        return value


@dataclass
class BuiltCampaign:
    operations: list[Any]
    plan: PlannedChange
    images: list[ImageFile]


def build(
    client: MutatingGoogleAdsClient, spec: CampaignSpec, *, customer_id: str | None = None
) -> BuiltCampaign:
    """Validate ``spec``, load its media, and build the whole request."""
    spec.validate()
    target = client.resolve_customer_id(customer_id)
    client.check_daily_budget(spec.budget_amount)

    temp = _TempIds()
    operations: list[Any] = []
    changes: list[FieldChange] = []
    warnings: list[str] = []
    images: list[ImageFile] = []

    def path(service: str, helper: str, temp_id: int) -> str:
        return getattr(client.service(service), helper)(target, temp_id)

    # -- budget ------------------------------------------------------------
    budget_id = temp.take()
    budget_name = spec.budget_name or f"{spec.name} budget"
    budget_resource = path("CampaignBudgetService", "campaign_budget_path", budget_id)
    op = client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.create
    budget.resource_name = budget_resource
    budget.name = budget_name
    budget.amount_micros = micros(spec.budget_amount)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(op)
    changes.append(FieldChange("budget", None, f"{spec.budget_amount:,.2f}/day"))

    # -- campaign ----------------------------------------------------------
    campaign_id = temp.take()
    campaign_resource = path("CampaignService", "campaign_path", campaign_id)
    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.create
    campaign.resource_name = campaign_resource
    campaign.name = spec.name
    campaign.campaign_budget = budget_resource
    campaign.status = getattr(client.enums.CampaignStatusEnum, spec.status)
    campaign.advertising_channel_type = getattr(
        client.enums.AdvertisingChannelTypeEnum, spec.channel
    )
    # REQUIRED on every new campaign -- creation is refused without it, with
    # field_error=REQUIRED on contains_eu_political_advertising.
    campaign.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.CONTAINS_EU_POLITICAL_ADVERTISING
        if spec.contains_eu_political_advertising
        else client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    if spec.channel == "VIDEO":
        # Video campaigns cannot use manual CPC; CPM is the simplest strategy
        # that does not require conversion history the campaign has none of.
        # copy_from() on the CLIENT, not CopyFrom() on the message -- proto-plus
        # wrappers do not carry the raw protobuf methods.
        client.raw.copy_from(campaign.target_cpm, client.get_type("TargetCpm"))
    else:
        campaign.manual_cpc.enhanced_cpc_enabled = False
    if spec.start_date:
        campaign.start_date = spec.start_date.replace("-", "")
    if spec.end_date:
        campaign.end_date = spec.end_date.replace("-", "")
    operations.append(op)
    changes.extend(
        [
            FieldChange("name", None, spec.name),
            FieldChange("channel", None, spec.channel),
            FieldChange("status", None, spec.status),
            # Always shown: this is a legal declaration, and one made silently
            # on someone's behalf is not a declaration.
            FieldChange(
                "EU political advertising",
                None,
                "CONTAINS" if spec.contains_eu_political_advertising
                else "does not contain",
            ),
        ]
    )
    if spec.status == "ENABLED":
        warnings.append(
            "status is ENABLED -- this campaign can begin spending as soon as "
            "it is created, before anyone has reviewed its targeting"
        )

    # -- ad groups, assets, ads -------------------------------------------
    for group in spec.ad_groups:
        group_id = temp.take()
        group_resource = path("AdGroupService", "ad_group_path", group_id)
        op = client.get_type("MutateOperation")
        ad_group = op.ad_group_operation.create
        ad_group.resource_name = group_resource
        ad_group.name = group.name
        ad_group.campaign = campaign_resource
        ad_group.status = getattr(client.enums.AdGroupStatusEnum, group.status)
        if group.cpc_bid is not None:
            ad_group.cpc_bid_micros = micros(group.cpc_bid)
        operations.append(op)
        changes.append(FieldChange(f"ad group {group.name}", None, f"{len(group.ads)} ad(s)"))

        for keyword in group.keywords:
            op = client.get_type("MutateOperation")
            criterion = op.ad_group_criterion_operation.create
            criterion.ad_group = group_resource
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.keyword.text = keyword
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.PHRASE
            operations.append(op)
        if group.keywords:
            changes.append(
                FieldChange(f"{group.name} keywords", None, len(group.keywords))
            )

        for ad in group.ads:
            built = _build_ad(client, ad, temp, target, path)
            # Asset operations MUST precede the ad that references them.
            operations.extend(built["asset_operations"])
            images.extend(built["images"])
            op = client.get_type("MutateOperation")
            ad_group_ad = op.ad_group_ad_operation.create
            ad_group_ad.ad_group = group_resource
            ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
            client.raw.copy_from(ad_group_ad.ad, built["ad"])
            operations.append(op)

    if images:
        changes.append(FieldChange("images uploaded", None, len(images)))

    plan = PlannedChange(
        action="create", entity="campaign", label=spec.name,
        service="GoogleAdsService", method="mutate (atomic)",
        operations=operations, changes=changes, warnings=warnings,
    )
    return BuiltCampaign(operations=operations, plan=plan, images=images)


def execute_build(
    client: MutatingGoogleAdsClient,
    built: BuiltCampaign,
    *,
    apply: bool = False,
    customer_id: str | None = None,
):
    """Send a built campaign. The one seam callers outside write/ use.

    Exists so the CLI never calls a mutate method itself: the read-only scan
    then stays a flat "nothing outside write/ mutates", with no per-file
    exceptions to argue about later.
    """
    return client.mutate_atomic(
        built.operations,
        customer_id=customer_id,
        apply=apply,
        describe=built.plan.describe(),
    )


def _image_asset_operation(client, image: ImageFile, resource_name: str):
    op = client.get_type("MutateOperation")
    asset = op.asset_operation.create
    asset.resource_name = resource_name
    asset.name = f"{image.path.stem} {image.width}x{image.height}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = image.data
    return op


def _build_ad(client, ad, temp, target, path) -> dict[str, Any]:
    """Build one ad plus the asset operations it depends on."""
    asset_operations: list[Any] = []
    images: list[ImageFile] = []
    built = client.get_type("Ad")
    built.final_urls.append(ad.final_url)

    if isinstance(ad, ResponsiveSearchAdSpec):
        for text in ad.headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            built.responsive_search_ad.headlines.append(asset)
        for text in ad.descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            built.responsive_search_ad.descriptions.append(asset)
        if ad.path1:
            built.responsive_search_ad.path1 = ad.path1
        if ad.path2:
            built.responsive_search_ad.path2 = ad.path2

    elif isinstance(ad, ResponsiveDisplayAdSpec):
        info = built.responsive_display_ad
        info.business_name = ad.business_name
        info.long_headline.text = ad.long_headline
        for text in ad.headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            info.headlines.append(asset)
        for text in ad.descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            info.descriptions.append(asset)

        for paths, requirement, target_list in (
            (ad.marketing_images, LANDSCAPE, info.marketing_images),
            (ad.square_marketing_images, SQUARE, info.square_marketing_images),
            # logo_images is the 4:1 slot; square logos go in their own field.
            (ad.logo_images, LOGO, info.logo_images),
            (ad.square_logo_images, SQUARE_LOGO, info.square_logo_images),
        ):
            loaded = [load_image(p) for p in paths]
            check(loaded, requirement)
            images.extend(loaded)
            for image in loaded:
                asset_id = temp.take()
                resource = path("AssetService", "asset_path", asset_id)
                asset_operations.append(
                    _image_asset_operation(client, image, resource)
                )
                image_asset = client.get_type("AdImageAsset")
                image_asset.asset = resource
                target_list.append(image_asset)

    elif isinstance(ad, VideoResponsiveAdSpec):
        info = built.video_responsive_ad
        for video_id in ad.youtube_video_ids:
            asset_id = temp.take()
            resource = path("AssetService", "asset_path", asset_id)
            op = client.get_type("MutateOperation")
            asset = op.asset_operation.create
            asset.resource_name = resource
            asset.name = f"YouTube {video_id}"
            asset.type_ = client.enums.AssetTypeEnum.YOUTUBE_VIDEO
            asset.youtube_video_asset.youtube_video_id = video_id
            asset_operations.append(op)
            video_asset = client.get_type("AdVideoAsset")
            video_asset.asset = resource
            info.videos.append(video_asset)
        for text in ad.headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            info.headlines.append(asset)
        for text in ad.long_headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            info.long_headlines.append(asset)
        for text in ad.descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = text
            info.descriptions.append(asset)
        if ad.call_to_action:
            asset = client.get_type("AdTextAsset")
            asset.text = ad.call_to_action
            info.call_to_actions.append(asset)

    return {"ad": built, "asset_operations": asset_operations, "images": images}
