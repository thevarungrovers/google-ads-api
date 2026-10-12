"""The log database is now the only record of a real-money mutation. Pin it.

Four properties matter more than the rest, and each is the kind that passes by
accident:

  - An outcome must UPDATE its intent row without touching what the intent
    recorded. The upsert form of this is a known silent-overwrite trap, so the
    test applies an outcome TWICE -- a single pass only exercises the INSERT.

  - ``entries()`` must come back OLDEST FIRST. ``list_my_changes`` slices
    ``[-limit:]`` off the end, so reversing the order silently returns the
    oldest changes while still looking like it works.

  - Observability must never raise and the ledger must always raise. Opposite
    requirements on the same module, so both are asserted against a database
    that cannot be opened at all.

  - A caller that breaks out of ``search_stream`` early must not be logged as
    a failed API call. The read chokepoints are generators, so an abandoned
    one raises GeneratorExit into the logging context manager.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from googleads_mcp import ledger
from googleads_reporting import logdb
from googleads_reporting.write import audit


@pytest.fixture
def db():
    """The scratch database conftest's autouse fixture already redirected to."""
    return logdb.DB_PATH


def an_intent(entry_id: str, **overrides) -> None:
    fields = dict(
        entry_id=entry_id,
        tool="apply_campaign_daily_budget",
        customer_id="1234567890",
        entity_type="campaign",
        entity_id="111",
        entity_name="Brand - Search",
        field_name="daily_budget",
        before="10.00",
        after="12.00",
        projected_delta="2.00",
    )
    fields.update(overrides)
    logdb.insert_change(**fields)


# --- schema and pragmas ----------------------------------------------------


def test_connect_creates_the_schema_and_sets_the_pragmas(db):
    conn = logdb.connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tool_calls", "api_calls", "changes", "mutations", "sessions", "meta"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 2 is FULL. The ledger half of this file is evidence, not telemetry.
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_the_database_file_is_not_world_readable(db):
    logdb.connect()
    assert oct(db.stat().st_mode)[-3:] == "600"


# --- the change ledger -----------------------------------------------------


def test_an_intent_with_no_outcome_is_not_applied(db):
    an_intent("aaa")
    entry = logdb.find_change("aaa")
    assert entry is not None
    assert entry["applied"] is False, "a write with no outcome must not look applied"
    assert entry["before"] == "10.00" and entry["after"] == "12.00"


def test_an_outcome_updates_its_intent_row(db):
    an_intent("bbb")
    logdb.complete_change(entry_id="bbb", ok=True, resource_name="customers/1/campaigns/111")
    entry = logdb.find_change("bbb")
    assert entry["applied"] is True
    assert entry["resource_name"] == "customers/1/campaigns/111"
    assert len(logdb.change_entries()) == 1, "the outcome must update, not insert a second row"


def test_a_second_outcome_does_not_wipe_the_intent(db):
    """The upsert trap, as a regression test.

    ``excluded.col`` is the value that WOULD have been inserted, so an upsert
    written to preserve the intent columns overwrites them instead -- and only
    on the SECOND pass, because the first is an INSERT that never reaches the
    DO UPDATE clause.
    """
    an_intent("ccc")
    logdb.complete_change(entry_id="ccc", ok=True, resource_name="r1")
    logdb.complete_change(entry_id="ccc", ok=False, error="second pass")

    entry = logdb.find_change("ccc")
    assert entry["tool"] == "apply_campaign_daily_budget"
    assert entry["customer_id"] == "1234567890"
    assert entry["entity_id"] == "111"
    assert entry["before"] == "10.00" and entry["after"] == "12.00"
    assert entry["projected_daily_spend_delta"] == "2.00"
    assert entry["applied"] is False and entry["error"] == "second pass"
    assert len(logdb.change_entries()) == 1


def test_entries_come_back_oldest_first(db):
    """list_my_changes does entries()[-limit:]; reversing this breaks it silently."""
    for index, entry_id in enumerate(["first", "second", "third"]):
        an_intent(entry_id)
        logdb.connect().execute(
            "UPDATE changes SET at=? WHERE entry_id=?",
            (f"2026-10-0{index + 1}T00:00:00+00:00", entry_id),
        )
    assert [e["entry_id"] for e in logdb.change_entries()] == ["first", "second", "third"]
    assert [e["entry_id"] for e in logdb.change_entries()[-2:]] == ["second", "third"]


def test_find_change_returns_none_for_an_unknown_id(db):
    assert logdb.find_change("nope") is None


