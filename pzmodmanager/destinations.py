"""Getting one computed order to every place that reads one.

The tool has always been able to work out the right load order. What it made
you do was carry that answer around by hand: export here, copy there, remember
to press a button in the game. This module removes the carrying.

Three places read a load order, and only one of them matters at any given
moment:

  - `Saves/<mode>/<save>/mods.txt` is what the game reads in single player. It
    is authoritative, and nothing else can override it.
  - The `Mods=` line of a server ini is what every client loads in multiplayer.
    The client's own list is ignored.
  - `Lua/sorting_rules.txt` feeds Mod Load Order Sorter, which sorts the list
    shown in the main menu. That list only seeds a world at creation, so it is
    the least important of the three, and it is the only one that still needs a
    click in the game afterwards.

So the module works out which of the three exist on this machine, what writing
to each would change, and writes only to the ones chosen. Nothing here decides
on its own: every write goes through the screen beside it, behind a
confirmation, and each one takes a backup first.

Nothing in here ever deletes a mod, unsubscribes from anything, or touches a
file outside the three named above.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import savegame
from .modinfo import read_text_tolerant

log = logging.getLogger(__name__)

# Mod Load Order Sorter reads its rules from here, relative to the Zomboid
# folder. The name is the mod's, not ours, so it is spelled out once.
RULES_NAME = "sorting_rules.txt"
RULES_FOLDER = "Lua"

# The mod id that owns that file. The rules destination is offered only when
# this is installed, because writing a config for a mod nobody has is noise.
SORTER_MOD_ID = "modloadordersorter_b42"

BACKUP_SUFFIX = ".pzmodmanager-backup"

# The keys Mod Load Order Sorter understands in a rules block. Everything else
# it prints an "unsupported key" warning for, so a rule we do not recognise is
# passed through untouched rather than dropped.
_RULE_KEYS = (
    "loadafter",
    "loadmodafter",
    "loadbefore",
    "loadmodbefore",
    "incompatiblemods",
    "incompatible",
    "loadfirst",
    "loadlast",
    "category",
)

_SECTION = re.compile(r"^\s*\[\s*(.-)\s*\]\s*$".replace(".-", ".*?"))


# --------------------------------------------------------------------------- #
# Mod Load Order Sorter rules
# --------------------------------------------------------------------------- #
#
# The sorter does a real topological sort and does read `require=`. What it does
# not do is clean the value first: it looks the requirement up as written, and
# when a mod.info says `require=\NeatUI_Framework` the lookup misses and the
# edge is dropped in silence. On a list of a hundred and twenty six mods that
# was sixteen of thirty six dependencies, quietly ignored.
#
# The rules file is the way around it, because the sorter *does* clean the
# values it reads from there. So the fix is to restate the dependencies this
# tool has already resolved, in the file the sorter trusts.


def rules_path(user_folder: Path | None = None) -> Path | None:
    """Where Mod Load Order Sorter keeps its rules, if the game folder is known."""
    if user_folder is None:
        from .discovery import default_user_folder

        user_folder = default_user_folder()
    if user_folder is None:
        return None
    return Path(user_folder) / RULES_FOLDER / RULES_NAME


def sorter_installed(mods) -> bool:
    """Whether the mod that reads those rules is on this machine."""
    return any(
        str(getattr(mod, "mod_id", "")).strip().lower() == SORTER_MOD_ID for mod in mods
    )


def parse_rules(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Read a rules file into blocks, keeping order and unknown keys.

    Returned as a list rather than a dict because the file is written back out
    and a rules file that reshuffles itself on every run is a file nobody can
    diff. Unknown keys are kept as they were: they belong to the sorter, not to
    us, and a future version of it may add more.
    """
    blocks: list[tuple[str, list[tuple[str, str]]]] = []
    current: list[tuple[str, str]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        section = re.match(r"^\[\s*(.*?)\s*\]$", line)
        if section is not None:
            # The sorter strips a leading backslash from the name it reads, so
            # a block written as [\CleanUI] and one written as [CleanUI] are the
            # same block to it. Store the stripped form so they cannot diverge.
            current = []
            blocks.append((section.group(1).lstrip("\\"), current))
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current.append((key.strip(), value.strip()))
    return blocks


def render_rules(by_key: dict, keys: set[str], existing: str = "") -> str:
    """Build a rules file that states every resolved dependency.

    Only `loadAfter` is written. Every other rule already in the file is carried
    through untouched, including for mods that are not selected, because those
    rules are the user's own work and this tool did not put them there.

    Written with CRLF, which is what the sorter writes itself.
    """
    from .selection import resolve_requirement

    kept: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    for name, pairs in parse_rules(existing):
        survivors = [(k, v) for k, v in pairs if k.strip().lower() not in ("loadafter", "loadmodafter")]
        if name not in kept:
            order.append(name)
            kept[name] = []
        kept[name].extend(survivors)

    wanted: dict[str, list[str]] = {}
    for key in sorted(keys):
        ref = by_key.get(key)
        if ref is None:
            continue
        deps: list[str] = []
        for raw in (getattr(ref, "requires_raw", None) or ref.requires):
            target = resolve_requirement(raw, by_key)
            if target is None or target not in keys:
                continue
            name = by_key[target].mod_id
            # A rule value is split on commas, so an id holding one could never
            # be read back. None do today, but silence would be the wrong way
            # to find that out.
            if "," in name or "]" in name:
                log.warning("Skipping a dependency rule for %s: the id holds a separator", name)
                continue
            if name not in deps and name != ref.mod_id:
                deps.append(name)
        if deps:
            wanted[ref.mod_id] = deps
            if ref.mod_id not in kept:
                order.append(ref.mod_id)
                kept[ref.mod_id] = []

    out: list[str] = []
    for name in order:
        lines = []
        if name in wanted:
            lines.append("loadAfter=" + ",".join(wanted[name]))
        lines.extend(f"{k}={v}" for k, v in kept.get(name, []))
        if lines:
            out.append(f"[{name}]\r\n" + "".join(line + "\r\n" for line in lines))
    return "".join(out)


def rules_summary(text: str) -> tuple[int, int]:
    """(blocks, loadAfter edges) in a rendered rules file, for the confirmation."""
    blocks = parse_rules(text)
    edges = sum(
        len([p for p in value.split(",") if p.strip()])
        for _name, pairs in blocks
        for key, value in pairs
        if key.strip().lower() in ("loadafter", "loadmodafter")
    )
    return len(blocks), edges


# --------------------------------------------------------------------------- #
# Destinations
# --------------------------------------------------------------------------- #


@dataclass
class Destination:
    """One place an order can be written, and what writing it would do."""

    key: str  # "save", "rules", "server"
    label: str
    target: Path | None = None
    detail: str = ""
    # Two different questions, and conflating them was a bug. `available` is
    # whether this tool could write here at all; `chosen` is whether it is
    # ticked. A destination that exists but would change nothing is available
    # and unticked, so the cursor can still reach it and you can still force it.
    available: bool = False
    chosen: bool = False
    reason: str = ""  # why it is not available, or why it is ticked off
    payload: str = ""  # what would be written, for the two file destinations
    plan: object | None = None  # savegame.Plan, for the save

    @property
    def where(self) -> str:
        return str(self.target) if self.target else ""


def plan_destinations(
    ordered: list[str],
    by_key: dict,
    keys: set[str],
    mods,
    server_ini: str = "",
    user_folder: Path | None = None,
) -> list[Destination]:
    """Work out the three destinations without writing anything.

    A destination that cannot be written is still returned, with the reason, so
    the screen can say why rather than quietly showing two rows where there
    should be three.
    """
    found: list[Destination] = []

    # ---------------------------------------------------------------- save --
    save = None
    folder = savegame.latest_save_folder()
    if folder is not None:
        save = savegame.read_save(folder)
    if save is None:
        saves = savegame.find_saves(limit=1)
        save = saves[0] if saves else None
    if save is None:
        found.append(
            Destination(
                key="save",
                label="this save",
                available=False,
                reason="no single player save on this machine yet",
            )
        )
    else:
        proposal = savegame.plan(save, ordered)
        moves = proposal.moves if proposal.safe else proposal.fitted_moves
        found.append(
            Destination(
                key="save",
                label="this save",
                target=save.path / savegame.MODS_FILE,
                detail=(
                    f"{save.label}   {len(moves)} of {len(save.mods)} mod(s) move"
                    if moves
                    else f"{save.label}   already in this order"
                ),
                available=True,
                chosen=bool(moves),
                reason="" if moves else "already in this order, nothing would change",
                plan=proposal,
            )
        )

    # --------------------------------------------------------------- rules --
    path = rules_path(user_folder)
    if not sorter_installed(mods):
        found.append(
            Destination(
                key="rules",
                label="the in-game list",
                available=False,
                reason="Mod Load Order Sorter is not installed",
            )
        )
    elif path is None:
        found.append(
            Destination(
                key="rules",
                label="the in-game list",
                available=False,
                reason="the Zomboid folder was not found",
            )
        )
    else:
        existing = read_text_tolerant(path) if path.is_file() else ""
        payload = render_rules(by_key, keys, existing)
        blocks, edges = rules_summary(payload)
        found.append(
            Destination(
                key="rules",
                label="the in-game list",
                target=path,
                detail=f"{blocks} rule block(s), {edges} dependency edge(s)",
                available=bool(payload),
                chosen=bool(payload) and payload != existing,
                reason=(
                    "no dependencies to state"
                    if not payload
                    else ("already up to date, nothing would change"
                          if payload == existing else "")
                ),
                payload=payload,
            )
        )

    # -------------------------------------------------------------- server --
    from .selection import export_server_ini

    lines = export_server_ini(by_key, ordered)
    if not server_ini:
        found.append(
            Destination(
                key="server",
                label="your server",
                detail="the two lines are copied for you to paste",
                available=False,
                reason="no server ini set up, see Settings",
                payload=lines,
            )
        )
    else:
        target = Path(server_ini).expanduser()
        found.append(
            Destination(
                key="server",
                label="your server",
                target=target,
                detail=f"{len(ordered)} mod(s) into Mods= and WorkshopItems=",
                available=target.is_file(),
                chosen=target.is_file(),
                reason="" if target.is_file() else f"no file at {target}",
                payload=lines,
            )
        )

    return found


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


@dataclass
class WriteResult:
    key: str
    ok: bool
    message: str
    backup: Path | None = None


def back_up(target: Path) -> Path | None:
    """Timestamped copy beside the file, or None when it could not be made."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy = target.with_name(f"{target.name}{BACKUP_SUFFIX}-{stamp}")
    try:
        shutil.copy2(target, copy)
    except OSError as exc:
        log.warning("Could not back up %s: %s", target, exc)
        return None
    return copy


def write_text_safely(target: Path, text: str) -> tuple[bool, str, Path | None]:
    """Back up, write to a temporary file, then replace. Never a partial file.

    `write_text` truncates before it writes, so a crash in between leaves the
    file empty. Writing beside it and renaming is atomic on Windows and on
    POSIX, which matters here because two of the three destinations belong to
    the game rather than to this tool.
    """
    copy = back_up(target) if target.is_file() else None
    if target.is_file() and copy is None:
        return False, "refused: the backup could not be written", None
    temp = target.with_name(target.name + ".pzmodmanager-tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
        temp.replace(target)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"could not write {target.name}: {exc}", copy
    return True, f"written to {target.name}", copy


def replace_ini_lines(text: str, lines: str) -> str:
    """Swap the Mods= and WorkshopItems= lines, leaving the rest of the ini alone.

    A server ini holds an RCON password, a seed, a world name and a hundred
    settings that took someone an evening. Only the two lines this tool owns
    are touched, and a file missing one of them is refused rather than repaired,
    because a server ini without a Mods= line is not a server ini we recognise.
    """
    wanted = {}
    for line in lines.splitlines():
        key, _, _value = line.partition("=")
        if key.strip():
            wanted[key.strip().lower()] = line
    if "mods" not in wanted or "workshopitems" not in wanted:
        raise ValueError("the export did not hold both lines")

    seen = set()
    out = []
    for line in text.splitlines():
        key = line.partition("=")[0].strip().lower()
        if key in wanted:
            out.append(wanted[key])
            seen.add(key)
        else:
            out.append(line)
    missing = set(wanted) - seen
    if missing:
        raise ValueError(f"the ini has no {', '.join(sorted(missing))} line")
    ending = "\r\n" if "\r\n" in text else "\n"
    return ending.join(out) + (ending if text.endswith(("\n", "\r")) else "")


def apply_destination(dest: Destination, ordered: list[str]) -> WriteResult:
    """Write one destination. Returns what happened, and never raises."""
    if dest.key == "save":
        plan = dest.plan
        if plan is None:
            return WriteResult(dest.key, False, "nothing planned")
        wanted = plan.ordered if plan.safe else plan.fitted
        ok, message, copy = savegame.apply(plan.save, wanted)
        return WriteResult(dest.key, ok, message, copy)

    if dest.key == "rules":
        if dest.target is None:
            return WriteResult(dest.key, False, "no rules file to write")
        ok, message, copy = write_text_safely(dest.target, dest.payload)
        if ok:
            message += "; open the mods screen in game and press Sort"
        return WriteResult(dest.key, ok, message, copy)

    if dest.key == "server":
        if dest.target is None:
            return WriteResult(dest.key, False, "no server ini set up")
        try:
            current = read_text_tolerant(dest.target)
            merged = replace_ini_lines(current, dest.payload)
        except (OSError, ValueError) as exc:
            return WriteResult(dest.key, False, f"refused: {exc}")
        ok, message, copy = write_text_safely(dest.target, merged)
        if ok:
            message += "; restart the server for it to be read"
        return WriteResult(dest.key, ok, message, copy)

    return WriteResult(dest.key, False, "unknown destination")
