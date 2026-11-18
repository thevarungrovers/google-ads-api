"""Names for the entities these tools create: ``<OWNER>-<agent>-<base>``.

A month after an agent created a campaign, nobody can tell it from one a person
made in the Google Ads UI. So every name created through this package starts
with the operator's initials and the agent's name, and can be filtered on:

    AB-max-spring_promo_week_12          (placeholder values, illustration only)

The two ``-`` after the initials and after the agent name are the delimiter;
the base keeps whatever separators were chosen. Neither part may contain a
``-``, so the full form always parses one way.

WHERE THE IDENTITY COMES FROM: this repository's ``.env`` (and ``config.env``,
if one exists; ``.env`` wins per key), and nothing else -- never a tool
argument, so an agent can neither spoof nor skip it, and never the process
environment, so one checkout has exactly one identity:

    GOOGLE_ADS_MCP_OWNER_INITIALS   2-4 uppercase letters
    GOOGLE_ADS_MCP_AGENT_NAME       a lowercase slug, 2-24 characters, no "-"

Each part is used only when it is set and non-empty, so an install that sets
neither gets plain names, exactly as asked for:

    both set        AB-max-<base>
    initials only   AB-<base>
    agent only      max-<base>
    neither         <base>

A value that is set but malformed is refused at preview (the message names the
key), because a bad prefix would make every later parse ambiguous. The CLI
stamps ``<OWNER>-cli-<base>`` when the initials are set, and the plain base
when they are not: the ``cli`` marker only tells a human from an agent under
the same owner.

Library layer, so the CLI and the MCP server share it. Pure functions, apart
from :func:`load_identity`, which reads the two files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

OWNER_KEY = "GOOGLE_ADS_MCP_OWNER_INITIALS"
AGENT_KEY = "GOOGLE_ADS_MCP_AGENT_NAME"

OWNER_PATTERN = re.compile(r"[A-Z]{2,4}")
AGENT_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,23}")
#: ``cli`` marks a human creating through the CLI; ``manual`` is what a name
#: with no prefix is called. Neither may be configured as an agent's name.
RESERVED_AGENTS = frozenset({"cli", "manual"})
CLI_AGENT = "cli"

#: Read in this order; a later file wins per key, so ``.env`` beats ``config.env``.
#: Tests point this somewhere empty (tests/conftest.py): a test run must never
#: pick up the operator's real identity.
IDENTITY_FILES: tuple[Path, ...] = (PROJECT_ROOT / "config.env", PROJECT_ROOT / ".env")

#: entity type -> (the longest final name Google accepts, and in what unit).
#:  * campaign: 248 UTF-8 bytes. The campaign's own limit is undocumented, but
#:    its budget is named "<campaign name> budget" and a budget name is at most
#:    255 bytes, so 255 - len(" budget").
#:  * ad_group: "fewer than 255 UTF-8 full-width characters".
#:  * label: "between 1 and 80, inclusive".
NAME_LIMITS: dict[str, tuple[int, str]] = {
    "campaign": (248, "bytes"),
    "ad_group": (254, "characters"),
    "label": (80, "characters"),
}

#: Google forbids NUL, LF and CR in names; TAB is refused for the same reason.
_CONTROL = re.compile(r"[\x00\t\n\r]")

#: The full form, ``OWNER-agent-base``. Only this form can be told apart from
#: a base that merely contains a hyphen.
_FULL = re.compile(r"([A-Z]{2,4})-([a-z][a-z0-9_]{1,23})-(.+)", re.S)


class NameRefused(ValueError):
    """A requested name that cannot be used, with the reason and the fix."""


@dataclass(frozen=True)
class Identity:
    """Whose name goes in front of what this install creates."""

    owner: str = ""
    agent: str = ""
    #: Which file(s) the values came from, e.g. ".env"; "none" when unset.
    source: str = "none"
    #: One message per malformed value, naming the .env key to fix.
    problems: tuple[str, ...] = ()

    @classmethod
    def from_values(cls, owner: str | None, agent: str | None, *,
                    source: str = "none") -> Identity:
        owner, agent = (owner or "").strip(), (agent or "").strip()
        problems = []
        if owner and not OWNER_PATTERN.fullmatch(owner):
            problems.append(f"{OWNER_KEY} in .env must be 2-4 uppercase letters, e.g. AB.")
        if agent and (not AGENT_PATTERN.fullmatch(agent) or agent in RESERVED_AGENTS):
            problems.append(
                f"{AGENT_KEY} in .env must be a lowercase slug of 2-24 letters, digits "
                f"or _, starting with a letter, with no '-', e.g. max; "
                f"{' and '.join(sorted(RESERVED_AGENTS))} are reserved."
            )
        return cls(owner=owner, agent=agent, source=source, problems=tuple(problems))

    @property
    def valid(self) -> bool:
        return not self.problems

    @property
    def parts(self) -> list[str]:
        """The configured parts, in order, that are well-formed."""
        out = []
        if self.owner and OWNER_PATTERN.fullmatch(self.owner):
            out.append(self.owner)
        if self.agent and (AGENT_PATTERN.fullmatch(self.agent)
                           and (self.agent not in RESERVED_AGENTS or self.agent == CLI_AGENT)):
            out.append(self.agent)
        return out

    @property
    def prefix(self) -> str:
        """``"AB-max-"``, ``"AB-"``, ``"max-"``, or ``""`` when nothing is set."""
        parts = self.parts
        return "-".join(parts) + "-" if parts else ""

    def cli(self) -> Identity:
        """The identity the CLI stamps: our initials and ``cli``, or nothing.

        With no initials there is no owner to tell a human from an agent
        under, so the CLI uses the plain base.
        """
        owner_ok = bool(self.owner) and bool(OWNER_PATTERN.fullmatch(self.owner))
        problems = tuple(p for p in self.problems if p.startswith(OWNER_KEY))
        return Identity(owner=self.owner if owner_ok else "",
                        agent=CLI_AGENT if owner_ok else "",
                        source=self.source, problems=problems)

    def describe(self) -> dict[str, Any]:
        """For get_guardrails: which parts are set, never anything else."""
        return {
            "owner_initials": self.owner or None,
            "agent_name": self.agent or None,
            "prefix": self.prefix,
            "source": self.source,
            "configured": bool(self.owner or self.agent),
            "problems": list(self.problems),
            "form": _form(self),
        }

    def ledger(self) -> dict[str, str]:
        """What a ledger row records about who created or renamed something."""
        return {"owner": self.owner, "agent": self.agent, "source": self.source}


def _form(identity: Identity) -> str:
    if identity.owner and identity.agent:
        return "<OWNER>-<agent>-<base>"
    if identity.owner:
        return "<OWNER>-<base>"
    if identity.agent:
        return "<agent>-<base>"
    return "<base> (no identity configured; names are used as given)"


def _read(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    values = dotenv_values(path)
    return {k: (values.get(k) or "").strip() for k in (OWNER_KEY, AGENT_KEY)}


def load_identity(files: tuple[Path, ...] | None = None) -> Identity:
    """The identity from ``.env`` (and ``config.env``), ``.env`` winning per key.

    The process environment is deliberately NOT consulted: see
    :func:`process_env_mismatches` for the startup warning when it disagrees.
    """
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for path in files if files is not None else IDENTITY_FILES:
        for key, value in _read(path).items():
            if value:
                values[key], sources[key] = value, path.name
    used = sorted(set(sources.values()))
    return Identity.from_values(values.get(OWNER_KEY), values.get(AGENT_KEY),
                                source=", ".join(used) if used else "none")


def process_env_mismatches(identity: Identity) -> list[str]:
    """Keys set in the PROCESS environment to something other than ``.env`` says.

    The process value is ignored either way; this is so the mismatch is
    visible at startup instead of being a mystery prefix later. Values are
    never echoed, only the key names.
    """
    out = []
    for key, ours in ((OWNER_KEY, identity.owner), (AGENT_KEY, identity.agent)):
        theirs = (os.environ.get(key) or "").strip()
        if theirs and theirs != ours:
            out.append(
                f"WARNING: {key} is set in the process environment to a value that "
                f"differs from .env. It is IGNORED: the naming identity comes only "
                f"from this repository's .env."
            )
    return out


@dataclass(frozen=True)
class Parsed:
    owner: str
    agent: str
    base: str

    @property
    def origin(self) -> str:
        return "cli" if self.agent == CLI_AGENT else "agent"


def parse(name: str | None) -> Parsed | None:
    """``"AB-max-x"`` -> Parsed("AB", "max", "x"); None for anything else.

    Only the full form parses: ``"AB-x"`` could as well be a person's base
    that starts with two capitals and a hyphen. :func:`attribute` also knows
    this install's own shorter prefixes.
    """
    found = _FULL.fullmatch(name or "")
    if found is None:
        return None
    return Parsed(*found.groups())


def foreign(identity: Identity, name: str | None) -> Parsed | None:
    """The full ``OWNER-agent-`` form ``name`` parses as, when that is NOT
    exactly this identity's owner and agent; None otherwise.

    An initials-only install is (AB, ""), so "AB-bot-x" -- another agent's --
    and "AB-cli-x" -- a human's -- are both foreign to it, though they start
    with its "AB-" prefix.
    """
    parsed = parse(name)
    if parsed is None or (parsed.owner, parsed.agent) == (identity.owner, identity.agent):
        return None
    return parsed


def owns(identity: Identity, name: str | None) -> bool:
    """Whether ``name`` carries THIS install's prefix (a convention, not proof).

    It starts with our prefix, has something after it, and does not parse as
    any OTHER full identity: "AB-" does not claim "AB-bot-x" or "AB-cli-x".
    """
    prefix = identity.prefix
    name = name or ""
    if not prefix or not name.startswith(prefix) or len(name) == len(prefix):
        return False
    return foreign(identity, name) is None


def _whose(parsed: Parsed) -> str:
    return "a CLI's" if parsed.agent == CLI_AGENT else "another agent's"


def attribute(name: str | None, identity: Identity | None = None) -> dict[str, Any]:
    """The parsed fields a read row carries: origin, owner, agent, base.

    ``origin`` is "agent", "cli" or "manual", from the NAME only -- a
    convention anyone can type. The ledger is the proof.
    """
    name = name or ""
    if identity is not None and owns(identity, name) and not (identity.owner and identity.agent):
        return {"origin": "agent", "owner": identity.owner or None,
                "agent": identity.agent or None, "base": name[len(identity.prefix):]}
    parsed = parse(name)
    if parsed is not None:
        return {"origin": parsed.origin, "owner": parsed.owner, "agent": parsed.agent,
                "base": parsed.base}
    return {"origin": "manual", "owner": None, "agent": None, "base": None}


def matches(name: str | None, *, origin: str = "any", owner: str | None = None,
            agent_name: str | None = None, mine: bool = False,
            identity: Identity | None = None) -> bool:
    """The local, exact half of the find_* filters."""
    name = name or ""
    found = attribute(name, identity)
    if origin != "any" and found["origin"] != origin:
        return False
    if owner and not name.startswith(f"{owner}-"):
        return False
    if agent_name and found["agent"] != agent_name and not name.startswith(f"{agent_name}-"):
        return False
    if mine and not owns(identity or Identity(), name):
        return False
    return True


def check_filter(owner: str | None, agent_name: str | None) -> None:
    """Refuse a filter value that is not a well-formed part, before any query."""
    if owner and not OWNER_PATTERN.fullmatch(owner):
        raise ValueError(f"owner must be 2-4 uppercase letters, got {owner!r}.")
    if agent_name and not AGENT_PATTERN.fullmatch(agent_name):
        raise ValueError(
            f"agent_name must be a lowercase slug (letters, digits, _), got {agent_name!r}."
        )


def final_name_problems(name: str, entity_type: str) -> list[str]:
    """Why a FINAL name, as it will be sent, breaks Google's rules: control
    characters, or over the entity type's length limit. Empty when it is fine."""
    out = []
    if _CONTROL.search(name):
        out.append("contains a control character (NUL, TAB, CR or LF)")
    limit = NAME_LIMITS.get(entity_type)
    if limit is not None and length_of(name, entity_type) > limit[0]:
        out.append(f"{length_of(name, entity_type)} {limit[1]} against {limit[0]}")
    return out


