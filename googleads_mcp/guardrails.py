"""Hard bounds on what the server will apply, and the session budget.

Three kinds of limit, and the last two matter more:

* **Per-call** -- this budget is not above X, this bid is not a 10x jump. They
  catch a fat finger or a misread number.
* **Per-session** -- at most N applies, at most X total projected daily spend
  added. A per-call limit does nothing against a loop making four hundred
  individually-legal changes, which is the failure mode an agent actually has.
* **Per-day** -- the same two bounds for the local calendar day, counted from
  the ledger rather than from memory. The session counters reset whenever the
  server process restarts, and an agent in continuous use restarts it often --
  one per client window, one per crash -- so on their own they bound a session
  and nothing longer. The daily cap survives restarts and is shared by every
  process writing to logs/google-ads.db.

Values come from ``guardrails.toml`` at the repo root, falling back to the
constants below. A key that is misspelled raises at startup rather than being
ignored, because a silently-ignored ceiling is worse than no ceiling: it reads
as protection that is not there.
"""

from __future__ import annotations

import os
import tomllib
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "guardrails.toml"

#: Written when writes are disabled; its presence is checked before every apply.
KILL_SWITCH = REPO_ROOT / "audit" / "WRITES_DISABLED"

DEFAULTS: dict[str, object] = {
    "max_daily_budget": Decimal("100.00"),
    "max_budget_change_pct": 50.0,
    "max_cpc_bid": Decimal("5.00"),
    "max_bid_increase_pct": 50.0,
    "max_entities_per_call": 25,
    "max_projected_delta_per_day": Decimal("50.00"),
    "max_applies_per_session": 20,
    "max_projected_session_delta": Decimal("150.00"),
    "max_applies_per_day": 40,
    "max_projected_delta_per_day_total": Decimal("200.00"),
    # bidding strategies: a target CPA above this is refused, and a target
    # (CPA or ROAS) may move at most this far in one change -- Google's own
    # guidance is ~20% at a time, or the learning period restarts
    "max_target_cpa": Decimal("100.00"),
    "max_target_change_pct": 25.0,
    # location / device / schedule bid modifiers: +/- this many percent at most
    "max_bid_modifier_pct": 50.0,
}

_DECIMAL_KEYS = {
    "max_daily_budget",
    "max_cpc_bid",
    "max_projected_delta_per_day",
    "max_projected_session_delta",
    "max_projected_delta_per_day_total",
    "max_target_cpa",
}
_FLOAT_KEYS = {
    "max_budget_change_pct", "max_bid_increase_pct", "max_target_change_pct",
    "max_bid_modifier_pct",
}
_INT_KEYS = {"max_entities_per_call", "max_applies_per_session", "max_applies_per_day"}


class WritesDisabled(RuntimeError):
    """The kill switch is set. A deliberate refusal, so its message must survive.

    Its own type rather than a reused one, because this is the refusal an
    operator most needs to read verbatim: it names the file to delete.
    """


class GuardrailConfigError(RuntimeError):
    """guardrails.toml is malformed. Raised at startup, never swallowed."""


@dataclass(frozen=True)
class Limits:
    max_daily_budget: Decimal
    max_budget_change_pct: float
    max_cpc_bid: Decimal
    max_bid_increase_pct: float
    max_entities_per_call: int
    max_projected_delta_per_day: Decimal
    max_applies_per_session: int
    max_projected_session_delta: Decimal
    # Defaults here as well as in DEFAULTS so a Limits built by hand (the
    # tests do) without the newer keys still constructs.
    max_applies_per_day: int = DEFAULTS["max_applies_per_day"]  # type: ignore[assignment]
    max_projected_delta_per_day_total: Decimal = DEFAULTS[  # type: ignore[assignment]
        "max_projected_delta_per_day_total"
    ]
    max_target_cpa: Decimal = DEFAULTS["max_target_cpa"]  # type: ignore[assignment]
    max_target_change_pct: float = DEFAULTS["max_target_change_pct"]  # type: ignore[assignment]
    max_bid_modifier_pct: float = DEFAULTS["max_bid_modifier_pct"]  # type: ignore[assignment]
    allowed_customer_ids: tuple[str, ...] = ()
    allowed_campaign_ids: tuple[str, ...] = ()
    allowed_currencies: tuple[str, ...] = ("CAD",)

    def describe(self) -> dict[str, object]:
        return {
            "max_daily_budget": str(self.max_daily_budget),
            "max_budget_change_pct": self.max_budget_change_pct,
            "max_cpc_bid": str(self.max_cpc_bid),
            "max_bid_increase_pct": self.max_bid_increase_pct,
            "max_entities_per_call": self.max_entities_per_call,
            "max_projected_delta_per_day": str(self.max_projected_delta_per_day),
            "max_applies_per_session": self.max_applies_per_session,
            "max_projected_session_delta": str(self.max_projected_session_delta),
            "max_applies_per_day": self.max_applies_per_day,
            "max_projected_delta_per_day_total": str(self.max_projected_delta_per_day_total),
            "max_target_cpa": str(self.max_target_cpa),
            "max_target_change_pct": self.max_target_change_pct,
            "max_bid_modifier_pct": self.max_bid_modifier_pct,
            "allowed_customer_ids": list(self.allowed_customer_ids) or
                ["(only GOOGLE_ADS_CUSTOMER_ID)"],
            "allowed_campaign_ids": list(self.allowed_campaign_ids) or ["(all)"],
            "allowed_currencies": list(self.allowed_currencies),
        }


