---
name: google-ads
description: Use the `google-ads` MCP server to read a Google Ads account and change it safely. Covers the preview/apply approval loop, campaign/ad group/keyword/ad status, budgets (including shared), CPC bids, bidding strategies, keywords and negative keywords, search terms, locations, languages, ad schedule, bid modifiers, networks, audiences, responsive search ads, sitelinks/callouts/snippets, creating campaigns, Google's recommendations, labels, performance reports, GAQL, guardrails and the session/daily caps, the change ledger and undo. Use whenever the task involves Google Ads, AdWords, campaigns, ad groups, keywords, CPC bids, budgets, GAQL, reports, or the apply_*/preview_* tools on that server.
---

# Google Ads

The `google-ads` MCP server talks to **one real, live advertising account**.
Campaigns are serving and spending today. A mistake costs money now.

## The one rule

**Read → preview → show the human → apply.** Never skip a step.

```
1. READ     find_* / get_* / list_* / run_report, then get_guardrails
2. PREVIEW  preview_<thing>  ->  returns a preview_token
3. SHOW     put the preview in front of the human, in your own words
4. APPLY    apply_<thing>(preview_token="...")
```

Step 3 is the authorization. The human approving the `apply_*` permission prompt
**is** the sign-off — never call an `apply_*` without having just shown them what
it does.

## DO

- **DO get ids from the read tools.** The Google Ads UI shows almost none.
  `find_campaigns`, `find_ad_groups`, `list_keywords`, `list_ads`,
  `list_labels`, `list_audiences`, `find_locations`, `find_languages`.
- **DO read the current state before proposing**: `get_campaign` (status
  reasons, bidding, budget and whether it is `limited_by_budget`, networks,
  locations, schedule) and `get_ad_group` (`bid_is_effective`, counts).
- **DO call `get_guardrails` first.** It shows the bounds and what is left of
  the **session** and **daily** change budgets.
- **DO write money as a decimal string**: `"2.50"`. Bid modifiers as signed
  percentages: `"+20"`, `"-15"`. Target ROAS as a ratio: `"4.0"` = 400%.
- **DO read the whole preview**: `checks` (OK / FAIL / SKIP),
  `projected_daily_spend_delta`, `warnings`, `note`, and for batches `items`.
- **DO tell the human the `entry_id`** (and the `batch_id` for a batch). It is
  what `revert_change` takes.
- **DO ask the human** for the EU political advertising declaration before
  `preview_create_campaign`. Never assume it.

## DON'T

- **DON'T call an `apply_*` without showing the preview first.**
- **DON'T pass micros.** `"2500000"` for `"2.50"` is a millionfold error.
- **DON'T re-use a `preview_token`.** Single use, 10 minutes, one tool.
- **DON'T try to remove anything.** No status is ever set to REMOVED; pausing
  is the undo. Created campaigns, ad groups and ads are never removed here.
- **DON'T work around a cap or the kill switch.** Say what refused and stop.
- **DON'T apply a recommendation without reading it** against `run_report`.
  Google is paid by the click.

## The caps

| Cap | Scope | Resets |
|---|---|---|
| `max_applies_per_session`, `max_projected_session_delta` | this server process | on restart |
| `max_applies_per_day`, `max_projected_delta_per_day_total` | the local calendar day, every process, from the ledger | local midnight |
| `max_projected_delta_per_day` | one change | — |

- A **batch** (20 keywords) counts as one apply. A **creation** counts as 3.
  A **revert** counts as 1 and projects the spend it restores.
- **Re-enabling** (campaign, ad group, keyword, ad) is exempt from the
  per-change limit — the check reads `SKIP` — and is counted in full against the
  daily total. The preview says "uses X of today's remaining Y".
- Bid and budget **increases** must pass both the per-change limit and the
  daily total. **Pausing** never uses any of it.

## The tools

### Read — safe, call freely

| Tool | Gives you |
|---|---|
| `list_accounts` | Which accounts the credentials reach |
| `find_campaigns`, `find_ad_groups` | Ids, status, budget / bid |
| `get_campaign`, `get_ad_group` | One entity in full, with 7- and 30-day totals |
| `run_report(report, days, ..., segment_by_date)` | `campaigns`, `ad_groups`, `keywords`, `search_terms`, `ads`, `locations`, `devices`, `hour_of_day`, `audiences`, `assets`, `conversions_by_action`, `accounts`. One row per entity unless `segment_by_date`; says `truncated` and `total_rows_available` |
| `list_keywords`, `list_negative_keywords`, `list_shared_negative_lists` | Keyword state and ids |
| `list_ads`, `list_assets`, `list_labels`, `list_audiences`, `list_conversion_actions` | Ids and text |
| `find_locations`, `find_languages` | Geo and language ids for targeting |
| `list_recommendations` | Google's suggestions, with impact |
| `list_recent_changes` | Who changed what in Google Ads (UI included) |
| `reconcile_ledger` | Which of this server's changes someone has since moved |
| `run_gaql` | Read-only escape hatch; row-limited |
| `get_guardrails`, `list_my_changes` | Bounds and budgets; what this server did |

