#!/usr/bin/env python
"""Mint the OAuth refresh token, once, on this machine.

    ./.venv/bin/python scripts/generate_refresh_token.py

Reads GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET from .env, opens a
browser for consent, and writes the resulting refresh token straight back into
.env.

The token is **not printed** by default -- it goes from Google into .env without
passing through the terminal, your scrollback or any log. Pass --print only if
you need to copy it somewhere else yourself.

Prerequisites in Google Cloud Console, on the project that owns the OAuth
client:
  * the Google Ads API must be enabled;
  * the OAuth client must be of type "Desktop app" (this flow needs a loopback
    redirect, and a "Web application" client will reject it);
  * while the consent screen is in Testing, your Google account must be listed
    under Audience > Test users, or consent fails with access_denied.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from googleads_reporting.config import (  # noqa: E402
    ENV_PATH,
    ConfigError,
    load_env,
    require,
)

#: The Google Ads API has exactly one scope. There is no read-only variant --
#: which is why read-only is enforced in googleads_reporting.client instead.
SCOPES = ["https://www.googleapis.com/auth/adwords"]

ENV_KEY = "GOOGLE_ADS_REFRESH_TOKEN"


def build_flow(client_id: str, client_secret: str) -> InstalledAppFlow:
    """An installed-app flow built in memory, with no client_secret.json."""
    return InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )


def write_into_env(token: str, env_path: Path = ENV_PATH) -> str:
    """Set ``GOOGLE_ADS_REFRESH_TOKEN`` in ``env_path``.

    Returns 'updated' or 'appended'. Rewrites an existing key in place rather
    than appending a second one, because python-dotenv would take the last
    occurrence and leave a stale token above it in the file.
    """
    if not env_path.exists():
        raise ConfigError(
            f"{env_path} does not exist. Run: cp .env.example .env "
            "and fill in the client ID and secret first."
        )

    lines = env_path.read_text().splitlines()
    pattern = re.compile(rf"^\s*(export\s+)?{ENV_KEY}\s*=")
    outcome = "appended"

    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{ENV_KEY}={token}"
            outcome = "updated"
            break
    else:
        lines.append(f"{ENV_KEY}={token}")

    # Write via a 0600 temp file and replace, so the token is never briefly
    # world-readable and a crash mid-write cannot truncate the real .env.
    temp = env_path.with_suffix(env_path.suffix + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temp, env_path)
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Google Ads API refresh token and store it in .env.",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_token",
        help="also print the refresh token to stdout (it will be in your "
        "scrollback; off by default)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="local port for the OAuth redirect (default: any free port)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the consent URL instead of opening a browser",
    )
    args = parser.parse_args(argv)

    load_env()
    try:
        client_id = require("GOOGLE_ADS_CLIENT_ID")
        client_secret = require("GOOGLE_ADS_CLIENT_SECRET")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("Requesting consent for scope:", SCOPES[0])
    print("Sign in as a Google account with access to the Google Ads MCC.\n")

    flow = build_flow(client_id, client_secret)
    try:
        flow.run_local_server(
            port=args.port,
            open_browser=not args.no_browser,
            authorization_prompt_message="Open this URL to grant access:\n{url}\n",
            success_message=(
                "Access granted. You can close this tab and return to the terminal."
            ),
            # Without this, Google returns only an access token on a repeat
            # consent and the refresh token comes back None.
            access_type="offline",
            prompt="consent",
        )
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"\nerror: consent failed: {exc}", file=sys.stderr)
        print(
            "Check that the OAuth client is a 'Desktop app' and that your "
            "account is a test user on the consent screen.",
            file=sys.stderr,
        )
        return 1

    token = flow.credentials.refresh_token
    if not token:
        print(
            "error: Google returned no refresh token. This happens when the "
            "grant already exists; re-run to force a fresh consent, or remove "
            "this app under myaccount.google.com/permissions and try again.",
            file=sys.stderr,
        )
        return 1

    outcome = write_into_env(token)
    print(f"\nRefresh token {outcome} in {ENV_PATH.name} ({len(token)} chars).")
    print(f"{ENV_PATH.name} permissions set to 600.")

    if args.print_token:
        print("\n--print was passed; the token follows. Treat it as a password.")
        print(token)

    print("\nNext: ./.venv/bin/python scripts/test_connection.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
