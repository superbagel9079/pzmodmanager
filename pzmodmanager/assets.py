"""Indexing the files shipped by each mod.

The engine loads mods in order and stacks their media/ folders. Two mods shipping
the same relative path collide: the one loaded last wins. This index is therefore
the raw material for conflict detection.

Build 41 layout:   <mod>/media/lua/client/...
Build 42 layout:   <mod>/common/media/...  plus one folder per game version,
                   <mod>/42.19/media/... , of which the game loads exactly one.

Only the branch the game would actually load is indexed. Indexing every version
folder would make a mod collide with its own older releases, which is noise.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .builds import COMMON_DIR
from .models import Mod
from .scripts import parse_script_file

log = logging.getLogger(__name__)

# Extensions the engine never loads.
IGNORED_SUFFIXES = {".md", ".txt~", ".bak", ".psd", ".xcf", ".zip", ".7z", ".rar"}
IGNORED_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}


def media_roots_for(mod: Mod, build: str) -> list[tuple[str, Path]]:
    """Return the (branch, media folder) pairs the game would actually load.

    A versioned mod loads common/ plus exactly one version branch, the one
    discovery already selected. A flat mod loads its own media/, and may also
    carry common/ and version folders alongside it.
    """
    roots: list[tuple[str, Path]] = []

    common = mod.root / COMMON_DIR / "media"
    if common.is_dir():
        roots.append((COMMON_DIR, common))

    if mod.layout == "versioned":
        if mod.branch:
            branch_media = mod.root / mod.branch / "media"
            if branch_media.is_dir():
                roots.append((mod.branch, branch_media))
        return roots

    legacy = mod.root / "media"
    if legacy.is_dir():
        roots.append(("media", legacy))
    versioned = mod.root / build / "media"
    if versioned.is_dir():
        roots.append((build, versioned))
    return roots


def _relevant(path: Path) -> bool:
    if path.name.lower() in IGNORED_NAMES:
        return False
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return False
    return True


def index_mod(mod: Mod, build: str = "42", parse_scripts: bool = True) -> None:
    """Fill mod.assets and mod.script_objects. Modifies the mod in place."""
    pairs = media_roots_for(mod, build)
    mod.build_targets = [branch for branch, _ in pairs]
    mod.media_roots = [path for _, path in pairs]
    mod.assets = {}
    mod.script_objects = []

    for _branch, media_root in pairs:
        for path in media_root.rglob("*"):
            if not path.is_file() or not _relevant(path):
                continue
            try:
                rel = path.relative_to(media_root).as_posix()
            except ValueError:
                continue
            # Normalised key: Windows is case-insensitive, Linux is not.
            # Comparing in lower case catches both.
            mod.assets.setdefault(rel.lower(), []).append(path)

            if parse_scripts and rel.lower().startswith("scripts/") and path.suffix.lower() == ".txt":
                try:
                    mod.script_objects.extend(parse_script_file(path, rel))
                except Exception as exc:  # a malformed script must not break everything
                    log.warning("Unreadable script %s in %s: %s", rel, mod.mod_id, exc)
                    mod.parse_errors.append(f"unreadable script: {rel} ({exc})")


def index_all(
    mods: list[Mod],
    build: str = "42",
    parse_scripts: bool = True,
    progress=None,
) -> int:
    """Index every mod. Returns the total number of files indexed."""
    total = 0
    for position, mod in enumerate(mods, start=1):
        try:
            index_mod(mod, build=build, parse_scripts=parse_scripts)
            total += len(mod.assets)
        except Exception as exc:
            log.exception("Indexing failed for %s", mod.mod_id)
            mod.parse_errors.append(f"indexing failed: {exc}")
        if progress and (position % 10 == 0 or position == len(mods)):
            progress(f"Indexing mod files... {position}/{len(mods)}")
    log.info("Indexing finished: %d file(s) across %d mod(s)", total, len(mods))
    return total


def classify(rel_path: str) -> str:
    """Sort a relative path into a category, used to weight severity."""
    p = rel_path.lower()
    if p.startswith("lua/shared/translate/"):
        return "translation"
    if p.startswith("lua/client/"):
        return "lua_client"
    if p.startswith("lua/server/"):
        return "lua_server"
    if p.startswith("lua/shared/"):
        return "lua_shared"
    if p.startswith("lua/"):
        return "lua_other"
    if p.startswith("scripts/"):
        return "script"
    if p.startswith("models") or p.endswith((".x", ".fbx")):
        return "model"
    if p.startswith("sound") or p.endswith((".ogg", ".wav")):
        return "sound"
    if p.startswith("ui/") or p.endswith((".png", ".jpg", ".tga")):
        return "texture"
    if p.startswith("maps/"):
        return "map"
    return "other"