def length_of(name: str, entity_type: str) -> int:
    unit = NAME_LIMITS.get(entity_type, (0, "characters"))[1]
    return len(name.encode("utf-8")) if unit == "bytes" else len(name)


@dataclass
class Stamped:
    """A name as it will be sent, and how it got that way."""

    requested: str
    final: str
    base: str
    prefix: str
    rule: str
    entity_type: str
    #: (check name, detail) for each reason the name cannot be used as is.
    problems: list[tuple[str, str]] = field(default_factory=list)

    @property
    def length(self) -> int:
        return length_of(self.final, self.entity_type)

    @property
    def max_length(self) -> int | None:
        limit = NAME_LIMITS.get(self.entity_type)
        return limit[0] if limit else None

    def block(self) -> dict[str, Any]:
        """The ``naming`` block every naming preview carries."""
        unit = NAME_LIMITS.get(self.entity_type, (0, "characters"))[1]
        return {
            "requested": self.requested, "final": self.final, "prefix": self.prefix,
            "base": self.base, "length": self.length, "max_length": self.max_length,
            "unit": unit, "rule": self.rule,
        }

    def warning(self) -> str | None:
        if self.final == self.requested:
            return None
        why = {"stamped": "prefix added", "restored_prefix": "restoring the prefix",
               "kept_prefix": "prefix kept"}.get(self.rule, self.rule)
        return f'the name will be "{self.final}" ({why})'


