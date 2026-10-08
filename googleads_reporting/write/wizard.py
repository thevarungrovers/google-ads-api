"""Interactive prompts that build a :class:`~.spec.CampaignSpec`.

This is a frontend, not the only way in. It asks questions and returns a spec;
everything that validates, builds and sends is reachable without it. That
matters for Phase 3, where an agent has no terminal to answer a prompt but can
construct the same spec directly and reuse every check.

Answering is never assumed: EOF or an interrupt aborts rather than accepting a
default, because a wizard that treats "no answer" as "yes" is a wizard that
creates a campaign nobody asked for.
"""

from __future__ import annotations

from pathlib import Path

from .client import MutationError
from .spec import (
    CREATABLE_CHANNELS,
    AdGroupSpec,
    CampaignSpec,
    ResponsiveDisplayAdSpec,
    ResponsiveSearchAdSpec,
)


class Aborted(MutationError):
    """The operator stopped the wizard."""


def _ask(prompt: str, *, default: str | None = None, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Aborted("\nAborted. Nothing was created.") from None
        if not answer and default is not None:
            return default
        if answer or not required:
            return answer
        print("  (required)")


def _ask_float(prompt: str, *, default: float | None = None) -> float:
    while True:
        raw = _ask(prompt, default=str(default) if default is not None else None)
        try:
            return float(raw)
        except ValueError:
            print(f"  '{raw}' is not a number. Amounts are in your account's "
                  "currency, e.g. 25 or 2.50 -- not micros.")


def _ask_choice(prompt: str, options: tuple[str, ...]) -> str:
    rendered = "/".join(options)
    while True:
        answer = _ask(f"{prompt} ({rendered})", default=options[0]).upper()
        if answer in options:
            return answer
        print(f"  choose one of: {rendered}")


def _ask_list(label: str, *, least: int, most: int, limit: int) -> list[str]:
    print(f"  {label}: {least}-{most} of them, max {limit} characters each. "
          "Blank line to finish.")
    values: list[str] = []
    while len(values) < most:
        value = _ask(f"    {label[:-1] if label.endswith('s') else label} "
                     f"{len(values) + 1}", required=False)
        if not value:
            if len(values) >= least:
                return values
            print(f"    (need at least {least})")
            continue
        if len(value) > limit:
            print(f"    too long: {len(value)} chars, max {limit}")
            continue
        values.append(value)
    return values


def normalize_path_input(raw: str) -> Path:
    """Turn whatever the terminal handed us into a usable path.

    The two obvious ways to enter a path on macOS both produce something
    ``Path()`` cannot open:

    * dragging a file into the terminal escapes spaces -- ``/tmp/my\\ hero.png``
    * "Copy as Pathname" wraps it in quotes -- ``'/tmp/my hero.png'``

    Refusing those would mean "no such file" for a file that is plainly there,
    so both are unwound here, along with ``~``.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    # Shell-style escapes, from a drag-and-drop.
    for escaped, plain in (("\\ ", " "), ("\\(", "("), ("\\)", ")"),
                           ("\\&", "&"), ("\\'", "'")):
        text = text.replace(escaped, plain)
    return Path(text).expanduser()


def _ask_images(label: str, *, required: bool) -> list[Path]:
    note = "required" if required else "optional"
    print(f"  {label} ({note}). One path per line, blank to finish.")
    paths: list[Path] = []
    while True:
        raw = _ask("    path", required=False)
        if not raw:
            if required and not paths:
                print("    (at least one is required)")
                continue
            return paths
        path = normalize_path_input(raw)
        if not path.is_file():
            print(f"    no such file: {path}")
            continue
        paths.append(path)


def ask_for_campaign() -> CampaignSpec:
    """Prompt for everything a new campaign needs."""
    print("\nNew campaign\n" + "-" * 60)
    channel = _ask_choice("Channel", CREATABLE_CHANNELS)
    name = _ask("Campaign name")
    budget = _ask_float("Daily budget (account currency, not micros)")
    status = _ask_choice("Start as", ("PAUSED", "ENABLED"))
    if status == "ENABLED":
        print("  ! ENABLED means it can start spending as soon as it is created.")

    political = _ask_choice(
        "Does this campaign contain EU political advertising? Google requires "
        "a declaration", ("NO", "YES")
    )

    print("\nAd group\n" + "-" * 60)
    group_name = _ask("Ad group name")
    bid = _ask_float("Max CPC bid", default=0.50)

    keywords: list[str] = []
    if channel == "SEARCH":
        print("  Keywords (phrase match). Blank line to finish.")
        while True:
            keyword = _ask("    keyword", required=False)
            if not keyword:
                break
            keywords.append(keyword)

    print("\nAd\n" + "-" * 60)
    final_url = _ask("Landing page URL")

    if channel == "SEARCH":
        ad = ResponsiveSearchAdSpec(
            headlines=_ask_list("headlines", least=3, most=15, limit=30),
            descriptions=_ask_list("descriptions", least=2, most=4, limit=90),
            final_url=final_url,
        )
    else:
        business = _ask("Business name (max 25 chars)")
        long_headline = _ask("Long headline (max 90 chars)")
        headlines = _ask_list("headlines", least=1, most=5, limit=30)
        descriptions = _ask_list("descriptions", least=1, most=5, limit=90)
        print("\n  Images. Google needs both shapes; the logo slots are optional.")
        ad = ResponsiveDisplayAdSpec(
            business_name=business,
            long_headline=long_headline,
            headlines=headlines,
            descriptions=descriptions,
            final_url=final_url,
            marketing_images=_ask_images(
                "Marketing images, 1.91:1 (e.g. 1200x628)", required=True
            ),
            square_marketing_images=_ask_images(
                "Square marketing images, 1:1 (e.g. 1200x1200)", required=True
            ),
            logo_images=_ask_images(
                "Logos, 4:1 (e.g. 1200x300)", required=False
            ),
            square_logo_images=_ask_images(
                "Square logos, 1:1 (e.g. 1200x1200)", required=False
            ),
        )

    return CampaignSpec(
        name=name,
        channel=channel,
        budget_amount=budget,
        status=status,
        contains_eu_political_advertising=(political == "YES"),
        ad_groups=[AdGroupSpec(
            name=group_name, cpc_bid=bid, keywords=keywords, ads=[ad]
        )],
    )
