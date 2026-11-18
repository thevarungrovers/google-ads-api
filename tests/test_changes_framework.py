"""The ChangeKind engine, exercised with a fake kind and nothing real behind it.

test_mcp_write_path.py drives the four original pairs through the engine; this
file pins what those four never exercise: batches (one ledger row per entity,
one shared batch_id, one apply against the caps), batch revert, and the
generated tool signatures the MCP SDK builds schemas from.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated, Any

import pytest
from pydantic import Field

from googleads_mcp import ledger
from googleads_mcp.changes.base import ChangeKind, LedgerItem, Proposal
from googleads_mcp.changes.context import WriteContext
from googleads_mcp.changes.engine import Engine, merge_plans, register_pair
from googleads_mcp.guardrails import Limits, SessionCounters
from googleads_mcp.previews import PreviewExpired, PreviewStale, PreviewStore
from googleads_reporting.write.plan import PlannedChange


def limits(**over) -> Limits:
    base = dict(
        max_daily_budget=Decimal("100"), max_budget_change_pct=50.0,
        max_cpc_bid=Decimal("5"), max_bid_increase_pct=50.0,
        max_entities_per_call=3, max_projected_delta_per_day=Decimal("50"),
        max_applies_per_session=5, max_projected_session_delta=Decimal("20"),
    )
    base.update(over)
    return Limits(**base)


class Account:
    """What the fake kind reads and writes: a set of keyword texts."""

    def __init__(self) -> None:
        self.keywords: set[str] = set()
        self.sent: list[tuple[bool, PlannedChange]] = []


class AddWords(ChangeKind):
    name = "add_words"
    preview_title = "Preview adding words"
    apply_title = "Apply adding words"
    ledger_keys = (("word", "presence"),)

    def __init__(self, account: Account) -> None:
        self.account = account

    def propose(
        self,
        ctx,
        words: Annotated[list[str], Field(description="Words to add")],
    ) -> Proposal:
        """Add some words. Writes nothing."""
        items = [
            LedgerItem("word", w, w, "presence", "absent", "present", {"word": w})
            for w in words
        ]
        return Proposal(
            entity_type="word", entity_id=",".join(words), entity_name=f"{len(words)} words",
            path="account", field_name="presence", before=self.current(ctx, {"words": words}),
            after="present", checks=[], projected=Decimal("1"),
            payload={"words": words}, items=items, batch=True,
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return "present" if set(payload["words"]) <= self.account.keywords else "absent"

    def build(self, ctx, payload):
        return PlannedChange(
            action="create", entity="word", label="words", service="S", method="m",
            operations=[("add", w) for w in payload["words"]],
        )

    def current_for_entry(self, ctx, entry):
        return "present" if entry["entity_id"] in self.account.keywords else "absent"

    def inverse(self, ctx, entry):
        return PlannedChange(
            action="remove", entity="word", label=entry["entity_id"], service="S",
            method="m", operations=[("remove", entry["entity_id"])],
        )


@pytest.fixture
def rig():
    account = Account()

    def execute(_client, plan, apply=False):
        account.sent.append((apply, plan))
        if apply:
            for verb, word in plan.operations:
                (account.keywords.add if verb == "add" else account.keywords.discard)(word)
        return SimpleNamespace(
            resource_names=[f"words/{w}" for _, w in plan.operations] if apply else []
        )

    state = SimpleNamespace(
        limits=limits(), counters=SessionCounters(limits()), previews=PreviewStore(),
        read_client=None, default_customer_id="1112223333",
    )
    engine = Engine(WriteContext(state, client=None, execute=execute))
    kind = engine.add(AddWords(account))
    return engine, kind, account


def test_a_batch_writes_one_row_per_entity_sharing_one_batch_id(rig):
    engine, kind, account = rig
    preview = engine.preview(kind, {"words": ["a", "b", "c"]})
    assert preview.blocked is False
    assert [i["entity_id"] for i in preview.items] == ["a", "b", "c"]

    result = engine.apply(kind, preview.preview_token)

    rows = ledger.batch_entries(result.batch_id)
    assert [r["entity_id"] for r in rows] == ["a", "b", "c"]
    assert [r["resource_name"] for r in rows] == ["words/a", "words/b", "words/c"]
    assert all(r["detail"] == {"word": r["entity_id"]} for r in rows)
    assert result.entry_ids == [r["entry_id"] for r in rows]
    # One validate_only round trip at preview, then the batch as ONE request...
    assert [apply for apply, _ in account.sent] == [False, True]
    # ...and cost ONE apply, not three.
    assert engine.ctx.counters.applies == 1


def test_a_batch_over_max_entities_per_call_is_blocked(rig):
    engine, kind, account = rig
    preview = engine.preview(kind, {"words": ["a", "b", "c", "d"]})
    assert preview.blocked is True
    assert any("entities_per_call" in line and "FAIL" in line for line in preview.checks)
    with pytest.raises(PreviewExpired, match="guardrail"):
        engine.apply(kind, preview.preview_token)
    assert account.sent == []


def test_one_entry_of_a_batch_can_be_reverted_alone(rig):
    engine, kind, account = rig
    result = engine.apply(kind, engine.preview(kind, {"words": ["a", "b"]}).preview_token)

    engine.revert(result.entry_ids[0])

    assert account.keywords == {"b"}


def test_a_whole_batch_reverts_in_one_request(rig):
    engine, kind, account = rig
    result = engine.apply(kind, engine.preview(kind, {"words": ["a", "b"]}).preview_token)

    reverted = engine.revert(result.batch_id)

    assert account.keywords == set()
    applied = [plan for apply, plan in account.sent if apply]
    assert len(applied) == 2, "the revert must be ONE request"
    assert len(reverted.entry_ids) == 2
    reverted_ids = ledger.reverted_entry_ids()
    assert set(result.entry_ids) <= reverted_ids


def test_a_batch_revert_skips_entries_already_reverted(rig):
    engine, kind, account = rig
    result = engine.apply(kind, engine.preview(kind, {"words": ["a", "b"]}).preview_token)
    engine.revert(result.entry_ids[0])

    engine.revert(result.batch_id)

    assert account.keywords == set()
    with pytest.raises(ValueError, match="nothing left to revert"):
        engine.revert(result.batch_id)


def test_a_batch_revert_refuses_when_one_entry_moved(rig):
    """Atomic or not at all: half a batch reverted is a state nobody asked for."""
    engine, kind, account = rig
    result = engine.apply(kind, engine.preview(kind, {"words": ["a", "b"]}).preview_token)
    account.keywords.discard("b")  # somebody removed it in the UI

    with pytest.raises(PreviewStale):
        engine.revert(result.batch_id)
    assert account.keywords == {"a"}


def test_merge_refuses_plans_that_cannot_travel_together():
    one = PlannedChange("a", "e", "l", "S", "m", operations=[1])
    other = PlannedChange("a", "e", "l", "T", "m", operations=[2])
    assert merge_plans([one, one]).operations == [1, 1]
    with pytest.raises(ValueError, match="one entry_id at a time"):
        merge_plans([one, other])


def test_the_generated_preview_has_the_kinds_signature(rig):
    engine, _, account = rig
    tools: dict[str, Any] = {}

    class Registrar:
        def tool(self, **_kw):
            def decorate(fn):
                tools[fn.__name__] = fn
                return fn
            return decorate

    fresh = Engine(engine.ctx)
    register_pair(
        Registrar(), fresh, AddWords(account),
        refusals=lambda fn: fn, apply_tool=Registrar().tool,
    )
    preview = tools["preview_add_words"]
    assert list(inspect.signature(preview).parameters) == ["words"]
    assert preview.__doc__.startswith("Add some words")
    assert list(inspect.signature(tools["apply_add_words"]).parameters) == ["preview_token"]


def test_a_kind_cannot_be_registered_twice(rig):
    engine, kind, _ = rig
    with pytest.raises(ValueError, match="twice"):
        engine.add(kind)