def _coerce(key: str, value: object) -> object:
    try:
        if key in _DECIMAL_KEYS:
            return Decimal(str(value))
        if key in _FLOAT_KEYS:
            return float(value)
        if key in _INT_KEYS:
            return int(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GuardrailConfigError(
            f"guardrails.toml: [limits] {key} = {value!r} is not valid."
        ) from exc
    raise GuardrailConfigError(
        f"guardrails.toml: unknown key [limits] {key!r}. "
        f"Known keys: {', '.join(sorted(DEFAULTS))}."
    )


def load_limits(path: Path | None = None) -> Limits:
    """Read guardrails.toml over the defaults."""
    path = path or CONFIG_PATH
    values = dict(DEFAULTS)
    scope: dict[str, object] = {}

    if path.is_file():
        try:
            parsed = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise GuardrailConfigError(f"guardrails.toml is not valid TOML: {exc}") from exc
        for key, value in (parsed.get("limits") or {}).items():
            values[key] = _coerce(key, value)
        scope = parsed.get("scope") or {}

    customers = tuple(str(c).replace("-", "") for c in scope.get("allowed_customer_ids", []))
    campaigns = tuple(str(c) for c in scope.get("allowed_campaign_ids", []))
    currencies = tuple(str(c).upper() for c in scope.get("allowed_currencies", ["CAD"]))

    # An env var can narrow the scope further but never widen it: a machine-local
    # override that could ADD accounts would defeat the committed file.
    env_customers = os.environ.get("GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS", "").strip()
    if env_customers:
        from_env = tuple(
            c.strip().replace("-", "") for c in env_customers.split(",") if c.strip()
        )
        customers = tuple(c for c in from_env if not customers or c in customers)

    return Limits(
        **{k: values[k] for k in DEFAULTS},
        allowed_customer_ids=customers,
        allowed_campaign_ids=campaigns,
        allowed_currencies=currencies,
    )


def writes_disabled() -> str | None:
    """The kill switch: a reason string if writes are off, else None."""
    if KILL_SWITCH.is_file():
        return KILL_SWITCH.read_text().strip() or "writes disabled"
    return None


def disable_writes(reason: str = "") -> Path:
    KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH.write_text(reason or "writes disabled")
    return KILL_SWITCH


@dataclass
class Check:
    """One named bound, and whether this change clears it."""

    name: str
    ok: bool
    detail: str
    #: Deliberately not applied to this change (it still passes). Rendered as
    #: SKIP so a human reading the preview sees the bound was considered and
    #: why it does not apply, rather than an OK that suggests it was checked.
    skipped: bool = False

    def render(self) -> str:
        if self.skipped:
            return f"SKIP {self.name} ({self.detail})"
        return f"{'OK  ' if self.ok else 'FAIL'} {self.name}: {self.detail}"


class SessionCounters:
    """What this server process has already spent of its change budget."""

    def __init__(self, limits: Limits) -> None:
        self._limits = limits
        self.applies = 0
        self.projected_delta = Decimal("0")

    @property
    def limits(self) -> Limits:
        return self._limits

    def would_exceed(self, projected_delta: Decimal, weight: int = 1) -> str | None:
        """Why this apply would break the session budget, or None.

        ``weight`` is what the apply costs: 1 for most, more for a creation.
        """
        if self.applies + weight > self._limits.max_applies_per_session:
            return (
                f"session apply limit reached: {self.applies} of "
                f"{self._limits.max_applies_per_session} already used"
                + (f" and this one costs {weight}" if weight > 1 else "")
                + ". Stop and show the human list_my_changes; they decide "
                "whether this session should do more."
            )
        total = self.projected_delta + max(projected_delta, Decimal("0"))
        if total > self._limits.max_projected_session_delta:
            return (
                f"this change would take the session's projected added daily "
                f"spend to {total}, over the {self._limits.max_projected_session_delta} "
                "ceiling."
            )
        return None

    def record(self, projected_delta: Decimal, weight: int = 1) -> None:
        self.applies += weight
        self.projected_delta += max(projected_delta, Decimal("0"))

    def snapshot(self) -> dict[str, object]:
        return {
            "applies_used": self.applies,
            "applies_remaining": max(
                0, self._limits.max_applies_per_session - self.applies
            ),
            "projected_session_delta": str(self.projected_delta),
            "projected_session_delta_remaining": str(
                max(
                    Decimal("0"),
                    self._limits.max_projected_session_delta - self.projected_delta,
                )
            ),
        }


def local_midnight_utc(now: dt.datetime | None = None) -> str:
    """Today's LOCAL midnight, as the UTC ISO string the ledger stores.

    "Today" is the operator's calendar day, not UTC's: after 19:00 or 20:00 in
    North America the UTC date has already rolled over, and a cap counted in
    UTC would reset in the middle of their evening. The ledger's `at` column is
    UTC ISO (logdb._now()), so the boundary is computed in local time here and
    converted, which keeps the query a plain indexed string comparison.

    A naive datetime's .astimezone() applies the local zone's rules FOR THAT
    instant, so a DST change between midnight and now is accounted for.
    """
    local_now = (now or dt.datetime.now().astimezone()).astimezone()
    midnight = dt.datetime.combine(local_now.date(), dt.time.min)
    return midnight.astimezone().astimezone(dt.timezone.utc).isoformat()


class DailyUsage:
    """Today's applies and projected spend, read back from the ledger.

    No table of its own and no in-memory state: every question is answered by
    the `changes` rows since local midnight, so two server processes -- or a
    process restarted ten times today -- see the same numbers.

    A batch counts once (rows share a batch_id and each carries the whole
    batch's delta and weight), a revert counts as one apply with zero delta,
    and a reduction refunds nothing, exactly as in SessionCounters.
    """

    def __init__(self, limits: Limits) -> None:
        self._limits = limits

    def usage(self) -> tuple[int, Decimal]:
        from googleads_reporting import logdb

        groups: dict[str, dict] = {}
        for row in logdb.successful_changes_since(local_midnight_utc()):
            groups.setdefault(row.get("batch_id") or row["entry_id"], row)
        applies = sum(int(row.get("weight") or 1) for row in groups.values())
        delta = Decimal("0")
        for row in groups.values():
            try:
                value = Decimal(str(row.get("projected_daily_spend_delta") or "0"))
            except InvalidOperation:
                value = Decimal("0")
            delta += max(value, Decimal("0"))
        return applies, delta

    def would_exceed(self, projected_delta: Decimal, weight: int = 1) -> str | None:
        applies, delta = self.usage()
        if applies + weight > self._limits.max_applies_per_day:
            return (
                f"daily apply limit reached: {applies} of "
                f"{self._limits.max_applies_per_day} already used today (local "
                "time, across every server process). It resets at local midnight. "
                "Stop and tell the human; this cap is set in guardrails.toml."
            )
        check = self.check(projected_delta, delta)
        if not check.ok:
            return (
                f"daily spend total reached: this change {check.detail} "
                f"(max_projected_delta_per_day_total = "
                f"{self._limits.max_projected_delta_per_day_total}, local day, across "
                "every server process). It resets at local midnight."
            )
        return None

    def check(self, projected_delta: Decimal, used: Decimal | None = None) -> Check:
        """The daily-total line for a preview: "uses X of today's remaining Y".

        Both figures, always: "over the ceiling" alone does not tell a human
        whether the change is too big or the day is already spent.
        """
        if used is None:
            used = self.usage()[1]
        remaining = max(Decimal("0"), self._limits.max_projected_delta_per_day_total - used)
        wanted = max(projected_delta, Decimal("0"))
        return Check(
            "max_projected_delta_per_day_total",
            wanted <= remaining,
            f"uses {wanted:.2f} of today's remaining {remaining:.2f}",
        )

    def snapshot(self) -> dict[str, object]:
        applies, delta = self.usage()
        return {
            "day_started_at_utc": local_midnight_utc(),
            "applies_used": applies,
            "applies_remaining": max(0, self._limits.max_applies_per_day - applies),
            "projected_delta": str(delta),
            "projected_delta_remaining": str(
                max(Decimal("0"), self._limits.max_projected_delta_per_day_total - delta)
            ),
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_customer_in_scope(limits: Limits, customer_id: str, default: str) -> Check:
    allowed = limits.allowed_customer_ids or (default,)
    ok = customer_id in allowed
    return Check(
        "customer_in_scope",
        ok,
        f"{customer_id} is {'in' if ok else 'NOT in'} scope "
        f"({', '.join(allowed)})",
    )


def check_campaign_in_scope(limits: Limits, campaign_id: str | None) -> Check:
    if not limits.allowed_campaign_ids:
        return Check("campaign_in_scope", True, "no campaign restriction configured")
    ok = campaign_id is not None and str(campaign_id) in limits.allowed_campaign_ids
    return Check(
        "campaign_in_scope",
        ok,
        f"{campaign_id} is {'in' if ok else 'NOT in'} the allowed list",
    )


def check_currency(limits: Limits, currency: str | None) -> Check:
    ok = bool(currency) and currency.upper() in limits.allowed_currencies
    return Check(
        "currency_allowed",
        ok,
        f"{currency or '(unknown)'} against {', '.join(limits.allowed_currencies)}",
    )


def check_ceiling(name: str, value: Decimal, ceiling: Decimal, unit: str = "") -> Check:
    ok = value <= ceiling
    suffix = f" {unit}" if unit else ""
    return Check(name, ok, f"{value}{suffix} against ceiling {ceiling}{suffix}")


def check_pct_increase(
    name: str, before: Decimal | None, after: Decimal, ceiling_pct: float
) -> Check:
    if before is None or before == 0:
        return Check(name, True, "no previous value to compare against")
    pct = float((after - before) / before * 100)
    ok = pct <= ceiling_pct
    return Check(name, ok, f"{pct:+.1f}% against ceiling +{ceiling_pct:.0f}%")


def check_abs_pct_change(
    name: str, before: Decimal | None, after: Decimal, ceiling_pct: float
) -> Check:
    """Like check_pct_increase, but a move in EITHER direction counts.

    For bidding targets, where a big cut is as disruptive as a big raise:
    either one restarts Google's learning.
    """
    if before is None or before == 0:
        return Check(name, True, "no previous target to compare against")
    pct = float((after - before) / before * 100)
    ok = abs(pct) <= ceiling_pct
    return Check(name, ok, f"{pct:+.1f}% against +/-{ceiling_pct:.0f}%")


def check_count(limits: Limits, count: int) -> Check:
    ok = count <= limits.max_entities_per_call
    return Check(
        "entities_per_call", ok, f"{count} against {limits.max_entities_per_call}"
    )


def check_projected_delta(limits: Limits, projected: Decimal) -> Check:
    ok = projected <= limits.max_projected_delta_per_day
    return Check(
        "projected_delta_per_day",
        ok,
        f"{projected} against {limits.max_projected_delta_per_day}",
    )


def check_writes_enabled() -> Check:
    reason = writes_disabled()
    return Check(
        "writes_enabled", reason is None, reason or "kill switch is not set"
    )


READ_ONLY_ENV = "GOOGLE_ADS_MCP_READ_ONLY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def read_only_mode() -> bool:
    """True when this process must publish no `apply_*` tool at all.

    A fourth layer, and the only one that works no matter which client is
    driving. Claude Code can allowlist tools by name, so there "never approve a
    write" is something the client can express. Claude Desktop, Cursor and Codex
    have no equivalent -- their MCP config says which servers to run, not which
    tools may be called -- so for them the only honest way to offer a read-only
    install is for the server to publish no write tools in the first place. An
    unregistered tool is unreachable, exactly as an unwritten one is.

    Read from the PROCESS environment rather than .env, and that is the point:
    every client's MCP config has its own `env` block, so one checkout can be
    read-only for one client and writable for another without two copies of the
    repo or two sets of credentials.

    Not the same thing as the kill switch. WRITES_DISABLED is a file, it stops
    an already-published apply mid-session, and it is account-wide. This is set
    once at startup, per client, and the tools simply never appear.
    """
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in _TRUTHY


def require_writes_enabled() -> None:
    """Raise if the kill switch is set. Call at APPLY time, never cached.

    `check_writes_enabled()` above returns a Check for the preview's list, which
    is the right shape there -- it shows up alongside the other bounds in the
    text a human reads. It is the wrong shape at apply time, because a preview
    is good for ten minutes and the switch's whole purpose is that somebody can
    throw it from another terminal and have the very next apply stop. A switch
    consulted only when the token was minted would let every outstanding preview
    through after it was thrown, which is precisely the moment it is needed.
    """
    reason = writes_disabled()
    if reason:
        raise WritesDisabled(
            f"writes are disabled: {reason}. This is the kill switch, and it was "
            f"set after this preview was taken. Remove {KILL_SWITCH} to re-enable "
            f"writes; do not work around it."
        )


def render_checks(checks: list[Check]) -> list[str]:
    return [c.render() for c in checks]


def any_failed(checks: list[Check]) -> bool:
    return any(not c.ok for c in checks)