def test_a_revert_marks_its_target_as_reverted(db):
    an_intent("target")
    logdb.complete_change(entry_id="target", ok=True)
    an_intent("undo", tool="revert_change")
    logdb.complete_change(entry_id="undo", ok=True, reverts="target")
    assert logdb.reverted_entry_ids() == {"target"}


def test_a_failed_revert_still_blocks_a_second_attempt(db):
    """Mirrors the JSONL fold, which counted `reverts` regardless of ok.

    The safe direction: the first attempt may yet have landed at Google's end,
    so a second one must not go out unexamined.
    """
    an_intent("target")
    logdb.complete_change(entry_id="target", ok=True)
    an_intent("undo", tool="revert_change")
    logdb.complete_change(entry_id="undo", ok=False, error="boom", reverts="target")
    assert logdb.reverted_entry_ids() == {"target"}


# --- the mutations table ---------------------------------------------------


def test_a_mutation_is_two_phase_and_records_dry_runs(db):
    log = audit.AuditLog()
    correlation_id = log.attempt(
        customer_id="1234567890",
        service="CampaignService",
        method="mutate_campaigns",
        operations=[{"update": {"status": "PAUSED"}}],
        validate_only=True,
    )
    row = logdb.connect().execute(
        "SELECT * FROM mutations WHERE correlation_id=?", (correlation_id,)
    ).fetchone()
    assert row["ok"] is None, "no outcome yet -- the request has not returned"
    assert row["validate_only"] == 1
    assert row["operation_count"] == 1

    log.outcome(correlation_id, ok=True, resource_names=["customers/1/campaigns/9"])
    row = logdb.connect().execute(
        "SELECT * FROM mutations WHERE correlation_id=?", (correlation_id,)
    ).fetchone()
    assert row["ok"] == 1
    assert json.loads(row["resource_names"]) == ["customers/1/campaigns/9"]
    # The intent half must survive the update.
    assert row["service"] == "CampaignService" and row["operation_count"] == 1


def test_a_dry_run_reaches_mutations_but_never_changes(db):
    """Why the two tables stay separate: validate_only is sent but changes nothing."""
    log = audit.AuditLog()
    log.attempt(
        customer_id="1", service="GoogleAdsService", method="mutate",
        operations=[{"x": 1}], validate_only=True,
    )
    assert logdb.connect().execute("SELECT COUNT(*) FROM mutations").fetchone()[0] == 1
    assert logdb.change_entries() == []


# --- tool calls and their api calls ----------------------------------------


def test_an_api_call_names_the_tool_call_that_caused_it(db):
    with logdb.record_tool_call("run_report", {"days": 7}):
        with logdb.record_api_call("search_stream", service="GoogleAdsService"):
            pass
        with logdb.record_api_call("search_stream", service="GoogleAdsService"):
            pass

    conn = logdb.connect()
    tool_row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert tool_row["tool_name"] == "run_report" and tool_row["ok"] == 1
    assert json.loads(tool_row["args_json"]) == {"days": 7}
    api_rows = conn.execute("SELECT * FROM api_calls ORDER BY id").fetchall()
    assert len(api_rows) == 2
    assert {r["tool_call_id"] for r in api_rows} == {tool_row["id"]}


def test_an_api_call_outside_a_tool_call_has_no_parent(db):
    with logdb.record_api_call("search"):
        pass
    assert logdb.connect().execute("SELECT * FROM api_calls").fetchone()["tool_call_id"] is None


def test_a_failing_tool_is_recorded_and_still_raises(db):
    with pytest.raises(ValueError):
        with logdb.record_tool_call("apply_campaign_status"):
            raise ValueError("boom")
    row = logdb.connect().execute("SELECT * FROM tool_calls").fetchone()
    assert row["ok"] == 0 and "boom" in row["error"]


def test_breaking_out_of_a_streamed_read_is_a_clean_close(db):
    """GeneratorExit is the caller stopping early, not a failed request."""

    def stream():
        with logdb.record_api_call("search_stream", service="GoogleAdsService") as slot:
            rows = 0
            try:
                for value in range(100):
                    rows += 1
                    yield value
            finally:
                slot["row_count"] = rows

    for value in stream():
        if value == 2:
            break  # abandons the generator -> GeneratorExit

    import gc

    gc.collect()
    row = logdb.connect().execute("SELECT * FROM api_calls").fetchone()
    assert row["ok"] == 1, "an early break must not look like a broken API call"
    assert row["error"] is None
    assert row["row_count"] == 3


