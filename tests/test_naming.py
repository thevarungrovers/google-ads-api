"""The naming convention: OWNER-agent-base, from .env only, refused never truncated.

Placeholders only: AB / max for this install, CD / bot for somebody else.
"""

from __future__ import annotations

import pytest

from googleads_reporting import naming
from googleads_reporting.naming import (
    AGENT_KEY,
    OWNER_KEY,
    Identity,
    NameRefused,
    attribute,
    load_identity,
    matches,
    owns,
    parse,
    process_env_mismatches,
    rename,
    stamp,
)

BOTH = Identity.from_values("AB", "max", source=".env")
OWNER_ONLY = Identity.from_values("AB", "", source=".env")
AGENT_ONLY = Identity.from_values("", "max", source=".env")
NEITHER = Identity.from_values("", "")

COMBOS = [
    (BOTH, "AB-max-spring_promo"),
    (OWNER_ONLY, "AB-spring_promo"),
    (AGENT_ONLY, "max-spring_promo"),
    (NEITHER, "spring_promo"),
]


# -- the identity ---------------------------------------------------------


@pytest.mark.parametrize("identity, prefix", [
    (BOTH, "AB-max-"), (OWNER_ONLY, "AB-"), (AGENT_ONLY, "max-"), (NEITHER, ""),
])
def test_the_prefix_uses_only_the_parts_that_are_set(identity, prefix):
    assert identity.prefix == prefix
    assert identity.valid


@pytest.mark.parametrize("owner", ["A", "ABCDE", "ab", "A1", "A-B"])
def test_malformed_initials_are_a_problem_naming_the_env_key(owner):
    identity = Identity.from_values(owner, "max")
    assert not identity.valid
    assert OWNER_KEY in identity.problems[0]


@pytest.mark.parametrize("agent", ["m", "Max", "my-agent", "1max", "cli", "manual", "a" * 25])
def test_malformed_or_reserved_agent_names_are_a_problem(agent):
    identity = Identity.from_values("AB", agent)
    assert not identity.valid
    assert AGENT_KEY in identity.problems[0]


def test_blank_values_are_skipped_not_errors():
    identity = Identity.from_values("  ", "\t")
    assert identity.valid and identity.prefix == ""


def write(path, **values):
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def test_identity_is_read_from_dotenv_and_dotenv_beats_config_env(tmp_path):
    config = write(tmp_path / "config.env", **{OWNER_KEY: "CD", AGENT_KEY: "bot"})
    dotenv = write(tmp_path / ".env", **{OWNER_KEY: "AB"})
    identity = load_identity((config, dotenv))
    assert (identity.owner, identity.agent) == ("AB", "bot")
    assert identity.source == ".env, config.env"


def test_empty_keys_in_dotenv_leave_names_plain(tmp_path):
    dotenv = write(tmp_path / ".env", **{OWNER_KEY: "", AGENT_KEY: ""})
    identity = load_identity((dotenv,))
    assert identity.prefix == "" and identity.source == "none"


def test_missing_files_mean_no_identity(tmp_path):
    assert load_identity((tmp_path / "nope.env",)).prefix == ""


def test_the_process_environment_is_ignored_and_a_mismatch_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv(OWNER_KEY, "CD")
    monkeypatch.setenv(AGENT_KEY, "max")
    identity = load_identity((write(tmp_path / ".env", **{OWNER_KEY: "AB", AGENT_KEY: "max"}),))
    assert identity.prefix == "AB-max-"
    warnings = process_env_mismatches(identity)
    assert len(warnings) == 1 and OWNER_KEY in warnings[0]
    assert "CD" not in warnings[0]  # the value is never echoed


@pytest.mark.parametrize("identity, prefix", [
    (BOTH, "AB-cli-"), (OWNER_ONLY, "AB-cli-"), (AGENT_ONLY, ""), (NEITHER, ""),
])
def test_the_cli_stamps_owner_cli_or_nothing(identity, prefix):
    assert identity.cli().prefix == prefix
    assert stamp(identity.cli(), "launch", entity_type="campaign").final == prefix + "launch"


# -- the parser -----------------------------------------------------------


