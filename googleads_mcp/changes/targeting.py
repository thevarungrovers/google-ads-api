"""Targeting kinds: where, when, on which device and network, and to whom.

Additions (locations, languages, schedule windows, audience segments) create
criteria and revert by removing exactly what was created. Modifiers and
network switches update a value in place and revert by setting it back.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from googleads_mcp.changes.base import NOTHING, ChangeKind, LedgerItem, Proposal
from googleads_mcp.changes.common import CreatesRemovable, campaign_headroom, one
from googleads_mcp.guardrails import Check
from googleads_reporting.query import Query
from googleads_reporting.write import adgroups, campaigns, criteria, targeting
from googleads_reporting.write.targeting import modifier_of, pct_from_modifier, pct_of, render_pct


def parse_pct(raw: str, name: str = "bid_modifier_pct") -> Decimal:
    """A signed percentage string: "+20", "-15", "0". Never a multiplier."""
    try:
        value = Decimal(str(raw).strip().rstrip("%"))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f'{name} must be a signed percentage like "+20" or "-15", got {raw!r}.') from exc
    if not value.is_finite() or value < -100 or value > 900:
        raise ValueError(
            f"{name} must be between -100 and +900 (percent), got {raw!r}. Pass a "
            'percentage, not a multiplier: "+20" rather than "1.2".'
        )
    return value.quantize(Decimal("1"))


def multiplier(pct: Decimal) -> Decimal:
    """+20 -> 1.2, -100 -> 0: what Google actually multiplies the bid by."""
    return (Decimal("100") + pct) / Decimal("100")


def modifier_check(ctx, pct: Decimal) -> Check:
    """The ABSOLUTE bound: the new modifier itself within +/-ceiling."""
    ceiling = ctx.limits.max_bid_modifier_pct
    return Check("max_bid_modifier_pct", abs(float(pct)) <= ceiling,
                 f"{render_pct(pct)} against +/-{ceiling:.0f}%")


def modifier_change_checks(ctx, before: Decimal, after: Decimal) -> list[Check]:
    """The absolute bound AND a bound on the CHANGE.

    The absolute bound alone lets -50% -> 0% through, which doubles every bid
    it touches. So the relative increase in the multiplier must also stay
    within max_bid_modifier_pct. Lifting an exclusion (from 0) has no ratio;
    it is projected as resumed serving instead (see modifier_projection).
    """
    ceiling = ctx.limits.max_bid_modifier_pct
    old, new = multiplier(before), multiplier(after)
    if old == 0:
        change = Check("max_bid_modifier_increase_pct", True,
                       "lifts an exclusion: no ratio, projected as resumed serving",
                       skipped=True)
    else:
        rise = (new / old - 1) * 100
        change = Check("max_bid_modifier_increase_pct", float(rise) <= ceiling,
                       f"multiplier {old:.2f} -> {new:.2f} ({rise:+.0f}%) "
                       f"against +{ceiling:.0f}%")
    return [modifier_check(ctx, after), change]


def modifier_reapply_checks(ctx, entry: dict[str, Any]) -> list[Check]:
    """modifier_change_checks for moving ``entry['after']`` back to ``entry['before']``."""
    def pct(rendered: str) -> Decimal:
        return Decimal("0") if rendered == NOTHING else pct_of(rendered)

    return modifier_change_checks(ctx, pct(entry["after"]), pct(entry["before"]))


def _modifier_pct(criterion) -> Decimal:
    """A criterion's modifier as a percentage, -100 when it is EXCLUDED."""
    return pct_from_modifier(modifier_of(criterion))


def modifier_projection(ctx, campaign_id: str, before: Decimal, after: Decimal,
                        where: str) -> tuple[Decimal, str, list[str]]:
    """(projected, note, warnings) for moving a modifier from ``before`` to ``after`` %.

    Leaving -100% is an UN-exclusion: the campaign starts serving somewhere it
    never did, bounded only by its unused budget.
    """
    if before <= -100 < after:
        projected, note = campaign_headroom(ctx, campaign_id)
        return projected, note, [
            f"{where} is EXCLUDED today (-100%); this resumes serving there, "
            "up to the campaign's unused budget"
        ]
    # From the move in the multiplier, not the sign of the new value: -50% ->
    # -10% raises every bid it touches, +40% -> +10% lowers them.
    if multiplier(after) > multiplier(before):
        projected, note = campaign_headroom(ctx, campaign_id)
        return projected, note, []
    return Decimal("0"), "A lower multiplier can only reduce spend.", []


