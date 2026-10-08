"""Accounts under the configured MCC.

``CustomerService.list_accessible_customers`` returns only the accounts the
OAuth user can reach *directly* -- usually just the MCC itself. To enumerate
what sits beneath it, query ``customer_client`` from the MCC, which is what this
report does.

Run it against the MCC (``--customer-id <MCC>``), not a leaf account: a leaf's
``customer_client`` contains only itself.
"""

from __future__ import annotations

from . import Report

ACCOUNTS = Report(
    name="accounts",
    description="Accounts under the MCC, with currency and time zone.",
    resource="customer_client",
    select=(
        "customer_client.id",
        "customer_client.descriptive_name",
        "customer_client.currency_code",
        "customer_client.time_zone",
        "customer_client.manager",
        "customer_client.test_account",
        "customer_client.status",
        "customer_client.level",
    ),
    order_by=("customer_client.level", "customer_client.descriptive_name"),
    # customer_client has no segments.date.
    supports_date_range=False,
)
