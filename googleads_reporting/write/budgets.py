"""Campaign budget mutations."""

from __future__ import annotations

from typing import Any

from ..query import Query, quote_literal
from .client import MutatingGoogleAdsClient
from .plan import FieldChange, PlannedChange, micros

SERVICE = "CampaignBudgetService"
METHOD = "mutate_campaign_budgets"


def _budget_row(client: MutatingGoogleAdsClient, budget_id: str, customer_id=None):
    rows = client.query(
        Query(
            select=("campaign_budget.id", "campaign_budget.name",
                    "campaign_budget.amount_micros",
                    "campaign_budget.explicitly_shared",
                    "campaign_budget.reference_count",
                    "campaign_budget.resource_name"),
            from_resource="campaign_budget",
            where=(f"campaign_budget.id = {int(budget_id)}",),
        ).to_gaql(),
        customer_id=customer_id,
    )
    if not rows:
        raise LookupError(f"No campaign budget with id {budget_id}.")
    return rows[0].campaign_budget


def plan_create(
    client: MutatingGoogleAdsClient,
    *,
    name: str,
    amount: float,
    shared: bool = False,
    override_guardrail: bool = False,
) -> PlannedChange:
    """A new daily budget of ``amount`` in account currency (not micros)."""
    client.check_daily_budget(amount, override=override_guardrail)

    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.create
    budget.name = name
    budget.amount_micros = micros(amount)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = shared

    return PlannedChange(
        action="create", entity="budget", label=name,
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[
            FieldChange("amount", None, round(amount, 2)),
            FieldChange("delivery_method", None, "STANDARD"),
            FieldChange("explicitly_shared", None, shared),
        ],
    )


def plan_set_amount(
    client: MutatingGoogleAdsClient,
    budget_id: str,
    *,
    amount: float,
    customer_id: str | None = None,
    override_guardrail: bool = False,
) -> PlannedChange:
    """Change a budget's daily amount."""
    client.check_daily_budget(amount, override=override_guardrail)
    current = _budget_row(client, budget_id, customer_id)
    before = current.amount_micros / 1_000_000

    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = current.resource_name
    budget.amount_micros = micros(amount)
    from .plan import set_update_mask

    set_update_mask(client, operation)

    warnings = []
    if current.explicitly_shared:
        warnings.append(
            "this budget is SHARED -- the change affects EVERY campaign using "
            "it, not just the one you named"
        )
    if before and amount > before * 10:
        warnings.append(
            f"that is {amount / before:,.0f}x the current amount; check it is "
            "not micros entered by mistake"
        )

    return PlannedChange(
        action="update", entity="budget", label=f"{current.name} (id {budget_id})",
        service=SERVICE, method=METHOD, operations=[operation],
        changes=[FieldChange("amount", round(before, 2), round(amount, 2))],
        warnings=warnings,
    )
