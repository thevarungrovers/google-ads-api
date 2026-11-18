"""The shapes every change kind speaks in.

A *kind* is one sort of change -- a campaign's status, an ad group's bid, a
batch of negative keywords. It knows four things and nothing else:

  * how to read the entity and turn the agent's arguments into a
    :class:`Proposal` (before, after, the bounds it has to clear, the projected
    spend, and a JSON-able ``payload`` the apply will need);
  * how to read the CURRENT value again at apply time, so a change somebody
    made in the Google Ads UI in between is caught rather than overwritten;
  * how to build the mutation from that payload;
  * how to build the mutation that undoes one ledger entry.

Everything else -- the token, the kill switch, the session and daily caps, the
ledger's two phases, the ``validate_only`` round trip, the ToolError mapping --
lives once, in :mod:`googleads_mcp.changes.engine`. That split is the point:
before it, each of the four original pairs hand-rolled its own copy of the
apply sequence, and the one that drifted (``revert_change``) was missing three
of its gates. A new kind cannot forget a gate it never implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.types import ToolAnnotations

from googleads_mcp.guardrails import Check
from googleads_reporting.fields import from_micros

# All four hints, always -- see the note in tools_read.py for why an omitted
# hint is worse than a wrong one.
#
# A preview reads the account, mints a token in memory and writes nothing, so it
# is safe to allowlist. The token is single-use, but calling a preview twice just
# produces a second token and changes nothing anybody can observe at Google's
# end, so idempotent_hint stays True. The validate_only round trip it makes is
# a server-side CHECK, not a write: Google evaluates the request and discards it.
PREVIEW_SAFE = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

#: destructive_hint is set only where the change moves money, stops delivery or
#: creates something. Marking a reversible action destructive would train
#: someone to click through the loud prompts, which is how loud prompts stop
#: working.
APPLY_SPENDS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

#: Reverting restores a previous value, and a low-risk change (a label, a
#: tracking template) cannot move money: writes, but not new risk.
APPLY_REVERSIBLE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

SETTABLE_STATUSES = ("ENABLED", "PAUSED")

#: What "before" reads as for something that does not exist yet. A creation's
#: staleness check compares against this, so it must never be a value a real
#: entity could hold.
NOTHING = "(none)"


def money(micros_value: Any) -> Decimal:
    """A raw micros field from a proto row, as currency units.

    *** THIS IS NOT OPTIONAL. *** `ReadOnlyGoogleAdsClient.rows()` returns
    flattened dicts with money ALREADY converted, but `.query()` -- which
    campaigns.find() and adgroups.find() use -- returns the raw proto, where a
    $1.00 budget is the integer 1000000. Reading one of those straight into a
    preview renders "1000000.00 CAD" as the before-value in the text a human is
    about to approve, and turns every derived number (the delta, the percentage
    change) into nonsense in the same breath.
    """
    return Decimal(str(from_micros(int(micros_value or 0))))


def parse_decimal(raw: str, name: str, *, ceiling: Decimal = Decimal("1000")) -> Decimal:
    """Money arrives as a decimal string in major units, never as micros.

    The argument is rendered verbatim in the prompt a human approves, so "2.50"
    has to mean two fifty. Accepting a float here would also let 0.1 + 0.2
    arrive as 0.30000000000000004 in the approval text.
    """
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(
            f"{name} must be a decimal string in the account's currency, "
            f'e.g. "2.50" -- got {raw!r}. Never pass micros.'
        ) from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be a finite number, got {raw!r}.")
    if value < 0:
        raise ValueError(f"{name} cannot be negative, got {value}.")
    if value > ceiling:
        # 2500000 is what you get typing $2.50 as micros. It is a valid Decimal
        # and a catastrophic budget, so it is caught before the ceiling check,
        # where the message can actually say what went wrong.
        raise ValueError(
            f"{name} of {value} looks like micros, not currency. "
            f"If you meant {value / 1_000_000}, pass that."
        )
    return value


def with_currency(amount: Decimal, currency: str) -> str:
    """How money is written into before/after, and so into the ledger.

    One spelling everywhere, because staleness and revert compare these strings
    exactly: "2.50 CAD" from the preview must equal "2.50 CAD" read back at
    apply time, and a revert parses the number back off the front.
    """
    return f"{amount:.2f} {currency}".strip()


def amount_of(rendered: str) -> Decimal:
    """The number off the front of a :func:`with_currency` string."""
    return Decimal(rendered.split()[0])


def _decimal(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


@dataclass
class LedgerItem:
    """One row in the ledger: one entity, one field, one before/after.

    A batch writes one of these per entity, all sharing a ``batch_id``, so any
    single keyword of twenty can be reverted on its own and the batch can be
    reverted as one.

    ``detail`` is whatever the inverse needs that before/after cannot carry --
    for a created criterion, which ad group or campaign it sits in. The created
    resource name itself is filled in from Google's response after the apply,
    so it never has to be guessed.
    """

    entity_type: str
    entity_id: str
    entity_name: str
    field_name: str
    before: str
    after: str
    detail: dict[str, Any] | None = None


@dataclass
class Proposal:
    """What a kind wants to do, before the engine checks and tokenises it."""

    entity_type: str
    entity_id: str
    entity_name: str
    path: str
    field_name: str
    before: str
    after: str
    checks: list[Check]
    projected: Decimal
    #: Everything the apply needs, carried in the token. JSON-able values only:
    #: the token outlives the request that minted it.
    payload: dict[str, Any]
    campaign_status: str | None = None
    metrics: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    note: str | None = None
    #: One per ledger row. Empty means "one row, described by the fields above".
    items: list[LedgerItem] = field(default_factory=list)
    #: The campaign this touches, for the campaign scope check. None when the
    #: change is account-level (a shared negative list, a label).
    campaign_id: str | None = None
    #: Whether this is a batch, so the engine adds the entities_per_call check.
    batch: bool = False
    #: An ENABLE -- resuming delivery of something paused. Its projection is
    #: the whole daily budget it can resume, which on any real campaign is
    #: over the per-change max_projected_delta_per_day; if that limit applied,
    #: a campaign with a budget over it could never be re-enabled. So an
    #: enable is exempt from the per-change limit and counted, in full,
    #: against the DAILY total (and the session total) instead. Bid and budget
    #: increases are not enables and must pass both.
    is_enable: bool = False
    #: This change can only NARROW delivery (a network off, a tighter target,
    #: a modifier cut, a first location/language/schedule target, a negative,
    #: an audience in targeting mode), so it projects 0 -- but undoing it
    #: widens delivery again. The engine records it on every ledger row as
    #: ``detail["narrowing"]`` (the campaigns it narrowed) so the revert
    #: projects their unused budget instead of the inverse of 0.
    narrows: bool = False
    #: The campaigns a narrowing change narrows. None means [campaign_id];
    #: a shared negative list sets every campaign attached to it.
    narrowed_campaigns: list[str] | None = None

    def ledger_items(self) -> list[LedgerItem]:
        if self.items:
            return self.items
        return [
            LedgerItem(
                entity_type=self.entity_type,
                entity_id=str(self.entity_id),
                entity_name=self.entity_name,
                field_name=self.field_name,
                before=self.before,
                after=self.after,
            )
        ]


def narrowed_campaigns(entry: dict[str, Any]) -> list[str] | None:
    """The campaigns a ledger row's change narrowed, or None if it did not narrow."""
    narrowed = (entry.get("detail") or {}).get("narrowing")
    return None if narrowed is None else [str(c) for c in narrowed]


