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

from typing import Any

from .. import logdb


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
