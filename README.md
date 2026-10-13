# Google Ads API [![M8ven Score](https://m8ven.ai/badge/mcp/thevarungrovers/google-ads-api)](https://m8ven.ai/mcp/thevarungrovers/google-ads-api?s=readme)

Read a Google Ads account from the command line — and let an AI agent read it
too, and change it, but only after a human says yes.

Three ways in:

| | |
|---|---|
| `scripts/fetch_report.py` | **Read.** Campaign, keyword and account reports |
| `scripts/manage.py` | **Write.** Find, create, update, pause — you drive it |
| `googleads_mcp/` | **An MCP server.** An AI agent drives it, you approve |

---

## The big idea, in one picture

```
   YOU ask for something
        |
   1.  READ      the agent looks at the account          (safe, no changes)
        |
   2.  PREVIEW   the agent shows you exactly what would  (safe, no changes)
                 change, and what it would cost per day
        |
   3.  YOU SAY YES                                        <-- this is the gate
        |
   4.  APPLY     the change happens, and is written down
```

**Step 3 cannot be skipped or automated away.** The "preview" and the "apply"
are two different tools with two different names, so you can tell your agent
*"always allow previews, never allow applies"*. If it were one tool with a
`safe=true` switch, clicking "always allow" once on a harmless preview would
hand over every future real change.

Each `apply_*` accepts **only** a token its own preview produced. It cannot be
called cold.

---

## Why this is careful

**Google gives you one key, and it opens everything.** The only permission
scope that exists is `.../auth/adwords`. There is no read-only version. So the
token sitting in your `.env` is perfectly capable of changing your account.

