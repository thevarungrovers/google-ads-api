#!/usr/bin/env python3
"""The `google-ads` MCP server: stdio, read tools plus guarded write tools.

Run it the way ~/.claude.json does -- absolute interpreter, absolute script:

    /Users/you/dev/google-ads-api/.venv/bin/python \\
        /Users/you/dev/google-ads-api/googleads_mcp/server.py

Never a bare `python3`, and not `-m`: the launching process's PATH and cwd are
not yours.

The server name matters more than it looks. Claude Code renders a permission
prompt as `google-ads - apply_campaign_daily_budget`, and its permission rules
key on the TOOL NAME -- which is the whole reason previews and applies are
separate tools rather than one tool with a `dry_run` flag. You can permanently
allowlist every `preview_*` and never allowlist a single `apply_*`.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

# Launched by absolute path, sys.path[0] is this package's directory, so the
# package itself is not importable until the repo root is on the path.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from googleads_mcp import (  # noqa: E402
    __version__,
    tools_audit,
    tools_lookup,
    tools_read,
    tools_write,
)
from googleads_mcp.guardrails import Limits, SessionCounters, load_limits  # noqa: E402
from googleads_mcp.previews import PreviewStore  # noqa: E402
from googleads_reporting import logdb  # noqa: E402

SERVER_NAME = "google-ads"

INSTRUCTIONS = """\
Google Ads, live production account. Read freely; write only through the
preview/apply pairs below.

THIS IS THE REAL ACCOUNT. Campaigns are serving and spending today. A mistake
costs money now, not at the next deploy.

HOW TO CHANGE SOMETHING
  1. Read first. find_campaigns / find_ad_groups give you the ids (which the
     Google Ads UI does not show), and run_report tells you what the entity has
     actually been doing.
  2. Call get_guardrails. It tells you the bounds and how much of this
     session's change budget is left, so you propose something that can be
     applied rather than something that will be refused.
  3. Call the matching preview_* tool. It returns a readable before/after, the
     campaign the entity sits in, its last 7 days, and an upper bound on the
     daily spend change.
  4. Show the human the preview, then call the apply_* tool with the
     preview_token it returned. The human approves at that prompt -- that
     approval IS the authorization, so never call apply_* without having just
     shown them what it does.

A preview_token is single-use, expires in 10 minutes, is not interchangeable
between tools, and is refused if the entity's current value has moved since the
preview was taken.

MONEY IS ALWAYS A DECIMAL STRING in major units: "2.50" means two dollars
fifty. Never pass micros -- "2500000" would be a millionfold error, and the
argument is rendered verbatim in the prompt a human is approving.

WHAT IS NOT HERE, deliberately: creating or removing campaigns, ad groups and
ads; setting any status to REMOVED (it is irreversible in Google Ads -- pause
instead, which is reversible and achieves the same thing); changing a SHARED
budget; anything touching billing or account access. There is no generic
mutate/passthrough tool, so if an operation is not listed it cannot be
performed here. Creating a campaign is done by a human running
`scripts/manage.py campaign new`, because it carries media and a legal
declaration someone has to answer for.

READ-ONLY INSTALLS. If get_guardrails reports read_only_server: true, this
process was started with GOOGLE_ADS_MCP_READ_ONLY set and published no apply_*
tool and no revert_change at all -- they are absent from the tool list, not
refused at call time. The preview_* tools still work, so the useful thing to do
is show the human exactly what you would change and let them make it elsewhere.

list_my_changes shows what you have already done in this account;
revert_change undoes one by its entry_id.
"""


@dataclass
class ServerState:
    """Everything the tools share, built once per server process."""

    limits: Limits
    counters: SessionCounters
    previews: PreviewStore
    read_client: Any = None
    write_client: Any = None
    default_customer_id: str = ""


def build_state() -> ServerState:
    limits = load_limits()
    return ServerState(
        limits=limits,
        counters=SessionCounters(limits),
        previews=PreviewStore(),
    )


# Built once at import, and the lifespan hands out THIS object rather than
# building a second one: two SessionCounters would mean two sets of counters
# and a session budget silently twice what it claims to be.
STATE = build_state()


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[ServerState]:
    yield STATE


mcp = MCPServer(
    name=SERVER_NAME,
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

# Registered through the instrumented wrapper so every tool call lands in
# logs/google-ads.db. `mcp` itself is what gets run; the wrapper only
# intercepts the decorator, so the server object the SDK sees is unchanged.
_registrar = logdb.InstrumentedServer(mcp)
tools_read.register(_registrar, STATE)
tools_lookup.register(_registrar, STATE)
tools_audit.register(_registrar, STATE)
tools_write.register(_registrar, STATE)


def budget_ceiling_warning(state: ServerState) -> str | None:
    """Warn when a campaign's budget is bigger than the whole day's allowance.

    Re-enabling a campaign projects its full daily budget against
    max_projected_delta_per_day_total. If one budget is bigger than that
    total, the campaign can never be re-enabled through this server once
    paused -- which is worth knowing at startup, not at the moment somebody
    needs it back on.
    """
    from googleads_reporting.query import Query

    rows = state.read_client.rows(Query(
        select=("campaign.id", "campaign.name", "campaign_budget.amount_micros"),
        from_resource="campaign",
        where=("campaign.status != 'REMOVED'",),
        order_by=("campaign_budget.amount_micros DESC",),
        limit=1,
    ))
    if not rows:
        return None
    biggest = rows[0]["campaign_budget.amount_micros"] or 0
    ceiling = state.limits.max_projected_delta_per_day_total
    if biggest > ceiling:
        return (
            f"WARNING: campaign {rows[0]['campaign.id']} has a daily budget of "
            f"{biggest:.2f}, above max_projected_delta_per_day_total ({ceiling}). "
            "Once paused it could not be re-enabled through this server. Raise the "
            "total in guardrails.toml to at least the largest campaign budget."
        )
    return None


def main() -> None:
    # Only when actually serving, never at import: the tests import this
    # module, and a startup check must not send a query from a test run.
    try:
        warning = budget_ceiling_warning(STATE)
    except Exception as exc:  # noqa: BLE001 - a startup courtesy, never fatal
        warning = f"WARNING: could not check campaign budgets at startup: {exc}"
    if warning:
        print(warning, file=sys.stderr)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
