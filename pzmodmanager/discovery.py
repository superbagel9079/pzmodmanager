"""Locating Project Zomboid mods on the client machine.

Two sources:
  1. The Steam Workshop : <library>/steamapps/workshop/content/108600/<wsid>/mods/<mod>/mod.info
  2. Manual mods        : <Zomboid user folder>/mods/<mod>/mod.info

The user folder is %USERPROFILE%\\Zomboid on Windows, ~/Zomboid on Linux and macOS.
Extra Steam libraries are declared in steamapps/libraryfolders.vdf, which is read
so that mods installed on another drive are not missed.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from .builds import COMMON_DIR, classify_layout, is_version_dir, select_branch
from .models import Mod
from .modinfo import build_mod, read_text_tolerant

log = logging.getLogger(__name__)

PZ_APP_ID = "108600"

# Maximum depth when looking for a mod.info under a given folder.
MAX_SCAN_DEPTH = 6


def default_steam_roots() -> list[Path]:
    """Usual Steam install locations, per operating system."""
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Steam")
        for letter in "CDEFG":
            candidates.append(Path(f"{letter}:/Steam"))
            candidates.append(Path(f"{letter}:/SteamLibrary"))
    elif sys.platform == "darwin":
        candidates.append(home / "Library/Application Support/Steam")
    else:
        candidates += [
            home / ".steam/steam",
            home / ".steam/root",
            home / ".local/share/Steam",
            home / "snap/steam/common/.local/share/Steam",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",
        ]
    found = [c for c in candidates if c.exists()]
    log.debug("Steam root candidates: %s", [str(c) for c in found])
    return found


def parse_library_folders(steam_root: Path) -> list[Path]:
    """Read libraryfolders.vdf to pick up secondary Steam libraries."""
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return []
    try:
        text = read_text_tolerant(vdf)
    except OSError as exc:
        log.warning("Could not read %s: %s", vdf, exc)
        return []
    paths = []
    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        candidate = Path(match.group(1).replace("\\\\", "\\"))
        if candidate.exists():
            paths.append(candidate)
    log.debug("Extra libraries from %s: %s", vdf, [str(p) for p in paths])
    return paths


def default_user_folder() -> Path | None:
    """The game data folder (~/Zomboid), where manual mods live."""
    home = Path.home()
    for candidate in (home / "Zomboid", home / ".zomboid", home / ".cache" / "Zomboid"):
        if candidate.is_dir():
            return candidate
    return None


def workshop_dirs() -> list[Path]:
    """Every .../workshop/content/108600 folder found on this machine."""
    found: list[Path] = []
    seen: set[str] = set()
    roots = list(default_steam_roots())
    for root in list(roots):
        roots.extend(parse_library_folders(root))
    for root in roots:
        content = root / "steamapps" / "workshop" / "content" / PZ_APP_ID
        key = str(content).lower()
        if content.is_dir() and key not in seen:
            seen.add(key)
            found.append(content)
    return found


def _subdirs(path: Path) -> list[Path]:
    try:
        return [entry for entry in path.iterdir() if entry.is_dir()]
    except (OSError, PermissionError) as exc:
        log.debug("Cannot list %s: %s", path, exc)
        return []


def read_mod_folder(
    mod_dir: Path,
    build: str,
    source: str,
    workshop_id: str | None,
) -> Mod | None:
    """Turn one mods/<Name> folder into a single Mod, whatever its layout.

    Build 41 mods keep mod.info at the top. Build 42 mods keep one folder per
    game version, each with its own mod.info, next to a shared common/ folder.
    Both are one mod.
    """
    subdirs = _subdirs(mod_dir)
    names = [d.name for d in subdirs]
    flat_info = mod_dir / "mod.info"
    layout = classify_layout(names, flat_info.is_file())

    if layout == "flat":
        mod = build_mod(flat_info, source=source, workshop_id=workshop_id)
        mod.root = mod_dir
        mod.info_path = flat_info
        mod.layout = "flat"
        mod.available_branches = sorted(n for n in names if is_version_dir(n))
        return mod

    if layout == "versioned":
        branch, warning = select_branch(names, build)
        candidates = []
        if branch:
            candidates.append(mod_dir / branch / "mod.info")
        candidates.append(mod_dir / COMMON_DIR / "mod.info")
        # Fall back to any version folder that does carry a mod.info.
        for name in sorted(names, reverse=True):
            candidates.append(mod_dir / name / "mod.info")

        info = next((c for c in candidates if c.is_file()), None)
        if info is None:
            log.debug("No mod.info anywhere under %s", mod_dir)
            return None

        mod = build_mod(info, source=source, workshop_id=workshop_id)
        mod.root = mod_dir
        mod.info_path = info
        mod.layout = "versioned"
        mod.branch = branch
        mod.branch_warning = warning
        mod.available_branches = sorted(n for n in names if is_version_dir(n))
        if warning:
            log.info("%s: %s", mod_dir.name, warning)
        return mod

    return None


def find_mod_folders(base: Path) -> list[Path]:
    """Find the mods/<Name> folders under a Workshop item or a mods directory.

    Stops as soon as a folder looks like a mod, so version subfolders are never
    mistaken for mods of their own.
    """
    results: list[Path] = []
    base_depth = len(base.parts)

    def looks_like_mod(directory: Path, children: list[Path]) -> bool:
        if (directory / "mod.info").is_file():
            return True
        names = [c.name for c in children]
        if not any(is_version_dir(n) for n in names):
            return False
        # A version folder only counts if it actually holds a mod.
        return any(
            (directory / n / "mod.info").is_file() or (directory / n / "media").is_dir()
            for n in names
            if is_version_dir(n)
        )

    def walk(directory: Path) -> None:
        if len(directory.parts) - base_depth > MAX_SCAN_DEPTH:
            return
        children = _subdirs(directory)
        if looks_like_mod(directory, children):
            results.append(directory)
            return
        for entry in children:
            if entry.name.lower() not in {"media", ".git", "__pycache__"}:
                walk(entry)

    walk(base)
    return results


def discover_workshop_mods(content_dir: Path, build: str = "42") -> list[Mod]:
    """Scan a workshop/content/108600 folder and return the mods found."""
    mods: list[Mod] = []
    try:
        entries = sorted(content_dir.iterdir())
    except (OSError, PermissionError) as exc:
        log.warning("Could not list %s: %s", content_dir, exc)
        return mods
    for item in entries:
        if not item.is_dir():
            continue
        workshop_id = item.name if item.name.isdigit() else None
        for mod_dir in find_mod_folders(item):
            mod = read_mod_folder(mod_dir, build, "workshop", workshop_id)
            if mod:
                mods.append(mod)
    return mods


def discover_local_mods(mods_dir: Path, build: str = "42") -> list[Mod]:
    """Scan a Zomboid/mods folder (manually installed mods)."""
    mods: list[Mod] = []
    if not mods_dir.is_dir():
        return mods
    for mod_dir in find_mod_folders(mods_dir):
        mod = read_mod_folder(mod_dir, build, "local", None)
        if mod:
            mods.append(mod)
    return mods


def discover_all(
    extra_paths: list[Path] | None = None,
    use_defaults: bool = True,
    build: str = "42",
    progress=None,
) -> tuple[list[Mod], list[str]]:
    """Entry point: returns (list of mods, list of scanned paths).

    `progress` is an optional callable taking a single message string; it lets the
    caller show what is happening while the disk is being walked.
    """

    def say(message: str) -> None:
        log.info(message)
        if progress:
            progress(message)

    mods: list[Mod] = []
    scanned: list[str] = []

    if use_defaults:
        say("Searching for the Steam library...")
        contents = workshop_dirs()
        if not contents:
            say("No Steam Workshop folder found in the usual locations.")
        for content in contents:
            say(f"Scanning Workshop folder: {content}")
            scanned.append(str(content))
            found = discover_workshop_mods(content, build)
            say(f"  {len(found)} mod(s) found there")
            mods.extend(found)

        say("Searching for the Zomboid user folder...")
        user_folder = default_user_folder()
        if user_folder:
            say(f"Game folder found: {user_folder}")
            local = user_folder / "mods"
            if local.is_dir():
                say(f"Scanning manual mods: {local}")
                scanned.append(str(local))
                found = discover_local_mods(local, build)
                say(f"  {len(found)} mod(s) found there")
                mods.extend(found)
        else:
            say("No Zomboid user folder found.")

    for path in extra_paths or []:
        path = Path(path).expanduser()
        if not path.exists():
            say(f"Path does not exist, skipped: {path}")
            continue
        say(f"Scanning: {path}")
        scanned.append(str(path))
        name = path.name
        # Work out what kind of folder was handed to us
        if name == PZ_APP_ID or (path / PZ_APP_ID).is_dir():
            content = path if name == PZ_APP_ID else path / PZ_APP_ID
            found = discover_workshop_mods(content, build)
        else:
            # Generic folder: one mod, or a parent holding several. The Workshop
            # id, when there is one, is the long numeric part of the path.
            found = []
            for mod_dir in find_mod_folders(path):
                ws = next(
                    (part for part in mod_dir.parts if part.isdigit() and len(part) >= 6),
                    None,
                )
                mod = read_mod_folder(
                    mod_dir, build, "workshop" if ws else "local", ws
                )
                if mod:
                    found.append(mod)
        say(f"  {len(found)} mod(s) found there")
        mods.extend(found)

    # Deduplicate by real path: one library can be reached through two different
    # paths, typically through a symbolic link.
    unique: dict[str, Mod] = {}
    for mod in mods:
        try:
            key = str(mod.root.resolve()).lower()
        except OSError:
            key = str(mod.root).lower()
        unique.setdefault(key, mod)

    result = list(unique.values())
    log.info("Discovery finished: %d unique mod(s)", len(result))
    return result, scanned