That means "the reporting code doesn't write" cannot be left to good manners.
It is enforced in code and tested — see [How reading and writing are kept
apart](#how-reading-and-writing-are-kept-apart).

---

## Install it

You need **Python 3.11+**.

```bash
# 1. Get the code and its dependencies
git clone https://github.com/thevarungrovers/google-ads-api.git
cd google-ads-api
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

# 2. Fill in your credentials
cp .env.example .env
chmod 600 .env

# 3. Get a refresh token (opens a browser)
./.venv/bin/python scripts/generate_refresh_token.py

# 4. Check all of it works
./.venv/bin/python scripts/test_connection.py

# 5. Add it to your AI agent
python3 install.py
```

Always run through `./.venv/bin/python`, never the system `python3`. Step 5 is
the one exception — it is deliberately plain `python3`, because it has to work
before the venv exists.

### What you need to collect

| Value | Needed? | From |
|---|---|---|
| OAuth client ID + secret | yes | Google Cloud Console |
| Customer ID | yes | The 10-digit id of the account to work on |
| Login customer ID | only sometimes | A manager account's id, if you reach the target *through* it |

> **Do the whole Cloud Console part in ONE project, and write down which one.**
> API access attaches to the project that issued your OAuth client. A client
> from project A with the approval sitting on project B means every real query
> is refused — with an error that names no project at all.

> **You probably do not need a manager account.** If you can open the target
> account directly after signing in, leave `GOOGLE_ADS_LOGIN_CUSTOMER_ID` blank.

Step 4 is an 8-rung ladder, and each rung assumes the one above passed, so **fix
the first failure**. It exists to tell apart three problems that otherwise look
like one broken report: bad credentials, a project without access, and an
account you cannot reach.

### Adding it to an agent

`python3 install.py` asks two questions.

**Which agent?** Claude Code, Claude Desktop, Cursor, or Codex.

**Basic permissions only?** Say **Y** if you want this agent to look but not
touch. What that means depends on the agent:

| Agent | Saying Y does this |
|---|---|
| Claude Code | The 10 safe tools stop asking permission. The 5 that change things still ask, every time |
| Claude Desktop, Cursor, Codex | The 5 tools that change things are not even given to the agent. It cannot call them |

That difference is not a preference — Claude Code is the only one of the four
that can be told "allow this tool, ask about that one". For the others the
limit has to live in the server.

Then **restart your agent**. It reads the tool list once, at startup.

```bash
python3 install.py --dry-run                          # show, write nothing
python3 install.py --agent cursor --read-only --yes   # no questions
```

It backs up every file before changing it, and keeps your other settings and
other MCP servers. Running it twice does nothing the second time.

---

## DOs and DON'Ts

### Setting it up

- **DO** keep `.env` at `chmod 600`. It holds a token that can change your ads.
- **DO** run `scripts/test_connection.py` before anything else.
- **DO** pin `allowed_campaign_ids` in `guardrails.toml` to one campaign for the
  first week, then widen.
- **DON'T** commit anything real — no customer id, no company name, no domain.
  This repo is public, and `example.com` exists for test data.
- **DON'T** put credentials in your agent's config file. They live in `.env`.
- **DON'T** call `os.getenv` directly for anything with a default. Use
  `config.env()`, which treats blank as absent — see
  [blank vs absent](#blank-is-not-the-same-as-absent).

### Letting an agent use it

- **DO** answer **Y** to "basic permissions only" the first time.
- **DO** read the preview before approving. The numbers are there to be read.
- **DON'T** allow a tool whose name starts with `apply_`.
- **DON'T** use a blanket `mcp__google-ads` permission rule. It approves
  everything, including the money.

### Changing things

- **DO** write money as a plain decimal: `"2.50"` is two dollars fifty.
- **DO** use `find_campaigns` to get an id. Google's web UI never shows them.
- **DON'T** pass micros. `"2500000"` instead of `"2.50"` is a **million** times
  too much. The tool refuses anything over 1000 for exactly this reason.
- **DON'T** try to set anything to REMOVED. It cannot be undone in Google Ads,
  so it is not available here. Pause instead.
- **DON'T** work around the kill switch. If writes are off, a person turned
  them off.

---

## Reading

```bash
# What is available
./.venv/bin/python scripts/fetch_report.py --list

# Last 30 days of campaign performance
./.venv/bin/python scripts/fetch_report.py campaigns --days 30

# Write it to output/
./.venv/bin/python scripts/fetch_report.py campaigns --days 30 --csv

# A fixed window
./.venv/bin/python scripts/fetch_report.py keywords --start 2026-09-01 --end 2026-09-30

# See the query without calling the API
./.venv/bin/python scripts/fetch_report.py campaigns --days 7 --dry-run
```

---

## Changing things yourself

```bash
# Find it first — ids are not shown in the Google Ads UI
./.venv/bin/python scripts/manage.py find campaign "brand"

# Validate against Google and stop. Changes nothing
./.venv/bin/python scripts/manage.py campaign pause --name "brand - search" --dry-run

# The same, then a typed confirmation before it applies
./.venv/bin/python scripts/manage.py campaign set-budget --name "brand - search" --amount 2.50

# Build a whole campaign, interactively, in one atomic request
./.venv/bin/python scripts/manage.py campaign new
```

Entities are addressable **by name**, because the UI does not show ids. An exact
match wins; otherwise a case-insensitive substring; an ambiguous name is refused
with the candidates listed rather than guessed at.

Every change is validated against Google **first** and only then applied, so a
rejected change costs an error message instead of a half-made edit.

---

## What the agent can do

**15 tools: 6 that read, 4 preview/apply pairs, and undo.**

### Can read (safe)

| Tool | Gives you |
|---|---|
| `list_accounts` | Which accounts the credentials reach |
| `find_campaigns` | Campaigns **and their ids** |
| `find_ad_groups` | Ad groups and their ids |
| `run_report` | Performance: accounts, campaigns or keywords |
| `get_guardrails` | The limits, and what is left of this session's budget |
| `list_my_changes` | What the server changed, with ids for undo |

### Can change (needs your yes)

| What | Tools |
|---|---|
| Pause or enable a campaign | `preview_campaign_status` / `apply_campaign_status` |
| A campaign's daily budget | `preview_campaign_daily_budget` / `apply_campaign_daily_budget` |
| Pause or enable an ad group | `preview_ad_group_status` / `apply_ad_group_status` |
| An ad group's CPC bid | `preview_ad_group_cpc_bid` / `apply_ad_group_cpc_bid` |
| Undo one change | `revert_change` |

### Cannot do at all

Creating or removing campaigns, ad groups and ads. Setting anything to REMOVED.
Changing a **shared** budget, which would quietly move every campaign using it.
Anything touching billing or account access — those are not on the write
client's allowlist at all.

**There is no general-purpose "run any mutation" tool**, so anything without its
own tool simply cannot be reached. A test pins the exact list.

Creating a campaign is a human job (`scripts/manage.py campaign new`), because it
carries media and a legal declaration somebody has to answer for.

---

## What a preview tells you

| Field | Why it matters |
|---|---|
| `entity_name` | `brand - search` means something. `campaign 12345` does not |
| `before` / `after` | In real money, already converted out of micros |
| `metrics_7d` | What it has actually been doing |
| `projected_daily_spend_delta` | Roughly how much more per day |
| `checks` | One line per limit, `OK` or `FAIL` |
| `blocked` | `true` means a limit was hit and the apply will refuse |

Two things worth saying out loud when you summarise one:

- **Pausing** cannot increase spend, so its projected delta is `0`.
- **Enabling** is bounded by the **daily budget**, not by past spend. A campaign
  that spent nothing last week can still spend its whole budget tomorrow.

The token the preview gives you works **once**, expires after **10 minutes**,
and is refused if anybody changed the value in the meantime.

---

## Safety net

Four layers, weakest first.

### 1. Limits per change

In `guardrails.toml`, **committed on purpose** — raising a limit should be a
change somebody can see, not an environment variable set once and forgotten.

| Limit | Default |
|---|---|
| `max_daily_budget` | 100.00 |
| `max_budget_change_pct` | 50 |
| `max_cpc_bid` | 5.00 |
| `max_bid_increase_pct` | 50 |
| `max_entities_per_call` | 25 |
| `max_projected_delta_per_day` | 50.00 |

### 2. Limits per conversation

| Limit | Default |
|---|---|
| `max_applies_per_session` | 20 |
| `max_projected_session_delta` | 150.00 |

**These are the ones that matter.** A per-change limit does nothing against a
loop making 400 perfectly legal changes. These do. They reset only when the
server restarts.

### 3. Scope

```toml
# guardrails.toml
allowed_customer_ids = []   # empty = only GOOGLE_ADS_CUSTOMER_ID, nothing else
allowed_campaign_ids = []   # empty = every campaign in that account
allowed_currencies   = ["CAD"]
```

One refresh token can reach every account under a manager, so the customer
default is deliberately the narrow one.

### 4. The stop button

```bash
touch audit/WRITES_DISABLED                            # stop all changes, now
echo "paused for the sale" > audit/WRITES_DISABLED     # with a reason
rm audit/WRITES_DISABLED                               # start again
```

A **file**, not a setting, because a server already running will never notice a
setting you change afterwards. It is re-read on **every** apply — including one
whose preview was taken before you threw the switch, which is the case that
matters.

And separately, **read-only mode**: start the server with
`GOOGLE_ADS_MCP_READ_ONLY=1` and the change tools are not published at all.
That is what `install.py` uses for Claude Desktop, Cursor and Codex.

---

## Everything is written down

One file, `logs/google-ads.db`, gitignored.

| Table | One row per |
|---|---|
| `tool_calls` | Thing the agent asked for |
| `api_calls` | Request sent to Google, linked to the ask that caused it |
| `changes` | Change made to the account |

```sql
-- the last 20 things the agent did
SELECT t.ts, t.tool_name, t.ok, COUNT(a.id) AS api_calls
FROM tool_calls t LEFT JOIN api_calls a ON a.tool_call_id = t.id
GROUP BY t.id ORDER BY t.ts DESC LIMIT 20;
```

Each change is written down **twice**: once before the request goes out, once
after the answer comes back. A row missing its second half means something may
have changed at Google's end with nothing local to say so.

### Undoing

```
revert_change(entry_id="...")
```

Refuses if the change never applied, if it has already been undone, or if
**somebody else moved the value since** — in that last case undoing yours would
throw away theirs.

---

## How reading and writing are kept apart

Three mechanisms, because one would be a convention, and conventions slip.

1. **The reading client allowlists services *and* methods.** Not just
   "GoogleAdsService is fine" — that service has a `.mutate` on it. The method
   list is what makes it real.
2. **A test parses every file outside `write/`** and fails on any mutate call
   found in one. It also asserts `write/` really does contain one, so the test
   cannot pass by the feature having quietly vanished.
3. **The writing client allowlists its own services too.** Billing and user
   access are absent by construction, not by being unused.

---

## Things that will bite you

### Money is micros going in, currency coming out

**You never type micros.** Every `--amount` is in the account's currency.

Reading is where it bites. `metrics.cost_micros` is obviously micros. But
`average_cpc`, `average_cpm`, `average_cost` and `cost_per_conversion` are
declared as plain decimals and are **also** micros — $1.50 arrives as
`1500000.0`. Meanwhile `conversions_value` is already in currency and must not
be divided.

Nothing in the name or the type tells you which. Dividing the wrong one is
silent and wrong by a factor of a million, so `fields.py` states the rule
explicitly and **refuses** any report selecting a money field with no ruling:

```
Report 'campaigns' selects monetary field(s) with no micros ruling ...
```

Decide which list it belongs in rather than working around it.

### Blank is not the same as absent

`os.getenv(name, default)` returns the default only when the key is **missing**.
`GOOGLE_ADS_API_VERSION=` in a `.env` makes it present-and-empty, so the default
never applies — which would silently override every default the moment somebody
copies `.env.example`. Use `config.env()`, which treats blank as absent.

### Four smaller ones

- **Enums arrive as integers** unless you ask for `.name`, so a careless
  `isinstance(x, int)` fills your CSV with `2` instead of `ENABLED`.
- **Zero rows is an answer, not an error.** No delivery in the window means no
  rows.
- **Averaging a column of CTRs is wrong.** It weights every row equally
  regardless of volume. Ratios are recomputed from summed parts.
- **`list_accessible_customers` is not "all our accounts".** It returns what the
  signed-in user reaches *directly*, usually just the manager. Use the
  `accounts` report to enumerate what sits under it.

---

## Tests

```bash
./.venv/bin/python -m pytest        # 539, all offline
```

No credentials, no network. Four groups are worth knowing about:

- **The read-only guard** parses the source and fails if a mutate call appears
  outside `write/`.
- **The MCP surface** pins the exact set of tool names and their permission
  hints, so the set an agent can reach cannot grow without somebody editing the
  test.
- **The write path** drives preview → apply with everything external stubbed, so
  the sequence that moves money is covered without touching an account.
- **Schema checks** resolve every field path against the real `GoogleAdsRow`
  descriptor. That caught two metric names that do not exist in v25 and would
  have silently matched nothing.

Each source scan is paired with a test proving its pattern matches a real
offender — a scan whose regex matched nothing would pass every file and look
like a green light.

---

## Teaching your agent to use it well

There is a skill file in this repo:

```
.claude/skills/google-ads/SKILL.md
```

Claude Code picks it up automatically when you work in this folder. To have it
everywhere:

```bash
mkdir -p ~/.claude/skills/google-ads
cp .claude/skills/google-ads/SKILL.md ~/.claude/skills/google-ads/
```

For Cursor or Codex, point them at that file or paste it into their rules.

---

## Layout

| Path | What it is |
|---|---|
| `install.py` | Adds the server to your agent. Standard library only |
| `googleads_reporting/` | The read side |
| `googleads_reporting/write/` | **The only place that can change anything** |
| `googleads_mcp/` | The MCP server an agent talks to |
| `.claude/skills/google-ads/SKILL.md` | How an agent should use it |
| `guardrails.toml` | The limits. **Committed**, so a change is visible |
| `scripts/` | `test_connection.py`, `fetch_report.py`, `manage.py`, `generate_refresh_token.py` |
| `tests/` | 539 offline tests |
| `.env` | Your credentials. Gitignored, `chmod 600` |
| `logs/google-ads.db` | Everything that happened. Gitignored |
| `audit/WRITES_DISABLED` | The stop button, when it exists |

---

## Security

- The refresh token is written **straight into `.env`**, never printed, so it
  does not land in your scrollback. Use `--print` only if you need it elsewhere.
- `.env` is the only credential at rest. There is no `google-ads.yaml`, and a
  test enforces that.
- `.env`, `logs/`, `audit/`, `output/`, `client_secret*.json`, `token*.json` and
  `*.pem` are all gitignored, and a test asserts none of them is tracked.
- Nothing prints a credential. `Settings.describe()` reports secrets as presence
  and length only, and a test asserts no prefix or suffix leaks.
- GAQL has no parameter binding, so `query.quote_literal` **refuses** a value
  containing a quote, backslash or newline rather than trying to escape it.
- **This repo is public.** No real id, domain or company name belongs in a
  committed file. A test checks that example data uses reserved domains like
  `example.com`.
- **If the refresh token leaks**, revoke it at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
  then re-run `scripts/generate_refresh_token.py`.

---

## When something goes wrong

| It says | It means |
|---|---|
| `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION`, or "only approved for test accounts" | The Cloud project behind your client ID is at Test access. Usually a project mismatch |
| API disabled for the project | The Google Ads API was never switched on — on the project that owns the OAuth client |
| `redirect_uri_mismatch` | Your OAuth client is a "Web application". It must be **Desktop app** |
| `USER_PERMISSION_DENIED` | No access to that customer id, or `LOGIN_CUSTOMER_ID` names an account that does not manage it. If you have direct access, try clearing it entirely |
| `CUSTOMER_NOT_ENABLED` | The account is cancelled or suspended |
| `invalid_grant` | Refresh token revoked, unused for 6 months, or 7 days old on an unpublished consent screen. Re-run the generator |
| `access_denied` at consent | Your account is not on Audience → Test users |
| No refresh token returned | The grant already existed. Remove the app at myaccount.google.com/permissions and retry |
| Rung 5 fails | Credentials. No customer id was sent, so account settings are not implicated |
| Rung 5 passes, 6 fails | The Cloud project's access level, or `GOOGLE_ADS_LOGIN_CUSTOMER_ID` |
| Rung 6 passes, 7 fails | `GOOGLE_ADS_CUSTOMER_ID`, or the manager does not manage it |
| `unknown, already used, or older than 10 minutes` | Preview tokens are single-use. Take a fresh one |
| `has changed since the preview` | Somebody edited it in between. Re-preview and look again |
| `writes are disabled` | The kill switch. `rm audit/WRITES_DISABLED` |
| An `apply_*` tool is missing entirely | Read-only mode. Remove `GOOGLE_ADS_MCP_READ_ONLY` from that agent's config |
| Server connects but no tools appear | The agent was not restarted. The tool list is read once |

---

## Licence

MIT. See [LICENSE](LICENSE).
