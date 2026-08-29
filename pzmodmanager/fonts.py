"""Embedding a custom font in the HTML report.

The report is a single self-contained file, so a font has to travel inside it as
a base64 data URL rather than as a link to a file that may not be there when the
report is opened somewhere else.

Where the font is applied is a deliberate choice. Pixter Granular is a display
face: lovely on a title, hard work on nine hundred lines of findings. So it is
used on the headings, the severity tags and the big numbers, and the body text
stays in a plain interface font. The report is meant to be read.

Nothing is embedded unless a font file is actually supplied or found, and the CSS
still names the font first in the stack, so a viewer who has it installed gets it
either way.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

log = logging.getLogger(__name__)

FONT_FAMILY = "Pixter Granular"
EMBEDDED_NAME = "PZModcheckDisplay"

# Names checked when no --font was given.
DEFAULT_FILENAMES = [
    "pixter-granular.ttf",
    "PixterGranular.ttf",
    "Pixter Granular.ttf",
    "pixter_granular.ttf",
]

MIME_BY_SUFFIX = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

# A font much bigger than this bloats every report for little gain.
MAX_FONT_BYTES = 2_000_000


def find_font(explicit: Path | None = None) -> Path | None:
    """Locate a font file: the one given, or a known name next to the tool."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        log.warning("Font file not found: %s", path)
        return None

    search_dirs = [Path.cwd(), Path(__file__).resolve().parent.parent]
    for directory in search_dirs:
        for name in DEFAULT_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                log.info("Using the font found at %s", candidate)
                return candidate
    return None


def font_face_css(path: Path | None) -> str:
    """Return an @font-face block embedding the font, or an empty string."""
    if path is None:
        return ""
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        log.warning("Unsupported font format %s, ignoring %s", suffix, path)
        return ""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("Could not read the font %s: %s", path, exc)
        return ""
    if len(raw) > MAX_FONT_BYTES:
        log.warning(
            "Font %s is %d bytes, over the %d byte limit, ignoring it",
            path,
            len(raw),
            MAX_FONT_BYTES,
        )
        return ""
    encoded = base64.b64encode(raw).decode("ascii")
    log.info("Embedding font %s (%d bytes)", path, len(raw))
    return (
        f"@font-face{{font-family:'{EMBEDDED_NAME}';"
        f"src:url(data:{mime};base64,{encoded}) format('{suffix.lstrip('.')}');"
        "font-display:swap;}"
    )


def display_stack() -> str:
    """The font stack used for headings, tags and figures."""
    return (
        f"'{EMBEDDED_NAME}', '{FONT_FAMILY}', 'Silkscreen', 'Press Start 2P', "
        "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    )
