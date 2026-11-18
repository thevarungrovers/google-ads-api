"""Keywords, negative keywords and shared negative lists.

Adding is a batch: a list of texts with one match type, sent as one request,
recorded as one ledger row per keyword sharing a batch_id. Any one keyword can
be reverted on its own, or the batch_id reverts them all in one request.

Positive keywords are created PAUSED unless ``enabled=true`` was previewed.
Negatives have no paused state -- they take effect the moment they exist -- so
their preview says so, and their undo is removing exactly the criterion this
server created (traced through the ledger).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from googleads_mcp.changes.adgroups import CPC_BID_STRATEGIES, bidding_strategy_type
from googleads_mcp.changes.base import (
    NOTHING,
    ChangeKind,
    LedgerItem,
    Proposal,
    amount_of,
    money,
    parse_decimal,
    with_currency,
)
from googleads_mcp.changes.campaigns import wanted_status
from googleads_mcp.changes.common import CreatesRemovable, campaign_headroom, one
from googleads_mcp.guardrails import (
    Check,
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
    check_pct_increase,
)
from googleads_mcp.previews import project_bid_change
from googleads_reporting.query import Query, last_n_days
from googleads_reporting.write import adgroups, criteria

MatchType = Literal["EXACT", "PHRASE", "BROAD"]

KeywordList = Annotated[
    list[str],
    Field(description="Keyword texts, without match-type punctuation", min_length=1),
]


def _existing(ctx, resource: str, where: list[str], text_field: str, match_field: str):
    """{(lower text, match type)} already present, to refuse duplicates."""
    rows = ctx.client.query(Query(
        select=(text_field, match_field), from_resource=resource, where=tuple(where),
    ).to_gaql())
    found = set()
    for r in rows:
        node = r
        for part in text_field.split(".")[:-1]:
            node = getattr(node, part)
        found.add((node.text.lower(), node.match_type.name))
    return found


def _duplicates_check(texts: list[str], match_type: str, existing: set) -> Check:
    dupes = [t for t in texts if (t.lower(), match_type) in existing]
    return Check(
        "not_already_present",
        not dupes,
        "none of these exist yet" if not dupes
        else f"already present as [{match_type}]: {', '.join(dupes)}",
    )


def _ad_group_keywords(ctx, ad_group_id: str, *, negative: bool) -> set:
    return _existing(
        ctx, "ad_group_criterion",
        [f"ad_group.id = {int(ad_group_id)}", "ad_group_criterion.type = 'KEYWORD'",
         f"ad_group_criterion.negative = {'TRUE' if negative else 'FALSE'}",
         "ad_group_criterion.status != 'REMOVED'"],
        "ad_group_criterion.keyword.text", "ad_group_criterion.keyword.match_type",
    )


# ---------------------------------------------------------------------------
# Positive keywords
# ---------------------------------------------------------------------------


class AddKeywords(CreatesRemovable):
    name = "add_keywords"
    preview_title = "Preview adding keywords to an ad group"
    apply_title = "Apply adding keywords (PAUSED unless previewed enabled)"
    ledger_keys = (("keyword", "created"),)
    resource = "ad_group_criterion"
    status_field = "status"

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        keywords: KeywordList,
        match_type: Annotated[MatchType, Field(description="One match type for the batch")] = "PHRASE",
        cpc_bid: Annotated[
            str | None, Field(description='Optional keyword bid, decimal string e.g. "0.80"')
        ] = None,
        enabled: Annotated[
            bool, Field(description="Create ENABLED (serving at once). Default: PAUSED")
        ] = False,
    ) -> Proposal:
        """Show what adding keywords to an ad group would do. Writes nothing.

        Created PAUSED by default, so they serve only after a separate
        preview_keyword_status. Duplicates of existing keywords (same text and
        match type) block the preview. Read search_terms / list_keywords first.
        """
        texts = criteria.validate_keywords(keywords, match_type)
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        status = "ENABLED" if enabled else "PAUSED"
        bid = parse_decimal(cpc_bid, "cpc_bid") if cpc_bid is not None else None
        checks = [_duplicates_check(texts, match_type, _ad_group_keywords(ctx, ad_group_id, negative=False))]
        warnings: list[str] = []
        if bid is not None:
            currency = ctx.currency_of()
            checks += [check_currency(ctx.limits, currency),
                       check_ceiling("max_cpc_bid", bid, ctx.limits.max_cpc_bid, currency)]
        if enabled and row.ad_group.status.name == "ENABLED":
            projected, note = campaign_headroom(ctx, str(row.campaign.id))
            warnings.append("created ENABLED: these keywords start serving immediately")
        else:
            projected = Decimal("0")
            note = ("Created PAUSED: no spend until enabled with preview_keyword_status."
                    if not enabled else "The ad group is not enabled, so nothing serves yet.")
        items = [
            LedgerItem("keyword", f"{ad_group_id}:{match_type}:{t}", f"[{match_type}] {t}",
                       "created", NOTHING, status,
                       {"ad_group_id": str(ad_group_id), "text": t, "match_type": match_type})
            for t in texts
        ]
        return Proposal(
            entity_type="keyword", entity_id=str(ad_group_id),
            entity_name=f"{len(texts)} keyword(s) in {row.ad_group.name}",
            path=f"{row.campaign.name} > {row.ad_group.name}", field_name="created",
            before=NOTHING, after=f"{len(texts)} x [{match_type}] {status}",
            checks=checks, projected=projected, campaign_id=str(row.campaign.id),
            warnings=warnings, note=note, items=items, batch=True,
            payload={"ad_group_id": str(ad_group_id), "texts": texts, "match_type": match_type,
                     "status": status, "cpc_bid": None if bid is None else str(bid),
                     "name": row.ad_group.name},
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        existing = _ad_group_keywords(ctx, payload["ad_group_id"], negative=False)
        present = [t for t in payload["texts"] if (t.lower(), payload["match_type"]) in existing]
        return NOTHING if not present else f"already present: {', '.join(present)}"

    def build(self, ctx, payload: dict[str, Any]):
        return criteria.plan_add_keywords(
            ctx.client, ad_group_id=payload["ad_group_id"], texts=payload["texts"],
            match_type=payload["match_type"], status=payload["status"],
            cpc_bid=None if payload["cpc_bid"] is None else float(payload["cpc_bid"]),
        )


def _keyword_row(ctx, ad_group_id: str, criterion_id: str):
    found = one(ctx, (
        "ad_group_criterion.criterion_id", "ad_group_criterion.keyword.text",
        "ad_group_criterion.keyword.match_type", "ad_group_criterion.status",
        "ad_group_criterion.cpc_bid_micros", "ad_group_criterion.effective_cpc_bid_micros",
        "ad_group_criterion.negative", "ad_group.id", "ad_group.name", "ad_group.status",
        "campaign.id", "campaign.name",
    ), "ad_group_criterion", [
        f"ad_group.id = {int(ad_group_id)}",
        f"ad_group_criterion.criterion_id = {int(criterion_id)}",
    ])
    if found is None or found.ad_group_criterion.negative:
        raise LookupError(
            f"No positive keyword {criterion_id} in ad group {ad_group_id}. See list_keywords."
        )
    return found


def _label(row) -> str:
    keyword = row.ad_group_criterion.keyword
    return f"[{keyword.match_type.name}] {keyword.text}"


def _split(entity_id: str) -> tuple[str, str]:
    ad_group_id, criterion_id = entity_id.split("~")
    return ad_group_id, criterion_id


class KeywordStatus(ChangeKind):
    name = "keyword_status"
    preview_title = "Preview pausing or enabling a keyword"
    apply_title = "Apply a keyword status change (AFFECTS DELIVERY)"
    ledger_keys = (("keyword", "status"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From list_keywords")],
        criterion_id: Annotated[str, Field(description="From list_keywords")],
        new_status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling one keyword would do. Writes nothing.

        Enabling is bounded by the campaign's unused daily budget, not by the
        keyword's past spend, which is zero for a keyword that was paused.
        """
        wanted = wanted_status(new_status)
        row = _keyword_row(ctx, ad_group_id, criterion_id)
        before = row.ad_group_criterion.status.name
        if wanted == "PAUSED":
            projected, note = Decimal("0"), "Pausing cannot increase spend."
        else:
            projected, note = campaign_headroom(ctx, str(row.campaign.id))
        return Proposal(
            entity_type="keyword", entity_id=f"{ad_group_id}~{criterion_id}",
            entity_name=_label(row), path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="status", before=before, after=wanted, checks=[],
            projected=projected, campaign_id=str(row.campaign.id), note=note,
            is_enable=wanted == "ENABLED",
            warnings=[f"already {wanted}"] if before == wanted else [],
            payload={"ad_group_id": str(ad_group_id), "criterion_id": str(criterion_id)},
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return _keyword_row(ctx, payload["ad_group_id"], payload["criterion_id"]).ad_group_criterion.status.name

    def build(self, ctx, payload: dict[str, Any]):
        if payload["before"] == payload["after"]:
            raise ValueError(f"The keyword is already {payload['after']}; nothing to do.")
        return criteria.plan_set_keyword_status(
            ctx.client, ad_group_id=payload["ad_group_id"],
            criterion_id=payload["criterion_id"], status=payload["after"],
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return _keyword_row(ctx, *_split(entry["entity_id"])).ad_group_criterion.status.name

    def revert_projection(self, ctx, entry: dict[str, Any]) -> tuple[Decimal, bool]:
        if entry["before"] != "ENABLED":
            return Decimal("0"), False
        row = _keyword_row(ctx, *_split(entry["entity_id"]))
        return campaign_headroom(ctx, str(row.campaign.id))[0], True

    def inverse(self, ctx, entry: dict[str, Any]):
        ad_group_id, criterion_id = _split(entry["entity_id"])
        return criteria.plan_set_keyword_status(
            ctx.client, ad_group_id=ad_group_id, criterion_id=criterion_id,
            status=entry["before"],
        )


#: How a keyword with no bid of its own is written into before/after. It
#: inherits the ad group's, and reverting to this CLEARS the keyword's bid.
INHERITED = "(ad group default)"


class KeywordCpcBid(ChangeKind):
    name = "keyword_cpc_bid"
    preview_title = "Preview a keyword's max CPC bid change"
    apply_title = "Apply a keyword CPC bid change (SPENDS MONEY)"
    ledger_keys = (("keyword", "cpc_bid"),)

    def _rendered(self, ctx, row) -> str:
        own = row.ad_group_criterion.cpc_bid_micros
        return with_currency(money(own), ctx.currency_of()) if own else INHERITED

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From list_keywords")],
        criterion_id: Annotated[str, Field(description="From list_keywords")],
        new_cpc_bid: Annotated[str, Field(description='Decimal string, e.g. "0.90". Never micros.')],
    ) -> Proposal:
        """Show what giving one keyword its own max CPC would do. Writes nothing.

        Compared against the keyword's EFFECTIVE bid today (its own, or the ad
        group's it inherits), so the increase limit cannot be dodged by a
        keyword that never had a bid of its own.
        """
        after = parse_decimal(new_cpc_bid, "new_cpc_bid")
        row = _keyword_row(ctx, ad_group_id, criterion_id)
        currency = ctx.currency_of()
        effective = money(row.ad_group_criterion.effective_cpc_bid_micros)
        clicks = self._clicks_7d(ctx, ad_group_id, criterion_id)
        projected = project_bid_change(effective, after, clicks)
        strategy = bidding_strategy_type(ctx, str(row.campaign.id))
        effective_bid = strategy in CPC_BID_STRATEGIES
        warnings = []
        if not effective_bid:
            warnings.append(f"the campaign bids with {strategy}, which IGNORES keyword max CPC bids")
            projected = Decimal("0")
        checks = [
            Check("bid_is_effective", True,
                  f"yes, {strategy} uses keyword bids" if effective_bid
                  else f"NO -- {strategy} ignores this bid (informational)"),
            check_currency(ctx.limits, currency),
            check_ceiling("max_cpc_bid", after, ctx.limits.max_cpc_bid, currency),
            check_pct_increase("max_bid_increase_pct", effective, after,
                               ctx.limits.max_bid_increase_pct),
        ]
        return Proposal(
            entity_type="keyword", entity_id=f"{ad_group_id}~{criterion_id}",
            entity_name=_label(row), path=f"{row.campaign.name} > {row.ad_group.name}",
            field_name="cpc_bid", before=self._rendered(ctx, row),
            after=with_currency(after, currency), checks=checks, projected=projected,
            campaign_id=str(row.campaign.id), warnings=warnings,
            metrics={"clicks_7d": clicks, "effective_bid_today": f"{effective:.2f}"},
            payload={"ad_group_id": str(ad_group_id), "criterion_id": str(criterion_id),
                     "after_amount": str(after)},
            note=("projected_daily_spend_delta assumes the keyword's 7-day click volume at "
                  "the new bid -- a bound to reason with, not a forecast."),
        )

    @staticmethod
    def _clicks_7d(ctx, ad_group_id: str, criterion_id: str) -> int:
        rows = ctx.read.rows(Query(
            select=("ad_group_criterion.criterion_id", "metrics.clicks"),
            from_resource="keyword_view",
            where=(f"ad_group.id = {int(ad_group_id)}",
                   f"ad_group_criterion.criterion_id = {int(criterion_id)}", last_n_days(7)),
        ))
        return sum(int(r.get("metrics.clicks") or 0) for r in rows)

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self._rendered(ctx, _keyword_row(ctx, payload["ad_group_id"], payload["criterion_id"]))

    def build(self, ctx, payload: dict[str, Any]):
        return criteria.plan_set_keyword_bid(
            ctx.client, ad_group_id=payload["ad_group_id"],
            criterion_id=payload["criterion_id"], amount=float(payload["after_amount"]),
        )

    def current_for_entry(self, ctx, entry: dict[str, Any]) -> str | None:
        return self._rendered(ctx, _keyword_row(ctx, *_split(entry["entity_id"])))

    def inverse(self, ctx, entry: dict[str, Any]):
        ad_group_id, criterion_id = _split(entry["entity_id"])
        before = entry["before"]
        return criteria.plan_set_keyword_bid(
            ctx.client, ad_group_id=ad_group_id, criterion_id=criterion_id,
            amount=None if before == INHERITED else float(amount_of(before)),
        )


# ---------------------------------------------------------------------------
# Negatives
# ---------------------------------------------------------------------------


NEGATIVE_NOTE = (
    "Negatives have no paused state: they take effect the moment they exist. "
    "revert_change on an entry (or the batch_id) removes exactly what this created."
)


class AddNegativeKeywords(CreatesRemovable):
    name = "add_negative_keywords"
    preview_title = "Preview adding negative keywords to a campaign or ad group"
    apply_title = "Apply adding negative keywords (TAKES EFFECT IMMEDIATELY)"
    ledger_keys = (("negative keyword", "created"),)
    status_field = None

    def propose(
        self,
        ctx,
        level: Annotated[Literal["campaign", "ad_group"], Field(description="Where they apply")],
        target_id: Annotated[str, Field(description="The campaign_id or ad_group_id")],
        keywords: KeywordList,
        match_type: Annotated[MatchType, Field(description="One match type for the batch")] = "PHRASE",
    ) -> Proposal:
        """Show what adding negative keywords would do. Writes nothing.

        The usual input is the search_terms report: terms that spent and did
        not convert. Negatives can only REDUCE spend, so the projected delta is
        0 -- but an over-broad one silently stops good traffic, so read the
        list back before approving.
        """
        texts = criteria.validate_keywords(keywords, match_type)
        if level == "ad_group":
            row = adgroups.find(ctx.client, ad_group_id=target_id)
            campaign_id, path = str(row.campaign.id), f"{row.campaign.name} > {row.ad_group.name}"
            existing = _ad_group_keywords(ctx, target_id, negative=True)
            resource = "ad_group_criterion"
        else:
            from googleads_reporting.write import campaigns

            row = campaigns.find(ctx.client, campaign_id=target_id)
            campaign_id, path = str(row.campaign.id), row.campaign.name
            existing = self._campaign_negatives(ctx, target_id)
            resource = "campaign_criterion"
        items = [
            LedgerItem("negative keyword", f"{level}:{target_id}:{match_type}:{t}",
                       f"-[{match_type}] {t}", "created", NOTHING, "present",
                       {"level": level, "target_id": str(target_id), "text": t,
                        "match_type": match_type, "resource": resource})
            for t in texts
        ]
        return Proposal(
            entity_type="negative keyword", entity_id=str(target_id),
            entity_name=f"{len(texts)} negative(s) on {path}", path=path,
            field_name="created", before=NOTHING, after=f"{len(texts)} x -[{match_type}]",
            checks=[_duplicates_check(texts, match_type, existing)],
            projected=Decimal("0"), campaign_id=campaign_id, items=items, batch=True,
            note=NEGATIVE_NOTE,
            payload={"level": level, "target_id": str(target_id), "texts": texts,
                     "match_type": match_type},
        )

    @staticmethod
    def _campaign_negatives(ctx, campaign_id: str) -> set:
        return _existing(
            ctx, "campaign_criterion",
            [f"campaign.id = {int(campaign_id)}", "campaign_criterion.type = 'KEYWORD'",
             "campaign_criterion.negative = TRUE", "campaign_criterion.status != 'REMOVED'"],
            "campaign_criterion.keyword.text", "campaign_criterion.keyword.match_type",
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        existing = (
            _ad_group_keywords(ctx, payload["target_id"], negative=True)
            if payload["level"] == "ad_group"
            else self._campaign_negatives(ctx, payload["target_id"])
        )
        present = [t for t in payload["texts"] if (t.lower(), payload["match_type"]) in existing]
        return NOTHING if not present else f"already present: {', '.join(present)}"

    def build(self, ctx, payload: dict[str, Any]):
        if payload["level"] == "ad_group":
            return criteria.plan_add_keywords(
                ctx.client, ad_group_id=payload["target_id"], texts=payload["texts"],
                match_type=payload["match_type"], negative=True,
            )
        return criteria.plan_add_campaign_negatives(
            ctx.client, campaign_id=payload["target_id"], texts=payload["texts"],
            match_type=payload["match_type"],
        )

    def resource_for(self, entry: dict[str, Any]) -> str:
        return (entry.get("detail") or {}).get("resource", "campaign_criterion")


def _shared_set(ctx, shared_set_id: str):
    found = one(ctx, ("shared_set.id", "shared_set.name", "shared_set.type",
                      "shared_set.status"), "shared_set",
                [f"shared_set.id = {int(shared_set_id)}"])
    if found is None or found.shared_set.type_.name != "NEGATIVE_KEYWORDS":
        raise LookupError(
            f"No shared negative keyword list {shared_set_id}. See list_shared_negative_lists."
        )
    return found


def _attached_campaigns(ctx, shared_set_id: str) -> list[tuple[str, str]]:
    rows = ctx.client.query(Query(
        select=("campaign.id", "campaign.name", "campaign_shared_set.status"),
        from_resource="campaign_shared_set",
        where=(f"shared_set.id = {int(shared_set_id)}", "campaign_shared_set.status = 'ENABLED'"),
    ).to_gaql())
    return [(str(r.campaign.id), r.campaign.name) for r in rows]


class AddToSharedNegativeList(CreatesRemovable):
    name = "add_to_shared_negative_list"
    preview_title = "Preview adding keywords to a shared negative list"
    apply_title = "Apply adding to a shared negative list (EVERY ATTACHED CAMPAIGN)"
    ledger_keys = (("shared negative keyword", "created"),)
    resource = "shared_criterion"
    status_field = None

    def propose(
        self,
        ctx,
        shared_set_id: Annotated[str, Field(description="From list_shared_negative_lists")],
        keywords: KeywordList,
        match_type: Annotated[MatchType, Field(description="One match type for the batch")] = "PHRASE",
    ) -> Proposal:
        """Show what adding negatives to a SHARED list would do. Writes nothing.

        A shared list applies to every campaign attached to it; the preview
        lists them, and each must be inside the campaign scope.
        """
        texts = criteria.validate_keywords(keywords, match_type)
        found = _shared_set(ctx, shared_set_id)
        attached = _attached_campaigns(ctx, shared_set_id)
        existing = _existing(
            ctx, "shared_criterion", [f"shared_set.id = {int(shared_set_id)}"],
            "shared_criterion.keyword.text", "shared_criterion.keyword.match_type",
        )
        checks = [_duplicates_check(texts, match_type, existing)]
        checks += [check_campaign_in_scope(ctx.limits, cid) for cid, _ in attached]
        items = [
            LedgerItem("shared negative keyword", f"{shared_set_id}:{match_type}:{t}",
                       f"-[{match_type}] {t}", "created", NOTHING, "present",
                       {"shared_set_id": str(shared_set_id), "text": t, "match_type": match_type})
            for t in texts
        ]
        names = ", ".join(n for _, n in attached) or "no campaigns yet"
        return Proposal(
            entity_type="shared negative keyword", entity_id=str(shared_set_id),
            entity_name=f"{len(texts)} negative(s) in list {found.shared_set.name}",
            path=f"shared list {found.shared_set.name} -> {names}", field_name="created",
            before=NOTHING, after=f"{len(texts)} x -[{match_type}]", checks=checks,
            projected=Decimal("0"), items=items, batch=True,
            warnings=[f"applies to every attached campaign: {names}"] if attached else [],
            note=NEGATIVE_NOTE,
            payload={"shared_set_id": str(shared_set_id), "texts": texts, "match_type": match_type},
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        existing = _existing(
            ctx, "shared_criterion", [f"shared_set.id = {int(payload['shared_set_id'])}"],
            "shared_criterion.keyword.text", "shared_criterion.keyword.match_type",
        )
        present = [t for t in payload["texts"] if (t.lower(), payload["match_type"]) in existing]
        return NOTHING if not present else f"already present: {', '.join(present)}"

    def build(self, ctx, payload: dict[str, Any]):
        return criteria.plan_add_shared_negatives(
            ctx.client, shared_set_id=payload["shared_set_id"], texts=payload["texts"],
            match_type=payload["match_type"],
        )


class LinkSharedNegativeList(CreatesRemovable):
    name = "link_shared_negative_list"
    preview_title = "Preview attaching a shared negative list to a campaign"
    apply_title = "Apply attaching a shared negative list (TAKES EFFECT IMMEDIATELY)"
    ledger_keys = (("campaign shared set", "created"),)
    resource = "campaign_shared_set"
    status_field = "status"

    def propose(
        self,
        ctx,
        shared_set_id: Annotated[str, Field(description="From list_shared_negative_lists")],
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
    ) -> Proposal:
        """Show what attaching a shared negative list to a campaign would do.
        Writes nothing. Every negative in the list starts applying at once."""
        from googleads_reporting.write import campaigns

        found = _shared_set(ctx, shared_set_id)
        row = campaigns.find(ctx.client, campaign_id=campaign_id)
        before = self._state(ctx, shared_set_id, campaign_id)
        return Proposal(
            entity_type="campaign shared set", entity_id=f"{campaign_id}~{shared_set_id}",
            entity_name=f"list {found.shared_set.name} on {row.campaign.name}",
            path=row.campaign.name, field_name="created", before=before, after="ENABLED",
            checks=[Check("not_already_attached", before == NOTHING,
                          "not attached yet" if before == NOTHING else f"already {before}")],
            projected=Decimal("0"), campaign_id=str(row.campaign.id), note=NEGATIVE_NOTE,
            payload={"shared_set_id": str(shared_set_id), "campaign_id": str(campaign_id)},
        )

    @staticmethod
    def _state(ctx, shared_set_id: str, campaign_id: str) -> str:
        found = one(ctx, ("campaign_shared_set.status",), "campaign_shared_set", [
            f"campaign.id = {int(campaign_id)}", f"shared_set.id = {int(shared_set_id)}",
            "campaign_shared_set.status = 'ENABLED'",
        ])
        return NOTHING if found is None else found.campaign_shared_set.status.name

    def current(self, ctx, payload: dict[str, Any]) -> str:
        return self._state(ctx, payload["shared_set_id"], payload["campaign_id"])

    def build(self, ctx, payload: dict[str, Any]):
        return criteria.plan_link_shared_set(
            ctx.client, campaign_id=payload["campaign_id"], shared_set_id=payload["shared_set_id"],
        )