def test_parse_reads_only_the_full_form():
    assert parse("AB-max-spring promo-2") == naming.Parsed("AB", "max", "spring promo-2")
    assert parse("AB-cli-x").origin == "cli"
    assert parse("AB-max-x").origin == "agent"
    for name in ("spring", "AB-spring", "ab-max-x", "AB-Max-x", "AB-max-", None, ""):
        assert parse(name) is None


@pytest.mark.parametrize("identity, final", COMBOS)
def test_parse_and_stamp_round_trip(identity, final):
    stamped = stamp(identity, "spring_promo", entity_type="campaign")
    assert stamped.final == final
    found = attribute(stamped.final, identity)
    if identity.prefix:
        assert owns(identity, stamped.final)
        assert found["origin"] == "agent" and found["base"] == "spring_promo"
    else:
        assert found["origin"] == "manual"


def test_attribute_without_an_identity_reads_any_full_prefix():
    assert attribute("CD-bot-x") == {"origin": "agent", "owner": "CD", "agent": "bot",
                                     "base": "x"}
    assert attribute("AB-cli-x")["origin"] == "cli"
    assert attribute("launch")["origin"] == "manual"


# -- stamping -------------------------------------------------------------


@pytest.mark.parametrize("identity, final", COMBOS)
def test_stamping_is_idempotent_on_our_own_prefix(identity, final):
    again = stamp(identity, final, entity_type="ad_group")
    assert again.final == final
    assert again.rule == ("already_ours" if identity.prefix else again.rule)


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, AGENT_ONLY])
def test_another_identitys_prefix_is_refused(identity):
    with pytest.raises(NameRefused, match="CD-bot-.*another agent"):
        stamp(identity, "CD-bot-launch", entity_type="campaign")


def test_with_no_identity_the_name_is_used_exactly_as_given():
    stamped = stamp(NEITHER, "  spring-launch ", entity_type="campaign")
    assert stamped.final == "spring-launch" and stamped.prefix == ""
    assert stamped.rule.startswith("no identity configured")
    assert stamped.problems == []


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\tb", "a\x00b"])
def test_control_characters_are_refused(bad):
    with pytest.raises(NameRefused, match="control character"):
        stamp(BOTH, bad, entity_type="campaign")


def test_empty_names_are_refused():
    with pytest.raises(NameRefused):
        stamp(BOTH, "   ", entity_type="campaign")
    with pytest.raises(NameRefused):
        stamp(BOTH, "AB-max-", entity_type="campaign")


@pytest.mark.parametrize("base", ["spring_promo", "spring promo", "spring-promo"])
def test_the_bases_own_separators_are_kept(base):
    assert stamp(BOTH, base, entity_type="campaign").final == f"AB-max-{base}"


def test_over_length_is_a_problem_naming_the_room_left_never_truncated():
    stamped = stamp(BOTH, "x" * 80, entity_type="label")
    assert stamped.final == "AB-max-" + "x" * 80
    (check, detail), = stamped.problems
    assert check == "name_length"
    assert "at most 73 characters" in detail and "it is 80" in detail


def test_the_campaign_limit_counts_utf8_bytes():
    fits = stamp(BOTH, "x" * 241, entity_type="campaign")
    assert fits.problems == []
    multibyte = stamp(BOTH, "é" * 121, entity_type="campaign")  # 242 bytes
    assert [c for c, _ in multibyte.problems] == ["name_length"]
    assert stamp(BOTH, "é" * 121, entity_type="ad_group").problems == []


def test_a_malformed_identity_is_a_problem_on_every_stamp():
    bad = Identity.from_values("a1", "max")
    assert [c for c, _ in stamp(bad, "x", entity_type="campaign").problems] == ["agent_identity"]


def test_the_naming_block_and_warning():
    stamped = stamp(BOTH, "spring", entity_type="campaign")
    assert stamped.block() == {
        "requested": "spring", "final": "AB-max-spring", "prefix": "AB-max-",
        "base": "spring", "length": 13, "max_length": 248, "unit": "bytes",
        "rule": "stamped",
    }
    assert stamped.warning() == 'the name will be "AB-max-spring" (prefix added)'
    assert stamp(NEITHER, "spring", entity_type="campaign").warning() is None


