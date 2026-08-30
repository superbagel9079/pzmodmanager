"""Matching this machine to a server's mod list.

A server states two things in its ini. `Mods=` is the ordered list of mod ids it
loads, and `WorkshopItems=` is the set of Steam items those mods come from.
Joining that server means having every one of them, and a mod the client lacks
does not degrade gracefully: the world dictionary refuses to load and the
connection ends on a stack trace naming a single sprite, with no hint as to
which mod owns it.

Matching by hand is the part nobody gets right, for three reasons that all look
like the same reason and are not:

  - The two lines are not the same list. One Workshop item can carry several
    mods, and several ids in `Mods=` can come from one number in
    `WorkshopItems=`. Counting them and finding different totals proves nothing.
  - The names in `Mods=` are the ids declared in mod.info, not the titles on the
    Workshop page. An item called "US Military Grenades" installs a mod called
    `Explosives`. Searching the Workshop for the id finds nothing.
  - Being subscribed and having the mod enabled are different states, and only
    the first is visible from the file system.

So this module does the comparison, and the screen beside it acts on the answer.

Nothing here subscribes, downloads, enables, deletes or writes. It reads a file
and returns a plan. Every part of that plan is applied elsewhere, behind a
confirmation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .modinfo import read_text_tolerant

log = logging.getLogger(__name__)


@dataclass
class ServerList:
    """The two lines that matter, read off a server ini."""

    mod_ids: list[str] = field(default_factory=list)
    workshop_ids: list[str] = field(default_factory=list)
    source: str = ""

    def __bool__(self) -> bool:
        return bool(self.mod_ids or self.workshop_ids)


@dataclass
class ServerDiff:
    """What it would take to match the server, split by the action needed."""

    server: ServerList
    to_subscribe: list[str] = field(default_factory=list)  # Workshop ids
    not_installed: list[str] = field(default_factory=list)  # mod ids still missing
    to_enable: list[str] = field(default_factory=list)
    to_disable: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        """Whether this machine already matches the server exactly."""
        return not (
            self.to_subscribe or self.not_installed or self.to_enable or self.to_disable
        )

    @property
    def selection(self) -> list[str]:
        """What to save as the selection: the server's list, in the server's order.

        Saved whole rather than filtered down to what is installed today. The
        stored selection is intersected with the known mods every time it is
        read, so an id that has not landed yet is simply ignored until it has,
        and then it is already selected. Filtering here would mean the mods
        being downloaded right now come back unticked.
        """
        return list(self.server.mod_ids)


def read_server_list(path: Path | str) -> ServerList:
    """Read `Mods=` and `WorkshopItems=` out of a file.

    A whole server ini works, and so does a file holding nothing but those two
    lines, which is what this tool exports. The rest of the ini is ignored on
    purpose: nothing else in it describes the mod set, and a server ini holds
    passwords and RCON details that have no business being parsed.
    """
    source = Path(path)
    text = read_text_tolerant(source)
    found = ServerList(source=str(source))
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("mods="):
            found.mod_ids = _split(stripped.partition("=")[2])
        elif lowered.startswith("workshopitems="):
            found.workshop_ids = _split(stripped.partition("=")[2])
    log.info(
        "Server list read from %s: %d mod(s), %d Workshop item(s)",
        source,
        len(found.mod_ids),
        len(found.workshop_ids),
    )
    return found


def _split(value: str) -> list[str]:
    """Semicolon separated, duplicates dropped, order kept.

    Order is kept because `Mods=` is a load order, not a set. Duplicates are
    dropped because a line that lists a mod twice is a copy and paste accident,
    and passing it on would make every later count wrong.
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in value.replace(",", ";").split(";"):
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def compare(server: ServerList, mods: list, selected=None) -> ServerDiff:
    """Work out what stands between this machine and that server.

    `selected` is the set of mod keys currently ticked. Passing None falls back
    to what the scan recorded as enabled, which is what the game last loaded.
    """
    by_key = {}
    for mod in mods:
        key = str(getattr(mod, "mod_id", "")).strip().lower()
        if key:
            by_key.setdefault(key, mod)

    installed_items = {
        str(mod.workshop_id) for mod in mods if getattr(mod, "workshop_id", None)
    }

    if selected is None:
        current = {
            key for key, mod in by_key.items() if getattr(mod, "enabled", False)
        }
    else:
        current = {str(key).strip().lower() for key in selected}

    diff = ServerDiff(server=server)

    for item in server.workshop_ids:
        if item not in installed_items:
            diff.to_subscribe.append(item)

    wanted: set[str] = set()
    for mod_id in server.mod_ids:
        key = mod_id.strip().lower()
        if key not in by_key:
            diff.not_installed.append(mod_id)
            continue
        wanted.add(key)
        if key in current:
            diff.unchanged.append(by_key[key].mod_id)
        else:
            diff.to_enable.append(by_key[key].mod_id)

    for key in sorted(current - wanted):
        mod = by_key.get(key)
        diff.to_disable.append(mod.mod_id if mod is not None else key)

    log.info(
        "Server comparison: %d to subscribe, %d not installed, %d to enable, "
        "%d to disable, %d already right",
        len(diff.to_subscribe),
        len(diff.not_installed),
        len(diff.to_enable),
        len(diff.to_disable),
        len(diff.unchanged),
    )
    return diff
