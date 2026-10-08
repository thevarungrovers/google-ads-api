#!/usr/bin/env python
"""Phase 2: change the account, with a confirmation step.

    ./.venv/bin/python scripts/manage.py campaign pause --campaign-id 123
    ./.venv/bin/python scripts/manage.py campaign enable --name "My campaign"
    ./.venv/bin/python scripts/manage.py budget set-amount --budget-id 456 --amount 25
    ./.venv/bin/python scripts/manage.py adgroup set-bid --ad-group-id 789 --amount 0.75
    ./.venv/bin/python scripts/manage.py ad pause --ad-id 321

Every command does the same three things, in this order:

1. Reads current state and builds the operations.
2. Sends them with ``validate_only=True``. Google checks the whole request
   server-side and changes nothing, so what you are shown is a verdict rather
   than a prediction. A request that would be rejected is rejected HERE,
   before you are asked to approve it.
3. Prints the diff and waits for you to type ``yes``.

``--dry-run`` stops after step 2. ``--yes`` skips the prompt, for scripts that
have already decided.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleads_reporting.config import ConfigError  # noqa: E402
from googleads_reporting.customer_id import (  # noqa: E402
    CustomerIdError,
    format_customer_id,
)
from googleads_reporting.write import (  # noqa: E402
    MutatingGoogleAdsClient,
    MutationError,
)
from googleads_reporting.write import adgroups, ads, budgets, campaigns  # noqa: E402
from googleads_reporting.write.plan import PlannedChange, execute  # noqa: E402

CONFIRM_WORD = "yes"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and update Google Ads entities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Shared flags, added to BOTH the top level and every leaf command.
    # argparse only accepts a global flag before the subcommand, and
    # `manage.py campaign pause --campaign-id X --dry-run` is how anyone would
    # actually type it -- having that fail with "unrecognized arguments" on the
    # one flag that means "do not change anything" is the worst possible place
    # for a usability wart.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--customer-id", help="account to act on")
    common.add_argument(
        "--dry-run",
        action="store_true",
        help="validate server-side and stop; change nothing",
    )
    common.add_argument(
        "--yes",
        action="store_true",
        help=f"skip the typed '{CONFIRM_WORD}' confirmation",
    )
    common.add_argument(
        "--override-budget-guardrail",
        action="store_true",
        help="allow a daily budget above the configured ceiling",
    )
    for action in common._actions:
        parser._add_action(action)

    entity = parser.add_subparsers(dest="entity", required=True)

    # -- campaign ---------------------------------------------------------
    campaign = entity.add_parser("campaign", parents=[common]).add_subparsers(
        dest="action", required=True
    )
    for verb in ("pause", "enable", "remove"):
        sub = campaign.add_parser(verb, parents=[common])
        sub.add_argument("--campaign-id")
        sub.add_argument("--name")
    rename = campaign.add_parser("rename", parents=[common])
    rename.add_argument("--campaign-id")
    rename.add_argument("--name")
    rename.add_argument("--new-name", required=True)
    create = campaign.add_parser("create", parents=[common])
    create.add_argument("--name", required=True)
    create.add_argument("--budget-resource-name", required=True)
    create.add_argument("--channel", default="SEARCH")
    create.add_argument("--status", default="PAUSED")
    create.add_argument("--start-date")
    create.add_argument("--end-date")

    # -- budget -----------------------------------------------------------
    budget = entity.add_parser("budget", parents=[common]).add_subparsers(dest="action", required=True)
    amount = budget.add_parser("set-amount", parents=[common])
    amount.add_argument("--budget-id", required=True)
    amount.add_argument("--amount", type=float, required=True)
    bcreate = budget.add_parser("create", parents=[common])
    bcreate.add_argument("--name", required=True)
    bcreate.add_argument("--amount", type=float, required=True)
    bcreate.add_argument("--shared", action="store_true")

    # -- ad group ---------------------------------------------------------
    adgroup = entity.add_parser("adgroup", parents=[common]).add_subparsers(dest="action", required=True)
    for verb in ("pause", "enable", "remove"):
        sub = adgroup.add_parser(verb, parents=[common])
        sub.add_argument("--ad-group-id")
        sub.add_argument("--name")
    bid = adgroup.add_parser("set-bid", parents=[common])
    bid.add_argument("--ad-group-id")
    bid.add_argument("--name")
    bid.add_argument("--amount", type=float, required=True)
    gcreate = adgroup.add_parser("create", parents=[common])
    gcreate.add_argument("--name", required=True)
    gcreate.add_argument("--campaign-resource-name", required=True)
    gcreate.add_argument("--cpc-bid", type=float)
    gcreate.add_argument("--status", default="PAUSED")

    # -- ad ---------------------------------------------------------------
    ad = entity.add_parser("ad", parents=[common]).add_subparsers(dest="action", required=True)
    for verb in ("pause", "enable", "remove"):
        sub = ad.add_parser(verb, parents=[common])
        sub.add_argument("--ad-id", required=True)
    acreate = ad.add_parser("create-rsa", parents=[common])
    acreate.add_argument("--ad-group-resource-name", required=True)
    acreate.add_argument("--headline", action="append", required=True, default=[])
    acreate.add_argument("--description", action="append", required=True, default=[])
    acreate.add_argument("--final-url", required=True)
    acreate.add_argument("--path1")
    acreate.add_argument("--path2")
    acreate.add_argument("--status", default="PAUSED")

    return parser


_STATUS_FOR = {"pause": "PAUSED", "enable": "ENABLED", "remove": "REMOVED"}


def build_plan(client, args) -> PlannedChange:
    entity, action = args.entity, args.action
    override = args.override_budget_guardrail

    if entity == "campaign":
        if action in _STATUS_FOR:
            return campaigns.plan_set_status(
                client, status=_STATUS_FOR[action],
                campaign_id=args.campaign_id, name=args.name,
            )
        if action == "rename":
            return campaigns.plan_rename(
                client, new_name=args.new_name,
                campaign_id=args.campaign_id, name=args.name,
            )
        if action == "create":
            return campaigns.plan_create(
                client, name=args.name,
                budget_resource_name=args.budget_resource_name,
                channel=args.channel, status=args.status,
                start_date=args.start_date, end_date=args.end_date,
            )

    if entity == "budget":
        if action == "set-amount":
            return budgets.plan_set_amount(
                client, args.budget_id, amount=args.amount,
                override_guardrail=override,
            )
        if action == "create":
            return budgets.plan_create(
                client, name=args.name, amount=args.amount, shared=args.shared,
                override_guardrail=override,
            )

    if entity == "adgroup":
        if action in _STATUS_FOR:
            return adgroups.plan_set_status(
                client, status=_STATUS_FOR[action],
                ad_group_id=args.ad_group_id, name=args.name,
            )
        if action == "set-bid":
            return adgroups.plan_set_cpc_bid(
                client, amount=args.amount,
                ad_group_id=args.ad_group_id, name=args.name,
            )
        if action == "create":
            return adgroups.plan_create(
                client, name=args.name,
                campaign_resource_name=args.campaign_resource_name,
                cpc_bid=args.cpc_bid, status=args.status,
            )

    if entity == "ad":
        if action in _STATUS_FOR:
            return ads.plan_set_status(
                client, ad_id=args.ad_id, status=_STATUS_FOR[action]
            )
        if action == "create-rsa":
            return ads.plan_create_responsive_search_ad(
                client,
                ad_group_resource_name=args.ad_group_resource_name,
                headlines=args.headline, descriptions=args.description,
                final_url=args.final_url, path1=args.path1, path2=args.path2,
                status=args.status,
            )

    raise MutationError(f"Unsupported: {entity} {action}")


def confirm(plan: PlannedChange, target: str) -> bool:
    print("\n" + "=" * 68)
    print(plan.render())
    print("=" * 68)
    print(f"Account: {format_customer_id(target)}")
    print(
        f"\nThis WILL change the account. Type '{CONFIRM_WORD}' to apply, "
        "anything else to abort."
    )
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        # No terminal, or the user interrupted: treat as a refusal. Defaulting
        # to "apply" when nobody can answer is how an unattended run mutates an
        # account nobody meant to touch.
        print("\nAborted (no confirmation received).")
        return False
    return answer == CONFIRM_WORD


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        client = MutatingGoogleAdsClient.from_env()
        target = client.resolve_customer_id(args.customer_id)
    except (ConfigError, CustomerIdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        plan = build_plan(client, args)
    except (MutationError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Step 2: server-side validation. A request that would be rejected is
    # rejected here, before anyone is asked to approve it.
    print("\nValidating with Google (nothing is changed yet)...")
    try:
        execute(client, plan, apply=False, customer_id=target)
    except MutationError as exc:
        print(f"\nvalidation FAILED -- nothing was changed:\n{exc}", file=sys.stderr)
        return 1
    print("  server validation: OK")

    if args.dry_run:
        print("\n" + plan.render())
        print("\n--dry-run: stopping here. Nothing was changed.")
        return 0

    if args.yes:
        print("\n" + plan.render())
        print("\n--yes: applying without confirmation.")
    elif not confirm(plan, target):
        print("Aborted. Nothing was changed.")
        return 1

    try:
        result = execute(client, plan, apply=True, customer_id=target)
    except MutationError as exc:
        print(f"\nAPPLY FAILED:\n{exc}", file=sys.stderr)
        return 1

    print(f"\n{result}")
    for name in result.resource_names:
        print(f"  {name}")
    if result.partial_failure_error:
        print(f"  partial failure: {result.partial_failure_error}")
    print(f"  audit: {client.settings.output_dir.parent / 'audit'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
