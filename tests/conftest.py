"""Shared test setup.

The one thing here is important: NO TEST MAY WRITE TO THE REAL LOG DATABASE.

``logs/google-ads.db`` is the live record of what this toolchain did to a
production ad account. Several tests call a CLI ``main()`` directly
(``tests/test_cli.py``), and those entry points are instrumented, so without
this fixture a plain ``pytest`` run appends dozens of rows to the operator's
actual log -- quietly, and indistinguishably from real activity.

So every test gets its own scratch database, autouse, no opt-in required. A
test that wants to inspect it just uses ``logdb.DB_PATH``.
"""

from __future__ import annotations

import pytest

from googleads_reporting import logdb, naming

# NO TEST MAY READ THE OPERATOR'S REAL NAMING IDENTITY either: it lives in the
# repo's .env, and server.STATE is built at import. Module level, so it is in
# place before any test module imports the server. A test that wants an
# identity builds one (placeholders only: AB / max).
naming.IDENTITY_FILES = ()


@pytest.fixture(autouse=True)
def _no_identity_in_the_process_env(monkeypatch):
    """Importing the server loads the real .env into os.environ (config.load_env),
    identity included, and process_env_mismatches would then report it."""
    for name in ("GOOGLE_ADS_MCP_OWNER_INITIALS", "GOOGLE_ADS_MCP_AGENT_NAME"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_log_database(tmp_path):
    """Point logdb at a per-test database and put the real one back after."""
    original_dir, original_path = logdb.LOG_DIR, logdb.DB_PATH
    logdb.reset_for_tests(tmp_path / "logs" / "google-ads.db")
    try:
        yield logdb.DB_PATH
    finally:
        logdb.LOG_DIR, logdb.DB_PATH = original_dir, original_path
        logdb.reset_for_tests()
