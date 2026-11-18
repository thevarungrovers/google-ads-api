"""Guardrail and preview-token tests. No API calls."""

from decimal import Decimal
from pathlib import Path

import pytest

from googleads_mcp.guardrails import (
    DEFAULTS,
    GuardrailConfigError,
    Limits,
    SessionCounters,
    check_ceiling,
    check_currency,
    check_customer_in_scope,
    check_pct_increase,
    load_limits,
)
from googleads_mcp.previews import (
    PreviewExpired,
    PreviewStore,
    project_bid_change,
    project_budget_change,
    project_pause,
)


def _limits(**over):
    base = dict(
        max_daily_budget=Decimal("100"), max_budget_change_pct=50.0,
        max_cpc_bid=Decimal("5"), max_bid_increase_pct=50.0,
        max_entities_per_call=25, max_projected_delta_per_day=Decimal("50"),
        max_applies_per_session=3, max_projected_session_delta=Decimal("20"),
    )
    base.update(over)
    return Limits(**base)


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------


def test_the_committed_file_loads():
    assert load_limits().max_daily_budget > 0


def test_a_missing_file_falls_back_to_defaults(tmp_path):
    limits = load_limits(tmp_path / "absent.toml")
    assert limits.max_daily_budget == DEFAULTS["max_daily_budget"]


def test_a_misspelled_key_raises_rather_than_being_ignored(tmp_path):
    """A silently-ignored ceiling reads as protection that is not there."""
    path = tmp_path / "g.toml"
    path.write_text('[limits]\nmax_dayly_budget = "10.00"\n')
    with pytest.raises(GuardrailConfigError, match="unknown key"):
        load_limits(path)


def test_an_unparseable_value_raises(tmp_path):
    path = tmp_path / "g.toml"
    path.write_text('[limits]\nmax_daily_budget = "ten dollars"\n')
    with pytest.raises(GuardrailConfigError, match="not valid"):
        load_limits(path)


def test_scope_is_read_and_dashes_are_stripped(tmp_path):
    path = tmp_path / "g.toml"
    path.write_text('[scope]\nallowed_customer_ids = ["123-456-7890"]\n')
    assert load_limits(path).allowed_customer_ids == ("1234567890",)


def test_an_env_override_can_narrow_scope_but_not_widen_it(tmp_path, monkeypatch):
    """A machine-local override that could ADD accounts would defeat the file."""
    path = tmp_path / "g.toml"
    path.write_text('[scope]\nallowed_customer_ids = ["1111111111", "2222222222"]\n')

    monkeypatch.setenv("GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS", "1111111111")
    assert load_limits(path).allowed_customer_ids == ("1111111111",)

    monkeypatch.setenv("GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS", "9999999999")
    assert load_limits(path).allowed_customer_ids == ()


# --------------------------------------------------------------------------
# Session budget -- the limit a per-call bound cannot provide
# --------------------------------------------------------------------------


def test_the_apply_count_is_capped():
    counters = SessionCounters(_limits(max_applies_per_session=2))
    assert counters.would_exceed(Decimal("0")) is None
    counters.record(Decimal("0"))
    counters.record(Decimal("0"))
    assert "session apply limit" in counters.would_exceed(Decimal("0"))


def test_the_session_spend_delta_is_capped():
    """A per-call limit does nothing against many individually-legal changes."""
    counters = SessionCounters(_limits(max_projected_session_delta=Decimal("20")))
    counters.record(Decimal("15"))
    assert counters.would_exceed(Decimal("4")) is None
    assert "projected added daily spend" in counters.would_exceed(Decimal("10"))


def test_a_reduction_does_not_refund_the_session_budget():
    """Otherwise a down-then-up pair would spend the budget twice."""
    counters = SessionCounters(_limits())
    counters.record(Decimal("-50"))
    assert counters.snapshot()["projected_session_delta"] == "0"