def _campaign(ctx, campaign_id: str):
    return campaigns.find(ctx.client, campaign_id=campaign_id)


def _criteria(ctx, campaign_id: str, kind: str, select: tuple[str, ...]):
    return ctx.client.query(Query(
        select=("campaign_criterion.criterion_id", "campaign_criterion.resource_name",
                "campaign_criterion.negative", "campaign_criterion.bid_modifier", *select),
        from_resource="campaign_criterion",
        where=(f"campaign.id = {int(campaign_id)}", f"campaign_criterion.type = '{kind}'",
               "campaign_criterion.status != 'REMOVED'"),
    ).to_gaql())


class _AddCampaignCriteria(CreatesRemovable):
    """Shared shape: a batch of new campaign criteria, one ledger row each."""

    resource = "campaign_criterion"
    status_field = "status"
    entity_type = ""
    widens = True

    def _existing_keys(self, ctx, campaign_id: str) -> set[str]:  # pragma: no cover
        raise NotImplementedError

    def _key(self, spec: dict[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    def _propose(self, ctx, campaign_id: str, specs: list[dict[str, Any]], *,
                 checks: list[Check] | None = None, note: str | None = None,
                 warnings: list[str] | None = None, widens: bool = True,
                 narrows: bool = False) -> Proposal:
        if not specs:
            raise ValueError("Nothing to add.")
        row = _campaign(ctx, campaign_id)
        existing = self._existing_keys(ctx, campaign_id)
        dupes = [targeting.criterion_spec_label(s) for s in specs if self._key(s) in existing]
        checks = [
            Check("not_already_present", not dupes,
                  "none present yet" if not dupes else f"already present: {', '.join(dupes)}"),
            *(checks or []),
        ]
        if widens:
            projected, bound = campaign_headroom(ctx, campaign_id)
        else:
            projected, bound = Decimal("0"), "This can only narrow delivery."
        items = [
            LedgerItem(self.entity_type, f"{campaign_id}:{self._key(s)}",
                       targeting.criterion_spec_label(s), "created", NOTHING, "ENABLED",
                       {"campaign_id": str(campaign_id), "spec": s})
            for s in specs
        ]
        return Proposal(
            entity_type=self.entity_type, entity_id=str(campaign_id),
            entity_name=f"{len(specs)} {self.entity_type}(s) on {row.campaign.name}",
            path=row.campaign.name, field_name="created", before=NOTHING,
            after="; ".join(targeting.criterion_spec_label(s) for s in specs),
            checks=checks, projected=projected, campaign_id=str(row.campaign.id),
            campaign_status=row.campaign.status.name, items=items, batch=True,
            warnings=warnings or [], note=" ".join(p for p in (note, bound) if p),
            narrows=narrows,
            payload={"campaign_id": str(campaign_id), "specs": specs},
        )

    def current(self, ctx, payload: dict[str, Any]) -> str:
        existing = self._existing_keys(ctx, payload["campaign_id"])
        present = [targeting.criterion_spec_label(s) for s in payload["specs"]
                   if self._key(s) in existing]
        return NOTHING if not present else f"already present: {', '.join(present)}"

    def build(self, ctx, payload: dict[str, Any]):
        return targeting.plan_add_campaign_criteria(
            ctx.client, campaign_id=payload["campaign_id"], specs=payload["specs"]
        )


IdList = Annotated[list[str], Field(description="Ids from find_locations / find_languages")]


class CampaignLocations(_AddCampaignCriteria):
    name = "campaign_locations"
    preview_title = "Preview adding or excluding campaign locations"
    apply_title = "Apply campaign location targeting (AFFECTS DELIVERY)"
    ledger_keys = (("location", "created"),)
    entity_type = "location"

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        add: Annotated[list[str], Field(description="geo_target_ids to TARGET (find_locations)")] = [],  # noqa: B006
        exclude: Annotated[list[str], Field(description="geo_target_ids to EXCLUDE")] = [],  # noqa: B006
    ) -> Proposal:
        """Show what targeting or excluding locations would do. Writes nothing.

        Adding a target can widen delivery (bounded by the campaign's unused
        budget); an exclusion can only narrow it. A campaign with no location
        targets serves everywhere, so its FIRST target narrows it -- the
        preview says which case this is.
        """
        specs = [{"kind": "location", "geo_target_id": str(int(g)), "negative": False} for g in add]
        specs += [{"kind": "location", "geo_target_id": str(int(g)), "negative": True} for g in exclude]
        current_targets = [r for r in _criteria(ctx, campaign_id, "LOCATION",
                                                ("campaign_criterion.location.geo_target_constant",))
                           if not r.campaign_criterion.negative]
        warnings = []
        if add and not current_targets:
            warnings.append(
                "the campaign has no location targets today, i.e. it serves EVERYWHERE; "
                "adding one restricts it to the listed locations"
            )
        return self._propose(ctx, campaign_id, specs, warnings=warnings,
                             widens=bool(add) and bool(current_targets),
                             narrows=bool(exclude) or (bool(add) and not current_targets))

    def _key(self, spec):
        return f"{'-' if spec['negative'] else '+'}{spec['geo_target_id']}"

    def _existing_keys(self, ctx, campaign_id):
        out = set()
        for r in _criteria(ctx, campaign_id, "LOCATION",
                           ("campaign_criterion.location.geo_target_constant",)):
            geo = r.campaign_criterion.location.geo_target_constant.rsplit("/", 1)[-1]
            out.add(f"{'-' if r.campaign_criterion.negative else '+'}{geo}")
        return out


class CampaignLanguages(_AddCampaignCriteria):
    name = "campaign_languages"
    preview_title = "Preview adding campaign language targets"
    apply_title = "Apply campaign language targeting (AFFECTS DELIVERY)"
    ledger_keys = (("language", "created"),)
    entity_type = "language"

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        language_ids: Annotated[list[str], Field(description="From find_languages", min_length=1)],
    ) -> Proposal:
        """Show what adding language targets would do. Writes nothing.

        A campaign with NO language targets serves in all languages; its first
        language target narrows it to that language."""
        specs = [{"kind": "language", "language_id": str(int(i))} for i in language_ids]
        has_any = bool(_criteria(ctx, campaign_id, "LANGUAGE",
                                 ("campaign_criterion.language.language_constant",)))
        warnings = [] if has_any else [
            "the campaign targets ALL languages today; adding one restricts it to the listed ones"
        ]
        return self._propose(ctx, campaign_id, specs, warnings=warnings, widens=has_any,
                             narrows=not has_any)

    def _key(self, spec):
        return spec["language_id"]

    def _existing_keys(self, ctx, campaign_id):
        return {
            r.campaign_criterion.language.language_constant.rsplit("/", 1)[-1]
            for r in _criteria(ctx, campaign_id, "LANGUAGE",
                               ("campaign_criterion.language.language_constant",))
        }