### Change — `preview_X` / `apply_X`

| X | Changes |
|---|---|
| `campaign_status`, `ad_group_status`, `keyword_status`, `ad_status` | Pause or enable |
| `campaign_daily_budget` | A campaign's own budget |
| `shared_budget_amount` | A shared budget; lists every campaign it feeds |
| `ad_group_cpc_bid`, `keyword_cpc_bid` | Max CPC; warns when automated bidding ignores it |
| `bidding_strategy` | Manual CPC / Max clicks / Max conversions (tCPA) / Max value (tROAS); resets learning |
| `add_keywords` | Batch, one match type, created PAUSED |
| `add_negative_keywords` | Campaign or ad group level, batch; immediate |
| `add_to_shared_negative_list`, `link_shared_negative_list` | Shared negative lists |
| `campaign_locations`, `campaign_languages`, `ad_schedule` | Targeting; the first one on a campaign narrows it |
| `device_bid_modifier`, `location_bid_modifier` | Signed percentages |
| `campaign_networks` | Search Partners, Display expansion |
| `audience_targeting` | User lists on an ad group, in its current mode |
| `create_responsive_search_ad` | PAUSED; 3 applies |
| `replace_responsive_search_ad` | "Edit" an RSA: new ad + pause old, one request |
| `add_sitelinks`, `add_callouts`, `add_structured_snippets` | Asset + link, atomic |
| `create_campaign` | Whole Search/Display campaign, everything PAUSED; 3 applies |
| `create_ad_group`, `rename_campaign`, `rename_ad_group` | |
| `recommendation` | A Google recommendation, through the matching typed change |
| `low_risk_change` | Labels, URL suffix, tracking template, dismiss a recommendation |

Plus `revert_change(entry_id)` — an `entry_id`, or a `batch_id` for a whole batch.

## Errors

| Starts with | Means | Do |
|---|---|---|
| `AUTH_EXPIRED` | Credentials refused | Tell the human; they re-authorise |
| `QUOTA` | Google's rate limit | Back off; do not loop |
| `INVALID_QUERY` | Malformed request | Fix it; do not retry as is |
| `NOT_FOUND` | No such entity | Re-read the ids |
| `POLICY` | Ad policy refused it | Change the text it names |
| `TRANSIENT` | Google had a moment | Retry shortly (reads already retried) |
| `has changed since the preview` | Someone edited it | Re-preview, decide again |
| `failed a guardrail check` | Preview was blocked | Read `checks`, propose smaller |
| `session` / `daily` limit | A cap | Stop; show `list_my_changes` |
| `writes are disabled` | Kill switch | A person set it; stop |

## Recipes

**Daily check.** `get_guardrails` → `run_report(report="campaigns", days=1)` and
`days=7` → `get_campaign` on anything `limited_by_budget` or with an odd
`primary_status_reasons` → `list_recent_changes(days=1)` → `reconcile_ledger`.
Report; propose nothing unless asked.

**Wasted-spend sweep.** `run_report(report="search_terms", days=30)` → pick terms
with cost and no conversions, not already excluded (`status`) →
`list_negative_keywords` → `preview_add_negative_keywords` with the batch →
show the list and its total cost → apply → hand over the `batch_id`.

**Launch a new campaign.** Ask for the EU political advertising declaration →
`preview_create_campaign` (everything PAUSED) → apply → `preview_campaign_locations`,
`preview_campaign_languages`, `preview_add_negative_keywords` → apply each →
when the human says go: `preview_campaign_status(ENABLED)` (counts the full
budget against today's total) → apply.

**Act on recommendations.** `list_recommendations` → check each against
`run_report` → `preview_recommendation` for the ones worth doing (it is held to
the typed change's bounds) → `preview_low_risk_change(resource="recommendation",
field="dismiss")` for the rest.

**Undo.** `list_my_changes` → `revert_change(entry_id)` (or the `batch_id`). It
refuses if someone changed the value since — check `list_recent_changes`.

## Read-only installs

If `get_guardrails` reports `read_only_server: true`, no `apply_*` and no
`revert_change` exist in this session. Previews still work: show the human
exactly what you would change and let them make it elsewhere.