_RULE_TEXT = "no identity configured; name used as given"


def _base_of(identity: Identity, requested: str) -> tuple[str, bool]:
    """(base, had our prefix). Refuses a prefix that is somebody else's."""
    if requested.startswith(identity.prefix) and not requested[len(identity.prefix):].strip():
        raise NameRefused("The name has nothing after the prefix.")
    if owns(identity, requested):
        base = requested[len(identity.prefix):]
        # Our prefix is stripped ONCE. What is left must be a plain base:
        # "AB-max-AB-max-x" would otherwise be sent doubled, and
        # "AB-max-CD-bot-x" would carry a second identity inside ours.
        again = parse(base)
        if base.startswith(identity.prefix) or again is not None:
            shown = identity.prefix if again is None else f"{again.owner}-{again.agent}-"
            raise NameRefused(
                f"after this checkout's {identity.prefix} prefix the name starts with "
                f"{shown} -- a prefix again. Pass the base name only, once."
            )
        return base, True
    parsed = foreign(identity, requested)
    if parsed is not None:
        _refuse_foreign(parsed)
    return requested, False


def _refuse_foreign(parsed: Parsed) -> None:
    """Impersonation: a requested name in another identity's full form."""
    raise NameRefused(
        f"name starts with {parsed.owner}-{parsed.agent}-, which is "
        f"{_whose(parsed)} prefix, not this checkout's; pass the base name only. "
        "(A base that merely looks like one -- two to four capitals, a hyphen, a "
        "lowercase word, a hyphen -- must be reordered.)"
    )


