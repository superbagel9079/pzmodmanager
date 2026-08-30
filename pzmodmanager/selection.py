"""Choosing which mods to run, and working out whether that choice holds up.

The analysis says what overlaps across everything installed. The selection says
what you actually intend to load. Those are different questions: a collision
between two mods you both switched off is not your problem.

So this module keeps a set of enabled mod ids and answers three things about it:

  * what has to come with a mod (its dependency closure);
  * what breaks if a mod goes away (its dependents);
  * which of the scan findings still apply once everything unselected is ignored.

It also produces the load order, dependencies before dependents, and the lines to
paste into a server ini. Nothing here touches the disk or the game.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import Finding, Severity

log = logging.getLogger(__name__)


def _order_notes(mod) -> list[str]:
    """Read once at scan time, for the same reason as the poster path."""
    from .steam import order_hints

    return order_hints(getattr(mod, "workshop_description", "") or "")


def _poster_path(mod) -> str:
    """Resolved once at scan time, so the manager never walks the disk again."""
    from .posters import find_poster

    found = find_poster(mod)
    return str(found) if found else ""

# Findings at or above this severity are treated as blocking problems in a
# selection. Texture clashes are noise here; a client Lua clash is not.
BLOCKING_SEVERITY = Severity.HIGH

WORKSHOP_APP_ID = "108600"


def workshop_search_url(name: str) -> str:
    """Where to look for a mod that is required but not installed.

    A missing dependency has no Workshop id, by definition: it is not on the
    disk. Searching the Workshop for its id is the one useful thing to offer,
    since that id is usually close to the mod's name.
    """
    from urllib.parse import quote_plus

    return (
        f"https://steamcommunity.com/workshop/browse/?appid={WORKSHOP_APP_ID}"
        f"&searchtext={quote_plus(name)}"
    )


@dataclass
class ModRef:
    """The little a selection needs to know about a mod.

    Kept separate from the full Mod so the manager can work from a scan reloaded
    off disk, without the file index that made it heavy.
    """

    mod_id: str
    name: str = ""
    workshop_id: str | None = None
    requires: list[str] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)
    source: str = ""
    was_enabled: bool = True
    order_index: int | None = None
    poster_path: str = ""
    # Lines from the Workshop page that talk about load order. Harvested at scan
    # time because the description is not kept in the saved scan, and the manager
    # must never go back to the network to draw a panel.
    order_notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.mod_id.strip().lower()

    @property
    def workshop_url(self) -> str | None:
        if not self.workshop_id:
            return None
        return (
            "https://steamcommunity.com/sharedfiles/filedetails/?id="
            f"{self.workshop_id}"
        )

    @property
    def steam_client_url(self) -> str | None:
        """Opens the item straight in the Steam client, where Unsubscribe lives."""
        if not self.workshop_id:
            return None
        return f"steam://url/CommunityFilePage/{self.workshop_id}"

    @classmethod
    def from_mod(cls, mod) -> "ModRef":
        return cls(
            mod_id=mod.mod_id,
            name=mod.workshop_title or mod.name,
            workshop_id=mod.workshop_id,
            requires=list(mod.requires),
            incompatible=list(mod.incompatible),
            source=mod.source,
            was_enabled=mod.enabled,
            order_index=mod.order_index,
            poster_path=_poster_path(mod),
            order_notes=_order_notes(mod),
        )

    def to_json(self) -> dict:
        return {
            "id": self.mod_id,
            "name": self.name,
            "workshop_id": self.workshop_id,
            "requires": self.requires,
            "incompatible": self.incompatible,
            "source": self.source,
            "enabled": self.was_enabled,
            "order_index": self.order_index,
            "poster": self.poster_path,
            "order_notes": self.order_notes,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ModRef":
        return cls(
            mod_id=data.get("id", ""),
            name=data.get("name", ""),
            workshop_id=data.get("workshop_id"),
            requires=list(data.get("requires", [])),
            incompatible=list(data.get("incompatible", [])),
            source=data.get("source", ""),
            was_enabled=bool(data.get("enabled", True)),
            order_index=data.get("order_index"),
            poster_path=data.get("poster", ""),
            order_notes=list(data.get("order_notes", [])),
        )


@dataclass
class Problem:
    """Something wrong with the current selection."""

    kind: str
    severity: Severity
    message: str
    mods: list[str] = field(default_factory=list)
    fix_hint: str = ""
    # (label, url) pairs: the Workshop page of each mod named, and a search for
    # anything required but missing.
    links: list[tuple[str, str]] = field(default_factory=list)


def index_by_key(refs: list[ModRef]) -> dict[str, ModRef]:
    index: dict[str, ModRef] = {}
    for ref in refs:
        index.setdefault(ref.key, ref)
    return index


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def dependency_closure(
    by_key: dict[str, ModRef],
    keys: set[str],
) -> tuple[set[str], list[str]]:
    """Expand a set of mods to include everything they require.

    Returns the closed set and the list of required ids that are not installed
    at all, which no amount of selecting can fix.
    """
    closed = set(keys)
    missing: list[str] = []
    queue = list(keys)
    while queue:
        current = queue.pop()
        ref = by_key.get(current)
        if ref is None:
            continue
        for required in ref.requires:
            if not (required or "").strip():
                continue
            rkey = resolve_requirement(required, by_key)
            if rkey is None:
                if required not in missing:
                    missing.append(required)
                continue
            if rkey not in closed:
                closed.add(rkey)
                queue.append(rkey)
    return closed, missing


def dependents_of(by_key: dict[str, ModRef], key: str, within: set[str]) -> list[str]:
    """Selected mods that require `key`, so would break if it were dropped."""
    broken = []
    for other_key in within:
        ref = by_key.get(other_key)
        if ref is None or other_key == key:
            continue
        if any(r.strip().lower() == key for r in ref.requires):
            broken.append(ref.mod_id)
    return sorted(broken)


# --------------------------------------------------------------------------- #
# Load order
# --------------------------------------------------------------------------- #


def pin_edges(
    by_key: dict[str, ModRef],
    keys: set[str],
    pins: list[tuple[str, str]] | None,
) -> list[tuple[str, str]]:
    """The pins that actually apply here, as (before key, after key) pairs.

    A pin naming a mod that is not installed, or not selected, is silently left
    out rather than treated as an error. Mods come and go; the file should not
    have to be curated every time one does.
    """
    applies: list[tuple[str, str]] = []
    for before, after in pins or []:
        first, second = before.strip().lower(), after.strip().lower()
        if first == second:
            continue
        if first in keys and second in keys and first in by_key and second in by_key:
            if (first, second) not in applies:
                applies.append((first, second))
    return applies


def topological_order(
    by_key: dict[str, ModRef],
    keys: set[str],
    preferred: list[str] | None = None,
    pins: list[tuple[str, str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Order the selection so every mod comes after what it requires.

    `preferred` is an existing order used to break ties, so an order that already
    works is disturbed as little as possible. `pins` are ordering constraints the
    user stated by hand, as (loads first, loads second) pairs; they are treated
    exactly like a declared requirement for the purpose of sorting, because that
    is what they are, just written down by the user instead of the author.

    Returns (ordered mod ids, ids involved in a cycle).
    """
    rank: dict[str, int] = {}
    for position, mod_id in enumerate(preferred or []):
        rank.setdefault(mod_id.strip().lower(), position)

    def tie_break(key: str) -> tuple[int, str]:
        return (rank.get(key, len(rank) + 1), key)

    # edges: dependency -> dependent
    waiting_on: dict[str, set[str]] = {k: set() for k in keys}
    for key in keys:
        ref = by_key.get(key)
        if ref is None:
            continue
        for required in ref.requires:
            # Through the shared resolver, like every other place that asks this
            # question. This line used to compare the raw string, so a mod whose
            # author wrote "require=\damnlib" got no ordering edge at all and the
            # library was free to land after the hundred vehicles that need it.
            # Nothing looked wrong: the panel said the order was resolved,
            # because the panel asks a different function.
            rkey = resolve_requirement(required, by_key)
            if rkey is not None and rkey in keys:
                waiting_on[key].add(rkey)
    for first, second in pin_edges(by_key, keys, pins):
        waiting_on[second].add(first)

    ordered: list[str] = []
    remaining = dict(waiting_on)
    while remaining:
        ready = sorted(
            (k for k, deps in remaining.items() if not deps), key=tie_break
        )
        if not ready:
            break  # everything left is in a cycle
        for key in ready:
            ordered.append(key)
            del remaining[key]
        for deps in remaining.values():
            deps.difference_update(ready)

    cycle = sorted(remaining, key=tie_break)
    if cycle:
        log.warning("Dependency cycle among: %s", cycle)
        ordered.extend(cycle)  # still emit them, at the end

    return (
        [by_key[k].mod_id if k in by_key else k for k in ordered],
        [by_key[k].mod_id if k in by_key else k for k in cycle],
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _links_for(by_key: dict[str, ModRef], mod_ids: list[str]) -> list[tuple[str, str]]:
    """Workshop pages for the mods a problem names, in the order it names them."""
    links: list[tuple[str, str]] = []
    for mod_id in dict.fromkeys(mod_ids):
        ref = by_key.get(mod_id.strip().lower())
        if ref and ref.workshop_url:
            links.append((ref.mod_id, ref.workshop_url))
    return links


def resolve_requirement(needed: str, by_key: dict) -> str | None:
    """The installed mod that satisfies this require= entry, or None.

    THE one place that answers "is this dependency installed". There were three,
    written at different times for the scan, the manager panel and the dependency
    closure, and they drifted: a fix went into one and the other two carried on
    reporting the same mod as missing. If you need this question answered
    anywhere else, call this rather than writing a fourth.

    Two ways to match. Exactly, on the lowercased id. Or after stripping stray
    punctuation, because mod.info is typed by hand and nothing checks it: a real
    machine has require=\\damnlib and require=\\tsarslib sitting next to the mods
    they name. Returns the key into by_key, so the caller can tell which it was.
    """
    direct = (needed or "").strip().lower()
    if not direct:
        return None
    if direct in by_key:
        return direct
    cleaned = direct.strip("\\/\"'`[](){}<>,;: ").strip()
    if cleaned and cleaned != direct and cleaned in by_key:
        return cleaned
    return None


def probable_typo(needed: str, by_key: dict) -> str | None:
    """The mod a mis-typed require= meant, when it took cleaning to find it.

    None when the entry matched exactly, because that is not a typo, and None
    when nothing matched at all, because guessing which mod an author meant is
    exactly the kind of helpfulness that invents facts.
    """
    key = resolve_requirement(needed, by_key)
    if key is None or key == (needed or "").strip().lower():
        return None
    return by_key[key].mod_id


@dataclass
class OrderNote:
    """A load order instruction an author wrote in prose, and where to read it.

    Kept apart from Problem on purpose. It is not a problem: nothing is wrong
    with the selection, there is nothing to fix, and putting these in the
    problems panel made three quotations look like three new errors. It is a
    pointer to a page, and it is reported as one.
    """

    mod_id: str
    lines: list[str] = field(default_factory=list)
    url: str = ""


def order_notes(by_key: dict[str, ModRef], keys: set[str]) -> list[OrderNote]:
    """The selected mods whose Workshop page says where to place them."""
    notes: list[OrderNote] = []
    for key in sorted(keys):
        ref = by_key.get(key)
        if ref is None or not ref.order_notes:
            continue
        notes.append(
            OrderNote(
                mod_id=ref.mod_id,
                lines=list(ref.order_notes),
                url=ref.workshop_url or "",
            )
        )
    return notes


def validate(
    by_key: dict[str, ModRef],
    keys: set[str],
    findings: list[Finding] | None = None,
    pins: list[tuple[str, str]] | None = None,
) -> list[Problem]:
    """Everything wrong with this selection, worst first.

    Dependency problems are recomputed live, because they depend on the choice.
    Overlap problems are reused from the scan: a finding still applies when every
    mod it names is selected.
    """
    problems: list[Problem] = []

    for key in sorted(keys):
        ref = by_key.get(key)
        if ref is None:
            continue

        for required in ref.requires:
            if not (required or "").strip():
                continue
            rkey = resolve_requirement(required, by_key)
            typo = probable_typo(required, by_key)

            if rkey is None:
                links = [(f"search the Workshop for {required}", workshop_search_url(required))]
                if ref.workshop_url:
                    links.append((f"{ref.mod_id} on the Workshop", ref.workshop_url))
                problems.append(
                    Problem(
                        kind="dependency_not_installed",
                        severity=Severity.CRITICAL,
                        message=f"{ref.mod_id} requires {required}, which is not installed",
                        mods=[ref.mod_id],
                        fix_hint="Subscribe to it, or drop the mod that needs it.",
                        links=links,
                    )
                )
                continue

            target = by_key[rkey].mod_id
            if rkey not in keys:
                message = f"{ref.mod_id} requires {required}, which is not selected"
                if typo:
                    message = (
                        f"{ref.mod_id} requires {required}, a typo for {target}, "
                        "which is not selected"
                    )
                problems.append(
                    Problem(
                        kind="dependency_not_selected",
                        severity=Severity.CRITICAL,
                        message=message,
                        mods=[ref.mod_id, target],
                        fix_hint=f"Select {target} as well.",
                        links=_links_for(by_key, [ref.mod_id, target]),
                    )
                )
                continue

            if typo:
                # Installed and selected, and the require= line still does not
                # name it correctly. Nothing to do here, but the game reads the
                # same broken id and will say so at load time, so it is worth
                # one quiet line rather than silence.
                problems.append(
                    Problem(
                        kind="dependency_typo",
                        severity=Severity.LOW,
                        message=(
                            f"{ref.mod_id} requires {required}, which is a typo "
                            f"for {target}"
                        ),
                        mods=[ref.mod_id, target],
                        fix_hint=(
                            f"Nothing to install: {target} is here and selected. "
                            "The stray character is in the mod's own mod.info, so "
                            "the game will complain too."
                        ),
                        links=_links_for(by_key, [ref.mod_id, target]),
                    )
                )

        for other in ref.incompatible:
            okey = other.strip().lower()
            if okey in keys and okey != key:
                pair = sorted([ref.mod_id, by_key[okey].mod_id])
                if any(p.kind == "declared_incompatibility" and p.mods == pair for p in problems):
                    continue
                problems.append(
                    Problem(
                        kind="declared_incompatibility",
                        severity=Severity.CRITICAL,
                        message=(
                            f"{ref.mod_id} declares itself incompatible with "
                            f"{by_key[okey].mod_id}, and both are selected"
                        ),
                        mods=pair,
                        fix_hint="Drop one of the two.",
                        links=_links_for(by_key, pair),
                    )
                )

    # With the pins, because a hand written constraint can close a loop just as
    # a require= line can, and a cycle the panel cannot see is a cycle the user
    # only finds out about from the exported file.
    _, cycle = topological_order(by_key, keys, pins=pins)
    if cycle:
        pinned = {m for pair in pin_edges(by_key, keys, pins) for m in pair}
        involved = any(m.strip().lower() in pinned for m in cycle)
        problems.append(
            Problem(
                kind="dependency_cycle",
                severity=Severity.CRITICAL,
                message="These mods have to come before each other: " + ", ".join(cycle),
                mods=cycle,
                fix_hint=(
                    "No order can satisfy them all. Drop one of your pins to break "
                    "the loop."
                    if involved
                    else "No order can satisfy them all. Drop one to break the loop."
                ),
            )
        )

    for finding in findings or []:
        if finding.severity.weight < BLOCKING_SEVERITY.weight:
            continue
        if finding.rule in {
            "missing_dependency",
            "declared_incompatibility",
            # Ordering findings come from the order the scan read. The manager
            # sorts the selection itself, so repeating them here would report a
            # problem it has already solved.
            "dependency_loaded_late",
            "dependency_disabled",
            "mod_not_installed",
        }:
            continue
        involved = [m.strip().lower() for m in finding.mods]
        if not involved or not all(m in keys for m in involved):
            continue
        problems.append(
            Problem(
                kind=finding.rule,
                severity=finding.severity,
                message=finding.title,
                mods=list(dict.fromkeys(finding.mods)),
                fix_hint=finding.advice,
                links=_links_for(by_key, finding.mods),
            )
        )

    problems.sort(key=lambda p: (-p.severity.weight, p.kind, p.message))
    return problems


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def export_server_ini(
    by_key: dict[str, ModRef],
    ordered_ids: list[str],
) -> str:
    """The two lines a Project Zomboid server ini needs.

    Mods= carries the mod ids in load order. WorkshopItems= carries the Workshop
    item ids, deduplicated, since several mods can ship inside one item.
    """
    workshop: list[str] = []
    for mod_id in ordered_ids:
        ref = by_key.get(mod_id.strip().lower())
        if ref and ref.workshop_id and ref.workshop_id not in workshop:
            workshop.append(ref.workshop_id)
    return (
        "Mods=" + ";".join(ordered_ids) + "\n"
        "WorkshopItems=" + ";".join(workshop) + "\n"
    )


def export_text(ordered_ids: list[str]) -> str:
    """One mod id per line, readable back with --order."""
    return "\n".join(ordered_ids) + "\n"


def export_links(by_key: dict[str, ModRef], ordered_ids: list[str]) -> str:
    """The Workshop page of every selected mod, in load order.

    Useful for setting the same list up on another machine, or for handing the
    players of a server the pages they need to subscribe to.
    """
    lines: list[str] = []
    local: list[str] = []
    for mod_id in ordered_ids:
        ref = by_key.get(mod_id.strip().lower())
        if ref is None:
            continue
        if ref.workshop_url:
            lines.append(f"{ref.name or ref.mod_id}\n  {ref.mod_id}\n  {ref.workshop_url}\n")
        else:
            local.append(ref.mod_id)
    body = "\n".join(lines)
    if local:
        body += (
            "\nInstalled by hand, no Workshop page:\n"
            + "".join(f"  {mod_id}\n" for mod_id in local)
        )
    return body


def summarise(
    by_key: dict[str, ModRef],
    keys: set[str],
    problems: list[Problem],
) -> str:
    counts: dict[str, int] = {}
    for problem in problems:
        counts[problem.severity.label] = counts.get(problem.severity.label, 0) + 1
    breakdown = "   ".join(f"{label} {count}" for label, count in counts.items())
    head = f"{len(keys)} of {len(by_key)} mods selected"
    if not problems:
        return f"{head}   no problems"
    return f"{head}   {len(problems)} problem(s)   {breakdown}"


# --------------------------------------------------------------------------- #
# Workshop items that hold more than one mod
# --------------------------------------------------------------------------- #
#
# A Workshop item and a mod are not the same thing, and this is where the
# difference bites. Plenty of items ship several mods: "42.20 | Every Texture
# Optimized" installs ETO_B and ETO_P, two variants you are meant to choose
# between. Enabling happens per mod. Subscribing happens per item, and
# unsubscribing takes every mod in the item with it.
#
# So deselecting one variant must never unsubscribe from the item, or the tool
# would delete the variant you explicitly kept, while the confirmation listed
# only the one you dropped.


@dataclass
class ItemToDrop:
    """A Workshop item where every mod it installs has been deselected."""

    workshop_id: str
    mod_ids: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ", ".join(self.mod_ids) or self.workshop_id


@dataclass
class ItemHeldBack:
    """An item that cannot be unsubscribed, because you still want part of it."""

    workshop_id: str
    dropping: list[str] = field(default_factory=list)
    keeping: list[str] = field(default_factory=list)


def unsubscribe_plan(by_key: dict, selected: set) -> tuple[list[ItemToDrop], list[ItemHeldBack]]:
    """Split the deselected mods into what can safely be unsubscribed and what cannot.

    Returns (safe, held back). An item is safe only when every mod it installs
    is deselected. One kept mod holds the whole item back, because Steam has no
    way to remove part of one.
    """
    by_item: dict[str, list] = {}
    for ref in by_key.values():
        if ref.workshop_id:
            by_item.setdefault(str(ref.workshop_id), []).append(ref)

    safe: list[ItemToDrop] = []
    held: list[ItemHeldBack] = []
    for workshop_id, refs in sorted(by_item.items()):
        dropping = sorted(r.mod_id for r in refs if r.key not in selected)
        keeping = sorted(r.mod_id for r in refs if r.key in selected)
        if not dropping:
            continue
        if keeping:
            held.append(ItemHeldBack(workshop_id, dropping, keeping))
        else:
            safe.append(ItemToDrop(workshop_id, dropping))
    return safe, held
