"""Repo hygiene: the right files are tracked, and the secret ones are not.

This file exists because of a real miss. `.gitignore` carries a deliberately
broad `*refresh_token*` so a stored token cannot slip in under any name -- and
that pattern also matched `scripts/generate_refresh_token.py`, which is source.
The script was silently absent from the first commit that should have contained
it: `git add -A` reported success, nothing warned, and the file simply was not
there.

So this checks both directions, since each is a different way to lose.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (PROJECT_ROOT / ".git").exists(),
    reason="not a git checkout",
)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def tracked_files() -> set[str]:
    return set(git("ls-files").splitlines())


def is_ignored(path: str) -> bool:
    """Whether git would ignore ``path``. Works for files that do not exist."""
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "check-ignore", "-q", path],
        capture_output=True,
    )
    return result.returncode == 0


# --------------------------------------------------------------------------
# Source must be tracked
# --------------------------------------------------------------------------


def test_every_source_file_is_tracked():
    """An ignore rule written for secrets must not swallow source."""
    tracked = tracked_files()
    missing = []
    for directory in ("googleads_reporting", "scripts", "tests"):
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = str(path.relative_to(PROJECT_ROOT))
            if relative not in tracked:
                missing.append(relative)
    assert missing == [], (
        "Untracked source file(s) -- check .gitignore is not matching them:\n"
        + "\n".join(missing)
    )


def test_the_refresh_token_generator_is_tracked():
    """The specific file the broad ignore rule swallowed."""
    assert "scripts/generate_refresh_token.py" in tracked_files()
    assert not is_ignored("scripts/generate_refresh_token.py")


def test_the_env_template_is_tracked():
    assert ".env.example" in tracked_files()
    assert not is_ignored(".env.example")


def test_the_essential_non_python_files_are_tracked():
    tracked = tracked_files()
    for name in ("README.md", "requirements.txt", ".gitignore", "pytest.ini"):
        assert name in tracked


# --------------------------------------------------------------------------
# Secrets must not be
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "production.env",
        "client_secret.json",
        "client_secret_1234.apps.googleusercontent.com.json",
        "token.json",
        "token_cache.json",
        "my_refresh_token.txt",
        "refresh_token",
        "private-key.pem",
        "output/campaigns_1234567890_20261008-120000.csv",
        "data/raw.csv",
        ".venv/lib/python3.13/site-packages/x.py",
        "__pycache__/config.cpython-313.pyc",
        ".DS_Store",
    ],
)
def test_secret_and_generated_paths_are_ignored(path):
    assert is_ignored(path), f"{path} would be committable"


def test_no_credential_shaped_file_is_currently_tracked():
    tracked = tracked_files()
    offenders = [
        name
        for name in tracked
        if name == ".env"
        or name.endswith((".pem", ".p12", ".key"))
        or "client_secret" in name
        or (name.endswith(".json") and "token" in name)
    ]
    assert offenders == []


#: Recognisable prefixes for a Google OAuth client secret, a refresh token and
#: an access token. Assembled from fragments so the literals never appear in
#: this file -- otherwise the scan below flags its own source, and the obvious
#: "fix" is to skip this file, which is the one file guaranteed to be scanned.
SECRET_NEEDLES = ("GOCSPX" + "-", "1//" + "0g", "ya29" + ".")


def test_the_secret_scan_would_catch_a_real_credential():
    """Negative control: a scan with no working needle passes everything."""
    assert any(n in "client_secret=GOCSPX" + "-abc123" for n in SECRET_NEEDLES)
    assert any(n in "refresh=1//" + "0gXYZ" for n in SECRET_NEEDLES)
    assert any(n in "bearer ya29" + ".a0ARr" for n in SECRET_NEEDLES)
    assert not any(n in "GOOGLE_ADS_REFRESH_TOKEN=" for n in SECRET_NEEDLES)


def test_no_tracked_file_contains_an_oauth_client_secret_pattern():
    """Google's client secrets and tokens have recognisable prefixes."""
    needles = SECRET_NEEDLES
    offenders = []
    for name in tracked_files():
        path = PROJECT_ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for needle in needles:
            if needle in text:
                offenders.append(f"{name}: contains {needle!r}")
    assert offenders == []