class ScheduleWindow(BaseModel):
    day: Literal["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    start_hour: int = Field(ge=0, le=23, description="Account time zone, 0-23")
    end_hour: int = Field(ge=1, le=24, description="Exclusive, 1-24")
    bid_modifier_pct: str | None = Field(default=None, description='Optional, e.g. "+10"')


class AdSchedule(_AddCampaignCriteria):
    name = "ad_schedule"
    preview_title = "Preview adding ad schedule windows to a campaign"
    apply_title = "Apply ad schedule windows (AFFECTS DELIVERY)"
    ledger_keys = (("ad schedule", "created"),)
    entity_type = "ad schedule"

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        windows: Annotated[list[ScheduleWindow], Field(min_length=1,
                           description="Day/hour windows; read the hour_of_day report first")],
    ) -> Proposal:
        """Show what adding ad schedule windows would do. Writes nothing.

        A campaign with NO schedule serves around the clock; its first window
        restricts it to the listed windows. Adding windows to an existing
        schedule adds serving hours, bounded by the campaign's unused budget.
        Optional bid modifiers are bounded by max_bid_modifier_pct."""
        specs, checks = [], []
        for w in windows:
            window = ScheduleWindow.model_validate(w) if not isinstance(w, ScheduleWindow) else w
            if window.end_hour <= window.start_hour:
                raise ValueError(f"{window.day}: end_hour must be after start_hour.")
            spec: dict[str, Any] = {"kind": "schedule", "day": window.day,
                                    "start_hour": window.start_hour, "end_hour": window.end_hour}
            if window.bid_modifier_pct is not None:
                pct = parse_pct(window.bid_modifier_pct)
                spec["bid_modifier_pct"] = str(pct)
                checks.append(modifier_check(ctx, pct))
            specs.append(spec)
        has_any = bool(_criteria(ctx, campaign_id, "AD_SCHEDULE",
                                 ("campaign_criterion.ad_schedule.day_of_week",)))
        raises = any(Decimal(s.get("bid_modifier_pct", "0")) > 0 for s in specs)
        warnings = [] if has_any else [
            "the campaign has no schedule today, i.e. it serves around the clock; "
            "adding windows restricts it to them"
        ]
        # With windows already in place, a new one adds serving hours whatever
        # its modifier; only a campaign's FIRST windows restrict it.
        return self._propose(ctx, campaign_id, specs, checks=checks, warnings=warnings,
                             widens=has_any or raises, narrows=not has_any)

    def _key(self, spec):
        return f"{spec['day']}:{spec['start_hour']}-{spec['end_hour']}"

    def _existing_keys(self, ctx, campaign_id):
        return {
            f"{r.campaign_criterion.ad_schedule.day_of_week.name}:"
            f"{r.campaign_criterion.ad_schedule.start_hour}-{r.campaign_criterion.ad_schedule.end_hour}"
            for r in _criteria(ctx, campaign_id, "AD_SCHEDULE", (
                "campaign_criterion.ad_schedule.day_of_week",
                "campaign_criterion.ad_schedule.start_hour",
                "campaign_criterion.ad_schedule.end_hour",
            ))
        }


