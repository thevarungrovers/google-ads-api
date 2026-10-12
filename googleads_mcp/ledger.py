"""What the agent changed, and how to undo one.

The storage moved to SQLite (``logs/google-ads.db``, see
``googleads_reporting.logdb``). This module stayed, as the ledger-shaped face
of the ``changes`` table, because ``tools_write`` speaks in intents and
outcomes rather than rows -- and because the entry dict it hands back is a
contract ``revert_change`` reads field by field.

Still separate from ``googleads_reporting/write/audit.py``, which records every
mutation the library sends from any caller. That one now writes the
``mutations`` table in the same database. Two tables rather than one because
they answer different questions: this is "what did the agent do that I might
want to undo", that is "what did this package send to Google", and a merged
table would null out half its columns on every row.

WHAT DID NOT CHANGE: two phases per change. ``write_intent`` goes in BEFORE the
request leaves the process and ``write_outcome`` after it returns, so an entry
with no outcome means the process died mid-request -- exactly when you need to
know what was in flight. ``entries()`` still comes back OLDEST FIRST, because
``list_my_changes`` slices ``[-limit:]`` off the end to get the most recent.

WHAT DID: an outcome is an UPDATE of the intent row rather than a second line
merged on read, and the ``path=`` argument is gone. Nothing passed it, and a
file path means nothing to a table; tests point at a scratch database with
``logdb.reset_for_tests()`` instead.

The retired ``audit/mcp-changes.jsonl`` is still on disk, frozen, and nothing
writes to it. ``migrate_ledger.py`` is what moved its contents in here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from googleads_reporting import logdb

#: The retired JSONL. Read for migration, never written.
LEGACY_LEDGER_DIR = Path(__file__).resolve().parent.parent / "audit"
LEGACY_LEDGER_PATH = LEGACY_LEDGER_DIR / "mcp-changes.jsonl"

new_entry_id = logdb.new_entry_id
entries = logdb.change_entries
find_entry = logdb.find_change
reverted_entry_ids = logdb.reverted_entry_ids


def write_intent(
    *,
    entry_id: str,
    tool: str,
    customer_id: str,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    field_name: str,
    before: str,
    after: str,
    projected_delta: str,
) -> None:
    """Record what we are ABOUT to do. Must be called before the mutation."""
    logdb.insert_change(
        entry_id=entry_id,
        tool=tool,
        customer_id=customer_id,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        field_name=field_name,
        before=before,
        after=after,
        projected_delta=projected_delta,
    )


def write_outcome(
    *,
    entry_id: str,
    ok: bool,
    resource_name: str | None = None,
    error: str | None = None,
    reverts: str | None = None,
) -> None:
    """Record how it went, and which earlier entry this one reverses."""
    logdb.complete_change(
        entry_id=entry_id,
        ok=ok,
        resource_name=resource_name,
        error=error,
        reverts=reverts,
    )


# --- migration off the JSONL ----------------------------------------------


def read_legacy_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Every line of the retired JSONL. A truncated final line is skipped."""
    target = path or LEGACY_LEDGER_PATH
    if not target.is_file():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # A process killed mid-write leaves a partial line. Skipping it
            # beats refusing to read the ledger.
            continue


def fold_legacy_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Intents joined to their outcomes, oldest first -- the old read path."""
    intents: dict[str, dict[str, Any]] = {}
    for record in read_legacy_records(path):
        entry_id = record.get("entry_id")
        if not entry_id:
            continue
        if record.get("record") == "intent":
            intents[entry_id] = dict(record, applied=False)
        elif entry_id in intents:
            intents[entry_id].update(
                applied=bool(record.get("ok")),
                resource_name=record.get("resource_name"),
                error=record.get("error"),
                reverts=record.get("reverts"),
                outcome_at=record.get("at"),
            )
    return list(intents.values())


def import_legacy_jsonl(path: Path | None = None) -> dict[str, int]:
    """Copy the retired JSONL into the database. Idempotent.

    An entry_id already present is left exactly as it is rather than
    refreshed: the database is the live record now, so a re-run after new
    writes must not roll one back to its state at migration time.
    """
    conn = logdb.connect()
    folded = fold_legacy_records(path)
    inserted = 0
    skipped = 0
    for entry in folded:
        entry_id = str(entry["entry_id"])
        if conn.execute(
            "SELECT 1 FROM changes WHERE entry_id=?", (entry_id,)
        ).fetchone() is not None:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO changes"
            "(entry_id, at, pid, tool, customer_id, entity_type, entity_id, entity_name, "
            " field, before, after, projected_daily_spend_delta, ok, outcome_at, "
            " resource_name, error, reverts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id,
                entry.get("at") or "",
                entry.get("pid"),
                entry.get("tool") or "",
                entry.get("customer_id"),
                entry.get("entity_type"),
                None if entry.get("entity_id") is None else str(entry.get("entity_id")),
                entry.get("entity_name"),
                entry.get("field"),
                entry.get("before"),
                entry.get("after"),
                entry.get("projected_daily_spend_delta"),
                1 if entry.get("applied") else 0,
                entry.get("outcome_at"),
                entry.get("resource_name"),
                entry.get("error"),
                entry.get("reverts"),
            ),
        )
        inserted += 1
    return {"inserted": inserted, "already_present": skipped, "read": len(folded)}
