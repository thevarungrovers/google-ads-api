"""Config tests.

The important one is `test_blank_optional_keys_fall_back_to_defaults`. It
reproduces the bug the way it actually reaches you: `.env.example` lists every
optional key with an empty value, a user copies it to `.env`, and every default
in the code is silently replaced by "". Setting the keys to a real value, or
leaving them unset, only exercises the path that was never broken.
"""

from pathlib import Path

import pytest

from googleads_reporting import config
from googleads_reporting.config import (
    ConfigError,
    DEFAULT_API_VERSION,
    DEFAULT_OUTPUT_DIR,
    Settings,
    bool_env,
    env,
    int_env,
    require,
)

REQUIRED = {
    "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token-placeholder",
    "GOOGLE_ADS_CLIENT_ID": "client-id-placeholder.apps.googleusercontent.com",
    "GOOGLE_ADS_CLIENT_SECRET": "client-secret-placeholder",
    "GOOGLE_ADS_REFRESH_TOKEN": "refresh-token-placeholder",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "123-456-7890",
}

ALL_KEYS = [
    *REQUIRED,
    "GOOGLE_ADS_CUSTOMER_ID",
    "GOOGLE_ADS_API_VERSION",
    "GOOGLE_ADS_OUTPUT_DIR",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """An environment with no real .env in play and no stray GOOGLE_ADS_* keys."""
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Point load_env at a path that does not exist, so a developer's real .env
    # can never leak into the test run.
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setattr(config, "_loaded", False)
    return monkeypatch


def test_env_treats_blank_as_absent(clean_env):
    clean_env.setenv("GOOGLE_ADS_API_VERSION", "")
    assert env("GOOGLE_ADS_API_VERSION", "v25") == "v25"

    clean_env.setenv("GOOGLE_ADS_API_VERSION", "   ")
    assert env("GOOGLE_ADS_API_VERSION", "v25") == "v25"

    clean_env.setenv("GOOGLE_ADS_API_VERSION", "v24")
    assert env("GOOGLE_ADS_API_VERSION", "v25") == "v24"


def test_env_strips_surrounding_whitespace(clean_env):
    clean_env.setenv("GOOGLE_ADS_API_VERSION", "  v24  ")
    assert env("GOOGLE_ADS_API_VERSION") == "v24"


def test_env_returns_default_when_absent(clean_env):
    assert env("GOOGLE_ADS_API_VERSION", "v25") == "v25"
    assert env("GOOGLE_ADS_API_VERSION") == ""


def test_require_rejects_blank(clean_env):
    clean_env.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    with pytest.raises(ConfigError, match="GOOGLE_ADS_DEVELOPER_TOKEN"):
        require("GOOGLE_ADS_DEVELOPER_TOKEN")


def test_require_error_never_contains_the_value(clean_env):
    clean_env.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
    try:
        require("GOOGLE_ADS_DEVELOPER_TOKEN", hint="Check the API Center.")
    except ConfigError as exc:
        assert "Check the API Center." in str(exc)


def test_typed_readers_inherit_blank_as_absent(clean_env):
    clean_env.setenv("SOME_INT", "")
    clean_env.setenv("SOME_BOOL", "")
    assert int_env("SOME_INT", 30) == 30
    assert bool_env("SOME_BOOL", True) is True

    clean_env.setenv("SOME_INT", "7")
    clean_env.setenv("SOME_BOOL", "no")
    assert int_env("SOME_INT", 30) == 7
    assert bool_env("SOME_BOOL", True) is False


def test_blank_optional_keys_fall_back_to_defaults(clean_env):
    """A .env copied verbatim from .env.example must not break the defaults."""
    for key, value in REQUIRED.items():
        clean_env.setenv(key, value)
    # Exactly what the shipped template produces: present, blank.
    clean_env.setenv("GOOGLE_ADS_CUSTOMER_ID", "")
    clean_env.setenv("GOOGLE_ADS_API_VERSION", "")
    clean_env.setenv("GOOGLE_ADS_OUTPUT_DIR", "")

    settings = Settings.from_env()

    assert settings.api_version == DEFAULT_API_VERSION
    assert settings.customer_id is None
    # Not Path("") -- which is Path("."), i.e. the repo root, which would make
    # an export clobber source files.
    assert settings.output_dir == config.PROJECT_ROOT / DEFAULT_OUTPUT_DIR
    assert settings.output_dir.name == DEFAULT_OUTPUT_DIR


def test_from_env_normalizes_customer_ids(clean_env):
    for key, value in REQUIRED.items():
        clean_env.setenv(key, value)
    clean_env.setenv("GOOGLE_ADS_CUSTOMER_ID", "098-765-4321")

    settings = Settings.from_env()

    assert settings.login_customer_id == "1234567890"
    assert settings.customer_id == "0987654321"


def test_from_env_reports_each_missing_required_key(clean_env):
    for missing in REQUIRED:
        for key, value in REQUIRED.items():
            clean_env.setenv(key, value)
        clean_env.setenv(missing, "")
        with pytest.raises(ConfigError, match=missing):
            Settings.from_env()


def test_relative_output_dir_resolves_against_project_root(clean_env):
    for key, value in REQUIRED.items():
        clean_env.setenv(key, value)
    clean_env.setenv("GOOGLE_ADS_OUTPUT_DIR", "exports/daily")
    assert Settings.from_env().output_dir == config.PROJECT_ROOT / "exports/daily"


def test_absolute_output_dir_is_left_alone(clean_env, tmp_path):
    for key, value in REQUIRED.items():
        clean_env.setenv(key, value)
    clean_env.setenv("GOOGLE_ADS_OUTPUT_DIR", str(tmp_path / "reports"))
    assert Settings.from_env().output_dir == tmp_path / "reports"


def _settings(**overrides):
    base = dict(
        developer_token="dev-token-placeholder",
        client_id="client-id-placeholder",
        client_secret="client-secret-placeholder",
        refresh_token="refresh-token-placeholder",
        login_customer_id="1234567890",
        customer_id="0987654321",
        api_version=DEFAULT_API_VERSION,
        output_dir=Path("/tmp/out"),
    )
    base.update(overrides)
    return Settings(**base)


def test_google_ads_dict_has_the_keys_the_library_requires():
    payload = _settings().to_google_ads_dict()
    assert payload["use_proto_plus"] is True
    assert set(payload) == {
        "developer_token",
        "client_id",
        "client_secret",
        "refresh_token",
        "login_customer_id",
        "use_proto_plus",
    }
    assert payload["login_customer_id"] == "1234567890"


def test_describe_never_leaks_a_secret_value():
    settings = _settings(
        developer_token="SUPERSECRETTOKEN",
        client_secret="SUPERSECRETCLIENT",
        refresh_token="SUPERSECRETREFRESH",
    )
    rendered = " ".join(settings.describe().values())

    for secret in ("SUPERSECRETTOKEN", "SUPERSECRETCLIENT", "SUPERSECRETREFRESH"):
        assert secret not in rendered
        # Not even a prefix or suffix of it.
        assert secret[:4] not in rendered
        assert secret[-4:] not in rendered

    assert settings.describe()["developer_token"] == "present (16 chars)"
    # Customer IDs are not secrets and should be legible for diagnostics.
    assert settings.describe()["login_customer_id"] == "1234567890"


def test_describe_marks_an_empty_secret_as_missing():
    assert _settings(refresh_token="").describe()["refresh_token"] == "MISSING"


def test_resolve_customer_id_prefers_the_override():
    settings = _settings()
    assert settings.resolve_customer_id("111-222-3333") == "1112223333"
    assert settings.resolve_customer_id(None) == "0987654321"
    assert settings.resolve_customer_id("") == "0987654321"


def test_resolve_customer_id_raises_when_nothing_is_configured():
    with pytest.raises(ConfigError, match="--customer-id"):
        _settings(customer_id=None).resolve_customer_id()
