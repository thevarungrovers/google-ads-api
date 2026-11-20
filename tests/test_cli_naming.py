"""The CLI names what a human creates OWNER-cli-<base>, or the plain base.

Placeholders only: AB / max, CD / bot.
"""

from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from googleads_reporting.naming import Identity
from googleads_reporting.write import wizard
from googleads_reporting.write.client import MutationError

ROOT = Path(__file__).resolve().parent.parent

IDENTITIES = [
    (Identity.from_values("AB", "max", source=".env"), "AB-cli-"),
    (Identity.from_values("AB", "", source=".env"), "AB-cli-"),
    (Identity.from_values("", "max", source=".env"), ""),
    (Identity(), ""),
]


def load_manage():
    path = ROOT / "scripts" / "manage.py"
    spec = importlib.util.spec_from_file_location("manage_for_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manage(monkeypatch):
    module = load_manage()
    captured = {}

    def capture(name):
        def plan(_client, **kwargs):
            captured[name] = kwargs
            return SimpleNamespace(**kwargs)
        return plan

    monkeypatch.setattr(module.campaigns, "plan_create", capture("campaign"))
    monkeypatch.setattr(module.budgets, "plan_create", capture("budget"))
    monkeypatch.setattr(module.adgroups, "plan_create", capture("ad_group"))
    monkeypatch.setattr(module.campaigns, "plan_rename", capture("rename"))
    return module, captured


def args_for(manage_module, argv, identity):
    args = manage_module.build_parser().parse_args(argv)
    args.cli_identity = identity.cli()
    return args


@pytest.mark.parametrize("identity, prefix", IDENTITIES)
@pytest.mark.parametrize("argv, kind", [
    (["campaign", "create", "--name", "launch", "--budget-resource-name", "b"], "campaign"),
    (["budget", "create", "--name", "launch", "--amount", "5"], "budget"),
    (["adgroup", "create", "--name", "launch", "--campaign-resource-name", "c"], "ad_group"),
])
def test_cli_creates_are_stamped_owner_cli_or_plain(manage, identity, prefix, argv, kind):
    module, captured = manage
    module.build_plan(None, args_for(module, argv, identity))
    assert captured[kind]["name"] == f"{prefix}launch"


def test_the_cli_refuses_another_identitys_prefix(manage):
    module, _ = manage
    args = args_for(module, ["campaign", "create", "--name", "CD-bot-x",
                             "--budget-resource-name", "b"], IDENTITIES[0][0])
    with pytest.raises(MutationError, match="another agent"):
        module.build_plan(None, args)


@pytest.mark.parametrize("current, wanted", [
    ("AB-cli-old", "AB-cli-new"),   # ours: the prefix is kept
    ("Hand made", "new"),           # a person's: renamed exactly as asked
])
def test_cli_rename_keeps_only_a_prefix_it_already_had(manage, monkeypatch, current, wanted):
    module, captured = manage
    row = SimpleNamespace(campaign=SimpleNamespace(id=11, name=current))
    monkeypatch.setattr(module.campaigns, "find", lambda *_a, **_k: row)
    args = args_for(module, ["campaign", "rename", "--campaign-id", "11", "--new-name", "new"],
                    IDENTITIES[0][0])
    module.build_plan(None, args)
    assert captured["rename"]["new_name"] == wanted


def test_the_cli_prints_the_identity_it_stamps():
    module = load_manage()
    assert "AB-cli-<name>" in module.naming_line(IDENTITIES[0][0].cli())
    assert "used exactly as given" in module.naming_line(Identity().cli())


@pytest.mark.parametrize("identity, prefix", IDENTITIES)
def test_the_wizard_stamps_and_echoes_both_names(monkeypatch, identity, prefix):
    queue = ["SEARCH", "launch", "5", "PAUSED", "NO", "General", "0.5", ""]
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    printed = []
    monkeypatch.setattr(builtins, "print", lambda *a, **_k: printed.append(" ".join(map(str, a))))
    with pytest.raises(wizard.Aborted):
        wizard.ask_for_campaign(identity.cli())  # stops at the URL question
    if prefix:
        assert any(f"'{prefix}launch'" in line for line in printed)
        assert any(f"'{prefix}General'" in line for line in printed)
        assert any(f"{prefix} prefix is added" in a for a in asked)
    else:
        assert not any("will be named" in line for line in printed)


@pytest.mark.parametrize("identity, prefix", IDENTITIES)
def test_the_cli_refuses_another_identity_whatever_is_configured(manage, monkeypatch,
                                                                 identity, prefix):
    """The CLI and the MCP server agree: no identity is no licence to forge one."""
    module, _ = manage
    args = args_for(module, ["campaign", "create", "--name", "CD-bot-x",
                             "--budget-resource-name", "b"], identity)
    with pytest.raises(MutationError, match="CD-bot-"):
        module.build_plan(None, args)
    row = SimpleNamespace(campaign=SimpleNamespace(id=11, name="Hand made"))
    monkeypatch.setattr(module.campaigns, "find", lambda *_a, **_k: row)
    args = args_for(module, ["campaign", "rename", "--campaign-id", "11",
                             "--new-name", "CD-bot-y"], identity)
    with pytest.raises(MutationError, match="CD-bot-"):
        module.build_plan(None, args)