# -- renames --------------------------------------------------------------


@pytest.mark.parametrize("identity, final", COMBOS)
def test_renaming_our_entity_keeps_the_prefix(identity, final):
    current = identity.prefix + "old"
    renamed = rename(identity, current, "spring_promo", entity_type="campaign",
                     ledger_owned=not identity.prefix)
    assert renamed.final == final and renamed.problems == []


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, AGENT_ONLY, NEITHER])
def test_renaming_a_manual_entity_is_refused(identity):
    renamed = rename(identity, "launch", "spring", entity_type="campaign", ledger_owned=False)
    assert [c for c, _ in renamed.problems] == ["own_entity"]
    assert "manual entity" in renamed.problems[0][1]


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, AGENT_ONLY, NEITHER])
def test_renaming_another_agents_entity_is_refused(identity):
    renamed = rename(identity, "CD-bot-launch", "spring", entity_type="campaign",
                     ledger_owned=False)
    assert "another agent" in renamed.problems[-1][1]


def test_a_ledger_owned_entity_that_lost_its_prefix_gets_it_back():
    renamed = rename(BOTH, "launch", "launch", entity_type="campaign", ledger_owned=True)
    assert renamed.final == "AB-max-launch" and renamed.rule == "restored_prefix"
    assert "restoring the prefix" in renamed.warning()


def test_an_owner_only_install_does_not_own_a_cli_entity():
    assert not owns(OWNER_ONLY, "AB-cli-launch")
    assert owns(OWNER_ONLY, "AB-launch")


# -- filters --------------------------------------------------------------


def test_filters_match_exactly():
    rows = ["AB-max-a", "AB-cli-b", "CD-bot-c", "manual d", "AB-bot-e"]
    pick = lambda **kw: [r for r in rows if matches(r, identity=BOTH, **kw)]  # noqa: E731
    assert pick(origin="manual") == ["manual d"]
    assert pick(origin="cli") == ["AB-cli-b"]
    assert pick(origin="agent") == ["AB-max-a", "CD-bot-c", "AB-bot-e"]
    assert pick(owner="AB") == ["AB-max-a", "AB-cli-b", "AB-bot-e"]
    assert pick(agent_name="bot") == ["CD-bot-c", "AB-bot-e"]
    assert pick(mine=True) == ["AB-max-a"]


def test_a_malformed_filter_is_refused():
    with pytest.raises(ValueError):
        naming.check_filter("ab'", None)
    with pytest.raises(ValueError):
        naming.check_filter(None, "x' OR 1=1")


# -- whose name is it: a full form that parses to another identity ---------
#
# owns(name) = it starts with our prefix AND (it does not parse as a full
# OWNER-agent- form, OR it parses to exactly our owner and agent). Without the
# second half, an initials-only install's "AB-" claims "AB-bot-x" (another
# agent's) and "AB-cli-x" (a human's).


@pytest.mark.parametrize("identity, name, ours", [
    (OWNER_ONLY, "AB-x", True),
    (OWNER_ONLY, "AB-bot-x", False),
    (OWNER_ONLY, "AB-cli-x", False),
    (OWNER_ONLY, "AB-max-x", False),
    (AGENT_ONLY, "max-x", True),
    (AGENT_ONLY, "max-AB-x", True),
    (AGENT_ONLY, "AB-max-x", False),
    (BOTH, "AB-max-x", True),
    (BOTH, "AB-bot-x", False),
    (BOTH, "AB-cli-x", False),
    (BOTH, "AB-x", False),
])
def test_owns_refuses_a_name_that_parses_as_another_identity(identity, name, ours):
    assert owns(identity, name) is ours


def test_initials_only_attribute_and_mine_file_other_identities_correctly():
    assert attribute("AB-bot-x", OWNER_ONLY) == {
        "origin": "agent", "owner": "AB", "agent": "bot", "base": "x"}
    assert attribute("AB-cli-x", OWNER_ONLY)["origin"] == "cli"
    assert attribute("AB-x", OWNER_ONLY) == {
        "origin": "agent", "owner": "AB", "agent": None, "base": "x"}
    rows = ["AB-x", "AB-bot-x", "AB-cli-x", "x"]
    assert [r for r in rows if matches(r, mine=True, identity=OWNER_ONLY)] == ["AB-x"]