def stamp(identity: Identity, requested: str, *, entity_type: str) -> Stamped:
    """The final name for a NEW entity of ``entity_type``.

    In order: trim and refuse empty; our own prefix is never doubled; another
    identity's full prefix is refused, even with no identity configured
    (impersonation); control characters are refused; over-length
    is a problem (never truncated; the prefix is never shortened).
    """
    requested = (requested or "").strip()
    if not requested:
        raise NameRefused("The name is empty.")
    prefix = identity.prefix
    if prefix:
        base, already = _base_of(identity, requested)
        rule = "already_ours" if already else "stamped"
    else:
        # No identity is not a licence to write somebody else's: "CD-bot-x"
        # would attribute the entity to agent bot of owner CD.
        parsed = foreign(identity, requested)
        if parsed is not None:
            _refuse_foreign(parsed)
        base, rule = requested, "no_identity"
    if not base.strip():
        raise NameRefused("The name has nothing after the prefix.")
    if _CONTROL.search(base):
        raise NameRefused("The name contains a control character (NUL, TAB, CR or LF).")
    forged = foreign(identity, prefix + base) if prefix else None
    if forged is not None:
        # Initials only: "AB-" + "bot-x" reads as agent bot's name.
        raise NameRefused(
            f"the name would be {prefix + base!r}, which reads as "
            f"{forged.owner}-{forged.agent}-, {_whose(forged)} prefix. A base must not "
            "start with a lowercase agent-like word and a hyphen here; reorder it or "
            "use '_' instead of that '-'."
        )
    out = Stamped(requested=requested, final=prefix + base, base=base, prefix=prefix,
                  rule=rule, entity_type=entity_type)
    if rule == "no_identity":
        out.rule = _RULE_TEXT
    for problem in identity.problems:
        out.problems.append(("agent_identity", problem))
    limit = out.max_length
    if limit is not None and out.length > limit:
        room = limit - length_of(prefix, entity_type)
        out.problems.append((
            "name_length",
            f"{out.length} {NAME_LIMITS[entity_type][1]} against {limit}: the base may be "
            f"at most {room} {NAME_LIMITS[entity_type][1]} after the {len(prefix)}-character "
            f"prefix; it is {length_of(base, entity_type)}. Shorten it; names are never "
            "truncated.",
        ))
    return out