def undone_change(entry: dict[str, Any]) -> dict[str, Any] | None:
    """For a REVERT's row, the change it undid -- which reverting it puts back.

    Every inverse row carries ``detail["undoes"]``: the undone row's projected
    delta, the campaigns it narrowed (None if it did not narrow) and its
    batch. Reverting a revert re-applies that change, so it costs what that
    change cost, and re-runs its checks. None for an original change, and for
    a revert row written before this was recorded.
    """
    undone = (entry.get("detail") or {}).get("undoes")
    return undone if isinstance(undone, dict) else None


def entry_campaigns(entry: dict[str, Any]) -> list[str] | None:
    """The campaign(s) a ledger row touched; [] for an account-level change,
    None when the row cannot say (it predates the scope being recorded).

    Older rows are read from what they did carry: a detail's campaign_id, the
    campaigns a narrowing recorded, or a campaign entity's own id.
    """
    detail = entry.get("detail") or {}
    if "scope_campaigns" in detail:
        return [str(c) for c in detail["scope_campaigns"]]
    if detail.get("campaign_id"):
        return [str(detail["campaign_id"])]
    if detail.get("narrowing"):
        return [str(c) for c in detail["narrowing"]]
    entity_id = str(entry.get("entity_id") or "")
    if entry.get("entity_type") == "campaign":
        head = entity_id.split("~", 1)[0]
        if head.isdigit():
            return [head]
    return None


