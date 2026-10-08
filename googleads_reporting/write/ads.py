"""Ad mutations.

Creation covers Responsive Search Ads, which is the format Google Search
campaigns actually run now -- expanded text ads can no longer be created.
"""

from __future__ import annotations

from typing import Sequence

from ..query import Query
from .client import MutatingGoogleAdsClient, MutationError
from .plan import FieldChange, PlannedChange, set_update_mask

SERVICE = "AdGroupAdService"
METHOD = "mutate_ad_group_ads"

SETTABLE_STATUSES = ("ENABLED", "PAUSED", "REMOVED")

#: Google's own limits. Checked locally so the error names the offending text
#: rather than arriving as a generic server-side STRING_TOO_LONG.
MAX_HEADLINE = 30
MAX_DESCRIPTION = 90
MIN_HEADLINES = 3
MAX_HEADLINES = 15
MIN_DESCRIPTIONS = 2
MAX_DESCRIPTIONS = 4

_FIELDS = (
    "ad_group_ad.ad.id", "ad_group_ad.status", "ad_group_ad.resource_name",
    "ad_group.id", "ad_group.name", "campaign.name",
)


def find(
    client: MutatingGoogleAdsClient, *, ad_id: str, customer_id: str | None = None
):
    rows = client.query(
        Query(
            select=_FIELDS,
            from_resource="ad_group_ad",
            where=(f"ad_group_ad.ad.id = {int(ad_id)}",),
        ).to_gaql(),
        customer_id=customer_id,
    )
    if not rows:
        raise LookupError(f"No ad with id {ad_id}.")
    return rows[0]


def plan_set_status(
    client: MutatingGoogleAdsClient,
    *,
    ad_id: str,
    status: str,
    customer_id: str | None = None,
) -> PlannedChange:
    wanted = status.upper()
    if wanted not in SETTABLE_STATUSES:
        raise MutationError(
            f"{status!r} is not a settable ad status. "
            f"Choose one of: {', '.join(SETTABLE_STATUSES)}."
        )
    row = find(client, ad_id=ad_id, customer_id=customer_id)
    before = row.ad_group_ad.status.name
    if before == wanted:
        raise MutationError(f"Ad {ad_id} is already {wanted}; nothing to do.")

    operation = client.get_type("AdGroupAdOperation")
    ad = operation.update
    ad.resource_name = row.ad_group_ad.resource_name
    ad.status = getattr(client.enums.AdGroupAdStatusEnum, wanted)
    set_update_mask(client, operation)

    return PlannedChange(
        action="update", entity="ad",
        label=f"id {ad_id} (in {row.ad_group.name} / {row.campaign.name})",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("status", before, wanted)],
    )


def validate_rsa_text(
    headlines: Sequence[str], descriptions: Sequence[str], final_url: str
) -> None:
    """Check Google's RSA limits locally, naming what is wrong.

    The API enforces these too, but reports them as a field path and an enum.
    Catching them here means the message says which headline is too long and by
    how much.
    """
    problems = []
    if not (MIN_HEADLINES <= len(headlines) <= MAX_HEADLINES):
        problems.append(
            f"need {MIN_HEADLINES}-{MAX_HEADLINES} headlines, got {len(headlines)}"
        )
    if not (MIN_DESCRIPTIONS <= len(descriptions) <= MAX_DESCRIPTIONS):
        problems.append(
            f"need {MIN_DESCRIPTIONS}-{MAX_DESCRIPTIONS} descriptions, "
            f"got {len(descriptions)}"
        )
    for index, text in enumerate(headlines, start=1):
        if len(text) > MAX_HEADLINE:
            problems.append(
                f"headline {index} is {len(text)} chars "
                f"(max {MAX_HEADLINE}): {text!r}"
            )
        if not text.strip():
            problems.append(f"headline {index} is blank")
    for index, text in enumerate(descriptions, start=1):
        if len(text) > MAX_DESCRIPTION:
            problems.append(
                f"description {index} is {len(text)} chars "
                f"(max {MAX_DESCRIPTION}): {text!r}"
            )
        if not text.strip():
            problems.append(f"description {index} is blank")
    if not final_url.startswith(("http://", "https://")):
        problems.append(f"final_url must be an absolute http(s) URL, got {final_url!r}")
    if problems:
        raise MutationError("Ad text is not valid:\n  - " + "\n  - ".join(problems))


def plan_create_responsive_search_ad(
    client: MutatingGoogleAdsClient,
    *,
    ad_group_resource_name: str,
    headlines: Sequence[str],
    descriptions: Sequence[str],
    final_url: str,
    path1: str | None = None,
    path2: str | None = None,
    status: str = "PAUSED",
) -> PlannedChange:
    """A new Responsive Search Ad. Paused by default."""
    validate_rsa_text(headlines, descriptions, final_url)

    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = getattr(client.enums.AdGroupAdStatusEnum, status.upper())
    ad_group_ad.ad.final_urls.append(final_url)

    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad_group_ad.ad.responsive_search_ad.headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        ad_group_ad.ad.responsive_search_ad.descriptions.append(asset)
    if path1:
        ad_group_ad.ad.responsive_search_ad.path1 = path1
    if path2:
        ad_group_ad.ad.responsive_search_ad.path2 = path2

    changes = [
        FieldChange("ad_group", None, ad_group_resource_name),
        FieldChange("status", None, status.upper()),
        FieldChange("final_url", None, final_url),
        FieldChange("headlines", None, list(headlines)),
        FieldChange("descriptions", None, list(descriptions)),
    ]
    return PlannedChange(
        action="create", entity="responsive search ad",
        label=headlines[0], service=SERVICE, method=METHOD,
        operations=[operation], changes=changes,
    )
