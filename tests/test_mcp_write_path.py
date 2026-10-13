"""The preview -> apply path, driven end to end with nothing real behind it.

The surface tests check what is registered and how it is annotated. The
guardrail tests check the bounds in isolation. Neither runs a tool, so the
sequence that actually moves money -- mint a token, re-read the entity, check
the switch, check the budget, mutate, write the ledger -- had no coverage at
all, and that is where the kill-switch hole below was living.

Everything external is stubbed: the mutating client never exists, `execute`
records instead of sending, and `campaigns.find` returns a fake row. So this is
offline and safe to run anywhere, but it exercises the real `commit()`.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from googleads_mcp import guardrails, tools_write
from googleads_mcp.guardrails import Limits, SessionCounters
from googleads_mcp.previews import PreviewStore
from googleads_reporting.write import campaigns

CAMPAIGN_ID = "12345"


def _limits(**over):
    base = dict(
        max_daily_budget=Decimal("100"), max_budget_change_pct=50.0,
        max_cpc_bid=Decimal("5"), max_bid_increase_pct=50.0,
        max_entities_per_call=25, max_projected_delta_per_day=Decimal("50"),
        max_applies_per_session=3, max_projected_session_delta=Decimal("20"),
    )
    base.update(over)
    return Limits(**base)


class FakeRow:
    """Just enough of a campaign proto row for the status path to read."""

    def __init__(self, status: str = "ENABLED", budget_micros: int = 10_000_000) -> None:
        self.campaign = SimpleNamespace(
            id=int(CAMPAIGN_ID), name="brand - search",
            status=SimpleNamespace(name=status),
        )
        self.campaign_budget = SimpleNamespace(amount_micros=budget_micros)


class FakeRead:
    """`read.rows()` for the 7-day metrics block."""

    def rows(self, _query):
        return [{
            "metrics.cost_micros": "7.00",
            "metrics.clicks": 10,
            "metrics.impressions": 100,
            "metrics.conversions": 1,
        }]


class Registrar:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kw):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


@pytest.fixture
def wired(monkeypatch):
    """The real write tools, with every external edge replaced.

    Returns (tools, recorder). `recorder.mutations` is what would have been
    sent to Google; an empty list after an apply means nothing went out.
    """
    recorder = SimpleNamespace(mutations=[], row=FakeRow())

    monkeypatch.setattr(
        tools_write.MutatingGoogleAdsClient, "from_env",
        classmethod(lambda cls: SimpleNamespace(name="fake-write-client")),
    )
    monkeypatch.setattr(campaigns, "find", lambda *a, **k: recorder.row)
    monkeypatch.setattr(tools_write.campaigns, "find", lambda *a, **k: recorder.row)
    monkeypatch.setattr(
        tools_write.campaigns, "plan_set_status",
        lambda *a, **k: SimpleNamespace(describe=lambda: "plan"),
    )

    def fake_execute(_client, _plan, apply=False):
        recorder.mutations.append({"apply": apply})
        return SimpleNamespace(resource_names=[f"customers/1/campaigns/{CAMPAIGN_ID}"])

    monkeypatch.setattr(tools_write, "execute", fake_execute)

    state = SimpleNamespace(
        limits=_limits(),
        counters=SessionCounters(_limits()),
        previews=PreviewStore(),
        read_client=FakeRead(),
        default_customer_id="1112223333",
        write_client=None,
    )
    registrar = Registrar()
    tools_write.register(registrar, state)
    recorder.state = state
    return registrar.tools, recorder


def _preview_then_apply(tools, status="PAUSED"):
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status=status)
    return tools["apply_campaign_status"](preview_token=preview.preview_token)


# --------------------------------------------------------------------------
# The happy path, so the rest of the file is known to be testing something
# --------------------------------------------------------------------------


def test_a_preview_writes_nothing(wired):
    tools, recorder = wired
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    assert preview.blocked is False
    assert preview.before == "ENABLED" and preview.after == "PAUSED"
    assert recorder.mutations == [], "a preview must not send a mutation"


def test_apply_sends_the_mutation_and_records_the_ledger(wired):
    tools, recorder = wired
    result = _preview_then_apply(tools)

    assert result.applied is True
    assert recorder.mutations == [{"apply": True}]
    assert result.entry_id
    assert result.before == "ENABLED" and result.after == "PAUSED"


# --------------------------------------------------------------------------
# The kill switch. This is the hole this file was written for.
# --------------------------------------------------------------------------


def test_the_kill_switch_stops_an_apply_from_a_preview_taken_before_it(wired, monkeypatch):
    """Throw the switch AFTER minting the token. The apply must still refuse.

    This is the realistic sequence: an operator sees something going wrong and
    creates audit/WRITES_DISABLED while an agent already holds a valid preview.
    Before this was fixed, `check_writes_enabled()` ran only while building the
    preview, so `blocked` was already False and `commit()` never looked again --
    every outstanding token stayed live for its full ten minutes.
    """
    tools, recorder = wired
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    assert preview.blocked is False

    monkeypatch.setattr(guardrails, "writes_disabled", lambda: "paused during the sale")

    with pytest.raises(ToolError, match="kill switch"):
        tools["apply_campaign_status"](preview_token=preview.preview_token)

    assert recorder.mutations == [], "nothing may reach Google once the switch is set"


def test_the_kill_switch_is_still_reported_at_preview_time(wired, monkeypatch):
    """The apply-time check adds to the preview-time one, it does not replace it.

    Catching it early is what puts a readable FAIL line in the text a human
    reads, instead of letting them approve something that then refuses.
    """
    tools, _ = wired
    monkeypatch.setattr(guardrails, "writes_disabled", lambda: "paused during the sale")

    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    assert preview.blocked is True
    assert any("writes_enabled" in line for line in preview.checks), preview.checks


def test_a_blocked_preview_cannot_be_applied(wired, monkeypatch):
    tools, recorder = wired
    monkeypatch.setattr(guardrails, "writes_disabled", lambda: "nope")
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    with pytest.raises(ToolError, match="guardrail"):
        tools["apply_campaign_status"](preview_token=preview.preview_token)
    assert recorder.mutations == []


# --------------------------------------------------------------------------
# The other things commit() is responsible for
# --------------------------------------------------------------------------


def test_a_token_is_single_use(wired):
    tools, recorder = wired
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    tools["apply_campaign_status"](preview_token=preview.preview_token)

    with pytest.raises(ToolError, match="already used"):
        tools["apply_campaign_status"](preview_token=preview.preview_token)
    assert len(recorder.mutations) == 1, "the second apply must not send anything"


def test_an_entity_that_moved_since_the_preview_is_refused(wired):
    """TOCTOU: somebody paused it in the Google Ads UI in between."""
    tools, recorder = wired
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")
    recorder.row = FakeRow(status="PAUSED")

    with pytest.raises(ToolError, match="changed since the preview"):
        tools["apply_campaign_status"](preview_token=preview.preview_token)
    assert recorder.mutations == []


def test_the_session_applies_cap_stops_a_loop(wired):
    """A per-call bound does nothing against a loop of individually-legal calls."""
    tools, recorder = wired
    for _ in range(3):  # max_applies_per_session=3
        _preview_then_apply(tools)
    assert len(recorder.mutations) == 3

    with pytest.raises(ToolError, match="session"):
        _preview_then_apply(tools)
    assert len(recorder.mutations) == 3, "the fourth apply must not reach Google"


def test_a_failed_mutation_is_recorded_as_failed_not_lost(wired, monkeypatch):
    """The ledger must say a write was attempted even when it blew up."""
    from googleads_mcp import ledger

    tools, recorder = wired
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN_ID, new_status="PAUSED")

    def boom(*_a, **_k):
        raise tools_write.MutationError("Google said no")

    monkeypatch.setattr(tools_write, "execute", boom)

    with pytest.raises(ToolError, match="Google said no"):
        tools["apply_campaign_status"](preview_token=preview.preview_token)

    entries = ledger.entries()
    assert entries, "an attempted write must leave a ledger row"
    assert entries[-1]["applied"] is False
    assert "Google said no" in (entries[-1]["error"] or "")


# --------------------------------------------------------------------------
# revert_change. It does not go through commit(), so it needs its own gates --
# and it was missing all three of them.
# --------------------------------------------------------------------------


def _applied_entry(tools, recorder):
    """Pause the campaign for real, so there is something to revert."""
    result = _preview_then_apply(tools)
    recorder.row = FakeRow(status="PAUSED")  # the account now reflects it
    return result.entry_id


def test_revert_puts_the_old_value_back(wired):
    tools, recorder = wired
    entry_id = _applied_entry(tools, recorder)

    result = tools["revert_change"](entry_id=entry_id)

    assert result.applied is True
    assert result.before == "PAUSED" and result.after == "ENABLED"
    assert len(recorder.mutations) == 2


def test_revert_obeys_the_kill_switch(wired, monkeypatch):
    """The switch must stop EVERY write, and revert does not go through commit()."""
    tools, recorder = wired
    entry_id = _applied_entry(tools, recorder)
    monkeypatch.setattr(guardrails, "writes_disabled", lambda: "paused during the sale")

    with pytest.raises(ToolError, match="kill switch"):
        tools["revert_change"](entry_id=entry_id)
    assert len(recorder.mutations) == 1, "the revert must not have been sent"


def test_revert_refuses_when_somebody_else_moved_the_value(wired):
    """Reverting over a third party's change would discard it, not undo ours.

    The ledger would then record a tidy undo of our change while the account
    had actually lost theirs -- the worst shape this can fail in, because
    nothing afterwards looks wrong.
    """
    tools, recorder = wired
    entry_id = _applied_entry(tools, recorder)
    recorder.row = FakeRow(status="ENABLED")  # someone re-enabled it in the UI

    with pytest.raises(ToolError, match="changed it since"):
        tools["revert_change"](entry_id=entry_id)
    assert len(recorder.mutations) == 1


def test_revert_counts_against_the_session_applies_cap(wired):
    """find_entry reaches over the whole ledger, so this loop had no bound."""
    tools, recorder = wired
    entry_id = _applied_entry(tools, recorder)
    tools["revert_change"](entry_id=entry_id)          # 2 of 3
    recorder.row = FakeRow(status="ENABLED")
    _preview_then_apply(tools)                          # 3 of 3
    recorder.row = FakeRow(status="PAUSED")

    with pytest.raises(ToolError, match="session"):
        tools["revert_change"](entry_id=entry_id)


def test_revert_refuses_an_entry_that_was_never_applied(wired):
    from googleads_mcp import ledger

    tools, _ = wired
    entry_id = ledger.new_entry_id()
    ledger.write_intent(
        entry_id=entry_id, tool="apply_campaign_status", customer_id="1112223333",
        entity_type="campaign", entity_id=CAMPAIGN_ID, entity_name="brand - search",
        field_name="status", before="ENABLED", after="PAUSED", projected_delta="0",
    )
    ledger.write_outcome(entry_id=entry_id, ok=False, error="Google said no")

    with pytest.raises(ToolError, match="never applied"):
        tools["revert_change"](entry_id=entry_id)
