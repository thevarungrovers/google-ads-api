"""Sitelinks, callouts and structured snippets: create the asset and link it.

Each asset is two operations -- create the Asset, then link it to a campaign
or ad group -- and they go in ONE atomic GoogleAdsService.mutate, wired by temp
resource names (customers/X/assets/-1), so a failed link never leaves an
orphan asset behind and a failed asset never leaves half a batch linked.

The undo is removing the LINK (criteria.plan_remove_created accepts
campaignAssets / adGroupAssets). The asset itself stays in the account's
library, unlinked: assets are shared objects, and an unlinked one serves
nowhere.
"""

from __future__ import annotations

from typing import Any, Sequence

from .client import MutatingGoogleAdsClient, MutationError
from .criteria import ad_group_path, campaign_path, customer
from .plan import FieldChange, PlannedChange

#: Google's limits, checked locally so the error names the offending text.
SITELINK_TEXT, SITELINK_DESCRIPTION, CALLOUT_TEXT, SNIPPET_VALUE = 25, 35, 25, 25
SNIPPET_MIN_VALUES, SNIPPET_MAX_VALUES = 3, 10

#: Structured snippet headers Google accepts (English names, as the API takes them).
SNIPPET_HEADERS = (
    "Amenities", "Brands", "Courses", "Degree programs", "Destinations",
    "Featured hotels", "Insurance coverage", "Models", "Neighborhoods",
    "Service catalog", "Shows", "Styles", "Types",
)

KINDS = {"sitelink": "SITELINK", "callout": "CALLOUT", "structured_snippet": "STRUCTURED_SNIPPET"}


def validate(kind: str, items: Sequence[dict[str, Any]]) -> None:
    problems: list[str] = []
    if not items:
        problems.append("nothing to add")

    def length(label: str, text: str | None, limit: int, required: bool = True) -> None:
        if text is None or not str(text).strip():
            if required:
                problems.append(f"{label} is blank")
        elif len(text) > limit:
            problems.append(f"{label} is {len(text)} chars (max {limit}): {text!r}")

    for n, item in enumerate(items, start=1):
        if kind == "sitelink":
            length(f"sitelink {n} link_text", item.get("link_text"), SITELINK_TEXT)
            length(f"sitelink {n} description1", item.get("description1"), SITELINK_DESCRIPTION, False)
            length(f"sitelink {n} description2", item.get("description2"), SITELINK_DESCRIPTION, False)
            if bool(item.get("description1")) != bool(item.get("description2")):
                problems.append(f"sitelink {n}: give both descriptions or neither")
            if not str(item.get("final_url", "")).startswith(("http://", "https://")):
                problems.append(f"sitelink {n} final_url must be an absolute http(s) URL")
        elif kind == "callout":
            length(f"callout {n}", item.get("text"), CALLOUT_TEXT)
        elif kind == "structured_snippet":
            if item.get("header") not in SNIPPET_HEADERS:
                problems.append(f"snippet {n} header must be one of: {', '.join(SNIPPET_HEADERS)}")
            values = item.get("values") or []
            if not SNIPPET_MIN_VALUES <= len(values) <= SNIPPET_MAX_VALUES:
                problems.append(f"snippet {n} needs {SNIPPET_MIN_VALUES}-{SNIPPET_MAX_VALUES} values")
            for m, value in enumerate(values, start=1):
                length(f"snippet {n} value {m}", value, SNIPPET_VALUE)
        else:
            raise MutationError(f"unknown asset kind {kind!r}")
    if problems:
        raise MutationError("Assets are not valid:\n  - " + "\n  - ".join(problems))


def label(kind: str, item: dict[str, Any]) -> str:
    if kind == "sitelink":
        return f"sitelink {item['link_text']!r} -> {item['final_url']}"
    if kind == "callout":
        return f"callout {item['text']!r}"
    return f"snippet {item['header']}: {', '.join(item['values'])}"


def plan_add_linked_assets(
    client: MutatingGoogleAdsClient, *, kind: str, level: str, target_id: str,
    items: Sequence[dict[str, Any]],
) -> PlannedChange:
    """Asset + link per item, atomic. Operation order: asset, link, asset, link."""
    validate(kind, items)
    field_type = getattr(client.enums.AssetFieldTypeEnum, KINDS[kind])
    operations = []
    for index, item in enumerate(items, start=1):
        temp = f"customers/{customer(client)}/assets/-{index}"
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = temp
        if kind == "sitelink":
            asset.final_urls.append(item["final_url"])
            asset.sitelink_asset.link_text = item["link_text"]
            if item.get("description1"):
                asset.sitelink_asset.description1 = item["description1"]
                asset.sitelink_asset.description2 = item["description2"]
        elif kind == "callout":
            asset.callout_asset.callout_text = item["text"]
        else:
            asset.structured_snippet_asset.header = item["header"]
            asset.structured_snippet_asset.values.extend(item["values"])
        operations.append(op)

        link = client.get_type("MutateOperation")
        if level == "campaign":
            created = link.campaign_asset_operation.create
            created.campaign = campaign_path(client, target_id)
        elif level == "ad_group":
            created = link.ad_group_asset_operation.create
            created.ad_group = ad_group_path(client, target_id)
        else:
            raise MutationError("level must be campaign or ad_group")
        created.asset = temp
        created.field_type = field_type
        operations.append(link)
    return PlannedChange(
        action="create", entity=kind, label=f"{len(items)} on {level} {target_id}",
        service="GoogleAdsService", method="mutate (atomic)", operations=operations,
        changes=[FieldChange(label(kind, i), None, "linked") for i in items],
    )