def test_an_oversized_payload_is_truncated_not_dropped(db):
    with logdb.record_api_call("search", request={"gaql": "x" * (logdb.MAX_JSON_CHARS * 2)}):
        pass
    stored = logdb.connect().execute("SELECT request_json FROM api_calls").fetchone()[0]
    assert stored.endswith("chars]")
    assert len(stored) < logdb.MAX_JSON_CHARS + 100


# --- the two error policies ------------------------------------------------


def test_observability_never_raises_when_the_database_is_unopenable(monkeypatch):
    monkeypatch.setattr(logdb, "LOG_DIR", Path("/dev/null/not-a-directory"))
    monkeypatch.setattr(logdb, "DB_PATH", Path("/dev/null/not-a-directory/x.db"))
    logdb._local.__dict__.pop("conn", None)
    monkeypatch.setattr(logdb, "_session_id", None)

    ran = []
    with logdb.record_tool_call("run_report"):
        with logdb.record_api_call("search"):
            ran.append(True)
    assert ran == [True], "the tool body must run even when logging is broken"
    logdb._local.__dict__.pop("conn", None)


def test_the_ledger_does_raise_when_it_cannot_be_written(monkeypatch):
    monkeypatch.setattr(logdb, "LOG_DIR", Path("/dev/null/not-a-directory"))
    monkeypatch.setattr(logdb, "DB_PATH", Path("/dev/null/not-a-directory/x.db"))
    logdb._local.__dict__.pop("conn", None)
    monkeypatch.setattr(logdb, "_session_id", None)

    with pytest.raises(Exception):
        an_intent("eee")
    logdb._local.__dict__.pop("conn", None)


# --- concurrency -----------------------------------------------------------


def test_a_second_process_can_write_while_the_first_holds_the_database(db):
    logdb.connect()
    an_intent("first")
    other = sqlite3.connect(db, timeout=10.0, isolation_level=None)
    other.execute("PRAGMA busy_timeout=10000")
    other.execute("INSERT INTO changes(entry_id, at, tool) VALUES('second','t','cli')")
    other.close()
    assert {e["entry_id"] for e in logdb.change_entries()} == {"first", "second"}


def test_a_worker_thread_gets_its_own_connection(db):
    errors: list[BaseException] = []

    def write():
        try:
            an_intent("from-thread")
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    thread.join()
    assert not errors, errors
    assert logdb.find_change("from-thread") is not None


# --- the tool wrapper ------------------------------------------------------


def test_instrumenting_a_tool_preserves_its_signature_and_resolves_hints(db):
    import inspect

    def sample(count: int = 3) -> str:
        """Docstring kept."""
        return "x" * count

    wrapped = logdb.instrument_tool(sample)
    assert wrapped.__name__ == "sample" and wrapped.__doc__ == "Docstring kept."

    original = inspect.signature(sample)
    wrapper_sig = inspect.signature(wrapped)
    assert list(wrapper_sig.parameters) == list(original.parameters)
    assert wrapper_sig.parameters["count"].default == 3

    # This module uses `from __future__ import annotations`, so the original's
    # annotations are strings. The wrapper's must be the resolved classes, or
    # pydantic would look them up in logdb's globals and fail.
    assert original.parameters["count"].annotation == "int"
    assert wrapper_sig.parameters["count"].annotation is int
    assert wrapper_sig.return_annotation is str

    assert wrapped(2) == "xx"
    assert logdb.connect().execute("SELECT tool_name FROM tool_calls").fetchone()[0] == "sample"


def test_every_registered_tool_was_instrumented():
    """UNINSTRUMENTED is a silent fallback. It must stay empty."""
    import googleads_mcp.server  # noqa: F401 -- importing registers every tool

    assert logdb.UNINSTRUMENTED == [], f"not being logged: {logdb.UNINSTRUMENTED}"


# --- migrating off the JSONL -----------------------------------------------


