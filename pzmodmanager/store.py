"""Saving the last scan so a later launch can offer it again.

A scan over a hundred and forty mods takes a while. Having to redo it just to
re-read yesterday's findings is wasteful, so the result is written to a small
JSON file and read back at startup. That is what turns the first-run menu, with
its greyed out entry, into the returning menu that offers "Last results" and
"Rescan".

Only what the results screen needs is stored: the findings, the counts, and
enough context to caption them. The full mod inventory stays in the HTML report.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import Finding, Severity
from .selection import ModRef

log = logging.getLogger(__name__)

STORE_NAME = "last-scan.json"
SELECTION_NAME = "selection.json"
PINS_NAME = "load-order-pins.json"
STORE_VERSION = 2

_SEVERITY_BY_LABEL = {s.label: s for s in Severity}


# Where the saved scan, the selection, the Workshop cache and the log go. Empty
# means the per-user location below. The settings screen writes it, and cli sets
# it once at startup before anything reads a path.
#
# One thing this deliberately does not move: settings.json itself. A setting
# cannot describe where it is stored, or nothing would know where to look for it,
# so the settings file stays in the per-user location and everything it points at
# can move. That is the only exception, and config_dir is what enforces it.
_data_dir: Path | None = None


def set_data_dir(path) -> None:
    """Point the state files somewhere else, or back at the default with None."""
    global _data_dir
    _data_dir = Path(path).expanduser() if path else None


def config_dir() -> Path:
    """Where settings.json lives. Never moves, see the note above."""
    return default_state_dir()


def state_dir() -> Path:
    """Where the saved scan, selection, cache and log live right now."""
    return _data_dir or default_state_dir()


def default_state_dir() -> Path:
    """Where per-user state lives, per operating system."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "pzmodmanager"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "pzmodmanager"
    else:
        base = os.environ.get("XDG_STATE_HOME")
        if base:
            return Path(base) / "pzmodmanager"
        return Path.home() / ".local" / "state" / "pzmodmanager"
    return Path.cwd()


def default_store_path() -> Path:
    return state_dir() / STORE_NAME


def default_steam_cache_path() -> Path:
    return state_dir() / "workshop-cache.json"


def default_selection_path() -> Path:
    return state_dir() / SELECTION_NAME


def default_pins_path() -> Path:
    return state_dir() / PINS_NAME


@dataclass
class StoredScan:
    """A scan result flattened enough to survive a restart."""

    findings: list[Finding] = field(default_factory=list)
    # The mod manager needs the mods themselves, not just the findings, so it
    # can work from a reloaded scan without walking the disk again.
    mods: list[ModRef] = field(default_factory=list)
    mod_count: int = 0
    file_count: int = 0
    has_order: bool = False
    order_source: str = ""
    duration: float = 0.0
    scanned: list[str] = field(default_factory=list)
    report_path: str = ""
    saved_at: float = 0.0
    steam_matched: int = 0
    # None means Steam was never asked, which is not the same as being
    # subscribed to nothing. The Add mods screen needs the difference to say
    # whether an item is already yours or genuinely new.
    subscribed_ids: list[str] | None = None

    @property
    def has_results(self) -> bool:
        """Whether opening this would show anything at all.

        A scan that found no mods still saves a record of itself, so the file
        existing is not the same as there being something to look at. Judging by
        the file left the menu offering "Last results" onto an empty screen.
        """
        return bool(self.mods) or bool(self.findings)

    @property
    def has_mods(self) -> bool:
        """Whether the manager would have anything to list."""
        return bool(self.mods)

    @property
    def saved_label(self) -> str:
        if not self.saved_at:
            return "unknown date"
        return datetime.fromtimestamp(self.saved_at).strftime("%d %b %Y at %H:%M")


def from_result(result, report_path: Path | None = None) -> StoredScan:
    ctx = result.ctx
    return StoredScan(
        findings=list(result.findings),
        mods=[ModRef.from_mod(m) for m in ctx.mods],
        mod_count=len(ctx.mods),
        file_count=result.file_count,
        has_order=ctx.has_order,
        order_source=ctx.order.source if ctx.order else "",
        duration=result.duration,
        scanned=list(result.scanned),
        report_path=str(report_path) if report_path else "",
        saved_at=time.time(),
        steam_matched=getattr(result, "steam_matched", 0),
        subscribed_ids=(
            list(result.subscribed_ids)
            if getattr(result, "subscribed_ids", None) is not None
            else None
        ),
    )


