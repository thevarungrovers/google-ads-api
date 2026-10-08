"""Append-only audit log for mutations.

Written as JSON Lines so it can be grepped, tailed and parsed without a
dependency, and appended to rather than rewritten so a crash cannot destroy
earlier entries.

Each mutation writes TWO records: ``attempt`` before the call and ``outcome``
after it. A lone ``attempt`` with no matching ``outcome`` means the process
died mid-request -- which is exactly the case where you need to know what was
in flight, and exactly the case a single after-the-fact record would lose.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    """Appends mutation records to ``<directory>/mutations-<date>.jsonl``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.directory / f"mutations-{day}.jsonl"

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path()
        line = json.dumps(record, default=str, ensure_ascii=False)
        # Opened per record, in append mode, and flushed: the log must survive
        # the process dying immediately after a mutation was sent.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

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
        correlation_id = uuid.uuid4().hex
        self._write(
            {
                "record": "attempt",
                "id": correlation_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "customer_id": customer_id,
                "service": service,
                "method": method,
                "validate_only": validate_only,
                "operation_count": len(operations),
                "operations": operations,
            }
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
        self._write(
            {
                "record": "outcome",
                "id": correlation_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "ok": ok,
                "resource_names": resource_names or [],
                "request_id": request_id,
                "error": error,
            }
        )