def _write_changes_jsonl(path: Path) -> None:
    lines = [
        {"record": "intent", "entry_id": "legacy-1", "at": "2026-10-08T12:00:00+00:00",
         "pid": 42, "tool": "apply_campaign_daily_budget", "customer_id": "1234567890",
         "entity_type": "campaign", "entity_id": "111", "entity_name": "Brand",
         "field": "daily_budget", "before": "10.00", "after": "12.00",
         "projected_daily_spend_delta": "2.00"},
        {"record": "outcome", "entry_id": "legacy-1", "at": "2026-10-08T12:00:02+00:00",
         "ok": True, "resource_name": "customers/1/campaigns/111", "error": None,
         "reverts": None},
        {"record": "intent", "entry_id": "legacy-2", "at": "2026-10-08T13:00:00+00:00",
         "tool": "apply_campaign_status", "customer_id": "1234567890",
         "entity_type": "campaign", "entity_id": "222", "entity_name": "Generic",
         "field": "status", "before": "ENABLED", "after": "PAUSED",
         "projected_daily_spend_delta": "0"},
        "{ truncated line",
    ]
    path.write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n"
    )


def test_importing_the_changes_jsonl_folds_intent_and_outcome(db, tmp_path):
    source = tmp_path / "mcp-changes.jsonl"
    _write_changes_jsonl(source)

    result = ledger.import_legacy_jsonl(source)
    assert result["read"] == 2, "the truncated line must be skipped, not fatal"
    assert result["inserted"] == 2

    done = logdb.find_change("legacy-1")
    assert done["applied"] is True and done["resource_name"] == "customers/1/campaigns/111"
    assert done["before"] == "10.00" and done["customer_id"] == "1234567890"

    unfinished = logdb.find_change("legacy-2")
    assert unfinished["applied"] is False, "an intent with no outcome stays unapplied"


def test_importing_twice_inserts_nothing_the_second_time(db, tmp_path):
    source = tmp_path / "mcp-changes.jsonl"
    _write_changes_jsonl(source)
    ledger.import_legacy_jsonl(source)
    logdb.complete_change(entry_id="legacy-2", ok=True, resource_name="later")

    again = ledger.import_legacy_jsonl(source)
    assert again["inserted"] == 0 and again["already_present"] == 2
    assert logdb.find_change("legacy-2")["applied"] is True, (
        "a re-run must not roll a row back to its state at migration time"
    )


def test_importing_the_mutations_jsonl(db, tmp_path):
    source = tmp_path / "mutations-2026-10-08.jsonl"
    source.write_text(
        json.dumps({"record": "attempt", "id": "m1", "at": "2026-10-08T12:00:00+00:00",
                    "host": "h", "pid": 1, "customer_id": "1234567890",
                    "service": "CampaignService", "method": "mutate_campaigns",
                    "validate_only": True, "operation_count": 1,
                    "operations": [{"update": {}}]}) + "\n"
        + json.dumps({"record": "outcome", "id": "m1", "at": "2026-10-08T12:00:01+00:00",
                      "ok": True, "resource_names": [], "request_id": "req-1",
                      "error": None}) + "\n"
    )
    result = audit.import_legacy_mutations(source)
    assert result["inserted"] == 1
    row = logdb.connect().execute("SELECT * FROM mutations").fetchone()
    assert row["ok"] == 1 and row["request_id"] == "req-1" and row["validate_only"] == 1
    assert audit.import_legacy_mutations(source)["inserted"] == 0


def test_legacy_mutation_paths_picks_up_sidecar_copies(db, tmp_path, monkeypatch):
    """A hand-rewritten ledger leaves copies beside it holding orphan records."""
    directory = tmp_path / "audit"
    directory.mkdir()
    (directory / "mutations-2026-10-08.jsonl").write_text("")
    (directory / "mutations-2026-10-08.jsonl.bak").write_text("")
    (directory / "mcp-changes.jsonl").write_text("")
    monkeypatch.setattr(audit, "legacy_audit_dirs", lambda: [directory])

    names = {p.name for p in audit.legacy_mutation_paths()}
    assert names == {"mutations-2026-10-08.jsonl", "mutations-2026-10-08.jsonl.bak"}, names


def test_legacy_audit_dirs_includes_the_derived_output_directory(tmp_path, monkeypatch):
    """GOOGLE_ADS_OUTPUT_DIR used to move this log outside the repo entirely."""
    elsewhere = tmp_path / "exports"
    (elsewhere.parent / "audit").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GOOGLE_ADS_OUTPUT_DIR", str(elsewhere))

    from googleads_reporting.config import Settings

    monkeypatch.setattr(
        Settings, "from_env", classmethod(lambda cls: _FakeSettings(elsewhere))
    )
    dirs = {str(d.resolve()) for d in audit.legacy_audit_dirs()}
    assert str((elsewhere.parent / "audit").resolve()) in dirs, dirs


class _FakeSettings:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