def test_the_snapshot_reports_what_is_left():
    counters = SessionCounters(_limits(max_applies_per_session=3))
    counters.record(Decimal("5"))
    snap = counters.snapshot()
    assert snap["applies_used"] == 1
    assert snap["applies_remaining"] == 2


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def test_a_customer_outside_the_scope_fails():
    limits = _limits()
    assert check_customer_in_scope(limits, "1234567890", "1234567890").ok
    object.__setattr__(limits, "allowed_customer_ids", ("1111111111",))
    assert not check_customer_in_scope(limits, "1234567890", "1234567890").ok


def test_an_unlisted_currency_fails():
    limits = _limits()
    object.__setattr__(limits, "allowed_currencies", ("CAD",))
    assert check_currency(limits, "CAD").ok
    assert not check_currency(limits, "USD").ok
    assert not check_currency(limits, None).ok


def test_a_ceiling_is_inclusive():
    assert check_ceiling("x", Decimal("100"), Decimal("100")).ok
    assert not check_ceiling("x", Decimal("100.01"), Decimal("100")).ok


def test_a_percentage_increase_is_measured_against_the_old_value():
    assert check_pct_increase("x", Decimal("1"), Decimal("1.5"), 50.0).ok
    assert not check_pct_increase("x", Decimal("1"), Decimal("2.5"), 50.0).ok
    # A decrease is never an increase.
    assert check_pct_increase("x", Decimal("10"), Decimal("1"), 50.0).ok
    # Nothing to compare against.
    assert check_pct_increase("x", None, Decimal("99"), 50.0).ok


# --------------------------------------------------------------------------
# Preview tokens
# --------------------------------------------------------------------------


@pytest.fixture
def store():
    return PreviewStore()


def _mint(store, *, tool="apply_x", blocked=False):
    return store.mint(
        tool=tool, payload={"before": "1"}, projected_delta=Decimal("0"),
        blocked=blocked,
    )


def test_a_token_is_single_use(store):
    token = _mint(store)
    store.take(token, "apply_x")
    with pytest.raises(PreviewExpired, match="already used"):
        store.take(token, "apply_x")


def test_a_token_is_not_valid_for_another_tool(store):
    token = _mint(store, tool="apply_campaign_status")
    with pytest.raises(PreviewExpired, match="not interchangeable"):
        store.take(token, "apply_campaign_daily_budget")


def test_a_blocked_preview_cannot_be_applied(store):
    token = _mint(store, blocked=True)
    with pytest.raises(PreviewExpired, match="failed a guardrail"):
        store.take(token, "apply_x")


def test_a_blocked_token_is_consumed_so_it_cannot_be_retried(store):
    """Otherwise a blocked change could be retried until something shifted."""
    token = _mint(store, blocked=True)
    with pytest.raises(PreviewExpired, match="failed a guardrail"):
        store.take(token, "apply_x")
    with pytest.raises(PreviewExpired, match="unknown"):
        store.take(token, "apply_x")


def test_an_expired_token_is_refused(store, monkeypatch):
    import googleads_mcp.previews as previews

    token = _mint(store)
    monkeypatch.setattr(previews, "TOKEN_TTL_SECONDS", -1)
    with pytest.raises(PreviewExpired):
        store.take(token, "apply_x")


def test_an_unknown_token_is_refused(store):
    with pytest.raises(PreviewExpired, match="unknown"):
        store.take("not-a-token", "apply_x")


# --------------------------------------------------------------------------
# Spend projections
# --------------------------------------------------------------------------


def test_a_budget_change_projects_exactly():
    assert project_budget_change(Decimal("1"), Decimal("2.5")) == Decimal("1.5")
    assert project_budget_change(Decimal("5"), Decimal("1")) == Decimal("-4")
    assert project_budget_change(None, Decimal("3")) == Decimal("3")


def test_pausing_can_never_increase_spend():
    assert project_pause(Decimal("100")) == Decimal("0")


def test_a_bid_change_projects_from_recent_click_volume():
    # +0.50 on 70 clicks in 7 days = 10 clicks/day = +5.00/day.
    assert project_bid_change(Decimal("1"), Decimal("1.5"), 70) == Decimal("5")