def rename(identity: Identity, current: str, requested: str, *, entity_type: str,
           ledger_owned: bool) -> Stamped:
    """The final name for renaming an existing entity -- only OUR entities.

    Ours means its live name carries our prefix, or the ledger shows this
    server created it. Anything else -- a person's entity, another agent's --
    comes back with an ``own_entity`` problem: the agent renames only its own.
    Ours keeps (or, if a person removed it in the UI, gets back) our prefix,
    and only the base can change.
    """
    has_prefix = owns(identity, current)
    # A live name that parses as a DIFFERENT full identity is that identity's
    # now, whatever the ledger says: renaming it would strip their marker.
    ledger_owned = ledger_owned and foreign(identity, current) is None
    out = stamp(identity, requested, entity_type=entity_type)
    if has_prefix:
        out.rule = "kept_prefix" if out.rule != _RULE_TEXT else out.rule
    elif ledger_owned and identity.prefix:
        out.rule = "restored_prefix"
    if not (has_prefix or ledger_owned):
        parsed = parse(current)
        whose = (f"{_whose(parsed)} entity ({parsed.owner}-{parsed.agent}-)"
                 if parsed is not None
                 else "a manual entity: it has no prefix of ours and the ledger shows "
                      "no creation of it by this server")
        out.problems.append((
            "own_entity",
            f"{whose}. The agent renames only entities it created; rename it in the "
            "Google Ads UI instead.",
        ))
    return out


def cli_identity() -> Identity:
    """What the CLI stamps: ``<OWNER>-cli-`` from .env, or nothing."""
    return load_identity().cli()


def stamp_for_cli(identity: Identity, requested: str, *, entity_type: str) -> Stamped:
    """:func:`stamp` for a human at the CLI: any problem is a refusal, at once."""
    stamped = stamp(identity, requested, entity_type=entity_type)
    if stamped.problems:
        raise NameRefused("; ".join(detail for _, detail in stamped.problems))
    return stamped
