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


def default_order_candidates() -> list[Path]:
    """Where to look for a client-side load order."""
    from .discovery import default_user_folder

    user = default_user_folder()
    if not user:
        return []
    return [p for p in (user / "Lua" / "saved_modlists.txt",) if p.is_file()]


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
