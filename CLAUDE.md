# CLAUDE.md

Guidance for agents working on this repository: a Python library for the Google
Ads API (`googleads_reporting/`) and the `google-ads` MCP server built on it
(`googleads_mcp/`), which lets an AI agent read a live account and change it
through guarded preview/apply pairs.

## Running things

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest                 # the whole suite; offline, no credentials used
.venv/bin/python googleads_mcp/server.py   # the MCP server (stdio)
```

- `google-ads==33.0.0`, API `v25`, pinned. Proto field names are checked
  offline against the v25 schema by the tests -- run them after any GAQL or
  operation change.
- The suite is green at every commit. Tests never write to the real log
  database (`tests/conftest.py` redirects it).

## Rules that are not style

- **The account is live.** Never call an `apply_*` tool, `revert_change`, or a
  script with `--apply` to "check something". Read tools and previews only;
  previews send `validate_only`, which changes nothing.
- **Money is a decimal string in major units** everywhere an agent or a human
  sees it: `"2.50"`, never micros, never a float. Bid modifiers are signed
  percentages (`"+20"`). Convert at the edge with `googleads_reporting.fields`
  and `write.plan.micros`.
- **Nothing is REMOVED.** No status is ever set to REMOVED and no campaign, ad
  group, ad or budget is removed. Pausing is the undo. The only removal path is
  `write/criteria.plan_remove_created`, used by `revert_change` to undo a
  criterion/link this server created, traced through the ledger.
- **Whole entities are created PAUSED** and cost 3 applies: a campaign, an
  ad group, a responsive search ad (an "edited" RSA also costs 3; its new ad
  takes the old one's status). `add_keywords` costs 1 and is PAUSED unless
  previewed with `enabled=True`. Asset adds (sitelinks, callouts, snippets)
  cost 1 and link LIVE -- they have no paused state.
- **Only `googleads_reporting/write/` may mutate.** `tests/test_readonly_guard.py`
  scans every other file for a `mutate*` call or string.
- **`ALLOWED_MUTATE_SERVICES`** (`write/client.py`) is pinned as a whole set in
  `tests/test_write_guards.py`. Adding a service is a deliberate, reviewed edit.
- **Every tool declares all four ToolAnnotations hints**, and the tool surface
  is pinned in `tests/test_mcp_surface.py` and `install.py` (SAFE_TOOLS /
  WRITE_TOOLS). Adding a tool means editing both.
- **Deliberate refusals reach the agent** via
  `googleads_mcp/errors.refusals_reach_the_caller`; Google failures are
  prefixed AUTH_EXPIRED / QUOTA / INVALID_QUERY / NOT_FOUND / POLICY / TRANSIENT.
- **Day boundaries are LOCAL time.** The ledger stores UTC (`logdb._now()`); the
  daily cap converts local midnight to UTC before comparing.
- **This repository is public.** No company, brand, campaign, keyword or
  person names from a real account, no real customer/campaign/budget ids, no
  credentials. Fixtures use placeholders (`Example Co`, `1234567890`,
  `example.com`). Nothing under `.claude/plans/` is ever committed.
- **Every created name goes through `googleads_reporting/naming.py`.** A kind
  that creates a named entity stamps it with `ctx.name_for_create`, lists what
  it sends in `Proposal.created_names` and declares `creates_named`
  (`tests/test_mcp_surface.py` fails one that does not). Renames go through
  `ctx.name_for_rename`: the agent renames only its own entities.
- **The naming identity comes only from `.env`** (`GOOGLE_ADS_MCP_OWNER_INITIALS`,
  `GOOGLE_ADS_MCP_AGENT_NAME`; `config.env` too, `.env` winning) -- never a tool
  argument or the process env -- so there is one identity per checkout. Empty
  parts are skipped. **Real initials and agent names never appear in the repo**:
  tests and docs use `AB` / `max` (and `CD` / `bot`), and `tests/conftest.py`
  keeps the suite from reading the real `.env`.

## Adding a change kind

A write is a `ChangeKind` (`googleads_mcp/changes/base.py`); the engine
(`changes/engine.py`) runs the one preview -> apply -> revert sequence for all
of them: token, kill switch, staleness, session and daily caps, two-phase
ledger, `validate_only` in every preview.

1. Put the mutation builder in `googleads_reporting/write/` (it returns a
   `PlannedChange`; a new service also needs `_REQUEST_TYPES` and the
   allowlist).
2. Subclass `ChangeKind` in `googleads_mcp/changes/`:
   - `name` -> `preview_<name>` / `apply_<name>`; titles; `apply_annotations`;
     `weight` (3 for a whole new entity); `ledger_keys` = the (entity_type, field)
     pairs its ledger rows use.
   - `propose(self, ctx, ...)` -- its parameters ARE the preview tool's schema;
     annotate each with `Annotated[..., Field(description=...)]`; the docstring
     is the tool description. Return a `Proposal` (before/after, checks,
     projected spend, JSON-able payload, `is_enable` for an enable, `items`
     and `batch=True` for a batch).
   - `current(ctx, payload)` -- today's value, spelled exactly like `before`.
   - `build(ctx, payload)` -- the `PlannedChange`.
   - `current_for_entry` / `inverse` (and `revert_projection` if undoing it
     restores spend) for `revert_change`. A change that can only NARROW
     delivery projects 0 but its undo widens: set `Proposal.narrows` (or the
     kind's `narrows` if it always narrows) and the revert projects the
     campaigns' unused budget.
3. Add it to `changes/__init__.all_kinds()`, pin both tool names in
   `tests/test_mcp_surface.py` and `install.py`, and mention it in
   `server.INSTRUCTIONS` and the skill.
4. Tests with `tests/kindkit.py` (real v25 protos, no network): preview renders
   before/after; a FAIL blocks the apply; a stale value is refused; the inverse
   round-trips.
