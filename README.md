# Google Ads API — read-only reporting

Fetches reporting data from a Google Ads account through a manager account (MCC).

**Phase 1 is read-only, and that is enforced in code, not by convention.** The
only OAuth scope the Google Ads API offers is
`https://www.googleapis.com/auth/adwords`, which has no read-only variant — the
refresh token in `.env` is fully capable of changing the account. See
[Read-only enforcement](#read-only-enforcement) for the three layers that stop
it, and [Phase 2](#phase-2-adding-writes) for how to add writes without
dismantling them.

---

## Setup

Python 3.11+ (developed on 3.13).

```bash
cd ~/dev/google-ads-api
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

Always run through `./.venv/bin/python`, never the system `python3`.

### 1. What you need to collect

Three things. The Cloud Console gives you the OAuth client; the Google Ads UI
gives you the account ID.

> **Do the whole of step 1 in ONE Cloud project, and note which one.**
> The API access level attaches to the Cloud project that issued your OAuth
> client. Using a client from project A while project B holds the approval
> means every production query is refused, with an error that names no project
> at all. Step 1f is the check; doing it now costs a minute and saves an hour.

| Value | Required? | From |
|---|---|---|
| OAuth client ID + secret | **yes** | Google Cloud Console (steps 1a–1d below) |
| Customer ID | **yes** | The 10-digit ID of the account to report on |
| Login customer ID | only sometimes | A manager account's own 10-digit ID — see below |

**You do not need a manager account (MCC).** `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
tells the API to *act as* a manager, and it matters only when your Google
account reaches the target account **through** that manager instead of having
direct access to it. If you can open the target account directly after signing
in, leave it blank — every query works without it, including listing the
accounts under a manager.

---

#### 1a. Create the Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Project dropdown (top bar) → **New Project**.
3. Name it something recognisable — `google-ads-api` — and **Create**.
4. Make sure that project is selected in the dropdown before continuing. Nearly
   every problem in this section is actually "configured the wrong project".

Billing is **not** required; the Google Ads API itself has no charge.

#### 1b. Enable the Google Ads API

1. **APIs & Services → Library**, or go straight to
   [the API library](https://console.cloud.google.com/apis/library).
2. Search for **Google Ads API**, open it, click **Enable**.

Skipping this is the usual cause of a request failing with the API being
disabled for the project, even though the credentials are perfectly valid.

#### 1c. Configure the consent screen

This used to be a single "OAuth consent screen" page. It is now **APIs &
Services → Google Auth Platform**, split across **Branding**, **Audience**,
**Data Access** and **Clients**.

1. **Google Auth Platform → Branding** — set an app name and your support
   email, then save.
2. **Google Auth Platform → Audience** — choose a user type:
   - **Internal** if the Cloud project belongs to a Google Workspace org.
     Anyone in the org can consent, there is no test-user list, and there is
     nothing to publish. Prefer this.
   - **External** otherwise. It starts in **Testing**, and in that state only
     accounts on the test-user list may consent.
3. If you chose External: still on **Audience**, under **Test users**, click
   **Add users** and add the Google account you will sign in with — the one
   with access to the MCC. Miss this and consent fails with `access_denied`,
   which reads like a permissions problem on the Ads side and is not.

While External + Testing, a refresh token expires after **7 days**. That is
fine for setup, but for anything recurring either use Internal or publish the
app (**Audience → Publish app**).

#### 1d. Create the OAuth client

1. **Google Auth Platform → Clients** (equivalently **APIs & Services →
   Credentials**) → **Create client**.
2. **Application type: Desktop app.** This matters — the consent flow in
   `scripts/generate_refresh_token.py` uses a loopback redirect, and a "Web
   application" client rejects it with a redirect-URI mismatch.
3. Name it, **Create**, then copy the **client ID** and **client secret** into
   `.env`. You can reopen the client later to see both again, so losing them is
   recoverable.
4. **Note the project number** — it is the part of the client ID before the
   first `-`, e.g. `960632522930-abc….apps.googleusercontent.com` →
   `960632522930`. Step 1f needs it. This is the project that must hold the
   access level, regardless of which project you were looking at elsewhere.

#### 1e. Find your account IDs

1. `GOOGLE_ADS_CUSTOMER_ID` — the 10-digit ID of the account you want to report
   on, shown at the top right in the Google Ads UI.
2. `GOOGLE_ADS_LOGIN_CUSTOMER_ID` — **only if** you reach that account through
   a manager account: the manager's own 10-digit ID. Otherwise leave it blank.

Both may be pasted in dashed form; they are normalized automatically.

#### 1f. Raise the access level — on the project that issued your client

**Do this before your first call, not after it fails.** A new Cloud project is
at **Test** access, which reaches test accounts only. Against a real account
every query is refused with:

> The Google Cloud project is only approved for use with test accounts.
> (`authorization_error=32`, or `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION`)

The trap is that this says nothing about *which* project. Upgrading the wrong
one looks exactly like having upgraded.

Once `.env` has a client ID, print the project that actually matters:

```bash
./.venv/bin/python -c "from googleads_reporting.config import Settings; \
print(Settings.from_env().oauth_project)"
```

(`scripts/test_connection.py` prints it as `oauth_cloud_project` on every run
too.) Then open that exact project:

```
https://console.cloud.google.com/google/ads-apis/overview?project=<PROJECT_NUMBER>
```

- Shows **Test** → apply for Explorer here. Usually auto-approved.
- Shows **Explorer** or better → you are done; the level is on the right
  project.

Prefer to reuse a project that is already approved? Create a new OAuth client
inside *it* (step 1d), put that ID and secret in `.env`, and re-run
`scripts/generate_refresh_token.py` — a refresh token is bound to the client
that issued it, so the old one will not carry over.

The tiers (per Cloud project):

| Level | Reaches | Production ops/day |
|---|---|---|
| **Test** | test accounts only | — |
| **Explorer** | test + production | 2,880 |
| **Basic** | test + production | 15,000 |
| **Standard** | test + production | unlimited |

Explorer is typically auto-approved and is plenty for reporting; move to Basic
if you start hitting the daily cap. Note Explorer still withholds the billing,
planning, account-creation and user-invitation services — none of which Phase 1
touches.

### 2. Fill in `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Then paste your values in. Every key is documented in the file. Customer IDs
can be pasted in the dashed form (`123-456-7890`); they are normalized to bare
digits automatically.

Optional keys may be left blank — blank is treated as *unset*, so the code
default applies. (This is deliberate and tested; see
[Config](#config-blank-vs-absent).)

### 3. Mint the refresh token

```bash
./.venv/bin/python scripts/generate_refresh_token.py
```

This opens a browser for consent and writes the refresh token **directly into
`.env`**. It is not printed, so it never lands in your scrollback or a log.
Pass `--print` if you need it elsewhere.

Sign in as a Google account that has access to the MCC.

### 4. Verify

```bash
./.venv/bin/python scripts/test_connection.py
```

An 8-rung ladder. Each rung assumes the ones above it passed, so **fix the
first failure** — later ones depend on it. The useful property is that it
separates three failures that look identical from a single broken report:

| Rung | Proves | A failure here means |
|---|---|---|
| 1–2 | `.env` exists, is `600`, every required key is set | a missing or blank key |
| 3 | **the OAuth client and refresh token** | `invalid_client` = wrong ID/secret; `invalid_grant` = revoked or expired refresh token |
| 4 | the read-only guard refuses write surfaces | checked *before* any live account is touched |
| 5 | the API answers this identity — no customer ID is sent | the Google Ads API is not enabled on the project. Note this call is *exempt* from the access-level check, so passing here does **not** prove production access |
| 6 | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` — the manager is reachable (skipped with a WARN if unset) | wrong manager ID, no access to it, **or the Cloud project is at Test access (§1f)** — the first real query is where that surfaces |
| 7 | `GOOGLE_ADS_CUSTOMER_ID` — the target is reachable | wrong account, or the MCC does not manage it |
| 8 | a real report runs end to end | |

Rung 3 is a real network call: the library refreshes the OAuth token eagerly
when the client is constructed, so bad OAuth credentials surface there rather
than on the first query.

---

## Usage

```bash
# What is available
./.venv/bin/python scripts/fetch_report.py --list

# Last 30 days of campaign performance, printed
./.venv/bin/python scripts/fetch_report.py campaigns --days 30

# A GAQL preset, written to output/
./.venv/bin/python scripts/fetch_report.py campaigns --during LAST_7_DAYS --csv

# A fixed window
./.venv/bin/python scripts/fetch_report.py keywords --start 2026-09-01 --end 2026-09-30 --csv

# Accounts under a manager (run it against the manager, not a leaf account)
./.venv/bin/python scripts/fetch_report.py accounts --customer-id <MANAGER-ID>

# See the GAQL without calling the API
./.venv/bin/python scripts/fetch_report.py campaigns --days 7 --show-query

# Extra filters, repeatable
./.venv/bin/python scripts/fetch_report.py campaigns --days 7 --where "campaign.status = 'ENABLED'"
```

`--days N` covers the N days ending **yesterday**. Today is excluded on
purpose: today's metrics are partial all day, and a partial day quietly drags
every average down. Use `--during TODAY` if you want it anyway.

Reports:

| Name | Resource | Notes |
|---|---|---|
| `accounts` | `customer_client` | No date range. Run against the MCC. |
| `campaigns` | `campaign` | One row per campaign per day. |
| `keywords` | `keyword_view` | Positive keywords on Search/Shopping only — Performance Max has none. |

CSV files land in `output/` (gitignored), named
`<report>_<customer_id>_<timestamp>.csv` so runs and accounts never overwrite
each other.

---

## Changing things (Phase 2)

`scripts/manage.py` creates and updates campaigns, budgets, ad groups and ads.

### Find what you want to change

Campaign IDs are not shown anywhere obvious in the Google Ads UI, but names are
on every screen — so start from a name:

```bash
./.venv/bin/python scripts/manage.py find campaign "cpm - desktop"
./.venv/bin/python scripts/manage.py find adgroup          # everything
```

```
4 campaign(s) matching 'cpm - desktop' (275-626-1338):

  24218821846      2026 - CPM - Desktop  [ENABLED, DISPLAY]
  24218821843      2026 - En - CPM - Desktop  [ENABLED, DISPLAY]
```

Then act by `--name` or by `--campaign-id`. Name matching is:

1. **exact first** — a name that matches exactly wins, even if it is also a
   substring of other names;
2. then **case-insensitive substring** — `"2026 - en - cpm"` finds
   `2026 - En - CPM - Desktop`;
3. and **several matches are refused**, never guessed, with the candidates and
   their IDs printed so you can pick one.

Names with apostrophes work (`Paniers d'été`). Names are *not* unique in Google
Ads, so two campaigns really can share one — hence the refusal.

### Make a change

```bash
# validate against Google and stop — changes nothing
./.venv/bin/python scripts/manage.py campaign pause --name "2026 - En - CPM - Desktop" --dry-run

# the same, then a typed confirmation before it applies
./.venv/bin/python scripts/manage.py campaign pause --campaign-id 24218821843

./.venv/bin/python scripts/manage.py campaign enable --campaign-id 24218821843
./.venv/bin/python scripts/manage.py campaign rename --campaign-id 123 --new-name "..."
./.venv/bin/python scripts/manage.py budget set-amount --budget-id 456 --amount 25
./.venv/bin/python scripts/manage.py adgroup set-bid --ad-group-id 789 --amount 0.75
./.venv/bin/python scripts/manage.py ad pause --ad-id 321
```

Every command runs in the same order, and the order is the point:

1. read current state and build the operations;
2. send them to Google with `validate_only=True` — the full server-side check,
   changing nothing;
3. print the diff and wait for you to type `yes`.

So a request Google would reject is rejected at step 2, **before** you are
asked to approve it. `--dry-run` stops after step 2; `--yes` skips step 3.

### What stops a bad change

| Guard | What it catches |
|---|---|
| `validate_only` first | Anything the API would reject, before the prompt |
| Created `PAUSED` by default | A new campaign spending before anyone reviewed it |
| Daily-budget ceiling | `$50` typed as micros — `$50,000,000` |
| Ambiguous name refused | Pausing the wrong campaign of four |
| No tty / EOF / Ctrl-C → abort | An unattended run mutating an account |
| `REMOVED` warns | It is permanent in Google Ads; `PAUSED` is reversible |
| Service allowlist | Billing and user-access services are not reachable at all |

Every applied mutation is written to `audit/mutations-<date>.jsonl` — two
records, `attempt` before the call and `outcome` after, so an attempt with no
outcome tells you the process died mid-request.

### The read-only guarantee still holds

Phase 2 lives entirely in `googleads_reporting/write/`. `ReadOnlyGoogleAdsClient`
gained nothing; reaching a write means importing a different class from a
different subpackage. `tests/test_readonly_guard.py` parses every *other* source
file with `ast` and fails if it so much as calls a `mutate*` method — and
separately asserts that `write/` really does contain one, so the carve-out
cannot quietly become decorative.

---

## Layout

```
googleads_reporting/
  config.py       .env loading; blank-is-absent accessor; Settings
  customer_id.py  10-digit normalization
  client.py       ReadOnlyGoogleAdsClient — the enforced read-only surface
  query.py        GAQL builder, date ranges, SELECT-only assertion
  fields.py       row extraction, enum names, micros conversion
  export.py       DataFrame / CSV output, totals
  reports/        declarative report definitions + registry
  write/          Phase 2 — the ONLY place that can mutate
    client.py     MutatingGoogleAdsClient, validate-then-apply
    plan.py       PlannedChange: the protos and the diff together
    lookup.py     find an entity by name, refuse ambiguity
    budgets.py / campaigns.py / adgroups.py / ads.py
    audit.py      append-only JSONL log of every mutation
scripts/
  generate_refresh_token.py   one-time OAuth consent
  test_connection.py          diagnostic ladder
  fetch_report.py             run a report (read)
  manage.py                   create and update (write)
tests/                        394 tests, all offline
```

Pinned in `requirements.txt`: `google-ads==33.0.0`, which bundles API versions
**v23, v24 and v25**; this project targets **v25**, the newest. Each release of
the library ships a fixed set of versions, so an unpinned upgrade can remove
the one this code targets — bump deliberately, then run the tests.

---

## Read-only enforcement

Three independent layers, because the token itself is not restricted:

1. **Service allowlist** — `client.ALLOWED_SERVICES` admits only
   `GoogleAdsService` and `CustomerService`. Anything else raises
   `ReadOnlyViolation`.
2. **Method allowlist** — every service is returned wrapped in
   `_ReadOnlyService`, which exposes only `search`, `search_stream` and
   `list_accessible_customers`. This is **not** redundant with layer 1:
   `GoogleAdsService` itself has a `.mutate` method, so allowlisting the
   service alone would leave the write path one attribute access away. A test
   asserts that `.mutate` really does exist on the generated client, so nobody
   later removes this layer as surplus.
3. **Source scan** — `tests/test_readonly_guard.py` greps
   `googleads_reporting/` and `scripts/` for any `.mutate*(` call, with a
   negative control proving the pattern matches a real one. A second scan
   refuses `load_from_storage` / `load_from_env`, so credentials cannot migrate
   out of `.env` into a `google-ads.yaml`.

The query layer helps too: GAQL has no mutating form, and
`query.assert_select_only` refuses anything that is not a bare `SELECT`,
including a write keyword smuggled through `--where`.

```bash
./.venv/bin/python -m pytest tests/test_readonly_guard.py -v
```

---

## Things that will bite you

### Money is in micros — and not only where the name says so

`metrics.cost_micros` is obviously micros. But `metrics.average_cpc`,
`average_cpm`, `average_cost` and `cost_per_conversion` are declared `double`
and are **also** micros: an average CPC of $1.50 arrives as `1500000.0`.
Nothing in the name, the type or the generated docstring says so.

It goes the other way too. `metrics.conversions_value` is **already** in
account currency and must not be divided. v25 has both
`cross_device_conversions_value_micros` and `cross_device_conversions_value`,
so no name pattern can decide this on its own.

Dividing the wrong column is silent — no error, just a number wrong by a factor
of a million. So `fields.py` states the rule explicitly in two curated sets,
and `Report.__post_init__` **refuses** a report that selects a monetary field
with no ruling. If you add a cost metric and get:

```
Report 'campaigns' selects monetary field(s) with no micros ruling ...
```

decide which set it belongs in rather than working around it.

Converted columns also drop the `_micros` suffix on output
(`metrics.cost_micros` → `metrics.cost`), because a divided value under a
`_micros` name is how it gets multiplied back up downstream.

### Config: blank vs absent

`os.getenv(name, default)` returns `default` only when the key is **absent**.
`load_dotenv()` turns `GOOGLE_ADS_API_VERSION=` into the empty string, so the
key *is* present and the default is never reached — which would silently
override every default in the package the moment you copy `.env.example`.

So all config goes through `config.env()`, which treats blank as absent. **Never
call `os.getenv` directly** for a value that has a default.

### Enums arrive as integers unless you ask for the name

proto-plus enums subclass `int`, so an `isinstance(value, int)` fast path
returns `2` instead of `ENABLED` and fills your CSV with magic numbers.
`fields.coerce` checks `.name` first.

### Zero rows is a valid answer

An account with no delivery in the window returns no rows. That is data, not an
error — the scripts report it as such, and `to_dataframe` keeps the header.

### Averaging ratios is wrong

Summing a column of CTRs or average-CPCs weights every row equally regardless
of volume. `export.summarise` recomputes ratios from the summed numerator and
denominator instead.

### `list_accessible_customers` is not "all our accounts"

It returns only what the OAuth user can reach **directly** — usually just the
MCC. To enumerate the accounts under it, use the `accounts` report, which
queries `customer_client` from the MCC.

---

## Tests

```bash
./.venv/bin/python -m pytest          # all 250, offline
```

Every test runs without credentials and without network. Two kinds are worth
knowing about:

- **Schema checks.** `tests/test_reports.py` resolves every selected field path
  against the real `GoogleAdsRow` descriptor, and `tests/test_fields.py` asserts
  every curated metric name exists in the v25 `Metrics` descriptor. That caught
  `average_cpv` and `view_through_conversions_value` — neither exists in v25, so
  both entries would have silently matched nothing. A hand-written stub cannot
  find that; only the real schema can.
- **Negative controls.** Each source scan is paired with a test proving its
  pattern matches a real offender. A scan whose regex matched nothing would pass
  every file and look like a green light.

---

## Phase 2: adding writes

Do **not** widen `ALLOWED_SERVICES` or `ALLOWED_METHODS`.

`client.build_raw_client(settings)` is the single place credentials become a
client (a test pins it as the only `load_from_dict` call). Reuse it from a new
sibling module with its own allowlist, its own confirmation prompt, and its own
tests. The read-only guarantee of `ReadOnlyGoogleAdsClient` then still means
something, and the mutate scan keeps covering everything outside the new module
— update its excluded paths deliberately, so the write surface stays a thing
someone chose rather than a thing that leaked.

---

## Security

- `.env` is the only credential at rest. There is no `google-ads.yaml`, and a
  test enforces that.
- `.gitignore` excludes `.env`, `*.env` (but not `.env.example`),
  `client_secret*.json`, `token*.json`, `*refresh_token*` and `*.pem`.
- `chmod 600 .env`. `test_connection.py` warns if it is group- or
  world-readable.
- Nothing prints a credential. `Settings.describe()` reports secrets as
  presence and length only, and a test asserts no prefix or suffix leaks.
- GAQL has no parameter binding, so `query.quote_literal` **refuses** values
  containing a quote, backslash or newline rather than trying to escape them.
- If the refresh token leaks: revoke it at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions),
  then re-run `scripts/generate_refresh_token.py`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` / `authorization_error=32` / "only approved for use with test accounts" | The **Cloud project** behind `GOOGLE_ADS_CLIENT_ID` is at Test access. Usually a project mismatch: see §1f. |
| API disabled for the project | The Google Ads API was never enabled — step 1b, on the project that owns the OAuth client. |
| `redirect_uri_mismatch` | The OAuth client is a "Web application". It must be **Desktop app**. |
| `USER_PERMISSION_DENIED` | The OAuth user has no access to that customer ID, or `LOGIN_CUSTOMER_ID` is set to an account that does not manage it. If the user has direct access, try clearing `LOGIN_CUSTOMER_ID` entirely. |
| `CUSTOMER_NOT_ENABLED` | The account is cancelled or suspended. |
| `invalid_grant` | Refresh token revoked, unused for 6 months, or 7 days old on an unpublished External consent screen. Re-run the generator. |
| `access_denied` at consent | Your account is not on Audience → Test users (External + Testing only). |
| No refresh token returned | The grant already existed. Remove the app at myaccount.google.com/permissions and retry. |
| Rung 5 fails | Credentials. No customer ID was sent, so account settings are not implicated. |
| Rung 5 passes, 6 fails | The Cloud project's access level (§1f), or `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. The hint distinguishes them. |
| Rung 6 passes, 7 fails | `GOOGLE_ADS_CUSTOMER_ID`, or the MCC does not manage it. |
