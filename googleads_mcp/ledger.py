"""Append-only record of what the agent changed, and how to undo one.

Separate from ``googleads_reporting/write/audit.py``, which logs every mutation
the library makes from any caller. This logs what the MCP SERVER did, in terms
an agent and a human can both read back: one line per change, with the
before-value needed to reverse it.

Two records per change -- ``intent`` before the call and ``outcome`` after --
so an intent with no outcome means the process died mid-request, which is
exactly when you need to know what was in flight.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
from pathlib import Path
from typing import Any, Iterator

LEDGER_DIR = Path(__file__).resolve().parent.parent / "audit"
LEDGER_PATH = LEDGER_DIR / "mcp-changes.jsonl"


def new_entry_id() -> str:
    return secrets.token_hex(6)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _append(record: dict[str, Any], path: Path | None = None) -> None:
    path = path or LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, ensure_ascii=False)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


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
    path: Path | None = None,
) -> None:
    _append(
        {
            "record": "intent",
            "entry_id": entry_id,
            "at": _now(),
            "pid": os.getpid(),
            "tool": tool,
            "customer_id": customer_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name,
            "field": field_name,
            "before": before,
            "after": after,
            "projected_daily_spend_delta": projected_delta,
        },
        path,
    )


def write_outcome(
    *,
    entry_id: str,
    ok: bool,
    resource_name: str | None = None,
    error: str | None = None,
    reverts: str | None = None,
    path: Path | None = None,
) -> None:
    _append(
        {
            "record": "outcome",
            "entry_id": entry_id,
            "at": _now(),
            "ok": ok,
            "resource_name": resource_name,
            "error": error,
            "reverts": reverts,
        },
        path,
    )


def read_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    path = path or LEDGER_PATH
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line is possible if the process was killed
                # mid-write. Skipping it beats refusing to read the ledger.
                continue


def entries(path: Path | None = None) -> list[dict[str, Any]]:
    """Intents joined to their outcomes, newest last."""
    intents: dict[str, dict[str, Any]] = {}
    for record in read_records(path):
        entry_id = record.get("entry_id")
        if record.get("record") == "intent":
            intents[entry_id] = dict(record, applied=False)
        elif entry_id in intents:
            intents[entry_id].update(
                applied=bool(record.get("ok")),
                resource_name=record.get("resource_name"),
                error=record.get("error"),
                reverts=record.get("reverts"),
            )
    return list(intents.values())


def find_entry(entry_id: str, path: Path | None = None) -> dict[str, Any] | None:
    for entry in entries(path):
        if entry.get("entry_id") == entry_id:
            return entry
    return None


def reverted_entry_ids(path: Path | None = None) -> set[str]:
    return {
        record["reverts"]
        for record in read_records(path)
        if record.get("record") == "outcome" and record.get("reverts")
    }
