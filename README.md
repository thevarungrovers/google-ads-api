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

Four things, from two different places. The Cloud Console gives you the OAuth
client; the Google Ads UI gives you the developer token and the account IDs.

| Value | From |
|---|---|
| OAuth client ID + secret | Google Cloud Console (steps 1a–1d below) |
| Developer token | Google Ads UI, **on the MCC** → Tools & Settings → Setup → API Center |
| Login customer ID | The MCC's own 10-digit ID (top right in the Google Ads UI) |
| Customer ID | The account you want to report on, under that MCC |

---

#### 1a. Create the Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Project dropdown (top bar) → **New Project**.
3. Name it something recognisable — `google-ads-api` — and **Create**.
4. Make sure that project is selected in the dropdown before continuing. Nearly
   every problem in this section is actually "configured the wrong project".

Billing is **not** required; the Google Ads API itself has no charge.

> One developer token can be used from several Cloud projects, but a given
> Cloud project can only ever use **one** developer token.

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

#### 1e. Get the developer token

1. In the **Google Ads UI, signed in to the MCC** (not a leaf account):
   **Tools & Settings → Setup → API Center**. The section only appears for
   users with admin access on the manager account.
2. Copy the developer token.

#### 1f. Raise the access level — on the right Cloud project

**The access level belongs to the Cloud project your OAuth client came from,
not to the developer token.** This trips people up: you can hold Explorer
access and still be refused, because the `GOOGLE_ADS_CLIENT_ID` in `.env` was
issued by a *different* project than the one you upgraded. A project that is
not approved silently falls back to **Test**, which reaches test accounts only
and fails on a real account with:

> The Google Cloud project is only approved for use with test accounts.

The project number is the part of the client ID before the first `-`
(`960632522930-abc….apps.googleusercontent.com` → `960632522930`). Check *that*
project:

```
https://console.cloud.google.com/google/ads-apis/overview?project=<PROJECT_NUMBER>
```

Apply for the upgrade there. The **API Center** in the Google Ads UI is the
legacy route and will not lift a Cloud project's restriction.

If you would rather keep an approved project you already have, create a new
OAuth client inside it (step 1d), put that ID and secret in `.env`, and re-run
`scripts/generate_refresh_token.py` — a refresh token is bound to the client
that issued it.

The tiers:

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
| 5 | **the developer token** — no customer ID is sent | token is Test-level, or wrong |
| 6 | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` — the MCC is reachable | wrong MCC ID, or this user has no access to it |
| 7 | `GOOGLE_ADS_CUSTOMER_ID` — the target is reachable | wrong account, or the MCC does not manage it |
| 8 | a real report runs end to end | |

Rung 3 is a real network call: the library refreshes the OAuth token eagerly
when the client is constructed, so bad OAuth credentials surface there rather
than on the first query. That is what lets rung 5 isolate the developer token.

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

# Accounts under the MCC (run it against the MCC, not a leaf account)
./.venv/bin/python scripts/fetch_report.py accounts --customer-id <MCC-ID>

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
scripts/
  generate_refresh_token.py   one-time OAuth consent
  test_connection.py          diagnostic ladder
  fetch_report.py             run a report
tests/                        250 tests, all offline
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
| `DEVELOPER_TOKEN_NOT_APPROVED` / `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` / "only approved for use with test accounts" | The **Cloud project** behind `GOOGLE_ADS_CLIENT_ID` is at Test access — not the developer token. Usually a project mismatch: see §1f. |
| API disabled for the project | The Google Ads API was never enabled — step 1b, on the project that owns the OAuth client. |
| `redirect_uri_mismatch` | The OAuth client is a "Web application". It must be **Desktop app**. |
| `USER_PERMISSION_DENIED` | The OAuth user has no access to that customer ID, or `LOGIN_CUSTOMER_ID` is not the managing MCC. |
| `CUSTOMER_NOT_ENABLED` | The account is cancelled or suspended. |
| `invalid_grant` | Refresh token revoked, unused for 6 months, or 7 days old on an unpublished External consent screen. Re-run the generator. |
| `access_denied` at consent | Your account is not on Audience → Test users (External + Testing only). |
| No refresh token returned | The grant already existed. Remove the app at myaccount.google.com/permissions and retry. |
| Rung 5 fails | Credentials. No customer ID was sent, so account settings are not implicated. |
| Rung 5 passes, 6 fails | `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. |
| Rung 6 passes, 7 fails | `GOOGLE_ADS_CUSTOMER_ID`, or the MCC does not manage it. |
