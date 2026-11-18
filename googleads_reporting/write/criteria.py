"""Keywords, negative keywords and shared negative lists.

Two rules shape everything here:

* **Nothing is removed, with one exception.** Keywords and negatives have no
  PAUSED state for a negative, and the only way to undo adding one is to
  remove it. :func:`plan_remove_created` exists for exactly that, and the MCP
  server reaches it only through ``revert_change`` on a ledger entry this
  server wrote -- the criterion's resource name comes from Google's own
  response to our create, not from an argument anyone typed. It refuses any
  resource that is not a criterion, link or label attachment, so a campaign,
  an ad group or an ad can never be removed through it.
* **Positive keywords are created PAUSED** unless the caller asks otherwise,
  for the same reason campaigns are: going live is a separate, deliberate act.
"""

from __future__ import annotations

from typing import Sequence

from .client import MutatingGoogleAdsClient, MutationError
from .plan import FieldChange, PlannedChange, micros, set_update_mask

AD_GROUP_CRITERION = ("AdGroupCriterionService", "mutate_ad_group_criteria")
CAMPAIGN_CRITERION = ("CampaignCriterionService", "mutate_campaign_criteria")
SHARED_CRITERION = ("SharedCriterionService", "mutate_shared_criteria")
CAMPAIGN_SHARED_SET = ("CampaignSharedSetService", "mutate_campaign_shared_sets")

MATCH_TYPES = ("EXACT", "PHRASE", "BROAD")

#: Google's own limits on keyword text, checked locally so the error names the
#: keyword rather than arriving as KEYWORD_TEXT_TOO_LONG on operation 7.
MAX_KEYWORD_CHARS = 80
MAX_KEYWORD_WORDS = 10

#: Resource collections whose members this module may remove -- criteria,
#: links and label attachments, all of which have no PAUSED state. Campaigns,
#: ad groups, ads, budgets and assets are absent on purpose.
_REMOVABLE = {
    "adGroupCriteria": ("AdGroupCriterionOperation", AD_GROUP_CRITERION),
    "campaignCriteria": ("CampaignCriterionOperation", CAMPAIGN_CRITERION),
    "sharedCriteria": ("SharedCriterionOperation", SHARED_CRITERION),
    "campaignSharedSets": ("CampaignSharedSetOperation", CAMPAIGN_SHARED_SET),
    "campaignAssets": ("CampaignAssetOperation", ("CampaignAssetService", "mutate_campaign_assets")),
    "adGroupAssets": ("AdGroupAssetOperation", ("AdGroupAssetService", "mutate_ad_group_assets")),
    "campaignLabels": ("CampaignLabelOperation", ("CampaignLabelService", "mutate_campaign_labels")),
    "adGroupLabels": ("AdGroupLabelOperation", ("AdGroupLabelService", "mutate_ad_group_labels")),
    "adGroupAdLabels": (
        "AdGroupAdLabelOperation", ("AdGroupAdLabelService", "mutate_ad_group_ad_labels")
    ),
    "adGroupCriterionLabels": (
        "AdGroupCriterionLabelOperation",
        ("AdGroupCriterionLabelService", "mutate_ad_group_criterion_labels"),
    ),
}


def customer(client: MutatingGoogleAdsClient) -> str:
    return client.resolve_customer_id()


def ad_group_path(client, ad_group_id: str) -> str:
    return f"customers/{customer(client)}/adGroups/{int(ad_group_id)}"


def campaign_path(client, campaign_id: str) -> str:
    return f"customers/{customer(client)}/campaigns/{int(campaign_id)}"


def shared_set_path(client, shared_set_id: str) -> str:
    return f"customers/{customer(client)}/sharedSets/{int(shared_set_id)}"


def ad_group_criterion_path(client, ad_group_id: str, criterion_id: str) -> str:
    return f"customers/{customer(client)}/adGroupCriteria/{int(ad_group_id)}~{int(criterion_id)}"


