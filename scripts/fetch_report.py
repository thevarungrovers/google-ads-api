#!/usr/bin/env python
"""Run a report and print it, or write it to CSV.

    ./.venv/bin/python scripts/fetch_report.py --list
    ./.venv/bin/python scripts/fetch_report.py campaigns --days 30
    ./.venv/bin/python scripts/fetch_report.py campaigns --during LAST_7_DAYS --csv
    ./.venv/bin/python scripts/fetch_report.py keywords --start 2026-09-01 --end 2026-09-30
    ./.venv/bin/python scripts/fetch_report.py accounts --customer-id <MCC>
    ./.venv/bin/python scripts/fetch_report.py campaigns --show-query

Cost columns are already converted out of micros, so ``metrics.cost`` is in the
account's currency. Zero rows is a valid result, not an error: an account with
no delivery in the window has nothing to report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from googleads_reporting.client import (  # noqa: E402
    GoogleAdsReportingError,
    ReadOnlyGoogleAdsClient,
)
from googleads_reporting import logdb  # noqa: E402
from googleads_reporting.config import ConfigError  # noqa: E402
from googleads_reporting.customer_id import (  # noqa: E402
    CustomerIdError,
    format_customer_id,
)
from googleads_reporting.export import (  # noqa: E402
    output_path,
    summarise,
    to_dataframe,
    write_csv,
)
from googleads_reporting.query import QueryError, between, during, last_n_days  # noqa: E402
from googleads_reporting.reports import REGISTRY, get_report, report_names  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a read-only Google Ads report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available reports:\n"
        + "\n".join(
            f"  {name:12s} {REGISTRY[name].description}" for name in report_names()
        ),
    )
    parser.add_argument(
        "report",
        nargs="?",
        choices=report_names(),
        help="which report to run",
    )
    parser.add_argument("--list", action="store_true", help="list reports and exit")
    parser.add_argument(
        "--customer-id",
        help="account to query (default: GOOGLE_ADS_CUSTOMER_ID). "
        "Dashes are fine.",
    )

    dates = parser.add_mutually_exclusive_group()
    dates.add_argument(
        "--days",
        type=int,
        help="window of N days ending YESTERDAY (today is always partial)",
    )
    dates.add_argument("--during", help="a GAQL preset, e.g. LAST_7_DAYS")
    dates.add_argument(
        "--start", help="start date YYYY-MM-DD (requires --end)"
    )
    parser.add_argument("--end", help="end date YYYY-MM-DD (requires --start)")

    parser.add_argument("--limit", type=int, help="cap the number of rows")
    parser.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="CONDITION",
        help="extra GAQL WHERE condition; repeatable",
    )
    parser.add_argument("--csv", action="store_true", help="write a CSV to the output dir")
    parser.add_argument(
        "--out", type=Path, help="explicit output file path (implies --csv)"
    )
    parser.add_argument(
        "--show-query",
        action="store_true",
        help="print the GAQL and exit without calling the API",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=30,
        help="rows to print to the terminal (default 30; the CSV is complete)",
    )
    return parser


def resolve_date_condition(args: argparse.Namespace, report) -> str | None:
    """Turn the date flags into one GAQL condition, or None for the default."""
    if args.start or args.end:
        if not (args.start and args.end):
            raise QueryError("--start and --end must be given together.")
        return between(args.start, args.end)
    if args.days is not None:
        return last_n_days(args.days)
    if args.during:
        return during(args.during)
    return None


def main(argv: list[str] | None = None) -> int:
    """Wraps `_run` so one command is one `tool_calls` row.

    A single invocation can send several requests -- the reads that build a
    diff, then the mutation -- and grouping them under one row is what makes
    the log read as "this command did this" rather than as unrelated traffic
    that happened close together.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    label = "fetch_report.py " + " ".join(argv[:2]) if argv else "fetch_report.py"
    with logdb.record_tool_call(label, {"argv": argv}):
        return _run(argv)


def _run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list or not args.report:
        print("Available reports:\n")
        for name in report_names():
            report = REGISTRY[name]
            print(f"  {name:12s} {report.description}")
            print(f"  {'':12s} FROM {report.resource}, {len(report.select)} fields")
            if not report.supports_date_range:
                print(f"  {'':12s} (no date range; run against the MCC)")
        if not args.report and not args.list:
            print("\nPass a report name. See --help.")
            return 2
        return 0

    report = get_report(args.report)

    try:
        date_condition = resolve_date_condition(args, report)
        if date_condition and not report.supports_date_range:
            print(
                f"error: the {report.name!r} report has no date segment, so "
                "--days/--during/--start cannot apply to it.",
                file=sys.stderr,
            )
            return 2
        query = report.build(
            date_condition=date_condition,
            extra_where=tuple(args.where),
            limit=args.limit,
        )
        # Inside the try: to_gaql() is what runs the SELECT-only check, so a
        # --where carrying a write keyword surfaces here as a clean error
        # rather than an uncaught traceback.
        gaql = query.to_gaql()
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.show_query:
        print(gaql)
        return 0

    try:
        client = ReadOnlyGoogleAdsClient.from_env()
        target = client.resolve_customer_id(args.customer_id)
    except (ConfigError, CustomerIdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Report:   {report.name} ({report.description})")
    print(f"Account:  {format_customer_id(target)}")
    print(f"API:      {client.api_version}\n")

    try:
        rows = client.rows(query, customer_id=target)
    except GoogleAdsReportingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    frame = to_dataframe(rows, report.select)

    if frame.empty:
        # A valid answer, not a failure: the account had no delivery in the
        # window, or the WHERE clause matched nothing.
        print("0 rows. The window has no data, or the filters matched nothing.")
        print("\nQuery was:")
        print("\n".join(f"  {line}" for line in gaql.splitlines()))
        return 0

    print(f"{len(frame)} rows.\n")
    with pd.option_context(
        "display.max_rows", args.max_rows,
        "display.max_columns", None,
        "display.width", 220,
    ):
        print(frame.head(args.max_rows).to_string(index=False))
    if len(frame) > args.max_rows:
        print(f"\n... {len(frame) - args.max_rows} more rows not shown.")

    totals = summarise(frame)
    if len(totals) > 1:
        print("\nTotals (ratios recomputed from the sums, not averaged):")
        for key, value in totals.items():
            if key == "rows":
                continue
            print(f"  {key:28s} {value:,.6g}")

    if args.csv or args.out:
        destination = args.out or output_path(
            client.settings.output_dir, report.name, target
        )
        written = write_csv(frame, destination)
        print(f"\nWrote {len(frame)} rows to {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
