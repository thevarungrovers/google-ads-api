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


def test_a_second_process_on_the_same_ledger_is_held_to_the_same_day(rig):
    """build_state() twice, one database: the daily cap is shared."""
    engine, kind, account = rig
    tight = limits(max_applies_per_day=1, max_applies_per_session=10)
    engine.ctx.state.limits = tight
    engine.ctx.state.counters = SessionCounters(tight)
    engine.apply(kind, engine.preview(kind, {"words": ["a"]}).preview_token)

    other_state = SimpleNamespace(
        limits=tight, counters=SessionCounters(tight), previews=PreviewStore(),
        read_client=None, default_customer_id="1112223333",
    )
    other = Engine(WriteContext(other_state, client=None, execute=engine.ctx._execute))
    other_kind = other.add(AddWords(account))
    preview = other.preview(other_kind, {"words": ["b"]})
    assert any("daily apply limit" in w for w in preview.warnings)

    with pytest.raises(PreviewExpired, match="daily apply limit"):
        other.apply(other_kind, preview.preview_token)
    assert account.keywords == {"a"}


# -- naming: the engine's half -------------------------------------------


class NameThing(ChangeKind):
    """Creates one named thing; ``stamped=False`` plays a kind that forgot."""

    name = "name_thing"
    preview_title = "Preview naming a thing"
    apply_title = "Apply naming a thing"
    ledger_keys = (("label", "created"),)
    creates_named = ("label",)

    def propose(self, ctx, name: str, stamped: bool = True, sent: str | None = None,
                declared: str | None = None) -> Proposal:
        """Create a named thing. Writes nothing.

        ``sent`` plays a kind whose request carries a name other than the
        one it stamped; ``declared`` one whose created_names lie."""
        names = [ctx.name_for_create("label", name)] if stamped else []
        final = names[0].final if names else name
        return Proposal(
            entity_type="label", entity_id=f"new:{final}", entity_name=final, path=final,
            field_name="created", before="(none)", after=final, checks=[],
            projected=Decimal("0"), payload={"name": sent if sent is not None else final},
            names=names, created_names=[("label", declared if declared is not None else final)],
        )

    def current(self, ctx, payload):
        return "(none)"

    def build(self, ctx, payload):
        from kindkit import RAW

        operation = RAW.get_type("LabelOperation")
        operation.create.name = payload["name"]
        return PlannedChange(action="create", entity="label", label=payload["name"],
                             service="S", method="m", operations=[operation])


def naming_rig(owner="", agent=""):
    from googleads_reporting.naming import Identity

    def execute(_client, plan, apply=False):
        return SimpleNamespace(resource_names=["labels/1"] if apply else [])

    state = SimpleNamespace(
        limits=limits(), counters=SessionCounters(limits()), previews=PreviewStore(),
        read_client=None, default_customer_id="1112223333",
        identity=Identity.from_values(owner, agent, source=".env"),
    )
    engine = Engine(WriteContext(state, client=None, execute=execute))
    return engine, engine.add(NameThing())


def fails(preview, name):
    return any(line.startswith("FAIL") and f" {name}:" in line for line in preview.checks)


@pytest.mark.parametrize("owner, agent, final", [
    ("AB", "max", "AB-max-x"), ("AB", "", "AB-x"), ("", "max", "max-x"), ("", "", "x"),
])
def test_a_stamped_create_shows_the_final_name_and_records_the_identity(owner, agent, final):
    engine, kind = naming_rig(owner, agent)
    preview = engine.preview(kind, {"name": "x"})
    assert not preview.blocked, preview.checks
    assert preview.after == final and preview.naming["final"] == final
    assert preview.naming["prefix"] == final[:-1]
    result = engine.apply(kind, preview.preview_token)
    detail = ledger.find_entry(result.entry_id)["detail"]
    assert detail["identity"] == {"owner": owner, "agent": agent, "source": ".env"}
    assert detail["requested_name"] == "x"
    assert detail["created_names"] == [["label", final]]


def test_the_backstop_fails_a_create_that_skipped_the_prefix():
    engine, kind = naming_rig("AB", "max")
    preview = engine.preview(kind, {"name": "x", "stamped": False})
    assert preview.blocked and fails(preview, "name_prefix")


def test_with_no_prefix_configured_the_backstop_does_not_apply():
    engine, kind = naming_rig()
    preview = engine.preview(kind, {"name": "x", "stamped": False})
    assert not preview.blocked
    assert not any(" name_prefix:" in line for line in preview.checks)


def test_a_malformed_identity_blocks_the_create_naming_the_env_key():
    engine, kind = naming_rig("a1", "max")
    preview = engine.preview(kind, {"name": "x"})
    assert preview.blocked and fails(preview, "agent_identity")
    assert any("GOOGLE_ADS_MCP_OWNER_INITIALS" in line for line in preview.checks)


def test_an_over_length_name_is_refused_not_truncated():
    engine, kind = naming_rig("AB", "max")
    preview = engine.preview(kind, {"name": "y" * 80})
    assert preview.blocked and fails(preview, "name_length")
    assert preview.after == "AB-max-" + "y" * 80


# -- the backstop reads what is BUILT, not what the kind declared ------------


def test_the_backstop_fails_a_request_that_sends_an_unstamped_name():
    """The kind stamped and declared AB-max-x, but its request carries "x"."""
    engine, kind = naming_rig("AB", "max")
    preview = engine.preview(kind, {"name": "x", "sent": "x"})
    assert preview.blocked and fails(preview, "name_prefix")


def test_the_backstop_fails_a_request_that_differs_from_the_preview():
    engine, kind = naming_rig("AB", "max")
    preview = engine.preview(kind, {"name": "x", "sent": "AB-max-y"})
    assert preview.blocked and fails(preview, "names_sent")
    assert any("AB-max-y" in line for line in preview.checks)


def test_the_backstop_fails_a_declaration_that_hides_the_real_name():
    """created_names is a declaration; the request is the truth."""
    engine, kind = naming_rig("AB", "max")
    preview = engine.preview(kind, {"name": "x", "stamped": False, "declared": "AB-max-x"})
    assert preview.blocked and fails(preview, "name_prefix")


def test_the_backstop_fails_a_sent_name_over_the_limit():
    engine, kind = naming_rig("AB", "max")
    long = "AB-max-" + "y" * 80
    preview = engine.preview(kind, {"name": "x", "sent": long, "declared": long})
    assert preview.blocked and fails(preview, "names_sent")


def test_the_backstop_fails_a_sent_name_forging_another_identity_with_no_prefix():
    engine, kind = naming_rig()
    preview = engine.preview(kind, {"name": "x", "sent": "CD-bot-x", "declared": "CD-bot-x"})
    assert preview.blocked and fails(preview, "names_sent")


def test_a_request_that_cannot_be_built_fails_under_its_own_reason():
    """A bad input (an image of the wrong shape) is not a naming problem."""
    engine, kind = naming_rig("AB", "max")
    kind.build = lambda ctx, payload: (_ for _ in ()).throw(ValueError("hero.jpg is 0.50:1"))
    preview = engine.preview(kind, {"name": "x"})
    assert preview.blocked and fails(preview, "request")
    assert not fails(preview, "names_sent")
    assert any("hero.jpg is 0.50:1" in line for line in preview.checks)


def test_the_backstop_passes_a_faithful_request():
    engine, kind = naming_rig("AB", "max")
    preview = engine.preview(kind, {"name": "x"})
    assert not preview.blocked, preview.checks
    assert any(line.startswith("OK") and " names_sent:" in line for line in preview.checks)