def validate_keywords(texts: Sequence[str], match_type: str) -> list[str]:
    """Trimmed keyword texts, or a MutationError naming every bad one."""
    if match_type not in MATCH_TYPES:
        raise MutationError(f"match_type must be one of {', '.join(MATCH_TYPES)}, got {match_type!r}.")
    cleaned, problems = [], []
    for index, raw in enumerate(texts, start=1):
        text = " ".join(str(raw).split())
        if not text:
            problems.append(f"keyword {index} is blank")
        elif len(text) > MAX_KEYWORD_CHARS:
            problems.append(f"keyword {index} is {len(text)} chars (max {MAX_KEYWORD_CHARS}): {text!r}")
        elif len(text.split()) > MAX_KEYWORD_WORDS:
            problems.append(f"keyword {index} has more than {MAX_KEYWORD_WORDS} words: {text!r}")
        cleaned.append(text)
    lowered = [t.lower() for t in cleaned]
    duplicates = sorted({t for t in lowered if lowered.count(t) > 1})
    if duplicates:
        problems.append(f"listed twice: {', '.join(duplicates)}")
    if not cleaned:
        problems.append("no keywords given")
    if problems:
        raise MutationError("Keywords are not valid:\n  - " + "\n  - ".join(problems))
    return cleaned


def plan_add_keywords(
    client: MutatingGoogleAdsClient,
    *,
    ad_group_id: str,
    texts: Sequence[str],
    match_type: str,
    status: str = "PAUSED",
    cpc_bid: float | None = None,
    negative: bool = False,
) -> PlannedChange:
    """Positive (or negative) keywords in one ad group, one operation each."""
    service, method = AD_GROUP_CRITERION
    operations = []
    for text in texts:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = ad_group_path(client, ad_group_id)
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match_type)
        if negative:
            criterion.negative = True
        else:
            criterion.status = getattr(client.enums.AdGroupCriterionStatusEnum, status)
            if cpc_bid is not None:
                criterion.cpc_bid_micros = micros(cpc_bid)
        operations.append(operation)
    return PlannedChange(
        action="create", entity="negative keyword" if negative else "keyword",
        label=f"{len(texts)} in ad group {ad_group_id}", service=service, method=method,
        operations=operations,
        changes=[FieldChange(f"[{match_type}] {t}", None, "negative" if negative else status)
                 for t in texts],
    )


def plan_add_campaign_negatives(
    client: MutatingGoogleAdsClient, *, campaign_id: str, texts: Sequence[str], match_type: str
) -> PlannedChange:
    service, method = CAMPAIGN_CRITERION
    operations = []
    for text in texts:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = campaign_path(client, campaign_id)
        criterion.negative = True
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match_type)
        operations.append(operation)
    return PlannedChange(
        action="create", entity="campaign negative keyword",
        label=f"{len(texts)} in campaign {campaign_id}", service=service, method=method,
        operations=operations,
        changes=[FieldChange(f"[{match_type}] {t}", None, "negative") for t in texts],
    )


def plan_set_keyword_status(
    client: MutatingGoogleAdsClient, *, ad_group_id: str, criterion_id: str, status: str
) -> PlannedChange:
    if status not in ("ENABLED", "PAUSED"):
        raise MutationError(f"{status!r} is not settable here; use ENABLED or PAUSED.")
    service, method = AD_GROUP_CRITERION
    operation = client.get_type("AdGroupCriterionOperation")
    operation.update.resource_name = ad_group_criterion_path(client, ad_group_id, criterion_id)
    operation.update.status = getattr(client.enums.AdGroupCriterionStatusEnum, status)
    set_update_mask(client, operation)
    return PlannedChange(
        action="update", entity="keyword", label=f"{ad_group_id}~{criterion_id}",
        service=service, method=method, operations=[operation],
        changes=[FieldChange("status", None, status)],
    )


