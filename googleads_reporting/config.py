"""Configuration, loaded from a single ``.env`` file.

Every setting is read through :func:`env`, which treats a key that is *present
but blank* exactly like a key that is *absent*.

That distinction is the whole reason this module exists. ``.env.example`` ships
every optional key spelled out with an empty value so the template documents
itself -- but ``os.getenv(name, default)`` returns ``default`` only when the key
is **absent**. ``load_dotenv()`` turns ``GOOGLE_ADS_API_VERSION=`` into the
empty string, so the key *is* present and the default is never reached. Copying
the template would silently override every default in this package with "".
So: never call ``os.getenv`` directly for a value that has a default. Call
:func:`env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .customer_id import normalize_customer_id

#: API version to target. google-ads 33.0.0 bundles v23, v24 and v25.
DEFAULT_API_VERSION = "v25"
DEFAULT_OUTPUT_DIR = "output"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


_loaded = False


def load_env(path: Path | None = None, *, force: bool = False) -> None:
    """Load ``.env`` into ``os.environ`` once per process.

    ``load_dotenv`` does not override variables already in the environment, so
    a value exported in the shell (or pre-seeded by a test) always wins.
    """
    global _loaded
    if _loaded and not force:
        return
    load_dotenv(dotenv_path=path or ENV_PATH, override=False)
    _loaded = True


def env(name: str, default: str = "") -> str:
    """Return the setting ``name``, treating blank as unset.

    This is the only correct way to read a setting that has a default.
    """
    value = os.environ.get(name) or ""
    value = value.strip()
    return value if value else default


def require(name: str, *, hint: str = "") -> str:
    """Return a required setting, or raise :class:`ConfigError`.

    The error names the missing key only -- never a value, not even a partial
    one.
    """
    value = env(name)
    if not value:
        message = f"{name} is not set in {ENV_PATH.name} (or is blank)."
        if hint:
            message += f" {hint}"
        raise ConfigError(message)
    return value


def int_env(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def bool_env(name: str, default: bool) -> bool:
    raw = env(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}.")


@dataclass(frozen=True)
class Settings:
    """Everything this package needs in order to talk to the API.

    ``developer_token``, ``client_secret`` and ``refresh_token`` are secrets.
    Nothing in this package prints, logs or serialises them; see
    :meth:`describe`.
    """

    developer_token: str
    client_id: str
    client_secret: str
    refresh_token: str
    login_customer_id: str
    customer_id: str | None
    api_version: str
    output_dir: Path

    @classmethod
    def from_env(cls, *, path: Path | None = None) -> "Settings":
        load_env(path)

        customer_id = env("GOOGLE_ADS_CUSTOMER_ID")
        output_dir = Path(env("GOOGLE_ADS_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
        if not output_dir.is_absolute():
            output_dir = PROJECT_ROOT / output_dir

        return cls(
            developer_token=require(
                "GOOGLE_ADS_DEVELOPER_TOKEN",
                hint="Find it under Tools & Settings > Setup > API Center on the MCC.",
            ),
            client_id=require("GOOGLE_ADS_CLIENT_ID"),
            client_secret=require("GOOGLE_ADS_CLIENT_SECRET"),
            refresh_token=require(
                "GOOGLE_ADS_REFRESH_TOKEN",
                hint="Generate one with scripts/generate_refresh_token.py.",
            ),
            login_customer_id=normalize_customer_id(
                require("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
            ),
            customer_id=(
                normalize_customer_id(customer_id) if customer_id else None
            ),
            api_version=env("GOOGLE_ADS_API_VERSION", DEFAULT_API_VERSION),
            output_dir=output_dir,
        )

    def to_google_ads_dict(self) -> dict[str, object]:
        """Build the mapping ``GoogleAdsClient.load_from_dict`` expects.

        Deliberately built in memory, never written to disk: this project has no
        ``google-ads.yaml``, so the credentials exist in exactly one place on
        disk (``.env``) and nowhere else.
        """
        return {
            "developer_token": self.developer_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "login_customer_id": self.login_customer_id,
            # proto-plus surfaces enums as objects with a .name, which
            # googleads_reporting.fields relies on when flattening rows.
            "use_proto_plus": True,
        }

    def describe(self) -> dict[str, str]:
        """A diagnostic summary that is safe to print.

        Secrets are reported as presence and length only -- no characters, not
        even a prefix. Customer IDs are not secret and are shown in full.
        """
        def secret(value: str) -> str:
            return f"present ({len(value)} chars)" if value else "MISSING"

        return {
            "developer_token": secret(self.developer_token),
            "client_id": secret(self.client_id),
            "client_secret": secret(self.client_secret),
            "refresh_token": secret(self.refresh_token),
            "login_customer_id": self.login_customer_id,
            "customer_id": self.customer_id or "(not set)",
            "api_version": self.api_version,
            "output_dir": str(self.output_dir),
        }

    def resolve_customer_id(self, override: str | None = None) -> str:
        """Pick the account to report on: ``override`` first, else the default."""
        if override:
            return normalize_customer_id(override)
        if self.customer_id:
            return self.customer_id
        raise ConfigError(
            "No customer ID given. Pass --customer-id, or set "
            "GOOGLE_ADS_CUSTOMER_ID in .env."
        )
