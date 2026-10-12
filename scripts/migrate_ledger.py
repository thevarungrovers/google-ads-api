#!/usr/bin/env python3
"""Move the retired JSONL ledgers into ``logs/google-ads.db``.

    .venv/bin/python scripts/migrate_ledger.py            # show what would move
    .venv/bin/python scripts/migrate_ledger.py --apply    # move it
    .venv/bin/python scripts/migrate_ledger.py --verify   # compare afterwards

This repo kept TWO, with different shapes and different homes:

  audit/mcp-changes.jsonl       what the MCP server changed  -> changes
  audit/mutations-<date>.jsonl  what the library sent        -> mutations

The second is the awkward one. It used to be written to
``settings.output_dir.parent / "audit"`` -- a path DERIVED from a configurable
setting -- so a run with GOOGLE_ADS_OUTPUT_DIR pointed elsewhere left its
mutations outside the repo entirely. This script looks in both places and says
which it found.

It also globs ``*.jsonl*`` rather than ``*.jsonl``, so hand-made sidecar copies
are picked up. A ledger that was ever rewritten by hand leaves copies beside
it, and those copies can hold entries the live file no longer has.

Idempotent: an id already in the database is left exactly as it is, so a re-run
after new writes cannot roll one back to its state at migration time.

Nothing is deleted. The JSONL stays on disk, frozen and unwritten, as the
fallback -- which is why --verify exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleads_mcp import ledger  # noqa: E402
from googleads_reporting import logdb  # noqa: E402
from googleads_reporting.write import audit  # noqa: E402


def _changes_preview() -> int:
    folded = ledger.fold_legacy_records()
    if not folded:
        print(f"changes:   nothing at {ledger.LEGACY_LEDGER_PATH}")
        return 0
    conn = logdb.connect()
    already = sum(
        1
        for e in folded
        if conn.execute(
            "SELECT 1 FROM changes WHERE entry_id=?", (str(e["entry_id"]),)
        ).fetchone()
        is not None
    )
    print(f"changes:   {ledger.LEGACY_LEDGER_PATH}")
    print(f"           {len(folded)} entries, {already} already in, "
          f"{len(folded) - already} to insert")
    unfinished = sum(1 for e in folded if not e.get("applied"))
    if unfinished:
        print(f"           {unfinished} never recorded as applied -- they migrate "
              f"that way, which is correct")
    return len(folded) - already


def _mutations_preview() -> int:
    paths = audit.legacy_mutation_paths()
    if not paths:
        print("mutations: no retired mutations-*.jsonl found")
        return 0
    conn = logdb.connect()
    total = 0
    dirs = {p.parent for p in paths}
    print(f"mutations: {len(paths)} file(s) across {len(dirs)} director"
          f"{'y' if len(dirs) == 1 else 'ies'}")
    for directory in sorted(dirs):
        outside = directory.resolve() != audit.LEGACY_AUDIT_DIR.resolve()
        print(f"           {directory}{'   <-- OUTSIDE the repo' if outside else ''}")
    for path in paths:
        folded = audit.fold_legacy_mutations(path)
        already = sum(
            1
            for e in folded
            if conn.execute(
                "SELECT 1 FROM mutations WHERE correlation_id=?", (str(e["id"]),)
            ).fetchone()
            is not None
        )
        total += len(folded) - already
        print(f"           {path.name}: {len(folded)} records, "
              f"{len(folded) - already} to insert")
    return total


def preview() -> int:
    print(f"target:    {logdb.DB_PATH}\n")
    pending = _changes_preview() + _mutations_preview()
    print(f"\n{pending} record(s) would be inserted."
          f"{' Add --apply to do it.' if pending else ''}")
    return 0


def apply() -> int:
    print(f"target: {logdb.DB_PATH}\n")
    result = ledger.import_legacy_jsonl()
    print(f"changes:   read {result['read']}, inserted {result['inserted']}, "
          f"already present {result['already_present']}")

    for path in audit.legacy_mutation_paths():
        outcome = audit.import_legacy_mutations(path)
        print(f"mutations: {path.name}: read {outcome['read']}, "
              f"inserted {outcome['inserted']}, "
              f"already present {outcome['already_present']}")

    print("\nNothing was deleted. Nothing writes to the JSONL any more.")
    return 0


def verify() -> int:
    """Every JSONL record must now be in the database, with the same outcome."""
    problems = 0

    folded = ledger.fold_legacy_records()
    missing = [e for e in folded if logdb.find_change(str(e["entry_id"])) is None]
    mismatched = [
        e
        for e in folded
        if (row := logdb.find_change(str(e["entry_id"]))) is not None
        and bool(row.get("applied")) != bool(e.get("applied"))
    ]
    print(f"changes:   {len(folded)} in the JSONL, "
          f"{len(logdb.change_entries())} rows in the database")
    for entry in missing:
        print(f"           MISSING {entry['entry_id']}")
    for entry in mismatched:
        print(f"           APPLIED DIFFERS {entry['entry_id']}")
    problems += len(missing) + len(mismatched)

    conn = logdb.connect()
    for path in audit.legacy_mutation_paths():
        folded = audit.fold_legacy_mutations(path)
        absent = [
            e
            for e in folded
            if conn.execute(
                "SELECT 1 FROM mutations WHERE correlation_id=?", (str(e["id"]),)
            ).fetchone()
            is None
        ]
        print(f"mutations: {path.name}: {len(folded)} in the JSONL, {len(absent)} missing")
        for entry in absent:
            print(f"           MISSING {entry['id']}")
        problems += len(absent)

    print("\n" + ("Every JSONL record is present." if not problems
                  else f"{problems} problem(s)."))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="perform the migration")
    group.add_argument(
        "--verify", action="store_true", help="check the database against the JSONL"
    )
    args = parser.parse_args(argv)

    if args.apply:
        return apply()
    if args.verify:
        return verify()
    return preview()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
