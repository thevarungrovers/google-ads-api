"""CLI tests that stop short of the network.

Everything here runs offline: argument parsing, date-flag resolution, the
report listing and --show-query, which is the whole pipeline from flags to GAQL
without a client. Nothing in this file needs credentials.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load(script_name):
    """Import a file from scripts/ as a module."""
    path = PROJECT_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fetch():
    return _load("fetch_report.py")


@pytest.fixture(scope="module")
def auth():
    return _load("generate_refresh_token.py")


# --------------------------------------------------------------------------
# fetch_report: listing and query rendering
# --------------------------------------------------------------------------


def test_list_exits_cleanly_and_names_every_report(fetch, capsys):
    assert fetch.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name in ("accounts", "campaigns", "keywords"):
        assert name in out


def test_no_arguments_asks_for_a_report_and_fails(fetch, capsys):
    assert fetch.main([]) == 2
    assert "Pass a report name" in capsys.readouterr().out


def test_show_query_renders_gaql_without_touching_the_api(fetch, capsys):
    assert fetch.main(["campaigns", "--during", "LAST_7_DAYS", "--show-query"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT ")
    assert "FROM campaign" in out
    assert "segments.date DURING LAST_7_DAYS" in out


def test_show_query_applies_limit_and_extra_where(fetch, capsys):
    assert fetch.main(
        [
            "campaigns",
            "--during", "LAST_7_DAYS",
            "--where", "campaign.status = 'ENABLED'",
            "--limit", "5",
            "--show-query",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "AND campaign.status = 'ENABLED'" in out
    assert "LIMIT 5" in out


def test_show_query_for_a_fixed_window(fetch, capsys):
    assert fetch.main(
        ["keywords", "--start", "2026-09-01", "--end", "2026-09-30", "--show-query"]
    ) == 0
    assert "BETWEEN '2026-09-01' AND '2026-09-30'" in capsys.readouterr().out


def test_accounts_report_renders_without_a_date_clause(fetch, capsys):
    assert fetch.main(["accounts", "--show-query"]) == 0
    out = capsys.readouterr().out
    assert "FROM customer_client" in out
    assert "segments.date" not in out


# --------------------------------------------------------------------------
# fetch_report: argument validation
# --------------------------------------------------------------------------


def test_an_unknown_report_is_refused_by_argparse(fetch):
    with pytest.raises(SystemExit) as exit_info:
        fetch.main(["campaign"])  # singular
    assert exit_info.value.code == 2


def test_date_flags_are_mutually_exclusive(fetch):
    with pytest.raises(SystemExit):
        fetch.main(["campaigns", "--days", "7", "--during", "LAST_7_DAYS"])


def test_start_without_end_is_refused(fetch, capsys):
    assert fetch.main(["campaigns", "--start", "2026-09-01", "--show-query"]) == 2
    assert "must be given together" in capsys.readouterr().err


def test_an_invented_preset_is_refused(fetch, capsys):
    assert fetch.main(["campaigns", "--during", "LAST_45_DAYS", "--show-query"]) == 2
    assert "not a GAQL date preset" in capsys.readouterr().err


def test_a_reversed_fixed_window_is_refused(fetch, capsys):
    assert fetch.main(
        ["campaigns", "--start", "2026-09-30", "--end", "2026-09-01", "--show-query"]
    ) == 2
    assert "is after end" in capsys.readouterr().err


def test_a_date_range_on_the_accounts_report_is_refused(fetch, capsys):
    assert fetch.main(["accounts", "--days", "7", "--show-query"]) == 2
    assert "no date segment" in capsys.readouterr().err


def test_a_non_select_where_condition_cannot_be_smuggled_in(fetch, capsys):
    """--where is appended to the query, so the SELECT-only check must cover it."""
    assert fetch.main(
        ["campaigns", "--where", "1=1; DELETE FROM campaign", "--show-query"]
    ) == 2
    assert "DELETE" in capsys.readouterr().err


# --------------------------------------------------------------------------
# fetch_report: date resolution
# --------------------------------------------------------------------------


def test_days_window_ends_yesterday(fetch, monkeypatch):
    from googleads_reporting import query as query_module

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 8)

    monkeypatch.setattr(query_module, "date", FrozenDate)

    args = fetch.build_parser().parse_args(["campaigns", "--days", "7"])
    condition = fetch.resolve_date_condition(args, None)
    assert condition == "segments.date BETWEEN '2026-10-01' AND '2026-10-07'"


def test_no_date_flag_defers_to_the_report_default(fetch):
    args = fetch.build_parser().parse_args(["campaigns"])
    assert fetch.resolve_date_condition(args, None) is None


# --------------------------------------------------------------------------
# generate_refresh_token: the .env rewrite
# --------------------------------------------------------------------------


def test_existing_key_is_rewritten_in_place(auth, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOGLE_ADS_CLIENT_ID=abc\n"
        "GOOGLE_ADS_REFRESH_TOKEN=old-token\n"
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890\n"
    )

    assert auth.write_into_env("new-token", env_file) == "updated"

    lines = env_file.read_text().splitlines()
    # Exactly one occurrence: a second one would win in python-dotenv and
    # leave a stale token silently above it.
    assert lines.count("GOOGLE_ADS_REFRESH_TOKEN=new-token") == 1
    assert "old-token" not in env_file.read_text()
    # Nothing else was disturbed.
    assert "GOOGLE_ADS_CLIENT_ID=abc" in lines
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890" in lines


def test_a_blank_key_from_the_template_is_filled_in(auth, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_ADS_REFRESH_TOKEN=\nGOOGLE_ADS_CLIENT_ID=abc\n")
    assert auth.write_into_env("new-token", env_file) == "updated"
    assert "GOOGLE_ADS_REFRESH_TOKEN=new-token" in env_file.read_text()


def test_a_missing_key_is_appended(auth, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_ADS_CLIENT_ID=abc\n")
    assert auth.write_into_env("new-token", env_file) == "appended"
    assert "GOOGLE_ADS_REFRESH_TOKEN=new-token" in env_file.read_text()
    assert "GOOGLE_ADS_CLIENT_ID=abc" in env_file.read_text()


def test_an_exported_form_of_the_key_is_also_matched(auth, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("export GOOGLE_ADS_REFRESH_TOKEN=old\n")
    assert auth.write_into_env("new-token", env_file) == "updated"
    assert "old" not in env_file.read_text()


def test_the_rewrite_leaves_the_file_owner_only(auth, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_ADS_REFRESH_TOKEN=old\n")
    env_file.chmod(0o644)

    auth.write_into_env("new-token", env_file)

    assert env_file.stat().st_mode & 0o777 == 0o600
    # No temp file left holding the token.
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_missing_env_file_is_an_error_not_a_new_file(auth, tmp_path):
    from googleads_reporting.config import ConfigError

    env_file = tmp_path / "absent.env"
    with pytest.raises(ConfigError, match="does not exist"):
        auth.write_into_env("new-token", env_file)
    assert not env_file.exists()


def test_the_scope_is_the_single_adwords_scope(auth):
    assert auth.SCOPES == ["https://www.googleapis.com/auth/adwords"]
