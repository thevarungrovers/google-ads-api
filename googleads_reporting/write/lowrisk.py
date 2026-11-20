"""The low-risk edits: labels, URL suffixes and tracking templates, dismissing
a recommendation. None of them can move money or change who sees an ad.

They sit behind ONE general MCP tool (preview_low_risk_change) rather than a
typed pair each, which is only safe because the set is closed: the server's
allowlist (googleads_mcp/changes/low_risk.py) names every resource/field pair,
and a test checks none of them is money, a bid, a budget, a status or
targeting.
"""

from __future__ import annotations

from .client import MutatingGoogleAdsClient, MutationError
from .criteria import customer
from .plan import FieldChange, PlannedChange

LABEL_LINKS = {
    # resource: (operation type, (service, method), field on the link, path)
    "campaign": ("CampaignLabelOperation", ("CampaignLabelService", "mutate_campaign_labels"),
                 "campaign", "campaigns", "campaignLabels"),
    "ad_group": ("AdGroupLabelOperation", ("AdGroupLabelService", "mutate_ad_group_labels"),
                 "ad_group", "adGroups", "adGroupLabels"),
    "ad": ("AdGroupAdLabelOperation", ("AdGroupAdLabelService", "mutate_ad_group_ad_labels"),
           "ad_group_ad", "adGroupAds", "adGroupAdLabels"),
    "keyword": ("AdGroupCriterionLabelOperation",
                ("AdGroupCriterionLabelService", "mutate_ad_group_criterion_labels"),
                "ad_group_criterion", "adGroupCriteria", "adGroupCriterionLabels"),
}

URL_FIELDS = ("final_url_suffix", "tracking_url_template")
URL_ENTITIES = {
    "campaign": ("CampaignOperation", ("CampaignService", "mutate_campaigns"), "campaigns"),
    "ad_group": ("AdGroupOperation", ("AdGroupService", "mutate_ad_groups"), "adGroups"),
}


def entity_path(client, resource: str, entity_id: str) -> str:
    """campaign 11 -> customers/C/campaigns/11; ad "22~5" -> .../adGroupAds/22~5."""
    collection = LABEL_LINKS[resource][3]
    return f"customers/{customer(client)}/{collection}/{entity_id}"


def label_link_path(client, resource: str, entity_id: str, label_id: str) -> str:
    return f"customers/{customer(client)}/{LABEL_LINKS[resource][4]}/{entity_id}~{int(label_id)}"


def plan_create_label(client: MutatingGoogleAdsClient, *, name: str) -> PlannedChange:
    if not name.strip() or len(name) > 80:
        raise MutationError("A label name must be 1-80 characters.")
    operation = client.get_type("LabelOperation")
    operation.create.name = name
    return PlannedChange(
        action="create", entity="label", label=name, service="LabelService",
        method="mutate_labels", operations=[operation],
        changes=[FieldChange("label", None, name)],
    )


def plan_attach_label(
    client: MutatingGoogleAdsClient, *, resource: str, entity_id: str, label_id: str
) -> PlannedChange:
    operation_type, (service, method), field, _, _ = LABEL_LINKS[resource]
    operation = client.get_type(operation_type)
    setattr(operation.create, field, entity_path(client, resource, entity_id))
    operation.create.label = f"customers/{customer(client)}/labels/{int(label_id)}"
    return PlannedChange(
        action="create", entity=f"{resource} label", label=f"{resource} {entity_id}",
        service=service, method=method, operations=[operation],
        changes=[FieldChange("label", None, label_id)],
    )


def plan_detach_label(
    client: MutatingGoogleAdsClient, *, resource: str, entity_id: str, label_id: str
) -> PlannedChange:
    """Remove a label ATTACHMENT -- the link, never the label or the entity."""
    operation_type, (service, method), _, _, _ = LABEL_LINKS[resource]
    operation = client.get_type(operation_type)
    operation.remove = label_link_path(client, resource, entity_id, label_id)
    return PlannedChange(
        action="remove", entity=f"{resource} label", label=f"{resource} {entity_id}",
        service=service, method=method, operations=[operation],
        changes=[FieldChange("label", label_id, None)],
    )


def plan_set_url_field(
    client: MutatingGoogleAdsClient, *, resource: str, entity_id: str, field: str, value: str
) -> PlannedChange:
    """Set (or, with "", clear) a campaign / ad group URL field."""
    if field not in URL_FIELDS:
        raise MutationError(f"{field!r} is not a URL field this can set.")
    operation_type, (service, method), collection = URL_ENTITIES[resource]
    operation = client.get_type(operation_type)
    operation.update.resource_name = f"customers/{customer(client)}/{collection}/{int(entity_id)}"
    setattr(operation.update, field, value)
    # By hand: "" (clearing it) is the proto default and would drop out of a
    # derived mask, sending no change at all.
    operation.update_mask.paths.append(field)
    return PlannedChange(
        action="update", entity=resource, label=f"id {entity_id}", service=service,
        method=method, operations=[operation], changes=[FieldChange(field, None, value)],
    )


def plan_dismiss_recommendation(
    client: MutatingGoogleAdsClient, *, recommendation_id: str
) -> PlannedChange:
    """Dismiss one recommendation. Google offers no validate_only for this."""
    request = client.get_type("DismissRecommendationRequest")
    operation = type(request).DismissRecommendationOperation()
    operation.resource_name = f"customers/{customer(client)}/recommendations/{recommendation_id}"
    return PlannedChange(
        action="dismiss", entity="recommendation", label=recommendation_id,
        service="RecommendationService", method="dismiss_recommendation",
        operations=[operation], changes=[FieldChange("dismissed", False, True)],
    )
