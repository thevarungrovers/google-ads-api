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
        state the account already had. Kinds whose original projected 0 but
        whose revert resumes delivery (undoing a pause) override this.
        """
        try:
            original = Decimal(str(entry.get("projected_daily_spend_delta") or "0"))
        except (InvalidOperation, ValueError):
            original = Decimal("0")
        restored = max(-original, Decimal("0"))
        return restored, restored > 0

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