class DeviceBidModifier(ChangeKind):
    name = "device_bid_modifier"
    preview_title = "Preview a campaign device bid modifier"
    apply_title = "Apply a device bid modifier (SPENDS MONEY)"
    ledger_keys = (("campaign", "device_bid_modifier"),)

    def _state(self, ctx, campaign_id: str, device: str) -> tuple[str, str | None]:
        """(rendered modifier or NOTHING, criterion_id)."""
        for r in _criteria(ctx, campaign_id, "DEVICE", ("campaign_criterion.device.type",)):
            if r.campaign_criterion.device.type_.name == device:
                return (render_pct(_modifier_pct(r.campaign_criterion)),
                        str(r.campaign_criterion.criterion_id))
        return NOTHING, None

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        device: Annotated[Literal["MOBILE", "DESKTOP", "TABLET"], Field(description="Device")],
        bid_modifier_pct: Annotated[str, Field(description='Signed percent, e.g. "-20" or "+15"')],
    ) -> Proposal:
        """Show what a device bid modifier would do. Writes nothing.

        Bounded by max_bid_modifier_pct either way, and so is the relative
        increase in the multiplier versus today (-50% -> 0% doubles bids). A
        higher multiplier than today projects the campaign's unused budget; a
        lower one projects 0. "-100" stops serving on that device entirely and
        needs the limit raised to 100."""
        pct = parse_pct(bid_modifier_pct)
        row = _campaign(ctx, campaign_id)
        before, criterion_id = self._state(ctx, campaign_id, device)
        before_pct = Decimal("0") if before == NOTHING else pct_of(before)
        projected, note, warnings = modifier_projection(ctx, campaign_id, before_pct, pct,
                                                        device)
        return Proposal(
            entity_type="campaign", entity_id=f"{campaign_id}~{device}",
            entity_name=f"{row.campaign.name} on {device}", path=row.campaign.name,
            field_name="device_bid_modifier", before=before, after=render_pct(pct),
            checks=modifier_change_checks(ctx, before_pct, pct), projected=projected,
            campaign_id=str(row.campaign.id), note=note, warnings=warnings,
            narrows=multiplier(pct) < multiplier(before_pct),
            items=[LedgerItem("campaign", f"{campaign_id}~{device}",
                              f"{row.campaign.name} on {device}", "device_bid_modifier",
                              before, render_pct(pct),
                              {"campaign_id": str(campaign_id), "device": device,
                               "criterion_id": criterion_id})],
            payload={"campaign_id": str(campaign_id), "device": device, "pct": str(pct),
                     "criterion_id": criterion_id},
        )

    def current(self, ctx, payload):
        return self._state(ctx, payload["campaign_id"], payload["device"])[0]

    def build(self, ctx, payload):
        if payload["criterion_id"]:
            return targeting.plan_set_campaign_criterion_modifier(
                ctx.client, campaign_id=payload["campaign_id"],
                criterion_id=payload["criterion_id"], pct=Decimal(payload["pct"]),
            )
        return targeting.plan_add_campaign_criteria(
            ctx.client, campaign_id=payload["campaign_id"],
            specs=[{"kind": "device", "device": payload["device"],
                    "bid_modifier_pct": payload["pct"]}],
        )

    def current_for_entry(self, ctx, entry):
        detail = entry.get("detail") or {}
        return self._state(ctx, detail["campaign_id"], detail["device"])[0]

    def reapply_checks(self, ctx, entry):
        return modifier_reapply_checks(ctx, entry)

    def inverse(self, ctx, entry):
        detail = entry.get("detail") or {}
        if entry["before"] == NOTHING:
            if not entry.get("resource_name"):
                raise ValueError("No resource name recorded for the created device criterion.")
            return criteria.plan_remove_created(ctx.client, resource_names=[entry["resource_name"]])
        criterion_id = detail.get("criterion_id") or self._state(
            ctx, detail["campaign_id"], detail["device"])[1]
        return targeting.plan_set_campaign_criterion_modifier(
            ctx.client, campaign_id=detail["campaign_id"], criterion_id=criterion_id,
            pct=pct_of(entry["before"]),
        )


