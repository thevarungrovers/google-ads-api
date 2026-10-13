---
name: google-ads
description: Use the `google-ads` MCP server to read a Google Ads account and change it safely. Covers the preview/apply approval loop, campaign and ad group pausing, daily budgets, ad group CPC bids, performance reports, guardrails, the change ledger and undo. Use whenever the task involves Google Ads, AdWords, campaigns, ad groups, CPC bids, daily budgets, GAQL, campaign reports, or the apply_*/preview_* tools on that server.
---

# Google Ads

The `google-ads` MCP server talks to **one real, live advertising account**.

Campaigns are serving and spending today. A mistake costs money now, not at the
next deploy.

## The one rule

**Read → preview → show the human → apply.** Never skip a step.

```
1. READ     find_campaigns / find_ad_groups / run_report, then get_guardrails
2. PREVIEW  preview_<thing>  ->  returns a preview_token
3. SHOW     put the preview in front of the human, in your own words
4. APPLY    apply_<thing>(preview_token="...")
```

Step 3 is the authorization. The human approving the `apply_*` permission prompt
**is** the sign-off — so never call an `apply_*` without having just shown them
what it does.

## DO

- **DO start with `find_campaigns`.** Campaign and ad group ids are not shown
  anywhere in the Google Ads web UI, so this is the only way to get one. It
  matches on name.
- **DO call `get_guardrails` before proposing a change.** It tells you the
  bounds and how much of this session's budget is left, so you propose something
  that can be approved rather than something that will be refused.
- **DO call `run_report` first** when the change is about performance. Deciding
  a bid without looking at what the entity has been doing is guessing.
- **DO write money as a decimal string**: `"2.50"` is two dollars fifty.
- **DO read the whole preview**, especially `projected_daily_spend_delta`,
  `warnings` and `checks`.
- **DO tell the human the `entry_id`.** It is what `revert_change` takes.

## DON'T

- **DON'T call an `apply_*` without showing the preview first.** The approval
  prompt is meaningless if nobody saw what it approves.
- **DON'T pass micros.** `"2500000"` where `"2.50"` was meant is a millionfold
  error, and the number is rendered verbatim in the prompt a human approves. The
  tool refuses anything over 1000 for this reason, but do not rely on that.
- **DON'T re-use a `preview_token`.** Single use, 10 minutes, and not
  interchangeable between tools.
- **DON'T try to set anything to REMOVED.** It is irreversible in Google Ads and
  is not exposed. Pause instead — same effect, and it can be undone.
- **DON'T try to create a campaign.** It is not exposed; a human runs
  `scripts/manage.py campaign new`, because it carries media and a legal
  declaration someone has to answer for.
- **DON'T work around the kill switch.** If a tool says writes are disabled, a
  person turned them off. Say so and stop.

## The tools

15 in total: 6 read, 4 `preview_*`/`apply_*` pairs, plus `revert_change`.

### Read — safe, call freely

| Tool | Gives you |
|---|---|
| `list_accounts` | Which accounts these credentials can reach |
| `find_campaigns(name_contains, status, limit)` | Campaigns and **their ids**, which the UI does not show |
| `find_ad_groups(campaign_id, name_contains, limit)` | Ad groups and their ids |
| `run_report(report, days, start_date, end_date, campaign_id, limit)` | Performance. `report` is one of `accounts`, `campaigns`, `keywords` |
| `get_guardrails` | The bounds, and this session's remaining budget |
| `list_my_changes(limit)` | What this server has changed, with `entry_id`s for undo |

### Change — preview first, always

| Pair | Changes |
|---|---|
| `preview_campaign_status` / `apply_campaign_status` | Pause or enable a **whole campaign** |
| `preview_campaign_daily_budget` / `apply_campaign_daily_budget` | A campaign's daily budget |
| `preview_ad_group_status` / `apply_ad_group_status` | Pause or enable an ad group |
| `preview_ad_group_cpc_bid` / `apply_ad_group_cpc_bid` | An ad group's CPC bid |
| `revert_change(entry_id)` | Undo one ledger entry |

Each `apply_*` takes **only** a `preview_token` — never the values. That is
deliberate: it means an apply cannot be called cold, with nothing shown first.

### Not here, on purpose

Creating or removing campaigns, ad groups and ads. Setting any status to
REMOVED. Changing a **shared** budget, which would silently move every campaign
using it. Anything touching billing or account access — those services are not
on the write client's allowlist at all.

There is **no generic mutate/passthrough tool**, so if an operation is not in
the tables above it cannot be done from here. If the human needs one, say so and
point them at `scripts/manage.py`.

## Reading the preview

| Field | Why it matters |
|---|---|
| `entity_name` / `path` | `brand - search` reads differently from `campaign 12345` |
| `before` / `after` | In currency units, already converted out of micros |
| `metrics_7d` | What it has actually been doing |
| `projected_daily_spend_delta` | Upper bound on extra spend per day |
| `checks` | One line per bound, `OK` or `FAIL` |
| `blocked` | `true` means a guardrail failed and the apply will refuse |

Two asymmetries worth repeating to the human:

- **Pausing** cannot increase spend, so its projected delta is `0`.
- **Enabling** is bounded by the **daily budget**, not by past spend — a
  campaign that spent nothing last week can still spend its full budget tomorrow.

If `blocked: true`, do not try the apply. Read the `FAIL` line and propose
something inside the bounds.

## Undo

```
revert_change(entry_id="...")
```

It refuses if the entry was never applied, if it has already been reverted, or
if **the value has moved since** — in that last case somebody edited it in the
Google Ads UI after your change, and reverting would discard their edit rather
than undo yours.

## When something goes wrong

| Message | What it means | What to do |
|---|---|---|
| `unknown, already used, or older than 10 minutes` | Tokens are single-use | Take a fresh preview |
| `has changed since the preview` | Somebody edited it in between | Re-preview, look at the new numbers, decide again |
| `failed a guardrail check` | The preview was blocked | Read `checks`, propose something smaller |
| `writes are disabled: ...` | The kill switch | A person set it. Say so and stop; do not work around it |
| `session` limit reached | Per-session cap | Show `list_my_changes` and let the human decide |
| `looks like micros, not currency` | You passed `2500000` for `$2.50` | Pass the decimal string |
| `REMOVED is not available here` | Working as intended | Use `PAUSED` |

## A worked example

> "Cut the budget on the brand search campaign to $40/day."

```
1. find_campaigns(name_contains="brand")   -> the id, which the UI never shows
2. get_guardrails()                        -> max_daily_budget, max_budget_change_pct,
                                              and how much session budget is left
3. run_report(report="campaigns", days=30, campaign_id=...)
                                           -> what it has actually been spending
4. preview_campaign_daily_budget(campaign_id=..., new_daily_budget="40.00")
5. Summarise: current budget, new budget, the 7-day numbers, the projected daily
   delta, and any FAIL line.
6. apply_campaign_daily_budget(preview_token="...")
7. Report `applied` and the `entry_id` for undo.
```

If step 4 comes back `blocked: true` because the cut exceeds
`max_budget_change_pct`, do not retry it smaller without saying so — tell the
human the bound, and what change would fit inside it.
