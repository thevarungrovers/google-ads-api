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
from googleads_reporting.naming import (  # noqa: E402
    Identity,
    load_identity,
    process_env_mismatches,
)

SERVER_NAME = "google-ads"

INSTRUCTIONS = """\
Google Ads, live production account. Read freely; write only through the
preview/apply pairs.

THIS IS THE REAL ACCOUNT. Campaigns are serving and spending today. A mistake
costs money now, not at the next deploy.

HOW TO CHANGE SOMETHING
  1. Read first. find_campaigns / find_ad_groups give the ids (the Google Ads
     UI shows almost none); get_campaign / get_ad_group give the full current
     state; run_report says what it has actually been doing.
  2. get_guardrails: the bounds, and what is left of this SESSION's and
     today's budget of changes.
  3. The matching preview_* tool. It returns before/after, where the entity
     sits, an upper bound on added daily spend, one line per bound
     (OK / FAIL / SKIP), and Google's own validate_only verdict.
  4. Show the human the preview, then call apply_* with its preview_token.
     The human approves at that prompt -- that approval IS the authorization,
     so never call apply_* without having just shown them what it does.

A preview_token is single-use, expires in 10 minutes, is not interchangeable
between tools, and is refused if the entity moved since the preview.

MONEY IS ALWAYS A DECIMAL STRING in major units: "2.50" means two dollars
fifty. Never pass micros -- "2500000" would be a millionfold error, and the
argument is rendered verbatim in the prompt a human is approving. Bid
modifiers are signed PERCENTAGES ("+20", "-15"), never multipliers. A target
ROAS is a ratio ("4.0" = 400%).

THE CAPS. Every apply_* and revert_change counts against two budgets:
  session  this server process; resets when it restarts
  daily    the LOCAL calendar day across every process, counted from the
           change ledger; resets at local midnight
A batch counts once; a creation counts 3; a revert counts 1 and projects the
spend it restores. Re-ENABLING (campaign, ad group, keyword, ad) is exempt from
the per-change max_projected_delta_per_day (shown as SKIP) and counted in full
against the daily total instead. Bid and budget increases need both. When a
cap refuses, stop and tell the human; do not work around it.

WHAT YOU CAN CHANGE (each a preview_X / apply_X pair)
  status         campaign_status, ad_group_status, keyword_status, ad_status
  money          campaign_daily_budget, shared_budget_amount, ad_group_cpc_bid,
                 keyword_cpc_bid, bidding_strategy (resets Google's learning)
  keywords       add_keywords (PAUSED unless enabled), add_negative_keywords,
                 add_to_shared_negative_list, link_shared_negative_list
  targeting      campaign_locations, campaign_languages, ad_schedule,
                 device_bid_modifier, location_bid_modifier, campaign_networks,
                 audience_targeting
  ads, assets    create_responsive_search_ad (PAUSED), replace_responsive_
                 search_ad (new ad + pause old, one request), add_sitelinks,
                 add_callouts, add_structured_snippets (assets link LIVE)
  creation       create_campaign (everything PAUSED; the EU political
                 advertising declaration is required -- ASK the human),
                 create_ad_group (PAUSED), rename_campaign, rename_ad_group
  recommendation Google's recommendation, applied through the typed change it
                 amounts to and that change's bounds
  low_risk_change  labels, final URL suffix, tracking template, dismissing a
                 recommendation -- a closed allowlist; nothing that spends

NAMING. Pass BASE names when creating: the server puts the configured
OWNER-agent- prefix in front (campaigns, their budgets, ad groups, labels), and
the preview's `naming` block shows the exact final name. The parts come from
the repo's .env only; get_guardrails.identity shows which are set, and an unset
part is simply left out (none set: the name is used as given). A malformed .env
value fails agent_identity on every create and rename -- tell the human the key
to fix. A name with ANOTHER agent's prefix is refused; over-long names are
refused (name_length says how long the base may be), never truncated. RSAs,
keywords and assets have no name. Renames: only your OWN entities (your prefix,
or the ledger shows you created them); the prefix is kept. find_campaigns,
find_ad_groups and list_labels filter with origin / agent_name / owner / mine.

NOTHING IS EVER REMOVED. No status is ever set to REMOVED; pausing is the undo.
Created campaigns, ad groups and ads start PAUSED and are never removed here
(a replaced RSA's new ad keeps the old ad's status; asset links are live).
The one exception: reverting a keyword, negative, location, link or asset link
THIS server added removes exactly that criterion, traced through the ledger.
Billing, account access and conversion-action edits are not reachable.

ERRORS start with their class: AUTH_EXPIRED (a human must re-authorise),
QUOTA (back off), INVALID_QUERY (fix the request), NOT_FOUND (re-read ids),
POLICY (names the policy topic), TRANSIENT (retry; reads already retry).

UNDO AND DRIFT. list_my_changes lists what this server did; revert_change takes
an entry_id, or a batch_id to undo a whole batch. reconcile_ledger flags
entries someone has changed since; list_recent_changes shows who.

READ-ONLY INSTALLS. If get_guardrails reports read_only_server: true, this
process was started with GOOGLE_ADS_MCP_READ_ONLY set and published no apply_*
tool and no revert_change at all -- they are absent from the tool list, not
refused at call time. The preview_* tools still work, so the useful thing to do
is show the human exactly what you would change and let them make it elsewhere.

run_gaql is a read-only escape hatch for questions no other tool answers.
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
    #: Whose prefix goes on created names -- from this repo's .env ONLY, read
    #: once here. See googleads_reporting/naming.py.
    identity: Identity = field(default_factory=Identity)


def build_state() -> ServerState:
    limits = load_limits()
    return ServerState(
        limits=limits,
        counters=SessionCounters(limits),
        previews=PreviewStore(),
        identity=load_identity(),
    )


def identity_warnings(state: ServerState) -> list[str]:
    """What to say on stderr at startup about the naming identity.

    A malformed value refuses every create and rename at preview, so it is
    worth seeing before then; a value in the process environment is ignored,
    and saying so beats a mystery prefix later. Values are never printed.
    """
    out = [f"ERROR: {problem} Creating and renaming are refused until it is fixed."
           for problem in state.identity.problems]
    out += process_env_mismatches(state.identity)
    return out


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
    for line in identity_warnings(STATE):
        print(line, file=sys.stderr)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
