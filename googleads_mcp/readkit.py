"""Fetching rows for an agent without silently dropping any.

The old run_report put the caller's limit straight into GAQL as LIMIT, so a
report that matched 900 rows returned 100 and said nothing -- and every total
was the total of those 100. An agent reasoning about "the account" from that
was reasoning about an arbitrary slice of it.

:func:`fetch` streams the WHOLE result (up to a scan cap), computes totals over
all of it, returns only the first ``limit`` rows, and says how many there were.
Ordering matters for the same reason: every report orders by cost descending
where it can, so the rows that are cut are the ones that matter least.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from googleads_reporting.fields import row_to_dict
from googleads_reporting.query import Query, parse_select
from googleads_reporting.retry import retry_read

#: Rows read before giving up on counting. A search-term report over a year can
#: run to six figures; past this the count is reported as "N+" rather than
#: holding the agent's tool call open while it streams.
SCAN_CAP = 20_000


def fetch(client, query: Query, limit: int, *, scan_cap: int = SCAN_CAP) -> dict[str, Any]:
    """{rows, all_rows, total_rows_available, truncated, scan_capped}."""
    unlimited = replace(query, limit=None)
    gaql = unlimited.to_gaql()
    paths = parse_select(gaql)

    def run() -> tuple[list[dict[str, Any]], bool]:
        collected: list[dict[str, Any]] = []
        capped = False
        for row in client.search_stream(gaql):
            if len(collected) >= scan_cap:
                capped = True
                break
            collected.append(row_to_dict(row, paths))
        return collected, capped

    all_rows, capped = retry_read(run)
    return {
        "rows": all_rows[:limit],
        "all_rows": all_rows,
        "total_rows_available": f"{scan_cap}+" if capped else len(all_rows),
        "truncated": capped or len(all_rows) > limit,
        "gaql": gaql,
    }