def test_a_bid_change_with_no_clicks_projects_nothing():
    assert project_bid_change(Decimal("1"), Decimal("5"), 0) == Decimal("0")


# --------------------------------------------------------------------------
# The daily cap -- counted from the ledger, so it survives restarts
# --------------------------------------------------------------------------


def _applied(entry_id, *, delta="0", batch_id=None, weight=1, ok=True, at=None):
    from googleads_mcp import ledger
    from googleads_reporting import logdb

    ledger.write_intent(
        entry_id=entry_id, tool="apply_x", customer_id="1", entity_type="campaign",
        entity_id="1", entity_name="c", field_name="status", before="a", after="b",
        projected_delta=delta, batch_id=batch_id, weight=weight,
    )
    ledger.write_outcome(entry_id=entry_id, ok=ok)
    if at is not None:
        logdb.connect().execute("UPDATE changes SET at=? WHERE entry_id=?", (at, entry_id))


def test_local_midnight_is_local_not_utc():
    import datetime as dt

    from googleads_mcp.guardrails import local_midnight_utc

    now = dt.datetime(2026, 11, 18, 22, 30).astimezone()
    bound = dt.datetime.fromisoformat(local_midnight_utc(now))
    local = bound.astimezone()
    assert (local.hour, local.minute, local.date()) == (0, 0, now.date())
    assert bound.utcoffset() == dt.timedelta(0), "stored in UTC, like ledger.at"


def test_the_daily_cap_counts_only_todays_successful_applies():
    from googleads_mcp.guardrails import DailyUsage

    _applied("today-1", delta="5")
    _applied("today-2", delta="-30")          # a reduction refunds nothing
    _applied("failed", delta="40", ok=False)  # Google refused it: spent nothing
    _applied("yesterday", delta="40", at="2000-01-01T00:00:00+00:00")

    assert DailyUsage(_limits()).usage() == (2, Decimal("5"))


def test_a_batch_counts_once_and_a_creation_counts_its_weight():
    from googleads_mcp.guardrails import DailyUsage

    for n in range(5):
        _applied(f"kw-{n}", delta="3", batch_id="batch-1")
    _applied("new-campaign", delta="0", weight=3)

    assert DailyUsage(_limits()).usage() == (4, Decimal("3"))


def test_the_daily_cap_is_shared_by_separate_processes():
    """Two server processes, one ledger: the second sees what the first did.

    That is the whole point of counting from the database. Separate
    SessionCounters stand in for separate processes; each thinks it has done
    nothing, and the day still says no.
    """
    from googleads_mcp.guardrails import DailyUsage

    limits = _limits(max_applies_per_day=2, max_applies_per_session=10)
    first, second = SessionCounters(limits), SessionCounters(limits)
    _applied("from-process-1")
    first.record(Decimal("0"))
    _applied("from-process-2")
    second.record(Decimal("0"))

    assert first.would_exceed(Decimal("0")) is None
    assert second.would_exceed(Decimal("0")) is None
    refusal = DailyUsage(limits).would_exceed(Decimal("0"))
    assert refusal and "daily apply limit" in refusal and "midnight" in refusal


def test_the_daily_spend_ceiling_refuses_the_change_that_crosses_it():
    from googleads_mcp.guardrails import DailyUsage

    limits = _limits(max_projected_delta_per_day_total=Decimal("10"))
    _applied("a", delta="8")
    usage = DailyUsage(limits)
    assert usage.would_exceed(Decimal("2")) is None
    refusal = usage.would_exceed(Decimal("2.01"))
    assert "uses 2.01 of today's remaining 2.00" in refusal
    snap = usage.snapshot()
    assert snap["applies_used"] == 1 and snap["projected_delta_remaining"] == "2"


def test_the_session_refusal_no_longer_says_restart():
    """Restarting resets the session counter -- advising it defeats the cap."""
    counters = SessionCounters(_limits(max_applies_per_session=1))
    counters.record(Decimal("0"))
    assert "restart" not in counters.would_exceed(Decimal("0")).lower()


