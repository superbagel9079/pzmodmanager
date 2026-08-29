"""Reading and interpreting mod.info files.

Real-world format: `key=value` lines, with a variable encoding (UTF-8 with or
without BOM, sometimes cp1252 in older mods). Some keys can appear several times
(notably `require`), others carry a comma- or semicolon-separated list. Everything
is therefore kept in `raw` as a list.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Mod

# Keys whose value is a list (comma or semicolon separated)
LIST_KEYS = {"require", "incompatible", "pack", "tiledef", "requires"}

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def read_text_tolerant(path: Path) -> str:
    """Read a text file, trying several encodings before giving up."""
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _split_list(value: str) -> list[str]:
    parts = re.split(r"[;,]", value)
    return [p.strip() for p in parts if p.strip()]


def parse_mod_info(path: Path) -> dict[str, list[str]]:
    """Turn a mod.info into a key -> list-of-values dictionary."""
    data: dict[str, list[str]] = {}
    text = read_text_tolerant(path)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        if key in LIST_KEYS:
            data.setdefault(key, []).extend(_split_list(value))
        else:
            data.setdefault(key, []).append(value)
    return data


def _first(data: dict[str, list[str]], key: str, default: str = "") -> str:
    values = data.get(key)
    return values[0] if values else default


def build_mod(mod_info_path: Path, source: str, workshop_id: str | None) -> Mod:
    """Build a Mod object from the path of its mod.info."""
    root = mod_info_path.parent
    data = parse_mod_info(mod_info_path)

    mod_id = _first(data, "id") or _first(data, "modid") or root.name
    name = _first(data, "name") or mod_id

    requires = list(dict.fromkeys(data.get("require", []) + data.get("requires", [])))

    mod = Mod(
        mod_id=mod_id,
        name=name,
        root=root,
        source=source,
        workshop_id=workshop_id,
        description=_first(data, "description"),
        author=_first(data, "author"),
        poster=_first(data, "poster"),
        mod_version=_first(data, "modversion") or _first(data, "version"),
        requires=requires,
        incompatible=list(dict.fromkeys(data.get("incompatible", []))),
        pack=list(dict.fromkeys(data.get("pack", []))),
        tiledefs=list(dict.fromkeys(data.get("tiledef", []))),
        raw=data,
    )

    if "id" not in data and "modid" not in data:
        mod.parse_errors.append(
            "mod.info has no id= key: the identifier was taken from the folder name"
        )
    return mod
