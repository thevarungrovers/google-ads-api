"""The one preview -> apply -> revert sequence every change kind goes through.

Before this existed each pair carried its own copy of the apply sequence, and
the copy that drifted -- revert_change -- had lost the kill switch, the
staleness check and the session cap. With the sequence written once, a kind
supplies only what is specific to it (see :mod:`.base`) and cannot skip a gate.

THE APPLY SEQUENCE, in this order, and the order matters:

  1. take the token        single-use, consumed before any check, so a token
                           that fails cannot be retried until something shifts
  2. the kill switch       re-read now, never cached: a preview is good for ten
                           minutes and the switch must stop the very next apply
  3. staleness             the entity's CURRENT value must still be the preview's
                           ``before``, or someone edited it in between
  4. the caps              session, then day -- after staleness, so a stale
                           apply does not burn a slot it never used
  5. ledger intent         BEFORE the request leaves the process
  6. the mutation
  7. ledger outcome        and only then the counters

This module deliberately has no ``from __future__ import annotations``: the
tool functions it generates carry annotations built at runtime (a Field whose
description names the matching preview), and string annotations would have to
be resolved against globals where those values do not exist.
"""

import inspect
import secrets
import typing
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Any, Callable

from pydantic import Field

from googleads_mcp import ledger
from googleads_mcp.changes.base import (
    PREVIEW_SAFE,
    ChangeKind,
    LedgerItem,
    Proposal,
    entry_campaigns,
    lands_on_change,
    undone_change,
)
from googleads_mcp.guardrails import (
    Check,
    any_failed,
    check_campaign_in_scope,
    check_count,
    check_customer_in_scope,
    check_projected_delta,
    check_writes_enabled,
    render_checks,
    require_writes_enabled,
)
from googleads_mcp.previews import (
    TOKEN_TTL_SECONDS,
    ApplyResult,
    ChangePreview,
    PreviewExpired,
    PreviewStale,
)


class RevertOutOfScope(RuntimeError):
    """A revert of an entry outside today's customer or campaign scope."""


