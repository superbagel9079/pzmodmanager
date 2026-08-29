"""The scan sequence, shared by the command line and the interactive interface.

Both front ends run exactly the same steps; they differ only in where the progress
messages are displayed. Passing a `progress` callable is how a caller listens in.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .analyzers import AnalysisContext, analyze
from .assets import index_all
from .discovery import discover_all
from .loadorder import (
    LoadOrder,
    apply_order,
    default_order_candidates,
    load_order_from_file,
)
from .models import Finding, Mod
from .steam import WorkshopCache, attach_to_mods, fetch_items

log = logging.getLogger(__name__)


@dataclass
class ScanOptions:
    """Everything that changes what a scan does."""

    extra_paths: list[Path] = field(default_factory=list)
    use_defaults: bool = True
    build: str = "42"
    parse_scripts: bool = True
    order_path: Path | None = None
    list_name: str | None = None
    only_enabled: bool = False
    use_steam: bool = True
    steam_cache: Path | None = None
    steam_sdk: Path | None = None


@dataclass
class ScanResult:
    """Everything a scan produced."""

    mods: list[Mod]
    findings: list[Finding]
    ctx: AnalysisContext
    scanned: list[str]
    order: LoadOrder | None
    unknown_in_order: list[str]
    file_count: int
    duration: float
    notes: list[str] = field(default_factory=list)
    steam_matched: int = 0
    # None when the Steam client was not consulted at all, which is different
    # from an empty list meaning nothing is subscribed.
    subscribed_ids: list[str] | None = None

    @property
    def ok(self) -> bool:
        return bool(self.mods)


def resolve_order(options: ScanOptions, say) -> tuple[LoadOrder | None, list[str]]:
    """Find the load order, either where the user pointed or automatically."""
    notes: list[str] = []
    if options.order_path:
        path = Path(options.order_path).expanduser()
        if not path.is_file():
            message = f"Load order file not found: {path}"
            log.warning(message)
            notes.append(message)
            return None, notes
        say(f"Reading load order: {path}")
        return load_order_from_file(path, options.list_name), notes

    say("Looking for a saved mod list...")
    for candidate in default_order_candidates():
        order = load_order_from_file(candidate, options.list_name)
        if order:
            say(f"Load order found: {candidate}")
            notes.append(f"load order read automatically from {candidate}")
            return order, notes
    say("No load order found. Collisions will be reported without a winner.")
    return None, notes


def read_subscriptions(sdk_path: Path, say) -> list[str] | None:
    """Ask the Steam client what this account is subscribed to.

    Only useful for comparing against the disk, and entirely optional: a missing
    SDK, a closed Steam client, or a client that simply does not answer all mean
    the same thing here, which is that the comparison is skipped and the scan
    carries on. It must never be able to stop a scan, so the work runs in a child
    process with a deadline rather than in this thread (see steambridge).
    """
    from .steambridge import list_subscriptions
    from .steamsdk import find_library

    library = find_library(sdk_path)
    if library is None:
        say("No Steam library configured, skipping the subscription check.")
        return None

    say(f"Reading your Steam subscriptions from {library.name}...")
    say("  (Steam must be running and logged in; this step is skipped if it is not)")
    answer = list_subscriptions(library, progress=lambda line: say(f"  {line}"))
    if not answer.usable:
        say(f"Subscription check skipped: {answer.error}")
        log.warning("Subscription check skipped: %s", answer.error)
        return None

    ids = [str(i) for i in answer.subscribed]
    say(f"Steam reports {len(ids)} subscription(s).")
    return ids


def run_scan(options: ScanOptions, progress=None) -> ScanResult:
    """Run the whole scan. `progress` receives one message string per step."""

    def say(message: str) -> None:
        log.info("[step] %s", message)
        if progress:
            progress(message)

    started = time.monotonic()
    log.info(
        "Scan starting (build=%s, defaults=%s, extra paths=%s)",
        options.build,
        options.use_defaults,
        [str(p) for p in options.extra_paths],
    )

    say("Searching for the game folder...")
    mods, scanned = discover_all(
        extra_paths=options.extra_paths,
        use_defaults=options.use_defaults,
        build=options.build,
        progress=progress,
    )

    if not mods:
        say("No mod found.")
        ctx = AnalysisContext(mods=[])
        return ScanResult(
            mods=[],
            findings=[],
            ctx=ctx,
            scanned=scanned,
            order=None,
            unknown_in_order=[],
            file_count=0,
            duration=time.monotonic() - started,
            notes=["no mod found in the scanned locations"],
        )

    say(f"{len(mods)} mod(s) discovered.")

    say("Indexing mod files...")
    file_count = index_all(
        mods,
        build=options.build,
        parse_scripts=options.parse_scripts,
        progress=progress,
    )
    say(f"{file_count} file(s) indexed.")

    steam_matched = 0
    if options.use_steam:
        workshop_ids = [m.workshop_id for m in mods if m.workshop_id]
        if workshop_ids:
            say(f"Querying the Steam Workshop for {len(workshop_ids)} item(s)...")
            cache = WorkshopCache(options.steam_cache) if options.steam_cache else None
            items = fetch_items(workshop_ids, cache=cache, progress=progress)
            steam_matched = attach_to_mods(mods, items)
            if steam_matched:
                say(f"Workshop metadata attached to {steam_matched} mod(s).")
            else:
                say("No Workshop metadata retrieved; continuing with local data only.")
    else:
        say("Steam Workshop lookup disabled.")

    subscribed_ids = None
    if options.steam_sdk is not None:
        subscribed_ids = read_subscriptions(options.steam_sdk, say)

    order, notes = resolve_order(options, say)
    unknown: list[str] = []
    if order:
        unknown = apply_order(mods, order)
        notes.extend(order.notes)
        active = sum(1 for m in mods if m.enabled)
        say(f"Load order applied: {active} of {len(mods)} mod(s) enabled.")

    say("Analysing overlaps...")
    findings, ctx = analyze(
        mods,
        order=order,
        unknown_in_order=unknown,
        only_enabled=options.only_enabled,
        subscribed_ids=subscribed_ids,
        progress=progress,
    )

    duration = time.monotonic() - started
    say(f"Done in {duration:.1f}s: {len(findings)} finding(s).")

    return ScanResult(
        mods=mods,
        findings=findings,
        ctx=ctx,
        scanned=scanned,
        order=order,
        unknown_in_order=unknown,
        file_count=file_count,
        duration=duration,
        notes=notes,
        steam_matched=steam_matched,
        subscribed_ids=subscribed_ids,
    )
