"""Reading the mod load order.

Order matters: when two mods ship the same file, the one loaded last wins.
Without an order the tool can only report a collision; with it, the tool can say
which mod wins and therefore which one silently loses its features.

Three sources are accepted and detected automatically:

  1. A server .ini file containing `Mods=` and `WorkshopItems=`.
  2. The client's Zomboid/Lua/saved_modlists.txt, which holds the mod lists saved
     from the in-game menu. The observed format is: a list name, then one mod id
     per line, lists separated by a blank line. That format is not documented by
     the developers and has already changed between builds: if the result looks
     wrong, use a plain text file with --order instead.
  3. A free-form text file: one mod id per line, or a single semicolon-separated
     line.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .modinfo import read_text_tolerant

log = logging.getLogger(__name__)


@dataclass
class LoadOrder:
    mod_ids: list[str] = field(default_factory=list)
    workshop_ids: list[str] = field(default_factory=list)
    source: str = ""
    kind: str = ""  # "ini", "modlist", "text"
    list_name: str = ""
    notes: list[str] = field(default_factory=list)

    def index_of(self, mod_id: str) -> int | None:
        target = mod_id.strip().lower()
        for i, value in enumerate(self.mod_ids):
            if value.strip().lower() == target:
                return i
        return None

    def __bool__(self) -> bool:
        return bool(self.mod_ids)


def _split_inline(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;,]", value) if p.strip()]


def parse_ini(text: str, source: str) -> LoadOrder:
    order = LoadOrder(source=source, kind="ini")
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("mods="):
            order.mod_ids = _split_inline(line.partition("=")[2])
        elif line.lower().startswith("workshopitems="):
            order.workshop_ids = _split_inline(line.partition("=")[2])
    return order


def parse_saved_modlists(text: str, source: str, wanted: str | None = None) -> LoadOrder:
    """Split saved_modlists.txt into blocks and keep the requested one."""
    blocks: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_name is not None:
                blocks.append((current_name, current))
            current_name, current = None, []
            continue
        if current_name is None:
            current_name = line
        else:
            current.extend(_split_inline(line) if ";" in line else [line])
    if current_name is not None:
        blocks.append((current_name, current))

    blocks = [b for b in blocks if b[1]]
    if not blocks:
        return LoadOrder(source=source, kind="modlist", notes=["no usable list found"])

    chosen = None
    if wanted:
        for name, mods in blocks:
            if name.strip().lower() == wanted.strip().lower():
                chosen = (name, mods)
                break
    if chosen is None:
        chosen = max(blocks, key=lambda b: len(b[1]))

    order = LoadOrder(
        mod_ids=chosen[1],
        source=source,
        kind="modlist",
        list_name=chosen[0],
    )
    if len(blocks) > 1:
        others = ", ".join(name for name, _ in blocks if name != chosen[0])
        order.notes.append(
            f'using list "{chosen[0]}"; other lists available: {others}'
        )
    return order


def parse_plain(text: str, source: str) -> LoadOrder:
    ids: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//") or line.startswith("["):
            continue
        ids.extend(_split_inline(line) if ";" in line or "," in line else [line])
    return LoadOrder(mod_ids=ids, source=source, kind="text")


def load_order_from_file(path: Path, list_name: str | None = None) -> LoadOrder:
    text = read_text_tolerant(path)
    lowered = text.lower()
    # A save's mods.txt, recognised by its own shape rather than its name, since
    # the name is shared with nothing else but the shape is unmistakable.
    if "mod =" in lowered or "mod=" in lowered:
        order = parse_save_mods(text, str(path))
        if order.mod_ids:
            log.info("Load order read from a save: %s (%d mods)", path, len(order.mod_ids))
            return order
    if "mods=" in lowered and "=" in lowered:
        order = parse_ini(text, str(path))
        if order.mod_ids or order.workshop_ids:
            log.info("Load order read from server ini: %s (%d mods)", path, len(order.mod_ids))
            return order
    if path.name.lower() == "saved_modlists.txt" or "\n\n" in text.strip():
        order = parse_saved_modlists(text, str(path), list_name)
    else:
        order = parse_plain(text, str(path))
    log.info("Load order read from %s (%d mods)", path, len(order.mod_ids))
    return order


def parse_save_mods(text: str, source: str) -> LoadOrder:
    """A save's own mods.txt, which is where Build 42 keeps the real order.

        mods
        {
            mod = ZombieBuddy,
            mod = ETO_B,
        }

    This is the authoritative source on a single player machine, and it was
    missing: the tool looked only for Lua/saved_modlists.txt, which does not
    exist in Build 42. With no order found, every mod came back with no
    order_index, so the sort had no existing order to preserve and was free to
    move anything anywhere. That is how a mod which patches vehicle skins ended
    up ahead of the vehicles.
    """
    from .savegame import parse_mods

    ids = parse_mods(text)
    return LoadOrder(mod_ids=ids, source=source, kind="save")


def default_order_candidates() -> list[Path]:
    """Where to look for a client-side load order, best source first.

    The save comes first because it is what the game actually reads. The Lua
    list is kept as a fallback for older builds that had one.

    Which save, though, is the whole question, and picking the most recently
    modified one was wrong twice over.

    A crash leaves a copy of the save beside it, named with `_crash` on the end,
    and that copy is written *after* the real save. So on any machine that has
    crashed even once, the newest mods.txt on disk belongs to a dead save, and
    the tool would present the mod list from the moment of the crash as the
    player's current order. That is not a stale reading, it is the wrong save.

    So ask the game instead. It writes down which save it last opened, and that
    answer is used when it is available. The date is only a fallback for when it
    is not, and crash copies are excluded from that fallback too.
    """
    from .discovery import default_user_folder
    from .savegame import MODS_FILE, is_crash_save, latest_save_folder

    user = default_user_folder()
    if not user:
        return []
    found: list[Path] = []

    current = latest_save_folder()
    if current is not None:
        found.append(current / MODS_FILE)

    saves = user / "Saves"
    if saves.is_dir():
        try:
            candidates = [
                p
                for p in saves.glob(f"*/*/{MODS_FILE}")
                if p.is_file() and not is_crash_save(p.parent) and p not in found
            ]
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            found.extend(candidates[:1])
        except OSError as exc:
            log.warning("Could not look for a save mod list: %s", exc)

    legacy = user / "Lua" / "saved_modlists.txt"
    if legacy.is_file():
        found.append(legacy)
    return found


def apply_order(mods: list, order: LoadOrder) -> list[str]:
    """Set mod.order_index / mod.enabled. Returns the ids that were not found."""
    if not order:
        return []
    known = {m.key for m in mods}
    for mod in mods:
        idx = order.index_of(mod.mod_id)
        mod.order_index = idx
        mod.enabled = idx is not None
    missing = [mid for mid in order.mod_ids if mid.strip().lower() not in known]
    if missing:
        log.warning("%d mod(s) in the load order are not installed: %s", len(missing), missing)
    return missing