@pytest.mark.parametrize("current", ["AB-bot-x", "AB-cli-x"])
def test_initials_only_cannot_rename_another_identitys_entity(current):
    renamed = rename(OWNER_ONLY, current, "y", entity_type="campaign", ledger_owned=False)
    assert [c for c, _ in renamed.problems] == ["own_entity"]


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, AGENT_ONLY, NEITHER])
def test_ledger_evidence_does_not_override_another_full_identity(identity):
    """Even if the ledger says we created it, a live name that parses as a
    DIFFERENT full identity is somebody else's now; renaming it would strip
    their marker."""
    renamed = rename(identity, "CD-bot-launch", "spring", entity_type="campaign",
                     ledger_owned=True)
    assert "own_entity" in [c for c, _ in renamed.problems]


@pytest.mark.parametrize("base", ["bot-x", "cli-x", "max-x", "spring-sale"])
def test_initials_only_refuses_a_base_that_would_forge_an_agent(base):
    """AB- + "bot-x" is "AB-bot-x": it would read as another agent's."""
    with pytest.raises(NameRefused, match="agent"):
        stamp(OWNER_ONLY, base, entity_type="campaign")


@pytest.mark.parametrize("base", ["bot_x", "Spring-sale", "x-bot", "2go-x", "a-x"])
def test_initials_only_still_stamps_bases_that_cannot_parse(base):
    assert stamp(OWNER_ONLY, base, entity_type="campaign").final == "AB-" + base


def test_cli_rename_under_initials_only_does_not_claim_another_agents_campaign():
    """The CLI identity (AB, cli) owns AB-cli-x and nothing else under AB."""
    cli = OWNER_ONLY.cli()
    assert owns(cli, "AB-cli-x")
    assert not owns(cli, "AB-bot-x") and not owns(cli, "AB-x")


# -- impersonation: refused even with no identity configured -----------------


@pytest.mark.parametrize("identity", [BOTH, OWNER_ONLY, AGENT_ONLY, NEITHER])
@pytest.mark.parametrize("name", ["CD-bot-x", "AB-cli-x", " CD-bot-x "])
def test_a_name_carrying_another_identity_is_refused_whatever_is_configured(identity, name):
    """No identity is not a licence to write somebody else's: "CD-bot-x" would
    attribute the entity to agent bot of owner CD."""
    with pytest.raises(NameRefused, match="prefix"):
        stamp(identity, name, entity_type="campaign")


def test_renaming_to_another_identity_is_refused_with_no_identity():
    with pytest.raises(NameRefused, match="CD-bot-"):
        rename(NEITHER, "old", "CD-bot-y", entity_type="campaign", ledger_owned=True)


def test_the_cli_with_no_initials_refuses_another_identity_too():
    with pytest.raises(NameRefused, match="CD-bot-"):
        naming.stamp_for_cli(NEITHER.cli(), "CD-bot-x", entity_type="campaign")


# -- a doubled prefix ---------------------------------------------------------


@pytest.mark.parametrize("identity, requested", [
    (BOTH, "AB-max-AB-max-x"),
    (BOTH, "AB-max-CD-bot-x"),
    (BOTH, "AB-max-AB-cli-x"),
    (OWNER_ONLY, "AB-AB-x"),
    (OWNER_ONLY, "AB-CD-bot-x"),
    (AGENT_ONLY, "max-max-x"),
    (AGENT_ONLY, "max-CD-bot-x"),
])
def test_a_base_still_carrying_a_prefix_after_ours_is_stripped_is_refused(identity, requested):
    """Stripping our prefix once must leave a plain base: "AB-max-AB-max-x"
    would otherwise be sent as is, doubled."""
    with pytest.raises(NameRefused, match="prefix"):
        stamp(identity, requested, entity_type="campaign")


def test_a_rename_to_a_doubled_prefix_is_refused():
    with pytest.raises(NameRefused, match="prefix"):
        rename(BOTH, "AB-max-old", "AB-max-AB-max-new", entity_type="campaign",
               ledger_owned=False)
