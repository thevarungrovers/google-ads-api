"""Write tools: a ``preview_*`` and an ``apply_*`` for each change.

SEPARATE TOOLS, NOT A ``dry_run`` PARAMETER. This is the most consequential
choice here. Claude Code's permission rules key on the tool NAME, so separate
names let you permanently allowlist every ``preview_*`` while never allowlisting
an ``apply_*``. With a flag, one "always allow" clicked during a harmless
preview would silently authorise every future real write -- exactly the failure
this is guarding against.

Every ``apply_*`` demands the token its own preview minted. That buys three
things at once: a readable preview always sits directly above the approval
prompt, an apply cannot be called cold, and the recorded ``before`` is
re-checked against the entity's CURRENT value, so a change someone made in the
Google Ads UI in between is caught rather than silently overwritten.

``destructiveHint`` is set only where the change moves money or stops delivery.
Marking a reversible pause destructive would train someone to click through the
loud prompts, which is how loud prompts stop working.

NOT EXPOSED, deliberately:
  - setting any status to REMOVED. It is irreversible in Google Ads, and PAUSED
    achieves the same outcome reversibly.
  - creating or deleting campaigns, ad groups and ads. Creation is a
    multi-entity atomic request with media and a legal declaration attached;
    it belongs in `scripts/manage.py campaign new`, where a human answers for
    it, not in a tool an agent can reach.
  - budget changes on a SHARED budget, which would silently move every campaign
    using it.
  - anything touching billing or account access -- those services are not on
    the write client's allowlist at all.

Enforcement is structural: there is no generic ``mutate(resource, body)``
passthrough, so an unregistered operation is unreachable rather than merely
undocumented. ``tests/test_mcp_surface.py`` pins the registered tool names.

THE PAIRS THEMSELVES live in :mod:`googleads_mcp.changes`, one ``ChangeKind``
each, and :mod:`googleads_mcp.changes.engine` runs the one shared preview ->
apply -> revert sequence for all of them. This module only wires them up.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from googleads_mcp.changes import all_kinds
from googleads_mcp.changes.base import (  # noqa: F401 - re-exported
    APPLY_REVERSIBLE,
    APPLY_SPENDS,
    PREVIEW_SAFE,
    SETTABLE_STATUSES,
    money,
    parse_decimal,
)
from googleads_mcp.changes.context import WriteContext
from googleads_mcp.changes.engine import Engine, register_pair
from googleads_mcp.errors import refusals_reach_the_caller
from googleads_mcp.guardrails import read_only_mode
from googleads_mcp import ledger
from googleads_mcp.previews import ApplyResult
from googleads_mcp.tools_read import READ_ONLY
from googleads_reporting.write import MutatingGoogleAdsClient, adgroups, budgets, campaigns  # noqa: F401
from googleads_reporting.write.client import MutationError  # noqa: F401 - tests raise it
from googleads_reporting.write.plan import execute

# Every tool here is wrapped in googleads_mcp.errors.refusals_reach_the_caller:
# the MCP runtime drops the message of any exception that is not a ToolError,
# and an agent that cannot read WHICH bound it broke will retry the same call.


def register(mcp, state) -> Engine:
    """Attach every write tool to ``mcp``. Returns the engine, for tests."""

    # Read-only mode: the preview_* tools still register, because a preview
    # writes nothing and being able to say "here is exactly what I would
    # change" is most of the value even when the agent cannot be the one to do
    # it. Every apply_* and revert_change is simply never handed to the
    # registrar, so they are absent from tools/list and unreachable over MCP --
    # not refused at call time, ABSENT. See guardrails.read_only_mode().
    read_only = read_only_mode()

    def apply_tool(**kwargs):
        """`mcp.tool(...)` for a write tool, or a no-op when read-only.

        Returning the bare function leaves it defined and callable in-process,
        which the tests rely on, while registering nothing -- so nothing
        crosses the MCP boundary.
        """
        if read_only:
            return lambda fn: fn
        return mcp.tool(**kwargs)

    write_client = MutatingGoogleAdsClient.from_env()
    state.write_client = write_client

    # `execute` through a lambda, so the name is looked up in THIS module's
    # globals on every call -- which is what lets a test swap it for a recorder.
    ctx = WriteContext(state, write_client, lambda *a, **k: execute(*a, **k))
    engine = Engine(ctx)
    for kind in all_kinds():
        register_pair(
            mcp, engine, kind, refusals=refusals_reach_the_caller, apply_tool=apply_tool
        )

    @apply_tool(title="Revert a change by its ledger entry id", annotations=APPLY_REVERSIBLE)
    @refusals_reach_the_caller
    def revert_change(
        entry_id: Annotated[
            str,
            Field(description="From list_my_changes; a batch_id reverts the whole batch"),
        ],
    ) -> ApplyResult:
        """Put a value back to what it was before an earlier change.

        Reverting is itself a change and is itself recorded, so the ledger
        stays a complete history rather than a list of things that are still
        true. Refuses if the value has moved since -- reverting then would
        discard someone else's edit rather than undo this one.
        """
        return engine.revert(entry_id)

    # A READ, registered here only because it needs the change kinds to know
    # how to read each ledger field back. Published in read-only mode too.
    @mcp.tool(title="Compare this server's changes with the live account",
              annotations=READ_ONLY)
    @refusals_reach_the_caller
    def reconcile_ledger(
        limit: Annotated[int, Field(description="Most recent N entity/field pairs",
                                    ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """For each entity/field this server last changed, is the live value
        still what we left it at?

        `drifted` means someone (usually a person in the Google Ads UI) changed
        it since -- revert_change will refuse those, and a fresh preview is
        needed before touching them. Only the latest applied, unreverted entry
        per entity and field is checked; older ones were superseded.
        """
        reverted = ledger.reverted_entry_ids()
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for entry in ledger.entries():
            if entry.get("applied") and entry["entry_id"] not in reverted:
                key = (entry["entity_type"], str(entry["entity_id"]), entry["field"])
                latest.pop(key, None)  # re-insert so dict order is recency
                latest[key] = entry
        checked = list(latest.values())[-limit:]
        results = []
        for entry in reversed(checked):
            line = {
                "entry_id": entry["entry_id"], "at": entry["at"],
                "entity": f"{entry['entity_type']} {entry['entity_name']}",
                "entity_id": entry["entity_id"], "field": entry["field"],
                "we_left_it_at": entry["after"],
            }
            try:
                kind = engine.kind_for_entry(entry)
                current = kind.current_for_entry(ctx, entry)
            except Exception as exc:  # noqa: BLE001 - reported per row, not fatal
                line.update(status="unreadable", detail=str(exc).splitlines()[0])
            else:
                if current is None:
                    line.update(status="unreadable", detail="no reader for this field")
                else:
                    line.update(status="matches" if current == entry["after"] else "drifted",
                                live_value=current)
            results.append(line)
        return {
            "checked": len(results),
            "drifted": sum(1 for r in results if r["status"] == "drifted"),
            "entries": results,
        }

    return engine
