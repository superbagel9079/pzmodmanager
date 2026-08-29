"""Mod artwork: finding it, and drawing it in a terminal.

Every mod ships a poster next to its mod.info, and the Workshop serves a preview
image for each item. Two different pictures, useful in two different places:

  * the HTML report links the Workshop preview, which keeps the report small;
  * the interactive interface draws the local poster, because a terminal cannot
    fetch a URL and because the local file is there even with no network.

Drawing an image in a terminal here means half blocks: the upper half block
character takes a foreground colour for the top pixel and a background colour for
the bottom one, so one cell shows two pixels. It works in any terminal with true
colour, which includes Windows Terminal, rather than needing Sixel or the Kitty
graphics protocol that most terminals lack.

Pillow is optional. Without it the interface simply shows no picture, and says
so, rather than failing.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Names to try when mod.info does not name a poster, or names one that is gone.
FALLBACK_NAMES = ["poster.png", "poster.jpg", "icon.png", "preview.png", "thumb.png"]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif"}

UPPER_HALF = "▀"


def _pillow():
    """Import Pillow on demand, returning None when it is not installed."""
    try:
        from PIL import Image  # noqa: PLC0415

        return Image
    except ImportError:
        return None


def pillow_available() -> bool:
    return _pillow() is not None


def find_poster(mod) -> Path | None:
    """Locate a mod's poster on disk.

    Looks where mod.info was read, which for a Build 42 mod is inside the version
    branch, then falls back to the mod folder and to the usual file names.
    """
    search_dirs: list[Path] = []
    if getattr(mod, "info_path", None):
        search_dirs.append(Path(mod.info_path).parent)
    search_dirs.append(Path(mod.root))

    declared = (getattr(mod, "poster", "") or "").strip()
    for directory in search_dirs:
        if declared:
            candidate = directory / declared
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES:
                return candidate
        for name in FALLBACK_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def poster_blocks(path: Path | None, width: int = 30, max_rows: int = 14):
    """Render an image as Rich Text using half blocks, two pixels per cell.

    Returns None when there is no image or Pillow is missing, so the caller can
    show something else.
    """
    if path is None:
        return None
    Image = _pillow()
    if Image is None:
        return None

    from rich.text import Text

    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            # Two pixels stack in one cell, and cells are about twice as tall as
            # they are wide, so the pixel grid is square-ish at width x height.
            height = max(2, min(max_rows * 2, round(width * image.height / image.width)))
            if height % 2:
                height += 1
            image = image.resize((width, height))
            pixels = image.load()
            text = Text()
            for row in range(0, height, 2):
                for column in range(width):
                    top = pixels[column, row]
                    bottom = pixels[column, row + 1]
                    text.append(
                        UPPER_HALF,
                        style=f"#{top[0]:02x}{top[1]:02x}{top[2]:02x} "
                        f"on #{bottom[0]:02x}{bottom[1]:02x}{bottom[2]:02x}",
                    )
                text.append("\n")
            return text
    except Exception as exc:  # a broken image must not take the interface down
        log.warning("Could not render the poster %s: %s", path, exc)
        return None


def embed_poster(path: Path | None, size: int = 96) -> str | None:
    """Downscale an image and return it as a data URL for the HTML report.

    Used only when the report is asked to be fully self-contained. A hundred and
    forty full size posters would make the report tens of megabytes, so they are
    shrunk first.
    """
    if path is None:
        return None
    Image = _pillow()
    if Image is None:
        return None

    import base64
    import io

    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((size, size))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=70)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception as exc:
        log.warning("Could not embed the poster %s: %s", path, exc)
        return None