class Engine:
    """Runs kinds against one :class:`~.context.WriteContext`."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.kinds: dict[str, ChangeKind] = {}

    # -- registry --------------------------------------------------------

    def add(self, kind: ChangeKind) -> ChangeKind:
        if kind.name in self.kinds:
            raise ValueError(f"change kind {kind.name!r} registered twice")
        self.kinds[kind.name] = kind
        return kind

    def kind_for_entry(self, entry: dict[str, Any]) -> ChangeKind:
        """The kind that wrote this ledger row, found by what the row changed.

        Keyed on (entity_type, field) rather than on the tool name, because a
        revert's own row carries tool="revert_change" -- and reverting a revert
        has to find the same kind the original did.
        """
        key = (entry.get("entity_type"), entry.get("field"))
        for kind in self.kinds.values():
            if key in kind.ledger_keys:
                return kind
        raise ValueError(
            f"Cannot revert field {entry.get('field')!r} on "
            f"{entry.get('entity_type')} automatically: no change kind owns it."
        )

    # -- caps ------------------------------------------------------------

    def _refuse_if_over_caps(self, projected: Decimal, weight: int) -> None:
        """Session first (the tighter, per-process bound), then the day."""
        blocked = self.ctx.counters.would_exceed(projected, weight=weight)
        if blocked:
            raise PreviewExpired(blocked)
        blocked = self.ctx.daily_would_exceed(projected, weight=weight)
        if blocked:
            raise PreviewExpired(blocked)

    # -- preview ---------------------------------------------------------

    def preview(self, kind: ChangeKind, args: dict[str, Any]) -> ChangePreview:
        ctx = self.ctx
        proposal: Proposal = kind.propose(ctx, **args)
        items = proposal.ledger_items()
        # Which campaigns this touches, on every row, so a revert can be held
        # to the campaign scope as it stands THEN, not as it stood at apply.
        scope = (
            [str(proposal.campaign_id)] if proposal.campaign_id is not None
            else [str(c) for c in (proposal.narrowed_campaigns or [])]
        )
        if scope:
            for item in items:
                item.detail = {**(item.detail or {}), "scope_campaigns": scope}
        if proposal.narrows or kind.narrows:
            # Recorded on the row, because undoing a narrowing WIDENS delivery
            # and the revert has to know which campaigns' budgets it reopens.
            narrowed = proposal.narrowed_campaigns
            if narrowed is None:
                narrowed = [] if proposal.campaign_id is None else [str(proposal.campaign_id)]
            for item in items:
                item.detail = {**(item.detail or {}), "narrowing": list(narrowed)}
        if proposal.names:
            # Who named it, on every row: reconcile_ledger tells two agents in
            # one checkout apart by this, and a rename's "ours" test reads it.
            record = {
                "identity": ctx.identity.ledger(),
                "requested_name": proposal.names[0].requested,
                "created_names": [list(pair) for pair in proposal.created_names],
            }
            for item in items:
                item.detail = {**(item.detail or {}), **record}

        payload = dict(proposal.payload)
        payload["before"] = proposal.before
        payload["after"] = proposal.after
        payload["_items"] = [asdict(item) for item in items]
        payload["_entity"] = {
            "entity_type": proposal.entity_type,
            "entity_id": str(proposal.entity_id),
            "entity_name": proposal.entity_name,
            "field_name": proposal.field_name,
        }

        checks: list[Check] = [
            check_writes_enabled(),
            check_customer_in_scope(
                ctx.limits, ctx.target_customer(), ctx.default_customer
            ),
        ]
        if proposal.campaign_id is not None:
            checks.append(check_campaign_in_scope(ctx.limits, str(proposal.campaign_id)))
        checks.extend(proposal.checks)
        checks.extend(self._naming_checks(kind, proposal, payload))
        if proposal.batch:
            checks.append(check_count(ctx.limits, len(items)))
        if proposal.is_enable:
            checks.append(Check(
                "projected_delta_per_day", True,
                "enable: counted against the daily total", skipped=True,
            ))
        else:
            checks.append(
                check_projected_delta(ctx.limits, max(proposal.projected, Decimal("0")))
            )
        daily_total = ctx.daily.check(proposal.projected)
        checks.append(daily_total)
        naming_warnings = [w for w in (s.warning() for s in proposal.names) if w]
        enable_warnings = []
        if proposal.is_enable and proposal.projected > 0:
            enable_warnings.append(
                f"re-enabling resumes spend up to the daily budget; it {daily_total.detail}"
            )

        # Google's own verdict, without changing anything. Only once our own
        # checks pass: a blocked preview will never be applied, so asking
        # Google about it costs a request and tells nobody anything.
        if kind.validates(payload) and not any_failed(checks):
            checks.append(ctx.validate(lambda: kind.build(ctx, payload)))

        blocked = any_failed(checks)
        token = ctx.previews.mint(
            tool=f"apply_{kind.name}",
            payload=payload,
            projected_delta=proposal.projected,
            blocked=blocked,
        )
        return ChangePreview(
            tool=f"apply_{kind.name}",
            preview_token=token,
            expires_in_seconds=TOKEN_TTL_SECONDS,
            entity_type=proposal.entity_type,
            entity_id=str(proposal.entity_id),
            entity_name=proposal.entity_name,
            path=proposal.path,
            field_name=proposal.field_name,
            before=proposal.before,
            after=proposal.after,
            customer_id=ctx.target_customer(),
            campaign_status=proposal.campaign_status,
            metrics_7d=proposal.metrics,
            projected_daily_spend_delta=f"{proposal.projected:.2f}",
            checks=render_checks(checks),
            blocked=blocked,
            warnings=[*proposal.warnings, *naming_warnings, *enable_warnings,
                      *ctx.cap_warnings(proposal.projected, kind.weight)],
            note=proposal.note,
            items=(
                [{k: v for k, v in asdict(i).items() if k != "detail"} for i in items]
                if proposal.batch
                else None
            ),
            apply_weight=kind.weight,
            naming=self._naming_block(proposal),
        )

    # -- naming ------------------------------------------------------------

    def _naming_checks(self, kind: ChangeKind, proposal: Proposal,
                       payload: dict[str, Any]) -> list[Check]:
        """FAIL lines for every naming problem, plus the backstop.

        The backstop reads the names off the REQUEST a creating kind builds --
        every ``create.name``, nested ad groups and the budget included -- not
        off what the kind declared in ``created_names``. When a prefix is
        configured each must carry it (``name_prefix``); whatever is
        configured, none may carry another identity's full prefix, break its
        length or character rules, or differ from the names the preview
        declared (``names_sent``). A kind that forgot to stamp, or stamped one
        name and sent another, is caught here, in the preview the human
        reads, rather than by a reader of the account a month later.
        """
        from googleads_reporting import naming

        checks = [Check(name, False, detail)
                  for stamped in proposal.names for name, detail in stamped.problems]
        if not kind.creates_named:
            return checks
        identity = self.ctx.identity
        try:
            built = built_names(kind.build(self.ctx, payload))
        except Exception as exc:  # noqa: BLE001 - reported as a FAIL, never sent
            # Usually the input itself (an image of the wrong shape), so the
            # check is named for the request, not for the names it carries.
            return [*checks, Check("request", False, f"the request cannot be built: {exc}")]
        if identity.prefix and identity.valid:
            unstamped = [f"{etype} {name!r}" for etype, name in built
                         if not naming.owns(identity, name)]
            checks.append(Check(
                "name_prefix", not unstamped,
                f"every created name starts with {identity.prefix}" if not unstamped
                else f"missing the {identity.prefix} prefix: {', '.join(unstamped)}",
            ))
        problems = []
        for etype, name in built:
            if (forged := naming.foreign(identity, name)) is not None:
                problems.append(f"{etype} {name!r} carries {forged.owner}-{forged.agent}-, "
                                "another identity's prefix")
            problems.extend(f"{etype} {name!r}: {why}"
                            for why in naming.final_name_problems(name, etype))
        declared = sorted(tuple(pair) for pair in proposal.created_names)
        sent = sorted(pair for pair in built if pair[0] in kind.creates_named)
        if sent != declared:
            problems.append(
                "the request does not create the names this preview shows: it sends "
                f"{[n for _, n in sent]}, the preview shows {[n for _, n in declared]}"
            )
        checks.append(Check(
            "names_sent", not problems,
            "the request creates exactly the names shown" if not problems
            else "; ".join(problems),
        ))
        return checks

    @staticmethod
    def _naming_block(proposal: Proposal) -> dict[str, Any] | None:
        if not proposal.names:
            return None
        block = proposal.names[0].block()
        if len(proposal.names) > 1:
            block["also"] = [s.block() for s in proposal.names[1:]]
        return block

    # -- apply -----------------------------------------------------------

    def apply(self, kind: ChangeKind, token: str) -> ApplyResult:
        ctx = self.ctx
        tool = f"apply_{kind.name}"
        pending = ctx.previews.take(token, tool)
        # The kill switch FIRST, re-read here and never cached. The preview ran
        # check_writes_enabled() too, but that was up to ten minutes ago, and
        # the switch is a file precisely so somebody can throw it from another
        # terminal mid-session.
        require_writes_enabled()
        payload = pending.payload
        entity = payload["_entity"]

        current = kind.current(ctx, payload)
        if payload["before"] != current:
            raise PreviewStale(
                f"{entity['entity_name']} has changed since the preview: it was "
                f"{payload['before']!r} then and is {current!r} now. "
                "Someone edited it in between. Take a fresh preview."
            )
        self._refuse_if_over_caps(pending.projected_delta, kind.weight)

        plan = kind.build(ctx, payload)
        items = [LedgerItem(**item) for item in payload["_items"]]
        result, entry_ids, batch_id = self._send_recorded(
            plan,
            items,
            tool=tool,
            projected=pending.projected_delta,
            weight=kind.weight,
            resource_names_for=lambda names: kind.resource_names_for(payload, names),
            record_created=bool(kind.creates_named),
        )
        ctx.counters.record(pending.projected_delta, weight=kind.weight)

        resources = list(result.resource_names)
        # The resource the kind says is ITS result -- for a whole campaign
        # build that is the campaign, not the budget created first.
        own = [n for n in kind.resource_names_for(payload, resources) if n]
        return ApplyResult(
            applied=True,
            entry_id=entry_ids[0],
            entity_type=entity["entity_type"],
            entity_id=entity["entity_id"],
            entity_name=entity["entity_name"],
            field_name=entity["field_name"],
            before=payload["before"],
            after=payload["after"],
            resource_name=own[0] if own else (resources[0] if resources else None),
            session=ctx.counters.snapshot(),
            batch_id=batch_id,
            entry_ids=entry_ids if batch_id else [],
            resource_names=resources if len(resources) > 1 else [],
            daily=ctx.daily_snapshot(),
        )

    def _send_recorded(
        self,
        plan,
        items: list[LedgerItem],
        *,
        tool: str,
        projected: Decimal,
        weight: int,
        resource_names_for: Callable[[list[str]], list[str | None]],
        reverts: list[str] | None = None,
        record_created: bool = False,
    ):
        """Ledger intents, the mutation, ledger outcomes. Raises on failure.

        ``record_created``: a creating kind's single row also records EVERY
        resource the request created -- a campaign's nested ad groups get no
        row of their own, and "did we create it?" must be answered by id.

        One ledger row per item. A batch's rows share a batch_id and each
        carries the WHOLE batch's projected delta and weight: the daily cap
        groups by batch and takes one row's numbers, so a batch of twenty
        keywords costs one apply, not twenty.
        """
        ctx = self.ctx
        batch_id = secrets.token_hex(6) if len(items) > 1 else None
        entry_ids: list[str] = []
        for item in items:
            entry_id = ledger.new_entry_id()
            entry_ids.append(entry_id)
            ledger.write_intent(
                entry_id=entry_id,
                tool=tool,
                customer_id=ctx.target_customer(),
                entity_type=item.entity_type,
                entity_id=str(item.entity_id),
                entity_name=item.entity_name,
                field_name=item.field_name,
                before=item.before,
                after=item.after,
                projected_delta=str(projected),
                batch_id=batch_id,
                detail=item.detail,
                weight=weight,
            )
        try:
            result = ctx.send(plan, apply=True)
        except Exception as exc:
            for entry_id in entry_ids:
                ledger.write_outcome(entry_id=entry_id, ok=False, error=str(exc))
            raise

        names = list(result.resource_names or [])
        per_item = resource_names_for(names)
        for index, entry_id in enumerate(entry_ids):
            ledger.write_outcome(
                entry_id=entry_id,
                ok=True,
                resource_name=per_item[index] if index < len(per_item) else None,
                reverts=reverts[index] if reverts else None,
                created_resources=(names if record_created and len(entry_ids) == 1
                                   and len(names) > 1 else None),
            )
        return result, entry_ids, batch_id

    # -- revert ----------------------------------------------------------

    def revert(self, entry_id: str) -> ApplyResult:
        ctx = self.ctx
        # A revert is a real write, so it answers to the same gates every
        # apply_* does: the kill switch, and the caps. Zero projected delta --
        # it restores a value the account already held -- but it still costs
        # one apply, because find_entry reaches back over the whole ledger,
        # including changes from earlier sessions this process never saw, so
        # without the APPLIES cap a revert loop had no bound at all.
        require_writes_enabled()
        # The APPLIES half of the caps first, before any lookup: a revert loop
        # must stop even on entries that turn out not to exist.
        self._refuse_if_over_caps(Decimal("0"), 1)

        entry = ledger.find_entry(entry_id)
        if entry is None:
            batch = ledger.batch_entries(entry_id)
            if batch:
                return self._revert_batch(entry_id, batch)
            raise ValueError(f"No ledger entry {entry_id!r}. See list_my_changes.")
        self._revertible(entry)
        self._in_scope(entry)
        kind = self.kind_for_entry(entry)
        if not kind.revertible:
            kind.inverse(ctx, entry)  # raises, with the kind's own reason
        if (refusal := kind.revert_refusal(entry)) is not None:
            raise ValueError(refusal)
        self._not_stale(kind, entry)
        # Then the SPEND half, now that we know what undoing this restores:
        # reverting a pause, a cut or any other narrowing change (a network
        # off, a negative, a first location target...) puts spend back, and
        # that counts against the session and daily totals like any enable
        # (exempt, like one, from the per-change limit).
        projected, _ = kind.revert_projection(ctx, entry)
        if kind.reapplies(entry):
            self._refuse_reapplication(kind, [entry], projected)
        self._refuse_if_over_caps(projected, 1)
        plan = kind.inverse(ctx, entry)

        _, entry_ids, _ = self._send_recorded(
            plan,
            [self._inverse_item(kind, entry)],
            tool="revert_change",
            projected=projected,
            weight=1,
            resource_names_for=lambda names: names[:1],
            reverts=[entry_id],
        )
        ctx.counters.record(projected)
        result = self._revert_result(entry, entry_ids[0])
        result.projected_daily_spend_delta = f"{projected:.2f}"
        return result

    def _revert_batch(self, batch_id: str, batch: list[dict[str, Any]]) -> ApplyResult:
        """Undo every still-applied row of one batch, as ONE request.

        Atomic or not at all: half a batch reverted is a state nobody asked
        for, and the ledger could not describe it as either.
        """
        ctx = self.ctx
        reverted = ledger.reverted_entry_ids()
        live = [e for e in batch if e.get("applied") and e["entry_id"] not in reverted]
        if not live:
            raise ValueError(
                f"Batch {batch_id} has nothing left to revert: every entry in it "
                "was never applied or has already been reverted."
            )
        for entry in live:
            self._in_scope(entry)
        plans = []
        projected = Decimal("0")
        # A narrowing batch carries the same campaign on every row; reopening
        # it once is the bound, so those are counted per campaign, not per row.
        reopened: set[str] = set()
        # Re-applying an earlier batch: each of its rows carries the WHOLE
        # batch's projection, so it is counted once per undone batch.
        reapplied: dict[str, Decimal] = {}
        reapplying: list[tuple[ChangeKind, dict[str, Any]]] = []
        kinds = []
        for entry in live:
            kind = self.kind_for_entry(entry)
            kinds.append(kind)
            if not kind.revertible:
                kind.inverse(ctx, entry)
            if (refusal := kind.revert_refusal(entry)) is not None:
                raise ValueError(refusal)
            self._not_stale(kind, entry)
            narrowed = kind.narrowed_for(entry)
            default = type(kind).revert_projection is ChangeKind.revert_projection
            if narrowed is not None and default and not kind.reapplies(entry):
                reopened.update(narrowed)
            elif kind.reapplies(entry):
                reapplying.append((kind, entry))
                undone = undone_change(entry) or {}
                key = undone.get("batch_id") or entry["entry_id"]
                reapplied[key] = kind.revert_projection(ctx, entry)[0]
            else:
                projected += kind.revert_projection(ctx, entry)[0]
            plans.append(kind.inverse(ctx, entry))
        if reapplying:
            total = sum(reapplied.values(), Decimal("0"))
            for kind in {type(k): k for k, _ in reapplying}.values():
                rows = [e for k, e in reapplying if type(k) is type(kind)]
                self._refuse_reapplication(kind, rows, total)
            projected += total
        if reopened:
            from googleads_mcp.changes.common import campaign_headroom

            projected += sum((campaign_headroom(ctx, c)[0] for c in sorted(reopened)),
                             Decimal("0"))
        self._refuse_if_over_caps(projected, 1)
        plan = merge_plans(plans)

        _, entry_ids, new_batch = self._send_recorded(
            plan,
            [self._inverse_item(k, e) for k, e in zip(kinds, live)],
            tool="revert_change",
            projected=projected,
            weight=1,
            resource_names_for=lambda names: names,
            reverts=[e["entry_id"] for e in live],
        )
        ctx.counters.record(projected)
        result = self._revert_result(live[0], entry_ids[0])
        result.projected_daily_spend_delta = f"{projected:.2f}"
        result.batch_id = new_batch
        result.entry_ids = entry_ids
        result.entity_name = f"{len(live)} entries of batch {batch_id}"
        return result

    @staticmethod
    def _revertible(entry: dict[str, Any]) -> None:
        if not entry.get("applied"):
            raise ValueError(f"Entry {entry['entry_id']} was never applied; nothing to revert.")
        if entry["entry_id"] in ledger.reverted_entry_ids():
            raise ValueError(f"Entry {entry['entry_id']} has already been reverted.")

    def _in_scope(self, entry: dict[str, Any]) -> None:
        """The scope checks every preview runs, against the ENTRY's own customer
        and campaign(s). The scope may have been narrowed since the change was
        made, and find_entry reaches back over every session's changes."""
        ctx = self.ctx
        customer = str(entry.get("customer_id") or "")
        checks = [check_customer_in_scope(ctx.limits, customer, ctx.default_customer)]
        target = ctx.target_customer()
        if customer != target:
            # The inverse is sent to the write client's customer, so an entry
            # from another account would be "undone" in the wrong one.
            checks.append(Check("same_customer", False,
                                f"entry is for {customer or '(unknown)'}, writes go to {target}"))
        campaigns = entry_campaigns(entry)
        if campaigns is None:
            if ctx.limits.allowed_campaign_ids:
                checks.append(Check(
                    "campaign_in_scope", False,
                    "cannot tell which campaign this entry touched, and a campaign "
                    "scope is configured",
                ))
        else:
            checks.extend(check_campaign_in_scope(ctx.limits, c) for c in campaigns)
        if any_failed(checks):
            failed = "; ".join(c.render() for c in checks if not c.ok)
            raise RevertOutOfScope(
                f"Entry {entry['entry_id']} is outside the write scope, so it cannot "
                f"be reverted here: {failed}"
            )

    def _not_stale(self, kind: ChangeKind, entry: dict[str, Any]) -> None:
        # STALENESS, the same check apply does. If the value is no longer what
        # our change left behind, somebody moved it since -- most often a
        # person in the Google Ads UI -- and putting our old value back would
        # silently discard THEIR change, while the ledger recorded it as a tidy
        # undo of ours.
        current = kind.current_for_entry(self.ctx, entry)
        if current is not None and current != entry["after"]:
            raise PreviewStale(
                f"{entry['entity_name']} {entry['field']} is {current!r} now, but this "
                f"entry left it at {entry['after']!r}. Something changed it since, "
                f"so reverting would overwrite that instead of undoing this. Look "
                f"at list_my_changes and the Google Ads UI before deciding."
            )

    def _refuse_reapplication(self, kind: ChangeKind, entries: list[dict[str, Any]],
                              projected: Decimal) -> None:
        """Reverting a revert puts the ORIGINAL change back, so it answers to the
        checks the original's preview ran: the per-change limit (which a plain
        revert, restoring a state the account had, is exempt from) and the
        kind's own bounds -- an increase ratio, a ceiling. The limits may have
        been tightened since, and then the re-application is refused."""
        checks = [check_projected_delta(self.ctx.limits, max(projected, Decimal("0")))]
        for entry in entries:
            checks.extend(kind.reapply_checks(self.ctx, entry))
        if any_failed(checks):
            failed = "; ".join(c.render() for c in checks if not c.ok)
            raise ValueError(
                f"Reverting {entries[0]['entry_id']} would re-apply a change it undid, "
                f"and that change no longer passes its checks: {failed}. Preview it "
                "afresh with the matching preview_* tool instead."
            )

    @staticmethod
    def _inverse_item(kind: ChangeKind, entry: dict[str, Any]) -> LedgerItem:
        """The revert's own row, recording what undoing IT would cost.

        Reverting this row re-applies ``entry``, so the row carries ``entry``'s
        projection and narrowing under ``undoes``. The undo of a narrowing
        WIDENS, so the row itself never claims to narrow -- unless ``entry``
        was itself the undo of a narrowing: then this row puts that narrowing
        back, and undoing it reopens the same campaigns again.
        """
        detail = dict(entry.get("detail") or {})
        detail.pop("narrowing", None)
        detail.pop("undoes", None)
        detail["undoes"] = {
            "entry_id": entry["entry_id"],
            "projected": str(entry.get("projected_daily_spend_delta") or "0"),
            "narrowing": kind.narrowed_for(entry),
            "batch_id": entry.get("batch_id"),
            # Parity: whether ``entry`` left a forward change in place, so
            # reverting this row re-applies it (base.reapplies_change).
            "forward": lands_on_change(entry),
        }
        previous = undone_change(entry)
        if previous is not None and previous.get("narrowing") is not None:
            detail["narrowing"] = list(previous["narrowing"])
        detail = kind.inverse_detail(detail)
        return LedgerItem(
            entity_type=entry["entity_type"],
            entity_id=str(entry["entity_id"]),
            entity_name=entry["entity_name"],
            field_name=entry["field"],
            before=entry["after"],
            after=entry["before"],
            detail=detail,
        )

    def _revert_result(self, entry: dict[str, Any], new_entry: str) -> ApplyResult:
        return ApplyResult(
            applied=True,
            entry_id=new_entry,
            entity_type=entry["entity_type"],
            entity_id=str(entry["entity_id"]),
            entity_name=entry["entity_name"],
            field_name=entry["field"],
            before=entry["after"],
            after=entry["before"],
            resource_name=entry.get("resource_name"),
            session=self.ctx.counters.snapshot(),
            daily=self.ctx.daily_snapshot(),
        )


