"""Audit log for mutations: every request this package sends to Google.

Rows go to the ``mutations`` table of ``logs/google-ads.db`` (see
``googleads_reporting.logdb``). It used to be ``audit/mutations-<date>.jsonl``;
that file is retired, frozen on disk, and nothing appends to it.

Each mutation writes TWO records: an ``attempt`` before the call and an
``outcome`` after it. A lone ``attempt`` means the process died mid-request --
which is exactly the case where you need to know what was in flight, and
exactly the case a single after-the-fact record would lose. In SQLite that is
one row, inserted then updated, rather than two lines folded on read.

THE DIRECTORY ARGUMENT IS GONE, deliberately. This log used to live at
``settings.output_dir.parent / "audit"``, a path DERIVED from a configurable
setting -- so pointing ``GOOGLE_ADS_OUTPUT_DIR`` outside the repo moved the
library's mutation log while the MCP server's ledger, hard-coded to the repo,
stayed behind. Two records of the same session, in two directories, and
nothing said so. The database is resolved from the package, so that cannot
happen now.

This log records what was SENT, including ``validate_only`` dry runs that
changed nothing. For what the agent actually changed and how to undo it, see
``googleads_mcp/ledger.py`` and the ``changes`` table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import logdb

#: The repo's own audit directory. Still exists: it holds WRITES_DISABLED (the
#: kill switch) and the retired JSONL, frozen.
LEGACY_AUDIT_DIR = logdb.PROJECT_ROOT / "audit"


class AuditLog:
    """Appends mutation records to the ``mutations`` table."""

    def attempt(
        self,
        *,
        customer_id: str,
        service: str,
        method: str,
        operations: list[dict[str, Any]],
        validate_only: bool,
    ) -> str:
        """Record an intent to mutate. Returns a correlation id."""
        correlation_id = logdb.new_correlation_id()
        logdb.insert_mutation(
            correlation_id=correlation_id,
            customer_id=customer_id,
            service=service,
            method=method,
            operations=operations,
            validate_only=validate_only,
        )
        return correlation_id

    def outcome(
        self,
        correlation_id: str,
        *,
        ok: bool,
        resource_names: list[str] | None = None,
        request_id: str | None = None,
        error: str | None = None,
    ) -> None:
        logdb.complete_mutation(
            correlation_id,
            ok=ok,
            resource_names=resource_names,
            request_id=request_id,
            error=error,
        )


# --- migration off the JSONL ----------------------------------------------


def legacy_audit_dirs() -> list[Path]:
    """Every directory a retired mutations log could be sitting in.

    The repo's own `audit/` is the obvious one. The second is the point: this
    log used to be written to `settings.output_dir.parent / "audit"`, so a
    run with GOOGLE_ADS_OUTPUT_DIR pointed elsewhere left its mutations
    OUTSIDE the repo entirely. A migration that only globbed `audit/` would
    report success while leaving those files behind for good.

    Settings may not load at all (no .env, missing keys), which must not stop
    a migration -- the repo directory is still worth doing.
    """
    directories = [LEGACY_AUDIT_DIR]
    try:
        from ..config import Settings

        derived = Settings.from_env().output_dir.parent / "audit"
        if derived.resolve() != LEGACY_AUDIT_DIR.resolve():
            directories.append(derived)
    except Exception:
        pass
    return [d for d in directories if d.is_dir()]


def legacy_mutation_paths() -> list[Path]:
    """Every retired mutations JSONL, including hand-made sidecar copies.

    The glob is `mutations-*.jsonl*` rather than `mutations-*.jsonl` on
    purpose: a ledger that was ever rewritten by hand leaves copies beside it,
    and those copies can hold entries the live file no longer has.
    """
    found: list[Path] = []
    for directory in legacy_audit_dirs():
        found.extend(p for p in directory.glob("mutations-*.jsonl*") if p.is_file())
    return sorted(set(found))


def read_legacy_records(path: Path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def fold_legacy_mutations(path: Path) -> list[dict[str, Any]]:
    """Attempts joined to their outcomes, oldest first."""
    attempts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in read_legacy_records(path):
        correlation_id = record.get("id")
        if not correlation_id:
            continue
        if record.get("record") == "attempt":
            if correlation_id not in attempts:
                order.append(correlation_id)
            attempts[correlation_id] = dict(record)
        elif correlation_id in attempts:
            attempts[correlation_id].update(
                ok=record.get("ok"),
                outcome_at=record.get("at"),
                resource_names=record.get("resource_names") or [],
                request_id=record.get("request_id"),
                error=record.get("error"),
            )
    return [attempts[cid] for cid in order]


def import_legacy_mutations(path: Path) -> dict[str, int]:
    """Copy one retired mutations JSONL into the database. Idempotent."""
    conn = logdb.connect()
    folded = fold_legacy_mutations(path)
    inserted = 0
    skipped = 0
    for entry in folded:
        correlation_id = str(entry["id"])
        if conn.execute(
            "SELECT 1 FROM mutations WHERE correlation_id=?", (correlation_id,)
        ).fetchone() is not None:
            skipped += 1
            continue
        ok = entry.get("ok")
        conn.execute(
            "INSERT INTO mutations"
            "(correlation_id, at, host, pid, customer_id, service, method, validate_only, "
            " operation_count, operations_json, ok, outcome_at, resource_names, "
            " request_id, error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                correlation_id,
                entry.get("at") or "",
                entry.get("host"),
                entry.get("pid"),
                entry.get("customer_id"),
                entry.get("service"),
                entry.get("method"),
                1 if entry.get("validate_only") else 0,
                entry.get("operation_count"),
                json.dumps(entry.get("operations") or [], default=str)[:logdb.MAX_JSON_CHARS],
                None if ok is None else (1 if ok else 0),
                entry.get("outcome_at"),
                json.dumps(entry.get("resource_names") or [], default=str),
                entry.get("request_id"),
                entry.get("error"),
            ),
        )
        inserted += 1
    return {"inserted": inserted, "already_present": skipped, "read": len(folded)}