def money_reapply_checks(entry: dict[str, Any], ceiling_name: str, ceiling: Decimal,
                         pct_name: str, pct_limit: float) -> list[Check]:
    """The ceiling and increase checks for putting ``entry['before']`` back over
    ``entry['after']`` -- a money value moving the way the original moved it."""
    from googleads_mcp.guardrails import check_ceiling, check_pct_increase

    try:
        now, wanted = amount_of(entry["after"]), amount_of(entry["before"])
    except (InvalidOperation, ValueError, IndexError):
        return []
    return [check_ceiling(ceiling_name, wanted, ceiling),
            check_pct_increase(pct_name, now, wanted, pct_limit)]


class ChangeKind:
    """One sort of change. Subclass, fill in the four methods, register it.

    ``propose`` is also the TOOL SIGNATURE: the engine publishes
    ``preview_<name>`` with exactly ``propose``'s parameters (minus ``ctx``)
    and its docstring as the description. So annotate every parameter with
    ``Annotated[..., Field(description=...)]`` the way a hand-written tool
    would, and write the docstring for the agent that will read it.
    """

    #: preview_<name> / apply_<name>.
    name: str = ""
    preview_title: str = ""
    apply_title: str = ""
    apply_annotations: ToolAnnotations = APPLY_SPENDS
    #: How many applies one of these costs against the session and daily caps.
    weight: int = 1
    #: (entity_type, field) pairs this kind writes to the ledger -- how
    #: revert_change finds the kind that can undo an entry.
    ledger_keys: tuple[tuple[str, str], ...] = ()
    #: False for a kind whose entries cannot be undone here at all (a created
    #: ad is never removed): revert_change then gives the kind's reason first,
    #: rather than a staleness complaint about a value it would never touch.
    revertible: bool = True
    #: Whether the preview also sends the mutation with validate_only=True, so
    #: it reflects whether GOOGLE will accept the change and not only whether
    #: our own bounds pass. Off only where the mutation cannot be built until
    #: the apply (none so far).
    validate_in_preview: bool = True
    #: Every change of this kind narrows delivery (negatives). For a kind that
    #: narrows only sometimes, set Proposal.narrows instead.
    narrows: bool = False

    def validates(self, payload: dict[str, Any]) -> bool:
        """Whether THIS change can be checked with validate_only.

        Per payload, because one general kind can cover a method Google gives
        no validate_only for (dismissing a recommendation).
        """
        return self.validate_in_preview

    def propose(self, ctx, **kwargs) -> Proposal:  # pragma: no cover - abstract
        raise NotImplementedError

    def current(self, ctx, payload: dict[str, Any]) -> str:  # pragma: no cover
        """Today's value, spelled exactly as the proposal spelled ``before``."""
        raise NotImplementedError

    def build(self, ctx, payload: dict[str, Any]):  # pragma: no cover
        """The :class:`~googleads_reporting.write.plan.PlannedChange` to send."""
        raise NotImplementedError

    def resource_names_for(
        self, payload: dict[str, Any], names: list[str]
    ) -> list[str | None]:
        """Which of Google's returned resource names belongs to which item.

        The default fits the common shapes: one operation per item, or a single
        item. A kind whose request carries extra operations (create an asset,
        THEN link it) overrides this to pick the ones its inverse needs.
        """
        items = payload.get("_items") or [None]
        if len(names) == len(items):
            return list(names)
        if len(items) == 1:
            return list(names[:1])
        return [None] * len(items)

    # -- revert ----------------------------------------------------------

    def revert_projection(self, ctx, entry: dict[str, Any]) -> tuple[Decimal, bool]:
        """(projected added daily spend, is_enable) for reverting ``entry``.

        A revert is not free: reverting a budget CUT puts spend back. The
        default is the inverse of what the original change projected -- a cut
        of 10 reverts as +10 -- counted like an enable, since it restores a
        state the account already had.

        A NARROWING change projected 0, so its inverse would be 0 too, yet
        undoing it widens delivery again. Its rows carry the campaigns it
        narrowed, and reverting one projects their unused daily budget.
        Kinds whose revert resumes delivery some other way (undoing a pause)
        override this.
        """
        narrowed = self.narrowed_for(entry)
        if narrowed is not None:
            from googleads_mcp.changes.common import campaign_headroom

            restored = sum((campaign_headroom(ctx, c)[0] for c in narrowed), Decimal("0"))
            return restored, restored > 0
        undone = undone_change(entry)
        if undone is not None:
            # Reverting a revert RE-APPLIES the change it undid. A narrowing
            # re-narrows (free); anything else costs what it cost the first time.
            if undone.get("narrowing") is not None:
                return Decimal("0"), False
            restored = max(_decimal(undone.get("projected")), Decimal("0"))
            return restored, restored > 0
        restored = max(-_decimal(entry.get("projected_daily_spend_delta")), Decimal("0"))
        return restored, restored > 0

    def narrowed_for(self, entry: dict[str, Any]) -> list[str] | None:
        """The campaigns ``entry``'s change narrowed, or None if it did not.

        A kind that ALWAYS narrows (negatives) wrote rows before the engine
        recorded ``narrowing``; such an original row still narrowed, so it
        falls back to the kind's flag and the campaigns the row touched. A
        revert's row never does: undoing a negative widens.
        """
        narrowed = narrowed_campaigns(entry)
        if narrowed is not None or not self.narrows:
            return narrowed
        detail = entry.get("detail") or {}
        if "undoes" in detail or entry.get("tool") == "revert_change":
            return None
        return entry_campaigns(entry) or []

    def reapplies(self, entry: dict[str, Any]) -> bool:
        """Whether reverting ``entry`` re-applies a change that did not narrow.

        Then it is not "restoring a state the account had" but the original
        change again, so the engine holds it to the per-change limit and to
        :meth:`reapply_checks`, as the original's preview did.
        """
        if type(self).revert_projection is not ChangeKind.revert_projection:
            return False
        if self.narrowed_for(entry) is not None:
            return False
        undone = undone_change(entry)
        return undone is not None and undone.get("narrowing") is None

    def reapply_checks(self, ctx, entry: dict[str, Any]) -> list[Check]:
        """The kind's own bounds on moving ``entry['after']`` back to
        ``entry['before']`` when that re-applies an earlier change (an
        increase ratio, a ceiling). Empty for a kind with none."""
        return []

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        """Today's value for a ledger entry's field, as the entry stored it.

        None means "cannot read it", and the revert skips the staleness check
        rather than refusing every revert of a field nobody taught it about.
        """
        return None

    def inverse(self, ctx, entry: dict[str, Any]):  # pragma: no cover
        """The PlannedChange that puts ``entry['before']`` back."""
        raise ValueError(
            f"{entry.get('field')!r} on {entry.get('entity_type')} cannot be "
            "reverted automatically. Change it back with the matching preview_* "
            "tool instead."
        )