#: Created message type -> naming entity type (NAME_LIMITS' keys).
_NAMED_TYPES = {"Campaign": "campaign", "AdGroup": "ad_group",
                "CampaignBudget": "budget", "Label": "label", "Asset": "asset"}


def built_names(plan) -> list[tuple[str, str]]:
    """(entity type, name) for every entity a plan's operations CREATE with a
    name: ``XOperation.create.name``, inside a ``MutateOperation`` or not."""
    out: list[tuple[str, str]] = []

    def walk(message) -> None:
        pb_of = getattr(type(message), "pb", None)
        pb = pb_of(message) if callable(pb_of) else message
        if not hasattr(pb, "DESCRIPTOR") or "operation" not in pb.DESCRIPTOR.oneofs_by_name:
            return
        which = pb.WhichOneof("operation")
        if which == "create":
            created = pb.create
            if "name" in created.DESCRIPTOR.fields_by_name and created.name:
                kind = created.DESCRIPTOR.name
                out.append((_NAMED_TYPES.get(kind, kind), created.name))
        elif which:
            walk(getattr(pb, which))

    for operation in getattr(plan, "operations", None) or []:
        walk(operation)
    return out


def merge_plans(plans: list[Any]):
    """Several PlannedChanges as one request, when they can travel together.

    Same service and method: one request with every operation. Otherwise they
    must all be atomic (GoogleAdsService MutateOperations), which can also be
    concatenated. Anything else is refused rather than sent as several requests
    that could half-succeed.
    """
    from googleads_reporting.write.plan import PlannedChange

    if len(plans) == 1:
        return plans[0]
    first = plans[0]
    if any((p.service, p.method) != (first.service, first.method) for p in plans):
        raise ValueError(
            "These entries cannot be reverted in one request; revert them one "
            "entry_id at a time."
        )
    return PlannedChange(
        action=first.action,
        entity=first.entity,
        label=f"{len(plans)} x {first.label}",
        service=first.service,
        method=first.method,
        operations=[op for p in plans for op in p.operations],
        changes=[c for p in plans for c in p.changes],
        warnings=[w for p in plans for w in p.warnings],
    )


