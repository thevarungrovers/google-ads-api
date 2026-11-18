"""The one general tool: low-risk edits on a closed allowlist.

Everything that spends money or changes targeting has its own typed pair with
its own bounds. What is left -- labels, URL suffixes and tracking templates,
dismissing a recommendation -- cannot move money or change who sees an ad, and
giving each a typed pair would add a dozen tools for no extra safety. So they
share preview_low_risk_change(resource, id, field, value), and the safety
lives in ALLOWLIST below:

  * it is a closed set, written out pair by pair -- there is no pattern, no
    "any string field", and anything not listed is refused before a read;
  * tests/test_low_risk.py pins it exactly, and checks every field against
    fields.py's money rulings and a forbidden-name list (bid, budget, cpc,
    cpa, roas, status, targeting, location, ...), so adding a money field here
    fails the build rather than a review.

Still a full preview/apply pair: the human sees the before/after and approves
the apply like any other. Marked non-destructive, because none of these can
stop delivery or spend.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from googleads_mcp.changes.base import APPLY_REVERSIBLE, NOTHING, ChangeKind, LedgerItem, Proposal
from googleads_mcp.changes.common import one
from googleads_mcp.guardrails import Check
from googleads_reporting.write import lowrisk

#: (resource, field) -> what it does. THE allowlist. Edit with care: see the
#: module docstring and tests/test_low_risk.py.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("label", "create"): "create a label named <value>",
    ("campaign", "attach_label"): "attach label <value> to the campaign",
    ("campaign", "detach_label"): "detach label <value> from the campaign",
    ("ad_group", "attach_label"): "attach label <value> to the ad group",
    ("ad_group", "detach_label"): "detach label <value> from the ad group",
    ("ad", "attach_label"): "attach label <value> to the ad (id is ad_group_id~ad_id)",
    ("ad", "detach_label"): "detach label <value> from the ad",
    ("keyword", "attach_label"): "attach label <value> to the keyword (id is ad_group_id~criterion_id)",
    ("keyword", "detach_label"): "detach label <value> from the keyword",
    ("campaign", "final_url_suffix"): 'set the final URL suffix ("" clears it)',
    ("campaign", "tracking_url_template"): 'set the tracking template ("" clears it)',
    ("ad_group", "final_url_suffix"): 'set the final URL suffix ("" clears it)',
    ("ad_group", "tracking_url_template"): 'set the tracking template ("" clears it)',
    ("recommendation", "dismiss"): "dismiss the recommendation (value ignored)",
}

EMPTY = "(empty)"

_LINK_RESOURCE = {
    "campaign": ("campaign_label", "campaign.id"),
    "ad_group": ("ad_group_label", "ad_group.id"),
    "ad": ("ad_group_ad_label", "ad_group_ad.ad.id"),
    "keyword": ("ad_group_criterion_label", "ad_group_criterion.criterion_id"),
}


def _entity_id(resource: str, raw: str) -> str:
    """Normalise and check an id: "11", or "22~5" for an ad / keyword."""
    parts = str(raw).split("~")
    expected = 2 if resource in ("ad", "keyword") else 1
    if len(parts) != expected or not all(p.strip().isdigit() for p in parts):
        shape = "ad_group_id~ad_id" if resource == "ad" else (
            "ad_group_id~criterion_id" if resource == "keyword" else "a numeric id")
        raise ValueError(f"id for a {resource} must be {shape}, got {raw!r}.")
    return "~".join(str(int(p)) for p in parts)


def _attached(ctx, resource: str, entity_id: str, label_id: str) -> str:
    link_resource, id_field = _LINK_RESOURCE[resource]
    where = [f"label.id = {int(label_id)}"]
    ids = entity_id.split("~")
    if resource in ("ad", "keyword"):
        where += [f"ad_group.id = {int(ids[0])}", f"{id_field} = {int(ids[1])}"]
    else:
        where.append(f"{id_field} = {int(ids[0])}")
    found = one(ctx, (f"{link_resource}.resource_name",), link_resource, where)
    return "attached" if found is not None else NOTHING


def _url_value(ctx, resource: str, entity_id: str, field: str) -> str:
    found = one(ctx, (f"{resource}.{field}",), resource, [f"{resource}.id = {int(entity_id)}"])
    if found is None:
        raise LookupError(f"No {resource} with id {entity_id}.")
    return getattr(getattr(found, resource), field) or EMPTY


def _check_url(field: str, value: str) -> None:
    if value == "":
        return
    if any(c.isspace() for c in value):
        raise ValueError(f"{field} cannot contain whitespace.")
    if field == "final_url_suffix" and value.startswith(("?", "&")):
        raise ValueError("final_url_suffix is the part AFTER '?': drop the leading ? or &.")
    if field == "tracking_url_template" and not (
        value.startswith(("http://", "https://", "{lpurl}"))
    ):
        raise ValueError("tracking_url_template must start with {lpurl} or http(s)://.")


class LowRiskChange(ChangeKind):
    name = "low_risk_change"
    preview_title = "Preview a low-risk edit (labels, URL suffix, tracking template, dismiss)"
    apply_title = "Apply a low-risk edit"
    apply_annotations = APPLY_REVERSIBLE
    ledger_keys = tuple(ALLOWLIST)

    def propose(
        self,
        ctx,
        resource: Annotated[
            Literal["label", "campaign", "ad_group", "ad", "keyword", "recommendation"],
            Field(description="What to edit"),
        ],
        id: Annotated[
            str,
            Field(description='campaign/ad_group id; "ad_group_id~ad_id" for an ad; '
                  '"ad_group_id~criterion_id" for a keyword; recommendation_id; '
                  'ignored for label create'),
        ],
        field: Annotated[
            str,
            Field(description="One of: " + "; ".join(f"{r}.{f}" for r, f in ALLOWLIST)),
        ],
        value: Annotated[str, Field(description="Label id/name, URL text, or empty")] = "",
    ) -> Proposal:
        """Show what a low-risk edit would do. Writes nothing.

        Only the resource/field pairs in the allowlist are accepted: labels
        (create, attach, detach), final URL suffix and tracking template on a
        campaign or ad group, and dismissing a recommendation. Nothing here can
        touch money, bids, budgets, status or targeting -- use the typed
        preview_* tools for those.
        """
        key = (resource, field)
        if key not in ALLOWLIST:
            raise ValueError(
                f"{resource}.{field} is not a low-risk edit. Allowed: "
                + ", ".join(f"{r}.{f}" for r, f in ALLOWLIST)
                + ". Money, bids, budgets, status and targeting have their own preview_* tools."
            )
        payload: dict[str, Any] = {"resource": resource, "field": field, "value": value}
        checks: list[Check] = []
        campaign_id = None
        if resource == "label":
            if not value.strip():
                raise ValueError("value must be the new label's name.")
            before, after, entity_id, name = NOTHING, value, f"new:{value}", f"label {value!r}"
        elif resource == "recommendation":
            entity_id = str(id).strip()
            before, after, name = "active", "dismissed", f"recommendation {entity_id}"
            payload["recommendation_id"] = entity_id
        elif field in ("attach_label", "detach_label"):
            entity_id = _entity_id(resource, id)
            if not str(value).strip().isdigit():
                raise ValueError("value must be the label_id (see list_labels).")
            payload.update(entity_id=entity_id, label_id=str(int(value)))
            before = _attached(ctx, resource, entity_id, value)
            after = "attached" if field == "attach_label" else NOTHING
            checks.append(Check("is_a_change", before != after,
                                "differs from today" if before != after else f"already {before}"))
            name = f"{resource} {entity_id}, label {value}"
            if resource == "campaign":
                campaign_id = entity_id
        else:
            entity_id = _entity_id(resource, id)
            _check_url(field, value)
            payload["entity_id"] = entity_id
            before = _url_value(ctx, resource, entity_id, field)
            after = value or EMPTY
            checks.append(Check("is_a_change", before != after,
                                "differs from today" if before != after else "identical to today"))
            name = f"{resource} {entity_id}"
            if resource == "campaign":
                campaign_id = entity_id
        return Proposal(
            entity_type=resource, entity_id=entity_id, entity_name=name, path=name,
            field_name=field, before=before, after=after, checks=checks,
            projected=Decimal("0"), campaign_id=campaign_id, payload=payload,
            items=[LedgerItem(resource, entity_id, name, field, before, after,
                              {"label_id": payload.get("label_id")})],
            note=f"Low-risk edit: {ALLOWLIST[key]}. Cannot move money or change targeting.",
        )

    def validates(self, payload):
        return payload["resource"] != "recommendation"

    def _now(self, ctx, resource, field, entity_id, label_id=None) -> str:
        if resource == "label":
            return NOTHING
        if resource == "recommendation":
            return "active"
        if field in ("attach_label", "detach_label"):
            return _attached(ctx, resource, entity_id, label_id)
        return _url_value(ctx, resource, entity_id, field)

    def current(self, ctx, payload):
        return self._now(ctx, payload["resource"], payload["field"],
                         payload.get("entity_id"), payload.get("label_id"))

    def _plan(self, ctx, resource, field, entity_id, value, label_id=None, recommendation_id=None):
        if resource == "label":
            return lowrisk.plan_create_label(ctx.client, name=value)
        if resource == "recommendation":
            return lowrisk.plan_dismiss_recommendation(ctx.client, recommendation_id=recommendation_id)
        if field == "attach_label":
            return lowrisk.plan_attach_label(ctx.client, resource=resource, entity_id=entity_id,
                                             label_id=label_id)
        if field == "detach_label":
            return lowrisk.plan_detach_label(ctx.client, resource=resource, entity_id=entity_id,
                                             label_id=label_id)
        return lowrisk.plan_set_url_field(ctx.client, resource=resource, entity_id=entity_id,
                                          field=field, value=value)

    def build(self, ctx, payload):
        return self._plan(ctx, payload["resource"], payload["field"], payload.get("entity_id"),
                          payload["value"], payload.get("label_id"),
                          payload.get("recommendation_id"))

    # -- revert: the opposite edit, from the ledger row ---------------------

    def current_for_entry(self, ctx, entry):
        resource, field = entry["entity_type"], entry["field"]
        if resource in ("label", "recommendation"):
            return None
        label_id = (entry.get("detail") or {}).get("label_id")
        return self._now(ctx, resource, field, entry["entity_id"], label_id)

    def inverse(self, ctx, entry):
        resource, field = entry["entity_type"], entry["field"]
        if resource == "label":
            raise ValueError("A created label is not removed here; an unused label is harmless.")
        if resource == "recommendation":
            raise ValueError("Google offers no way to un-dismiss a recommendation.")
        if field in ("attach_label", "detach_label"):
            label_id = (entry.get("detail") or {}).get("label_id")
            opposite = "detach_label" if field == "attach_label" else "attach_label"
            return self._plan(ctx, resource, opposite, entry["entity_id"], "", label_id)
        before = "" if entry["before"] == EMPTY else entry["before"]
        return self._plan(ctx, resource, field, entry["entity_id"], before)