def save(scan: StoredScan, path: Path | None = None) -> Path | None:
    target = Path(path) if path else default_store_path()
    payload = {
        "version": STORE_VERSION,
        "saved_at": scan.saved_at or time.time(),
        "mod_count": scan.mod_count,
        "file_count": scan.file_count,
        "has_order": scan.has_order,
        "order_source": scan.order_source,
        "duration": scan.duration,
        "scanned": scan.scanned,
        "report_path": scan.report_path,
        "steam_matched": scan.steam_matched,
        "subscribed_ids": scan.subscribed_ids,
        "mods": [ref.to_json() for ref in scan.mods],
        "findings": [
            {
                "rule": f.rule,
                "severity": f.severity.label,
                "title": f.title,
                "detail": f.detail,
                "mods": list(dict.fromkeys(f.mods)),
                "evidence": f.evidence,
                "winner": f.winner,
                "advice": f.advice,
            }
            for f in scan.findings
        ],
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save the last scan: %s", exc)
        return None
    log.info("Last scan saved to %s", target)
    return target


def load(path: Path | None = None) -> StoredScan | None:
    """Read the stored scan back. Returns None when there is nothing usable."""
    source = Path(path) if path else default_store_path()
    if not source.is_file():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable stored scan %s: %s", source, exc)
        return None
    if payload.get("version") != STORE_VERSION:
        log.info("Stored scan is from another version, ignoring it")
        return None

    findings = []
    for entry in payload.get("findings", []):
        severity = _SEVERITY_BY_LABEL.get(entry.get("severity", ""), Severity.INFO)
        findings.append(
            Finding(
                rule=entry.get("rule", ""),
                severity=severity,
                title=entry.get("title", ""),
                detail=entry.get("detail", ""),
                mods=list(entry.get("mods", [])),
                evidence=list(entry.get("evidence", [])),
                winner=entry.get("winner"),
                advice=entry.get("advice", ""),
            )
        )
    findings.sort(key=lambda f: f.sort_key)

    scan = StoredScan(
        findings=findings,
        mods=[ModRef.from_json(entry) for entry in payload.get("mods", [])],
        mod_count=int(payload.get("mod_count", 0)),
        file_count=int(payload.get("file_count", 0)),
        has_order=bool(payload.get("has_order", False)),
        order_source=payload.get("order_source", ""),
        duration=float(payload.get("duration", 0.0)),
        scanned=list(payload.get("scanned", [])),
        report_path=payload.get("report_path", ""),
        saved_at=float(payload.get("saved_at", 0.0)),
        steam_matched=int(payload.get("steam_matched", 0)),
        # Kept as None when absent. A scan saved before Steam was configured
        # said nothing about subscriptions, and that must not read as "you are
        # subscribed to nothing".
        subscribed_ids=(
            [str(i) for i in payload["subscribed_ids"]]
            if payload.get("subscribed_ids") is not None
            else None
        ),
    )
    log.info("Loaded a stored scan from %s (%d findings)", source, len(findings))
    return scan


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def save_selection(mod_ids: list[str], path: Path | None = None) -> Path | None:
    """Remember which mods are selected, in load order."""
    target = Path(path) if path else default_selection_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"version": STORE_VERSION, "saved_at": time.time(), "mods": mod_ids}
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not save the selection: %s", exc)
        return None
    log.info("Selection saved to %s (%d mods)", target, len(mod_ids))
    return target


def load_selection(path: Path | None = None) -> list[str] | None:
    """Read the saved selection back, or None when there is not one."""
    source = Path(path) if path else default_selection_path()
    if not source.is_file():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable selection %s: %s", source, exc)
        return None
    mods = payload.get("mods")
    if not isinstance(mods, list):
        return None
    log.info("Loaded a selection of %d mod(s) from %s", len(mods), source)
    return [str(m) for m in mods]


# --------------------------------------------------------------------------- #
# Load order pins
# --------------------------------------------------------------------------- #
#
# A pin is one ordering constraint the user stated by hand: "this mod loads
# before that one". It exists because most ordering in Project Zomboid is not
# declared anywhere a machine can read it. require= says a mod must be present,
# not that it must come first, and the rest is prose on a Workshop page.
#
# Stored as pairs of mod ids rather than anything richer, so the file stays
# readable and a mod that disappears from the disk just stops applying.


def save_pins(pins: list[tuple[str, str]], path: Path | None = None) -> Path | None:
    """Write the ordering constraints. Returns the path, or None on failure."""
    target = Path(path) if path else default_pins_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "version": STORE_VERSION,
                    "saved_at": time.time(),
                    "pins": [{"before": a, "after": b} for a, b in pins],
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not save the load order pins: %s", exc)
        return None
    log.info("Saved %d load order pin(s) to %s", len(pins), target)
    return target


def load_pins(path: Path | None = None) -> list[tuple[str, str]]:
    """Read the ordering constraints back. Never raises; returns [] on trouble."""
    source = Path(path) if path else default_pins_path()
    if not source.is_file():
        return []
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable pin file %s: %s", source, exc)
        return []
    pins: list[tuple[str, str]] = []
    for entry in payload.get("pins", []):
        if not isinstance(entry, dict):
            continue
        before = str(entry.get("before", "")).strip()
        after = str(entry.get("after", "")).strip()
        # A pin to itself is meaningless and would look like a cycle.
        if not before or not after or before.lower() == after.lower():
            continue
        if (before, after) not in pins:
            pins.append((before, after))
    log.info("Loaded %d load order pin(s) from %s", len(pins), source)
    return pins