class LocationBidModifier(ChangeKind):
    name = "location_bid_modifier"
    preview_title = "Preview a bid modifier on a targeted location"
    apply_title = "Apply a location bid modifier (SPENDS MONEY)"
    ledger_keys = (("location", "bid_modifier"),)

    def _row(self, ctx, campaign_id: str, criterion_id: str):
        found = one(ctx, ("campaign_criterion.criterion_id", "campaign_criterion.bid_modifier",
                          "campaign_criterion.negative", "campaign_criterion.type",
                          "campaign_criterion.location.geo_target_constant", "campaign.name"),
                    "campaign_criterion",
                    [f"campaign.id = {int(campaign_id)}",
                     f"campaign_criterion.criterion_id = {int(criterion_id)}"])
        if found is None or found.campaign_criterion.type_.name != "LOCATION" \
                or found.campaign_criterion.negative:
            raise LookupError(
                f"No targeted location {criterion_id} on campaign {campaign_id}. "
                "get_campaign lists them."
            )
        return found

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        criterion_id: Annotated[str, Field(description="From get_campaign's locations")],
        bid_modifier_pct: Annotated[str, Field(description='Signed percent, e.g. "+15"')],
    ) -> Proposal:
        """Show what a bid modifier on one targeted location would do. Writes
        nothing. Bounded by max_bid_modifier_pct either way, and so is the
        relative increase in the multiplier versus today."""
        pct = parse_pct(bid_modifier_pct)
        found = self._row(ctx, campaign_id, criterion_id)
        geo = found.campaign_criterion.location.geo_target_constant
        before_pct = _modifier_pct(found.campaign_criterion)
        projected, note, warnings = modifier_projection(ctx, campaign_id, before_pct, pct,
                                                        f"location {geo}")
        return Proposal(
            entity_type="location", entity_id=f"{campaign_id}~{criterion_id}",
            entity_name=f"{geo} on {found.campaign.name}", path=found.campaign.name,
            field_name="bid_modifier",
            before=render_pct(before_pct),
            after=render_pct(pct), checks=modifier_change_checks(ctx, before_pct, pct),
            projected=projected, narrows=multiplier(pct) < multiplier(before_pct),
            campaign_id=str(campaign_id), note=note, warnings=warnings,
            payload={"campaign_id": str(campaign_id), "criterion_id": str(criterion_id),
                     "pct": str(pct)},
        )

    def _rendered(self, ctx, campaign_id, criterion_id):
        return render_pct(_modifier_pct(self._row(ctx, campaign_id, criterion_id).campaign_criterion))

    def current(self, ctx, payload):
        return self._rendered(ctx, payload["campaign_id"], payload["criterion_id"])

    def build(self, ctx, payload):
        return targeting.plan_set_campaign_criterion_modifier(
            ctx.client, campaign_id=payload["campaign_id"],
            criterion_id=payload["criterion_id"], pct=Decimal(payload["pct"]),
        )

    def current_for_entry(self, ctx, entry):
        return self._rendered(ctx, *entry["entity_id"].split("~"))

    def reapply_checks(self, ctx, entry):
        return modifier_reapply_checks(ctx, entry)

    def inverse(self, ctx, entry):
        campaign_id, criterion_id = entry["entity_id"].split("~")
        return targeting.plan_set_campaign_criterion_modifier(
            ctx.client, campaign_id=campaign_id, criterion_id=criterion_id,
            pct=pct_of(entry["before"]),
        )