def plan_set_keyword_bid(
    client: MutatingGoogleAdsClient, *, ad_group_id: str, criterion_id: str,
    amount: float | None,
) -> PlannedChange:
    """Set a keyword's own max CPC, or CLEAR it (``amount=None``).

    Clearing is how a keyword goes back to inheriting the ad group's bid --
    which is what reverting a first-ever keyword bid has to do. A cleared
    field is not "populated", so the mask cannot be derived from the payload
    and is written by hand.
    """
    service, method = AD_GROUP_CRITERION
    operation = client.get_type("AdGroupCriterionOperation")
    operation.update.resource_name = ad_group_criterion_path(client, ad_group_id, criterion_id)
    if amount is None:
        operation.update_mask.paths.append("cpc_bid_micros")
    else:
        operation.update.cpc_bid_micros = micros(amount)
        set_update_mask(client, operation)
    return PlannedChange(
        action="update", entity="keyword", label=f"{ad_group_id}~{criterion_id}",
        service=service, method=method, operations=[operation],
        changes=[FieldChange("cpc_bid", None,
                             "ad group default" if amount is None else round(amount, 2))],
    )


def plan_add_shared_negatives(
    client: MutatingGoogleAdsClient, *, shared_set_id: str, texts: Sequence[str], match_type: str
) -> PlannedChange:
    service, method = SHARED_CRITERION
    operations = []
    for text in texts:
        operation = client.get_type("SharedCriterionOperation")
        criterion = operation.create
        criterion.shared_set = shared_set_path(client, shared_set_id)
        criterion.keyword.text = text
        criterion.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match_type)
        operations.append(operation)
    return PlannedChange(
        action="create", entity="shared negative keyword",
        label=f"{len(texts)} in shared list {shared_set_id}", service=service, method=method,
        operations=operations,
        changes=[FieldChange(f"[{match_type}] {t}", None, "negative") for t in texts],
    )


def plan_link_shared_set(
    client: MutatingGoogleAdsClient, *, campaign_id: str, shared_set_id: str
) -> PlannedChange:
    service, method = CAMPAIGN_SHARED_SET
    operation = client.get_type("CampaignSharedSetOperation")
    operation.create.campaign = campaign_path(client, campaign_id)
    operation.create.shared_set = shared_set_path(client, shared_set_id)
    return PlannedChange(
        action="create", entity="campaign shared set",
        label=f"list {shared_set_id} -> campaign {campaign_id}", service=service, method=method,
        operations=[operation], changes=[FieldChange("attached", None, True)],
    )


def plan_remove_created(
    client: MutatingGoogleAdsClient, *, resource_names: Sequence[str]
) -> PlannedChange:
    """Remove criteria / links THIS SERVER CREATED, to revert adding them.

    The narrow exception to "nothing is removed": negatives, keywords, links
    and label attachments cannot be paused, so removal is their only undo. The
    caller must pass resource names taken from Google's response to our own
    create (the ledger records them); this refuses anything outside
    ``_REMOVABLE`` and any mix of collections in one request.
    """
    if not resource_names:
        raise MutationError("Nothing to remove.")
    collections = {name.split("/")[2] if name.count("/") >= 3 else "" for name in resource_names}
    if len(collections) != 1 or next(iter(collections)) not in _REMOVABLE:
        raise MutationError(
            f"Refusing to remove {sorted(resource_names)}: only criteria, shared-list "
            "links and label attachments this server created can be removed, one "
            "kind per request."
        )
    operation_type, (service, method) = _REMOVABLE[next(iter(collections))]
    operations = []
    for name in resource_names:
        operation = client.get_type(operation_type)
        operation.remove = name
        operations.append(operation)
    return PlannedChange(
        action="remove", entity=next(iter(collections)),
        label=f"{len(operations)} created by this server", service=service, method=method,
        operations=operations,
        changes=[FieldChange(name, "present", "(none)") for name in resource_names],
    )
