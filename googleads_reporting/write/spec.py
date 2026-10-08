"""What a new campaign is, as data.

The interactive prompts build one of these; so can any caller. Keeping the
description separate from both the asking and the sending means the wizard is a
frontend rather than the only way in -- Phase 3's agent layer can construct a
:class:`CampaignSpec` directly and reuse every check below without going near a
terminal prompt.

Nothing here touches the API or reads a file. :mod:`.builder` turns a spec into
operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .client import MutationError

#: Channels this tool can CREATE a campaign on.
#:
#: VIDEO is deliberately absent. Google does not permit creating a video
#: campaign through the API: every sub-type was tried against the live account
#: on 2026-10-08 and all were refused --
#:   (none)                        mutate_error=9    not allowed for the resource
#:   VIDEO_NON_SKIPPABLE           mutate_error=9
#:   VIDEO_SEQUENCE                mutate_error=9
#:   VIDEO_ACTION                  context_error=2   not permitted in this context
#:   VIDEO_REACH_TARGET_FREQUENCY  field_error=2     required field missing
#: Those errors name no cause and read like a bug in the caller, so the refusal
#: happens here instead, with the reason attached. Video campaigns are built in
#: the Google Ads UI; VideoResponsiveAdSpec stays because adding a video ad to
#: an EXISTING video campaign is a different operation.
CREATABLE_CHANNELS = ("SEARCH", "DISPLAY")
CHANNELS = ("SEARCH", "DISPLAY", "VIDEO")

# Google's text limits, per format.
RSA_HEADLINE, RSA_DESCRIPTION = 30, 90
RDA_HEADLINE, RDA_LONG_HEADLINE, RDA_DESCRIPTION, RDA_BUSINESS = 30, 90, 90, 25
VIDEO_HEADLINE, VIDEO_LONG_HEADLINE, VIDEO_DESCRIPTION = 30, 90, 90


def _check_texts(
    label: str, values: list[str], *, limit: int, least: int, most: int
) -> list[str]:
    problems = []
    if not (least <= len(values) <= most):
        problems.append(f"need {least}-{most} {label}, got {len(values)}")
    for index, text in enumerate(values, start=1):
        if not text.strip():
            problems.append(f"{label} {index} is blank")
        elif len(text) > limit:
            problems.append(
                f"{label} {index} is {len(text)} chars (max {limit}): {text!r}"
            )
    return problems


def _check_url(url: str) -> list[str]:
    if not url.startswith(("http://", "https://")):
        return [f"final_url must be an absolute http(s) URL, got {url!r}"]
    return []


@dataclass
class ResponsiveSearchAdSpec:
    headlines: list[str]
    descriptions: list[str]
    final_url: str
    path1: str | None = None
    path2: str | None = None
    channel = "SEARCH"
    kind = "responsive_search"

    def problems(self) -> list[str]:
        return (
            _check_texts("headlines", self.headlines, limit=RSA_HEADLINE, least=3, most=15)
            + _check_texts(
                "descriptions", self.descriptions, limit=RSA_DESCRIPTION, least=2, most=4
            )
            + _check_url(self.final_url)
        )


@dataclass
class ResponsiveDisplayAdSpec:
    business_name: str
    long_headline: str
    headlines: list[str]
    descriptions: list[str]
    final_url: str
    marketing_images: list[Path] = field(default_factory=list)
    square_marketing_images: list[Path] = field(default_factory=list)
    #: 4:1 landscape, despite the bare name. See media.LOGO.
    logo_images: list[Path] = field(default_factory=list)
    #: 1:1.
    square_logo_images: list[Path] = field(default_factory=list)
    channel = "DISPLAY"
    kind = "responsive_display"

    def problems(self) -> list[str]:
        problems = (
            _check_texts("headlines", self.headlines, limit=RDA_HEADLINE, least=1, most=5)
            + _check_texts(
                "descriptions", self.descriptions, limit=RDA_DESCRIPTION, least=1, most=5
            )
            + _check_url(self.final_url)
        )
        if len(self.long_headline) > RDA_LONG_HEADLINE:
            problems.append(
                f"long_headline is {len(self.long_headline)} chars "
                f"(max {RDA_LONG_HEADLINE})"
            )
        if not self.long_headline.strip():
            problems.append("long_headline is required")
        if len(self.business_name) > RDA_BUSINESS:
            problems.append(
                f"business_name is {len(self.business_name)} chars "
                f"(max {RDA_BUSINESS})"
            )
        if not self.business_name.strip():
            problems.append("business_name is required")
        # Google requires BOTH aspect ratios; an ad with only one cannot fill
        # every placement and is rejected outright.
        if not self.marketing_images:
            problems.append("at least one 1.91:1 marketing image is required")
        if not self.square_marketing_images:
            problems.append("at least one 1:1 square marketing image is required")
        return problems

    def image_paths(self) -> list[Path]:
        return [
            *self.marketing_images, *self.square_marketing_images,
            *self.logo_images, *self.square_logo_images,
        ]


@dataclass
class VideoResponsiveAdSpec:
    youtube_video_ids: list[str]
    headlines: list[str]
    long_headlines: list[str]
    descriptions: list[str]
    business_name: str
    final_url: str
    call_to_action: str | None = None
    channel = "VIDEO"
    kind = "video_responsive"

    def problems(self) -> list[str]:
        problems = (
            _check_texts("headlines", self.headlines, limit=VIDEO_HEADLINE, least=1, most=5)
            + _check_texts(
                "long_headlines", self.long_headlines,
                limit=VIDEO_LONG_HEADLINE, least=1, most=5,
            )
            + _check_texts(
                "descriptions", self.descriptions,
                limit=VIDEO_DESCRIPTION, least=1, most=5,
            )
            + _check_url(self.final_url)
        )
        if not self.youtube_video_ids:
            problems.append("at least one YouTube video id is required")
        for video_id in self.youtube_video_ids:
            # A YouTube id is 11 characters. A pasted watch URL is the usual
            # mistake, and it fails server-side as an opaque asset error.
            if "/" in video_id or "watch?" in video_id:
                problems.append(
                    f"{video_id!r} looks like a URL; use just the 11-character "
                    "video id (the v= part)"
                )
            elif len(video_id) != 11:
                problems.append(
                    f"{video_id!r} is {len(video_id)} chars; a YouTube video id "
                    "is 11"
                )
        if not self.business_name.strip():
            problems.append("business_name is required")
        return problems


AdSpec = ResponsiveSearchAdSpec | ResponsiveDisplayAdSpec | VideoResponsiveAdSpec


@dataclass
class AdGroupSpec:
    name: str
    ads: list[AdSpec] = field(default_factory=list)
    cpc_bid: float | None = None
    keywords: list[str] = field(default_factory=list)
    status: str = "PAUSED"

    def problems(self, channel: str) -> list[str]:
        problems = []
        if not self.name.strip():
            problems.append("ad group name is required")
        if not self.ads:
            problems.append(f"ad group {self.name!r} has no ads")
        for ad in self.ads:
            if ad.channel != channel:
                problems.append(
                    f"ad group {self.name!r} has a {ad.kind} ad, which needs a "
                    f"{ad.channel} campaign, not {channel}"
                )
            problems.extend(f"{self.name}: {p}" for p in ad.problems())
        if self.keywords and channel != "SEARCH":
            problems.append(
                f"ad group {self.name!r} has keywords, which only apply to "
                f"SEARCH campaigns, not {channel}"
            )
        return problems


@dataclass
class CampaignSpec:
    name: str
    channel: str
    budget_amount: float
    ad_groups: list[AdGroupSpec] = field(default_factory=list)
    budget_name: str | None = None
    status: str = "PAUSED"
    start_date: str | None = None
    end_date: str | None = None
    #: Google REQUIRES every new campaign to declare this; creation is refused
    #: without it. It is a legal declaration under the EU political advertising
    #: rules, so it defaults to "does not contain" but is always shown in the
    #: plan -- a declaration nobody is told they are making is not one.
    contains_eu_political_advertising: bool = False

    def problems(self) -> list[str]:
        problems = []
        if not self.name.strip():
            problems.append("campaign name is required")
        if self.channel == "VIDEO":
            problems.append(
                "video campaigns cannot be created through the Google Ads API "
                "-- every sub-type is refused server-side. Create the campaign "
                "in the Google Ads UI, then add ads to it."
            )
        elif self.channel not in CREATABLE_CHANNELS:
            problems.append(
                f"channel must be one of {', '.join(CREATABLE_CHANNELS)}, "
                f"got {self.channel!r}"
            )
        if self.budget_amount <= 0:
            problems.append(f"budget must be positive, got {self.budget_amount}")
        if not self.ad_groups:
            problems.append("a campaign needs at least one ad group")
        for group in self.ad_groups:
            problems.extend(group.problems(self.channel))
        return problems

    def validate(self) -> None:
        """Raise with every problem at once, rather than one per attempt."""
        problems = self.problems()
        if problems:
            raise MutationError(
                "This campaign cannot be built:\n  - " + "\n  - ".join(problems)
            )

    def image_paths(self) -> list[Path]:
        return [
            path
            for group in self.ad_groups
            for ad in group.ads
            if hasattr(ad, "image_paths")
            for path in ad.image_paths()
        ]