# ---------------------------------------------------------------------------
# Publishing a kind as two MCP tools
# ---------------------------------------------------------------------------


def _preview_function(engine: Engine, kind: ChangeKind) -> Callable[..., ChangePreview]:
    """``preview_<name>``, with exactly ``kind.propose``'s parameters.

    The MCP SDK builds the tool's input schema from the function signature, so
    the generated function has to LOOK like the hand-written tool it replaces:
    same parameter names, same Annotated descriptions, same docstring.
    """
    method = kind.propose
    signature = inspect.signature(method)
    hints = typing.get_type_hints(method, include_extras=True)
    parameters = [p for p in signature.parameters.values() if p.name != "ctx"]

    def preview(**kwargs: Any) -> ChangePreview:
        return engine.preview(kind, kwargs)

    preview.__name__ = preview.__qualname__ = f"preview_{kind.name}"
    preview.__doc__ = inspect.getdoc(method)
    preview.__signature__ = signature.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in parameters],
        return_annotation=ChangePreview,
    )
    preview.__annotations__ = {
        **{p.name: hints[p.name] for p in parameters if p.name in hints},
        "return": ChangePreview,
    }
    return preview


def _apply_function(engine: Engine, kind: ChangeKind) -> Callable[..., ApplyResult]:
    """``apply_<name>(preview_token)`` -- and nothing else, ever.

    An apply that accepted the values directly could be called cold, with no
    preview shown first; test_mcp_surface pins that every apply takes only the
    token.
    """
    token_type = Annotated[str, Field(description=f"From preview_{kind.name}")]

    def apply(preview_token: str) -> ApplyResult:
        return engine.apply(kind, preview_token)

    apply.__name__ = apply.__qualname__ = f"apply_{kind.name}"
    apply.__doc__ = (
        f"Apply the change preview_{kind.name} showed. Takes only its token; "
        "show the human the preview first -- their approval of this call is the "
        "authorization."
    )
    apply.__signature__ = inspect.Signature(
        [inspect.Parameter("preview_token", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           annotation=token_type)],
        return_annotation=ApplyResult,
    )
    apply.__annotations__ = {"preview_token": token_type, "return": ApplyResult}
    return apply


def register_pair(
    mcp,
    engine: Engine,
    kind: ChangeKind,
    *,
    refusals: Callable,
    apply_tool: Callable,
) -> None:
    """Register ``preview_<kind>`` and ``apply_<kind>`` for one kind.

    ``apply_tool`` is the read-only-aware decorator from tools_write: in
    read-only mode it registers nothing, so the apply is absent from tools/list
    rather than refused at call time.
    """
    engine.add(kind)
    mcp.tool(title=kind.preview_title, annotations=PREVIEW_SAFE)(
        refusals(_preview_function(engine, kind))
    )
    apply_tool(title=kind.apply_title, annotations=kind.apply_annotations)(
        refusals(_apply_function(engine, kind))
    )
