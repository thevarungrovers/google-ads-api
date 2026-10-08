"""Reading and checking image files before they are uploaded.

Dimensions are parsed straight out of the file header rather than through an
imaging library. Width, height and format are all that is needed, the three
formats Google accepts have well-defined headers, and a new dependency to read
eight bytes is not a good trade.

Checking locally matters because the server-side error for a wrong image is
``ASSET_IMAGE_ASPECT_RATIO_NOT_ALLOWED`` against an asset index -- true, but it
does not tell you that hero.jpg is 1200x600 when it needed to be 1.91:1.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .client import MutationError

#: Google's limit for an image asset.
MAX_FILE_BYTES = 5_120 * 1024

#: Google deduplicates image assets by CONTENT hash, not by name: uploading
#: the same bytes twice in one request fails with asset_error=DUPLICATE_ASSET.
#: Two genuinely different images never collide; two copies of one file do.
DEDUPLICATED_BY_CONTENT = True

MIME_TYPES = {
    "JPEG": "IMAGE_JPEG",
    "PNG": "IMAGE_PNG",
    "GIF": "IMAGE_GIF",
}

#: Aspect ratios are never exactly achievable at integer pixel sizes, so a
#: small tolerance is required. 1200x628 is 1.9108, not 1.91.
RATIO_TOLERANCE = 0.02


@dataclass(frozen=True)
class ImageFile:
    path: Path
    width: int
    height: int
    fmt: str
    data: bytes

    @property
    def ratio(self) -> float:
        return self.width / self.height

    @property
    def mime_type(self) -> str:
        return MIME_TYPES[self.fmt]

    def describe(self) -> str:
        return (
            f"{self.path.name} ({self.width}x{self.height}, "
            f"{self.ratio:.2f}:1, {len(self.data) / 1024:.0f} KB, {self.fmt})"
        )


@dataclass(frozen=True)
class ImageRequirement:
    """What a particular image slot demands."""

    label: str
    ratio: float
    min_width: int
    min_height: int

    def check(self, image: ImageFile) -> list[str]:
        problems = []
        if abs(image.ratio - self.ratio) > RATIO_TOLERANCE:
            problems.append(
                f"{image.path.name} is {image.ratio:.2f}:1, "
                f"needs {self.ratio:.2f}:1 "
                f"({image.width}x{image.height})"
            )
        if image.width < self.min_width or image.height < self.min_height:
            problems.append(
                f"{image.path.name} is {image.width}x{image.height}, "
                f"minimum is {self.min_width}x{self.min_height}"
            )
        return problems


#: Requirements per slot of a Responsive Display Ad.
#:
#: The two logo slots are the trap. A Responsive Display Ad has BOTH
#: ``logo_images`` and ``square_logo_images``, and despite the plain name it is
#: ``logo_images`` that wants the LANDSCAPE 4:1 shape -- the square one belongs
#: in ``square_logo_images``. Established against the live API, which accepted
#: 512x128 and 1200x300 in ``logo_images`` and rejected 600x600 there with
#: ASPECT_RATIO_NOT_ALLOWED.
LANDSCAPE = ImageRequirement("marketing image", 1.91, 600, 314)
SQUARE = ImageRequirement("square marketing image", 1.0, 300, 300)
LOGO = ImageRequirement("logo (logo_images)", 4.0, 512, 128)
SQUARE_LOGO = ImageRequirement("square logo (square_logo_images)", 1.0, 128, 128)


def _png_size(data: bytes) -> tuple[int, int]:
    # IHDR is always the first chunk: 8-byte signature, 4 length, 4 "IHDR".
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_size(data: bytes) -> tuple[int, int]:
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    # Walk the segment chain to a Start-Of-Frame marker, which carries the
    # dimensions. They are NOT at a fixed offset: the number and size of
    # preceding segments (EXIF, ICC profiles, comments) vary per file.
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # SOF0-SOF15, excluding DHT (c4), JPG (c8) and DAC (cc).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        index += 2 + length
    raise MutationError("Could not read the dimensions of this JPEG.")


def load_image(path: str | Path) -> ImageFile:
    """Read an image and its dimensions, or say precisely what is wrong."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise MutationError(f"No such image file: {path}")

    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise MutationError(
            f"{path.name} is {len(data) / 1024:,.0f} KB; the limit is "
            f"{MAX_FILE_BYTES / 1024:,.0f} KB."
        )
    if not data:
        raise MutationError(f"{path.name} is empty.")

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        fmt, (width, height) = "PNG", _png_size(data)
    elif data[:3] == b"GIF":
        fmt, (width, height) = "GIF", _gif_size(data)
    elif data[:2] == b"\xff\xd8":
        fmt, (width, height) = "JPEG", _jpeg_size(data)
    else:
        raise MutationError(
            f"{path.name} is not a JPEG, PNG or GIF. Google accepts only those "
            "for image assets (the extension is not what is checked -- the "
            "file's own header is)."
        )

    if width <= 0 or height <= 0:
        raise MutationError(f"{path.name} reports a {width}x{height} size.")
    return ImageFile(path=path, width=width, height=height, fmt=fmt, data=data)


def check(images: list[ImageFile], requirement: ImageRequirement) -> None:
    """Raise if any image fails ``requirement``, naming every failure."""
    problems = [p for image in images for p in requirement.check(image)]
    if problems:
        raise MutationError(
            f"{requirement.label} problems:\n  - " + "\n  - ".join(problems)
        )
