#!/usr/bin/env python
"""Diagnostic ladder for the Google Ads connection.

    ./.venv/bin/python scripts/test_connection.py

Each rung tests one thing and reports PASS, WARN or FAIL. The order matters:
every rung assumes the ones above it passed, so the FIRST failure names the
real problem. A credential error and a wrong-account error look identical from
a single failing report, which is the thing this script exists to separate:

  * ``list_accessible_customers`` needs no customer ID, so it proves the
    CREDENTIALS on their own.
  * a query against the MCC proves GOOGLE_ADS_LOGIN_CUSTOMER_ID.
  * a query against the target proves GOOGLE_ADS_CUSTOMER_ID and that the MCC
    actually manages it.

Nothing here prints a credential value. Secrets are reported as presence and
length only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleads_reporting.client import (  # noqa: E402
    GoogleAdsReportingError,
    ReadOnlyGoogleAdsClient,
    ReadOnlyViolation,
)
from googleads_reporting.config import ENV_PATH, ConfigError, Settings  # noqa: E402
from googleads_reporting.customer_id import format_customer_id  # noqa: E402
from googleads_reporting.query import Query  # noqa: E402
from googleads_reporting.reports import get_report  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class Ladder:
    """Collects rung results and prints them as it goes."""

    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def record(self, status: str, rung: str, detail: str = "") -> str:
        self.results.append((status, rung, detail))
        line = f"  [{status}] {rung}"
        print(line if not detail else f"{line}\n         {detail}")
        return status

    @property
    def failed(self) -> bool:
        return any(status == FAIL for status, _, _ in self.results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--customer-id",
        help="account to test against (default: GOOGLE_ADS_CUSTOMER_ID)",
    )
    args = parser.parse_args(argv)

    ladder = Ladder()
    print("Google Ads API connection check\n")

    # -- 1. the .env file -------------------------------------------------
    if ENV_PATH.exists():
        mode = oct(ENV_PATH.stat().st_mode & 0o777)
        detail = f"{ENV_PATH} (mode {mode})"
        if ENV_PATH.stat().st_mode & 0o077:
            ladder.record(
                WARN, "1. .env present", f"{detail} is group/world readable; chmod 600 it"
            )
        else:
            ladder.record(PASS, "1. .env present", detail)
    else:
        ladder.record(
            FAIL, "1. .env present", f"{ENV_PATH} not found; cp .env.example .env"
        )
        return _finish(ladder)

    # -- 2. configuration -------------------------------------------------
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        ladder.record(FAIL, "2. configuration complete", str(exc))
        return _finish(ladder)

    ladder.record(PASS, "2. configuration complete")
    for key, value in settings.describe().items():
        print(f"         {key:20s} {value}")

    # -- 3. client construction -------------------------------------------
    try:
        client = ReadOnlyGoogleAdsClient.from_env(settings=settings)
    except Exception as exc:  # noqa: BLE001
        ladder.record(FAIL, "3. client constructed", f"{type(exc).__name__}: {exc}")
        return _finish(ladder)
    ladder.record(PASS, "3. client constructed", f"API {settings.api_version}")

    # -- 4. the read-only guard -------------------------------------------
    # Checked before any network call: if the guard is broken, stop before
    # touching a live account.
    try:
        client._service("CampaignService")
        ladder.record(FAIL, "4. read-only guard", "CampaignService was NOT refused")
        return _finish(ladder)
    except ReadOnlyViolation:
        pass
    try:
        client._service("GoogleAdsService").mutate
        ladder.record(FAIL, "4. read-only guard", "GoogleAdsService.mutate was NOT refused")
        return _finish(ladder)
    except ReadOnlyViolation:
        ladder.record(PASS, "4. read-only guard", "write surfaces refused")

    # -- 5. credentials ----------------------------------------------------
    # No customer ID involved, so a failure here is the credentials themselves.
    try:
        accessible = client.list_accessible_customers()
    except GoogleAdsReportingError as exc:
        ladder.record(FAIL, "5. credentials valid", _first_lines(exc))
        print(
            "\n  The developer token, OAuth client or refresh token is the "
            "problem -- no customer ID was sent, so the account settings are "
            "not implicated.",
            file=sys.stderr,
        )
        return _finish(ladder)
    except Exception as exc:  # noqa: BLE001
        ladder.record(FAIL, "5. credentials valid", f"{type(exc).__name__}: {exc}")
        return _finish(ladder)

    ladder.record(
        PASS,
        "5. credentials valid",
        f"{len(accessible)} directly accessible account(s): "
        + ", ".join(format_customer_id(cid) for cid in accessible),
    )

    # -- 6. the MCC --------------------------------------------------------
    mcc = settings.login_customer_id
    try:
        rows = client.rows(
            get_report("accounts").build(limit=200), customer_id=mcc
        )
    except GoogleAdsReportingError as exc:
        ladder.record(FAIL, "6. MCC reachable", _first_lines(exc))
        print(
            f"\n  GOOGLE_ADS_LOGIN_CUSTOMER_ID ({format_customer_id(mcc)}) is "
            "rejected, but the credentials themselves work. Check it is the "
            "manager account's ID and that this user has access to it.",
            file=sys.stderr,
        )
        return _finish(ladder)

    ladder.record(
        PASS,
        f"6. MCC {format_customer_id(mcc)} reachable",
        f"{len(rows)} account(s) under it",
    )
    for row in rows[:15]:
        print(
            f"         {format_customer_id(row['customer_client.id'])}  "
            f"{row['customer_client.descriptive_name'] or '(no name)'}  "
            f"[{row['customer_client.currency_code']}, "
            f"{row['customer_client.status']}"
            + (", manager" if row["customer_client.manager"] else "")
            + (", TEST" if row["customer_client.test_account"] else "")
            + "]"
        )
    if len(rows) > 15:
        print(f"         ... and {len(rows) - 15} more")

    # -- 7. the target account --------------------------------------------
    try:
        target = settings.resolve_customer_id(args.customer_id)
    except ConfigError as exc:
        ladder.record(
            WARN,
            "7. target account reachable",
            f"skipped: {exc}",
        )
        return _finish(ladder)

    probe = Query(
        select=("customer.id", "customer.descriptive_name", "customer.currency_code",
                "customer.time_zone"),
        from_resource="customer",
    )
    try:
        info = client.rows(probe, customer_id=target)
    except GoogleAdsReportingError as exc:
        ladder.record(FAIL, "7. target account reachable", _first_lines(exc))
        print(
            f"\n  The credentials and the MCC both work, so the problem is "
            f"{format_customer_id(target)} specifically: check it is managed "
            "by this MCC and is not cancelled.",
            file=sys.stderr,
        )
        return _finish(ladder)

    if not info:
        ladder.record(
            WARN,
            f"7. target {format_customer_id(target)} reachable",
            "the query succeeded but returned no rows",
        )
    else:
        row = info[0]
        ladder.record(
            PASS,
            f"7. target {format_customer_id(target)} reachable",
            f"{row['customer.descriptive_name'] or '(no name)'} "
            f"[{row['customer.currency_code']}, {row['customer.time_zone']}]",
        )

    # -- 8. a real report --------------------------------------------------
    try:
        sample = client.rows(
            get_report("campaigns").build(limit=5), customer_id=target
        )
    except GoogleAdsReportingError as exc:
        ladder.record(FAIL, "8. campaigns report runs", _first_lines(exc))
        return _finish(ladder)

    if not sample:
        # Not a failure: an account with no delivery in the window returns
        # zero rows, and that is a valid answer.
        ladder.record(
            WARN,
            "8. campaigns report runs",
            "0 rows for the last 30 days -- no campaigns, or no delivery",
        )
    else:
        total = sum(row["metrics.cost_micros"] or 0 for row in sample)
        ladder.record(
            PASS,
            "8. campaigns report runs",
            f"{len(sample)} sample row(s), cost in them: {total:,.2f}",
        )

    return _finish(ladder)


def _first_lines(exc: Exception, count: int = 6) -> str:
    lines = str(exc).splitlines()
    shown = lines[:count]
    if len(lines) > count:
        shown.append(f"... ({len(lines) - count} more lines)")
    return "\n         ".join(shown)


def _finish(ladder: Ladder) -> int:
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for status, _, _ in ladder.results:
        counts[status] += 1
    print(
        f"\n{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed."
    )
    if ladder.failed:
        print("Fix the FIRST failure above; later rungs depend on it.")
        return 1
    print("Connection is good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
