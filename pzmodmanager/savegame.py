"""The one place that writes to the game.

Everything else in this tool reads. This module rewrites the load order inside a
save, which is where Build 42 actually keeps it:

    Zomboid/Saves/<mode>/<save name>/mods.txt

        VERSION = 1,

        mods
        {
            mod = ZombieBuddy,
            mod = AlicesMultiWearVanilla,
        }

        maps
        {
        }

Verified against a real machine: the sequence in that file is exactly the
sequence the game logs as it loads, mod for mod.

Two rules make this safe enough to offer at all.

**It reorders, it never adds or removes.** The set of mods in a save is part of
that save: dropping one can break a world that has its items in the ground, and
adding one mid-save is a decision no tool should make on someone's behalf. So a
write is refused outright unless the new order contains exactly the same mods as
the file already does, and the refusal names the difference.

**It keeps a copy first.** The original is copied to a timestamped file next to
it before a single byte is written, and restoring is one call. Everything in the
file that is not the mod list, the version line and the maps block, is carried
through untouched rather than regenerated.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MODS_FILE = "mods.txt"
BACKUP_SUFFIX = ".pzmodmanager-backup"

# When the game dies, it copies the save next to itself with this on the end.
# The copy is a corpse: it holds the mod list as it was at the moment of the
# crash, and its files are written after the real save's, so sorting saves by
# date puts the corpse first. That is how a crash folder ends up being read as
# "your current load order", which is exactly backwards.
CRASH_SUFFIX = "_crash"

# The game records which save it last opened, one line for the save, one for the
# game mode. Believing this rather than a timestamp is the difference between
# asking the game and guessing.
LATEST_SAVE_FILE = "latestSave.ini"

_MOD_LINE = re.compile(r"^(\s*)mod\s*=\s*(.+?),?\s*$", re.MULTILINE)


def is_crash_save(folder: Path) -> bool:
    """Whether this folder is the copy the game left behind after a crash."""
    return Path(folder).name.endswith(CRASH_SUFFIX)


def latest_save_folder() -> Path | None:
    """The save the game itself says it opened last, if it can be found.

    `latestSave.ini` holds the save name on the first line and the game mode on
    the second, which together give `Saves/<mode>/<name>`. This is the game's own
    answer, so it beats any guess made from file dates.

    Returns None when the file is missing, malformed, or points somewhere that
    is not a save any more, and never raises: every caller has a fallback.
    """
    from .discovery import default_user_folder

    user = default_user_folder()
    if user is None:
        return None
    marker = user / LATEST_SAVE_FILE
    if not marker.is_file():
        return None
    try:
        lines = [
            line.strip()
            for line in marker.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        log.warning("Could not read %s: %s", marker, exc)
        return None
    if len(lines) < 2:
        log.warning("%s did not hold a save name and a game mode", marker)
        return None
    name, mode = lines[0], lines[1]
    # A name with a separator in it would let this walk out of Saves/.
    if any(part in (".", "..") or "/" in part or "\\" in part for part in (name, mode)):
        log.warning("%s named a path rather than a save, ignoring it", marker)
        return None
    folder = user / "Saves" / mode / name
    if not (folder / MODS_FILE).is_file():
        return None
    return folder


@dataclass
class SaveGame:
    """One save folder, and the mod order it holds."""

    path: Path
    mode: str = ""
    name: str = ""
    mods: list[str] = field(default_factory=list)
    saved_at: float = 0.0
    raw: str = ""

    @property
    def label(self) -> str:
        return f"{self.mode}/{self.name}" if self.mode else self.name

    @property
    def when(self) -> str:
        from datetime import datetime

        if not self.saved_at:
            return "unknown date"
        return datetime.fromtimestamp(self.saved_at).strftime("%d %b %Y at %H:%M")

    @property
    def backups(self) -> list[Path]:
        """Copies this tool has taken of this save's mod list, newest first."""
        folder = self.path
        found = sorted(
            folder.glob(f"{MODS_FILE}{BACKUP_SUFFIX}*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return found


def parse_mods(text: str) -> list[str]:
    """The mod ids in a mods.txt, in order. Everything else is ignored."""
    return [m.group(2).strip() for m in _MOD_LINE.finditer(text or "")]


def render(text: str, ordered: list[str]) -> str:
    """The same file with its mod lines rewritten in the given order.

    Every other line is kept byte for byte, including the version line, the maps
    block and whatever indentation the game used. Only the mod lines move, and
    only among themselves: the first mod line in the file becomes the first mod
    in `ordered`, and so on. Rebuilding the file from scratch instead would mean
    guessing at a format that is not documented and does change between builds.
    """
    remaining = list(ordered)

    def swap(match: re.Match) -> str:
        indent = match.group(1)
        value = remaining.pop(0) if remaining else match.group(2).strip()
        return f"{indent}mod = {value},"

    return _MOD_LINE.sub(swap, text)


def read_save(folder: Path) -> SaveGame | None:
    """Load one save's mod list, or None when there is nothing to read."""
    target = Path(folder) / MODS_FILE
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not read %s: %s", target, exc)
        return None
    folder = Path(folder)
    return SaveGame(
        path=folder,
        mode=folder.parent.name,
        name=folder.name,
        mods=parse_mods(text),
        saved_at=target.stat().st_mtime,
        raw=text,
    )


def find_saves(limit: int = 25, include_crash: bool = False) -> list[SaveGame]:
    """Every save that has a mod list, the one the game last opened first.

    Crash copies are left out. They are not saves you can go back to, and
    offering to write a load order into one is offering to edit a corpse.

    The rest are ordered by date, except that the save named by
    `latestSave.ini` is lifted to the front. A save's files are touched by more
    than playing it, so the newest timestamp is not reliably the current game.
    """
    from .discovery import default_user_folder

    user = default_user_folder()
    if user is None:
        return []
    root = user / "Saves"
    if not root.is_dir():
        return []
    found: list[SaveGame] = []
    try:
        for candidate in root.glob(f"*/*/{MODS_FILE}"):
            folder = candidate.parent
            if not include_crash and is_crash_save(folder):
                continue
            save = read_save(folder)
            if save is not None:
                found.append(save)
    except OSError as exc:
        log.warning("Could not walk %s: %s", root, exc)
        return []
    found.sort(key=lambda s: s.saved_at, reverse=True)

    current = latest_save_folder()
    if current is not None:
        here = Path(current).resolve()
        for i, save in enumerate(found):
            if save.path.resolve() == here:
                found.insert(0, found.pop(i))
                break
    return found[:limit]


def fit(save: SaveGame, ordered: list[str]) -> list[str]:
    """The save's own mods, resequenced by a computed order.

    The strict rule refuses anything that is not the same set, and it is right
    to: the mods in a save are part of that save. But the usual reason the sets
    differ is harmless. A save records what was active the last time it ran, and
    a selection has moved on since, so the save still lists three variants that
    were switched off and does not list a mod that was switched on.

    This is the middle path. The set is still exactly the save's, untouched.
    Mods the order knows about are put into its relative sequence. Mods it does
    not know keep the exact index they already occupy, so they do not drift
    while everything moves around them, which is the behaviour that can be
    explained in one sentence and checked in one glance.
    """
    here = list(save.mods)
    known_keys = {m.strip().lower() for m in ordered}
    fixed = {i: m for i, m in enumerate(here) if m.strip().lower() not in known_keys}

    save_keys = {m.strip().lower(): m for m in here}
    moving = [save_keys[m.strip().lower()] for m in ordered if m.strip().lower() in save_keys]

    out: list[str] = []
    feed = iter(moving)
    for index in range(len(here)):
        out.append(fixed[index] if index in fixed else next(feed))
    return out


@dataclass
class Plan:
    """What a write would do, worked out before anything is written."""

    save: SaveGame
    ordered: list[str] = field(default_factory=list)
    # Mods in the proposed order that the save does not have, and vice versa.
    # Either being non-empty is what stops the write.
    extra: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        """Whether this is a pure reordering of exactly the same mods."""
        return not self.extra and not self.absent

    @property
    def moves(self) -> list[tuple[str, int, int]]:
        """(mod, place now, place after) for every mod that would move."""
        if not self.safe:
            return []
        before = {m: i for i, m in enumerate(self.save.mods)}
        return [
            (mod, before[mod], index)
            for index, mod in enumerate(self.ordered)
            if before.get(mod) != index
        ]

    @property
    def fitted(self) -> list[str]:
        """The order narrowed to this save's own mods. Always the same set."""
        return fit(self.save, self.ordered)

    @property
    def fitted_moves(self) -> list[tuple[str, int, int]]:
        """(mod, place now, place after) for the narrowed order."""
        before = {m: i for i, m in enumerate(self.save.mods)}
        return [
            (mod, before[mod], index)
            for index, mod in enumerate(self.fitted)
            if before.get(mod) != index
        ]

    @property
    def shared(self) -> int:
        """How many mods the order and the save have in common."""
        here = {m.strip().lower() for m in self.save.mods}
        return sum(1 for m in self.ordered if m.strip().lower() in here)

    @property
    def refusal(self) -> str:
        """Why this cannot be written, in one line. Empty when it can."""
        if self.safe:
            return ""
        parts = []
        if self.extra:
            parts.append(
                f"{len(self.extra)} mod(s) are not in this save: "
                + ", ".join(self.extra[:4])
            )
        if self.absent:
            parts.append(
                f"{len(self.absent)} mod(s) in this save are missing from the order: "
                + ", ".join(self.absent[:4])
            )
        return "; ".join(parts)


def plan(save: SaveGame, ordered: list[str]) -> Plan:
    """Work out whether an order can be applied to a save, without writing."""
    here = {m.strip().lower() for m in save.mods}
    want = {m.strip().lower() for m in ordered}
    return Plan(
        save=save,
        ordered=list(ordered),
        extra=[m for m in ordered if m.strip().lower() not in here],
        absent=[m for m in save.mods if m.strip().lower() not in want],
    )


def back_up(save: SaveGame) -> Path | None:
    """Copy the save's mod list before touching it. Returns the copy."""
    source = save.path / MODS_FILE
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = save.path / f"{MODS_FILE}{BACKUP_SUFFIX}-{stamp}"
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        log.warning("Could not back up %s: %s", source, exc)
        return None
    log.info("Backed up %s to %s", source, target)
    return target


def apply(save: SaveGame, ordered: list[str]) -> tuple[bool, str, Path | None]:
    """Write the order into the save. Returns (done, message, backup path).

    Refuses unless the order is exactly the same set of mods the save already
    has, and refuses if the backup cannot be made. Nothing is written in either
    case: the caller can report the message and the save is untouched.
    """
    proposal = plan(save, ordered)
    if not proposal.safe:
        return False, f"refused: {proposal.refusal}", None
    if not proposal.moves:
        return True, "nothing to do: the save is already in this order", None

    copy = back_up(save)
    if copy is None:
        return False, "refused: the backup could not be written, so neither was the order", None

    target = save.path / MODS_FILE
    text = render(save.raw, list(ordered))
    # Read back what was rendered before replacing the real file. A mangled mod
    # list can stop a save from loading, and there is no undo inside the game.
    if parse_mods(text) != list(ordered):
        return False, "refused: the rewritten list did not come back as expected", copy
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        return False, f"could not write {target}: {exc}", copy
    log.info("Wrote a new load order to %s (%d mods)", target, len(ordered))
    return True, f"{len(proposal.moves)} mod(s) moved; the old list is in {copy.name}", copy


def restore(save: SaveGame, backup: Path) -> tuple[bool, str]:
    """Put a backup back. The undo the game does not have."""
    target = save.path / MODS_FILE
    try:
        shutil.copy2(backup, target)
    except OSError as exc:
        return False, f"could not restore {backup.name}: {exc}"
    log.info("Restored %s from %s", target, backup)
    return True, f"restored from {backup.name}"