def test_a_weighted_apply_needs_room_for_its_whole_weight():
    counters = SessionCounters(_limits(max_applies_per_session=3))
    counters.record(Decimal("0"))
    assert counters.would_exceed(Decimal("0"), weight=2) is None
    assert counters.would_exceed(Decimal("0"), weight=3)


def test_startup_warns_when_a_budget_is_over_the_daily_total():
    from types import SimpleNamespace

    from googleads_mcp.server import budget_ceiling_warning

    class Read:
        def __init__(self, amount):
            self.amount = amount

        def rows(self, _query):
            return [{"campaign.id": 7, "campaign.name": "x",
                     "campaign_budget.amount_micros": self.amount}]

    limits = _limits(max_projected_delta_per_day_total=Decimal("150"))
    assert budget_ceiling_warning(SimpleNamespace(read_client=Read(100.0), limits=limits)) is None
    warning = budget_ceiling_warning(SimpleNamespace(read_client=Read(250.0), limits=limits))
    assert "250.00" in warning and "could not be re-enabled" in warning


def test_build_state_reads_the_naming_identity_from_dotenv(tmp_path, monkeypatch):
    from googleads_mcp import server
    from googleads_reporting import naming

    (tmp_path / ".env").write_text(
        "GOOGLE_ADS_MCP_OWNER_INITIALS=AB\nGOOGLE_ADS_MCP_AGENT_NAME=max\n")
    monkeypatch.setattr(naming, "IDENTITY_FILES", (tmp_path / ".env",))
    monkeypatch.setenv("GOOGLE_ADS_MCP_AGENT_NAME", "bot")
    state = server.build_state()
    assert state.identity.prefix == "AB-max-"
    warnings = server.identity_warnings(state)
    assert len(warnings) == 1 and "GOOGLE_ADS_MCP_AGENT_NAME" in warnings[0]
    assert "bot" not in warnings[0]


def test_a_malformed_identity_is_a_startup_error_not_a_crash(tmp_path, monkeypatch):
    from googleads_mcp import server
    from googleads_reporting import naming

    (tmp_path / ".env").write_text("GOOGLE_ADS_MCP_OWNER_INITIALS=a1\n")
    monkeypatch.setattr(naming, "IDENTITY_FILES", (tmp_path / ".env",))
    state = server.build_state()
    assert not state.identity.valid
    assert any(w.startswith("ERROR: GOOGLE_ADS_MCP_OWNER_INITIALS")
               for w in server.identity_warnings(state))


@pytest.mark.parametrize("owner, agent, prefix", [
    ("AB", "max", "AB-max-"), ("AB", "", "AB-"), ("", "max", "max-"), ("", "", ""),
])
def test_get_guardrails_shows_which_identity_parts_are_set(monkeypatch, owner, agent, prefix):
    from googleads_reporting.naming import Identity

    identity = Identity.from_values(owner, agent, source=".env")
    out = _guardrails_with(monkeypatch, identity)
    assert out["identity"]["prefix"] == prefix
    assert out["identity"]["owner_initials"] == (owner or None)
    assert out["identity"]["agent_name"] == (agent or None)
    assert out["identity"]["problems"] == []


def _guardrails_with(monkeypatch, identity):
    from types import SimpleNamespace

    from googleads_mcp import tools_read
    from googleads_mcp.guardrails import SessionCounters
    from googleads_mcp.previews import PreviewStore
    from googleads_reporting.client import ReadOnlyGoogleAdsClient
    from kindkit import ReadAccount, Registrar, limits, read_client

    client = read_client(ReadAccount())
    monkeypatch.setattr(ReadOnlyGoogleAdsClient, "from_env", classmethod(lambda cls: client))
    lim = limits()
    state = SimpleNamespace(limits=lim, counters=SessionCounters(lim), previews=PreviewStore(),
                            identity=identity)
    registrar = Registrar()
    tools_read.register(registrar, state)
    return registrar.tools["get_guardrails"]()