def _networks(ctx, campaign_id: str) -> tuple[str, dict[str, bool], Any]:
    found = one(ctx, ("campaign.id", "campaign.name", "campaign.status",
                      "campaign.network_settings.target_search_network",
                      "campaign.network_settings.target_content_network"),
                "campaign", [f"campaign.id = {int(campaign_id)}"])
    if found is None:
        raise LookupError(f"No campaign with id {campaign_id}.")
    state = {
        "search_partners": bool(found.campaign.network_settings.target_search_network),
        "display_network": bool(found.campaign.network_settings.target_content_network),
    }
    return _render_networks(state), state, found


def _render_networks(state: dict[str, bool]) -> str:
    return ", ".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(state.items()))


class CampaignNetworks(ChangeKind):
    name = "campaign_networks"
    preview_title = "Preview turning Search Partners or Display expansion on or off"
    apply_title = "Apply a campaign network change (AFFECTS DELIVERY)"
    ledger_keys = (("campaign", "networks"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[str, Field(description="From find_campaigns")],
        search_partners: Annotated[bool | None, Field(description="Search Partners on/off")] = None,
        display_network: Annotated[
            bool | None, Field(description="Display expansion on/off (Search campaigns)")
        ] = None,
    ) -> Proposal:
        """Show what switching Search Partners or Display expansion would do.
        Writes nothing. Turning one ON can spend up to the campaign's unused
        budget on inventory it does not reach today."""
        if search_partners is None and display_network is None:
            raise ValueError("Pass search_partners and/or display_network.")
        before, state, found = _networks(ctx, campaign_id)
        after_state = dict(state)
        if search_partners is not None:
            after_state["search_partners"] = search_partners
        if display_network is not None:
            after_state["display_network"] = display_network
        widens = any(after_state[k] and not state[k] for k in state)
        projected, note = (campaign_headroom(ctx, campaign_id) if widens
                           else (Decimal("0"), "Turning a network off can only reduce spend."))
        return Proposal(
            entity_type="campaign", entity_id=str(campaign_id), entity_name=found.campaign.name,
            path=found.campaign.name, field_name="networks", before=before,
            after=_render_networks(after_state),
            checks=[Check("is_a_change", after_state != state,
                          "differs from today" if after_state != state else "identical to today")],
            projected=projected, campaign_id=str(campaign_id),
            campaign_status=found.campaign.status.name, note=note,
            # Any network turned OFF is one its revert turns back on.
            narrows=any(state[k] and not after_state[k] for k in state),
            items=[LedgerItem("campaign", str(campaign_id), found.campaign.name, "networks",
                              before, _render_networks(after_state),
                              {"before": state, "after": after_state})],
            payload={"campaign_id": str(campaign_id), "after_state": after_state},
        )

    def current(self, ctx, payload):
        return _networks(ctx, payload["campaign_id"])[0]

    def build(self, ctx, payload):
        state = payload["after_state"]
        return targeting.plan_set_networks(
            ctx.client, campaign_id=payload["campaign_id"],
            search_partners=state["search_partners"], display_network=state["display_network"],
        )

    def current_for_entry(self, ctx, entry):
        return _networks(ctx, entry["entity_id"])[0]

    def inverse(self, ctx, entry):
        before = (entry.get("detail") or {}).get("before")
        if not before:
            raise ValueError("This entry carries no structured before-state to restore.")
        return targeting.plan_set_networks(
            ctx.client, campaign_id=entry["entity_id"],
            search_partners=before["search_partners"], display_network=before["display_network"],
        )


class AudienceTargeting(CreatesRemovable):
    name = "audience_targeting"
    preview_title = "Preview adding audience segments to an ad group"
    apply_title = "Apply adding audience segments (AFFECTS DELIVERY IN TARGETING MODE)"
    ledger_keys = (("audience", "created"),)
    resource = "ad_group_criterion"
    status_field = "status"

    @staticmethod
    def _mode(row) -> str:
        """The ad group's AUDIENCE targeting mode. No setting = observation."""
        for item in row.ad_group.targeting_setting.target_restrictions:
            if item.targeting_dimension.name == "AUDIENCE":
                return "observation" if item.bid_only else "targeting"
        return "observation"

    def _existing(self, ctx, ad_group_id: str) -> set[str]:
        rows = ctx.client.query(Query(
            select=("ad_group_criterion.user_list.user_list",),
            from_resource="ad_group_criterion",
            where=(f"ad_group.id = {int(ad_group_id)}", "ad_group_criterion.type = 'USER_LIST'",
                   "ad_group_criterion.status != 'REMOVED'"),
        ).to_gaql())
        return {r.ad_group_criterion.user_list.user_list.rsplit("/", 1)[-1] for r in rows}

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[str, Field(description="From find_ad_groups")],
        user_list_ids: Annotated[list[str], Field(description="From list_audiences", min_length=1)],
        mode: Annotated[
            Literal["observation", "targeting"],
            Field(description="Must match the ad group's current audience mode"),
        ] = "observation",
    ) -> Proposal:
        """Show what adding audience segments (user lists) to an ad group
        would do. Writes nothing.

        Observation mode only reports on them (and allows bid modifiers);
        targeting mode RESTRICTS the ad group to them. The mode is a setting
        on the ad group that governs every audience in it, so this does not
        switch it: `mode` must match what the ad group already uses."""
        ids = [str(int(i)) for i in user_list_ids]
        row = adgroups.find(ctx.client, ad_group_id=ad_group_id)
        detail = one(ctx, ("ad_group.targeting_setting.target_restrictions",), "ad_group",
                     [f"ad_group.id = {int(ad_group_id)}"])
        current_mode = self._mode(detail) if detail is not None else "observation"
        existing = self._existing(ctx, ad_group_id)
        dupes = [i for i in ids if i in existing]
        checks = [
            Check("mode_matches", mode == current_mode,
                  f"ad group is in {current_mode} mode" + (
                      "" if mode == current_mode else
                      f"; switching to {mode} changes every audience in it -- do that in the UI")),
            Check("not_already_present", not dupes,
                  "none present yet" if not dupes else f"already present: {', '.join(dupes)}"),
        ]
        items = [LedgerItem("audience", f"{ad_group_id}:{i}", f"user list {i}", "created",
                            NOTHING, "ENABLED", {"ad_group_id": str(ad_group_id), "user_list_id": i})
                 for i in ids]
        return Proposal(
            entity_type="audience", entity_id=str(ad_group_id),
            entity_name=f"{len(ids)} audience(s) on {row.ad_group.name}",
            path=f"{row.campaign.name} > {row.ad_group.name}", field_name="created",
            before=NOTHING, after=f"{len(ids)} user list(s), {mode}", checks=checks,
            projected=Decimal("0"), campaign_id=str(row.campaign.id), items=items, batch=True,
            narrows=current_mode == "targeting",
            note=("Observation adds reporting only; targeting can only narrow who sees the "
                  "ads. Neither raises spend on its own, so the projected delta is 0."),
            payload={"ad_group_id": str(ad_group_id), "user_list_ids": ids},
        )

    def current(self, ctx, payload):
        present = [i for i in payload["user_list_ids"] if i in self._existing(ctx, payload["ad_group_id"])]
        return NOTHING if not present else f"already present: {', '.join(present)}"

    def build(self, ctx, payload):
        return targeting.plan_add_ad_group_audiences(
            ctx.client, ad_group_id=payload["ad_group_id"], user_list_ids=payload["user_list_ids"]
        )
