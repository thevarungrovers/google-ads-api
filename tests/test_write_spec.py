"""Spec and media tests. No API calls, no network."""

import random
import struct
import zlib
from pathlib import Path

import pytest

from googleads_reporting.write.client import MutationError
from googleads_reporting.write.media import (
    LANDSCAPE,
    LOGO,
    MAX_FILE_BYTES,
    SQUARE,
    SQUARE_LOGO,
    load_image,
)
from googleads_reporting.write.spec import (
    CREATABLE_CHANNELS,
    AdGroupSpec,
    CampaignSpec,
    ResponsiveDisplayAdSpec,
    ResponsiveSearchAdSpec,
    VideoResponsiveAdSpec,
)


def png(path: Path, w: int, h: int, seed: str = "x") -> Path:
    rnd = random.Random(seed)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(
        b"\x00" + bytes(rnd.randrange(256) for _ in range(w * 3)) for _ in range(h)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


# --------------------------------------------------------------------------
# Image reading
# --------------------------------------------------------------------------


@pytest.mark.parametrize("w,h", [(1200, 628), (300, 300), (512, 128), (1, 1)])
def test_png_dimensions_are_read(tmp_path, w, h):
    image = load_image(png(tmp_path / "i.png", w, h))
    assert (image.width, image.height) == (w, h)
    assert image.fmt == "PNG"
    assert image.mime_type == "IMAGE_PNG"


def test_gif_dimensions_are_read(tmp_path):
    path = tmp_path / "i.gif"
    path.write_bytes(b"GIF89a" + struct.pack("<HH", 300, 250) + b"\x00" * 7 + b";")
    image = load_image(path)
    assert (image.width, image.height) == (300, 250)


def test_jpeg_dimensions_survive_a_variable_length_preamble(tmp_path):
    """JPEG size is not at a fixed offset -- EXIF and ICC segments move it."""
    path = tmp_path / "i.jpg"
    app0 = b"\xff\xe0" + struct.pack(">H", 60) + b"JFIF\x00" + b"\x00" * 53
    app1 = b"\xff\xe1" + struct.pack(">H", 200) + b"Exif\x00\x00" + b"\x00" * 192
    sof0 = (
        b"\xff\xc0" + struct.pack(">H", 11) + b"\x08"
        + struct.pack(">HH", 628, 1200) + b"\x01\x01\x11\x00"
    )
    path.write_bytes(b"\xff\xd8" + app0 + app1 + sof0 + b"\xff\xd9")
    image = load_image(path)
    assert (image.width, image.height) == (1200, 628)


def test_a_non_image_is_refused_by_its_header_not_its_name(tmp_path):
    path = tmp_path / "lies.png"
    path.write_bytes(b"this is not an image")
    with pytest.raises(MutationError, match="not a JPEG, PNG or GIF"):
        load_image(path)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(MutationError, match="No such image file"):
        load_image(tmp_path / "absent.png")


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    with pytest.raises(MutationError, match="empty"):
        load_image(path)


def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    import googleads_reporting.write.media as media

    monkeypatch.setattr(media, "MAX_FILE_BYTES", 100)
    with pytest.raises(MutationError, match="the limit is"):
        load_image(png(tmp_path / "big.png", 200, 200))


# --------------------------------------------------------------------------
# Image requirements -- the logo slots are the trap
# --------------------------------------------------------------------------


def test_logo_images_wants_four_to_one_not_square(tmp_path):
    """Established live: logo_images rejected 600x600, accepted 1200x300."""
    assert LOGO.ratio == 4.0
    square = load_image(png(tmp_path / "sq.png", 600, 600))
    assert LOGO.check(square), "a 1:1 logo must not pass the 4:1 slot"
    wide = load_image(png(tmp_path / "wide.png", 1200, 300))
    assert LOGO.check(wide) == []


def test_square_logo_images_wants_one_to_one(tmp_path):
    assert SQUARE_LOGO.ratio == 1.0
    assert SQUARE_LOGO.check(load_image(png(tmp_path / "s.png", 300, 300))) == []
    assert SQUARE_LOGO.check(load_image(png(tmp_path / "w.png", 1200, 300)))


def test_the_marketing_slots_differ(tmp_path):
    hero = load_image(png(tmp_path / "h.png", 1200, 628))
    assert LANDSCAPE.check(hero) == []
    assert SQUARE.check(hero), "1.91:1 must not pass the 1:1 slot"


def test_an_undersized_image_names_the_minimum(tmp_path):
    small = load_image(png(tmp_path / "s.png", 100, 52))
    problems = " ".join(LANDSCAPE.check(small))
    assert "minimum is 600x314" in problems


# --------------------------------------------------------------------------
# Spec validation
# --------------------------------------------------------------------------


def _rsa():
    return ResponsiveSearchAdSpec(
        headlines=["a", "b", "c"], descriptions=["x", "y"],
        final_url="https://example.com",
    )


def test_a_valid_search_campaign_has_no_problems():
    spec = CampaignSpec(
        name="C", channel="SEARCH", budget_amount=5.0,
        ad_groups=[AdGroupSpec(name="G", ads=[_rsa()])],
    )
    assert spec.problems() == []


def test_video_campaign_creation_is_refused_with_the_reason():
    """Google refuses every video sub-type; say so instead of relaying a code."""
    assert "VIDEO" not in CREATABLE_CHANNELS
    spec = CampaignSpec(
        name="C", channel="VIDEO", budget_amount=5.0,
        ad_groups=[AdGroupSpec(name="G", ads=[
            VideoResponsiveAdSpec(
                youtube_video_ids=["dQw4w9WgXcQ"], headlines=["h"],
                long_headlines=["l"], descriptions=["d"], business_name="B",
                final_url="https://example.com",
            )])],
    )
    problems = " ".join(spec.problems())
    assert "cannot be created through the Google Ads API" in problems
    assert "Google Ads UI" in problems


def test_an_ad_format_must_match_the_channel():
    spec = CampaignSpec(
        name="C", channel="DISPLAY", budget_amount=5.0,
        ad_groups=[AdGroupSpec(name="G", ads=[_rsa()])],
    )
    assert any("needs a SEARCH campaign" in p for p in spec.problems())


def test_keywords_outside_search_are_refused():
    ad = ResponsiveDisplayAdSpec(
        business_name="B", long_headline="L", headlines=["h"], descriptions=["d"],
        final_url="https://example.com",
        marketing_images=[Path("a")], square_marketing_images=[Path("b")],
    )
    spec = CampaignSpec(
        name="C", channel="DISPLAY", budget_amount=5.0,
        ad_groups=[AdGroupSpec(name="G", ads=[ad], keywords=["x"])],
    )
    assert any("only apply to SEARCH" in p for p in spec.problems())


def test_a_display_ad_needs_both_image_shapes():
    ad = ResponsiveDisplayAdSpec(
        business_name="B", long_headline="L", headlines=["h"], descriptions=["d"],
        final_url="https://example.com", marketing_images=[Path("a")],
    )
    problems = " ".join(ad.problems())
    assert "1:1 square marketing image is required" in problems


def test_a_youtube_url_pasted_instead_of_an_id_is_caught():
    ad = VideoResponsiveAdSpec(
        youtube_video_ids=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
        headlines=["h"], long_headlines=["l"], descriptions=["d"],
        business_name="B", final_url="https://example.com",
    )
    assert any("looks like a URL" in p for p in ad.problems())


def test_a_wrong_length_video_id_is_caught():
    ad = VideoResponsiveAdSpec(
        youtube_video_ids=["abc"], headlines=["h"], long_headlines=["l"],
        descriptions=["d"], business_name="B", final_url="https://example.com",
    )
    assert any("is 3 chars" in p for p in ad.problems())


def test_a_campaign_with_no_ad_groups_is_refused():
    spec = CampaignSpec(name="C", channel="SEARCH", budget_amount=5.0)
    assert any("at least one ad group" in p for p in spec.problems())


def test_a_zero_budget_is_refused():
    spec = CampaignSpec(
        name="C", channel="SEARCH", budget_amount=0.0,
        ad_groups=[AdGroupSpec(name="G", ads=[_rsa()])],
    )
    assert any("budget must be positive" in p for p in spec.problems())


def test_validate_reports_every_problem_at_once():
    spec = CampaignSpec(name="", channel="NOPE", budget_amount=-1)
    with pytest.raises(MutationError) as exc:
        spec.validate()
    message = str(exc.value)
    assert "campaign name is required" in message
    assert "budget must be positive" in message
    assert "at least one ad group" in message


def test_the_eu_declaration_defaults_to_not_containing():
    spec = CampaignSpec(name="C", channel="SEARCH", budget_amount=5.0)
    assert spec.contains_eu_political_advertising is False


def test_image_paths_collects_every_slot(tmp_path):
    ad = ResponsiveDisplayAdSpec(
        business_name="B", long_headline="L", headlines=["h"], descriptions=["d"],
        final_url="https://example.com",
        marketing_images=[Path("a")], square_marketing_images=[Path("b")],
        logo_images=[Path("c")], square_logo_images=[Path("d")],
    )
    spec = CampaignSpec(
        name="C", channel="DISPLAY", budget_amount=5.0,
        ad_groups=[AdGroupSpec(name="G", ads=[ad])],
    )
    assert len(spec.image_paths()) == 4


# --------------------------------------------------------------------------
# Path entry: what a terminal actually hands you
# --------------------------------------------------------------------------


def test_a_dragged_path_with_escaped_spaces_resolves(tmp_path):
    """Dragging a file into Terminal escapes spaces; Path() cannot open that."""
    from googleads_reporting.write.wizard import normalize_path_input

    target = png(tmp_path / "my hero.png", 10, 10)
    assert normalize_path_input(str(target).replace(" ", "\\ ")) == target
    assert normalize_path_input(str(target).replace(" ", "\\ ")).is_file()


@pytest.mark.parametrize("wrap", ["'{}'", '"{}"', "  {}  "])
def test_a_quoted_or_padded_path_resolves(tmp_path, wrap):
    """'Copy as Pathname' wraps in quotes."""
    from googleads_reporting.write.wizard import normalize_path_input

    target = png(tmp_path / "hero.png", 10, 10)
    assert normalize_path_input(wrap.format(target)) == target


def test_a_tilde_path_is_expanded():
    from googleads_reporting.write.wizard import normalize_path_input

    assert str(normalize_path_input("~/x.png")).startswith("/")


def test_a_plain_path_is_untouched(tmp_path):
    from googleads_reporting.write.wizard import normalize_path_input

    target = png(tmp_path / "plain.png", 10, 10)
    assert normalize_path_input(str(target)) == target
