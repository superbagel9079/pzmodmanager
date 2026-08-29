"""Steam Workshop lookups.

Uses the public endpoint

    POST https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/

which takes `itemcount` plus `publishedfileids[0]`, `publishedfileids[1]`... and
needs no API key. It returns, per item, the real Workshop title, the last update
time, the file size, the tags, and the full description.

That buys three things the disk cannot tell us:

  * the real title, so the report names mods the way the Workshop does;
  * the last update date, which flags a mod untouched since an older build;
  * the description, where authors very often state an incompatibility in prose
    ("do not use with X") because there is no machine-readable field for it.

Everything here is optional and best effort. No network, a proxy in the way, or a
Steam outage must never stop a scan: the lookup fails quietly, the finding set is
simply smaller. Results are cached on disk so repeated scans do not hammer the
API.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .models import Mod

log = logging.getLogger(__name__)

API_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
BATCH_SIZE = 100
TIMEOUT = 20
USER_AGENT = "pzmodmanager"

# How long a cached answer stays good. Workshop metadata moves slowly.
CACHE_TTL_SECONDS = 24 * 3600

# result=1 means OK; anything else means the item is gone, hidden or private.
RESULT_OK = 1

# Phrases authors use to state an incompatibility in the description. Kept
# deliberately narrow: a loose pattern turns every "compatible with" sentence
# into a false alarm.
_INCOMPAT_PATTERNS = [
    re.compile(r"\bnot compatible with\b", re.I),
    re.compile(r"\bincompatible with\b", re.I),
    re.compile(r"\bdo(?:es)? not work with\b", re.I),
    re.compile(r"\bwon'?t work with\b", re.I),
    re.compile(r"\bdo not use (?:this )?(?:together )?with\b", re.I),
    re.compile(r"\bconflicts? with\b", re.I),
]


@dataclass
class WorkshopItem:
    workshop_id: str
    title: str = ""
    description: str = ""
    time_updated: int | None = None
    file_size: int | None = None
    preview_url: str = ""
    tags: list[str] = field(default_factory=list)
    missing: bool = False

    def to_json(self) -> dict:
        return {
            "workshop_id": self.workshop_id,
            "title": self.title,
            "description": self.description,
            "time_updated": self.time_updated,
            "file_size": self.file_size,
            "preview_url": self.preview_url,
            "tags": self.tags,
            "missing": self.missing,
        }

    @classmethod
    def from_json(cls, data: dict) -> "WorkshopItem":
        return cls(
            workshop_id=str(data.get("workshop_id", "")),
            title=data.get("title", ""),
            description=data.get("description", ""),
            time_updated=data.get("time_updated"),
            file_size=data.get("file_size"),
            preview_url=data.get("preview_url", ""),
            tags=list(data.get("tags", [])),
            missing=bool(data.get("missing", False)),
        )


class WorkshopCache:
    """A small JSON file keyed by Workshop id."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, tuple[float, WorkshopItem]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable Workshop cache %s: %s", self.path, exc)
            return
        for key, entry in raw.get("items", {}).items():
            try:
                self.entries[key] = (
                    float(entry["fetched_at"]),
                    WorkshopItem.from_json(entry["item"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        log.debug("Workshop cache loaded: %d entry(ies)", len(self.entries))

    def get(self, workshop_id: str, ttl: float = CACHE_TTL_SECONDS) -> WorkshopItem | None:
        found = self.entries.get(workshop_id)
        if not found:
            return None
        fetched_at, item = found
        if time.time() - fetched_at > ttl:
            return None
        return item

    def put(self, item: WorkshopItem) -> None:
        self.entries[item.workshop_id] = (time.time(), item)

    def save(self) -> None:
        payload = {
            "items": {
                key: {"fetched_at": fetched_at, "item": item.to_json()}
                for key, (fetched_at, item) in self.entries.items()
            }
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write the Workshop cache: %s", exc)


def _post(ids: list[str]) -> list[dict]:
    """One API call. Raises on network trouble; the caller decides what to do."""
    fields = [("itemcount", str(len(ids)))]
    fields += [(f"publishedfileids[{i}]", value) for i, value in enumerate(ids)]
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.load(response)
    return payload.get("response", {}).get("publishedfiledetails", [])


def fetch_items(
    workshop_ids: list[str],
    cache: WorkshopCache | None = None,
    progress=None,
) -> dict[str, WorkshopItem]:
    """Look up Workshop metadata, using the cache and never raising."""
    wanted = [wid for wid in dict.fromkeys(workshop_ids) if wid]
    found: dict[str, WorkshopItem] = {}

    if cache:
        for wid in list(wanted):
            hit = cache.get(wid)
            if hit is not None:
                found[wid] = hit
                wanted.remove(wid)
        if found:
            log.info("Workshop cache hit for %d item(s)", len(found))

    if not wanted:
        return found

    batches = [wanted[i : i + BATCH_SIZE] for i in range(0, len(wanted), BATCH_SIZE)]
    for number, batch in enumerate(batches, start=1):
        if progress:
            progress(
                f"Querying the Steam Workshop... batch {number}/{len(batches)} "
                f"({len(batch)} items)"
            )
        try:
            details = _post(batch)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            # Offline, blocked by a proxy, or Steam is down. Not fatal.
            log.warning("Steam Workshop lookup failed: %s", exc)
            if progress:
                progress(f"Steam Workshop unreachable, continuing without it ({exc})")
            break

        for entry in details:
            wid = str(entry.get("publishedfileid", ""))
            if not wid:
                continue
            if entry.get("result") != RESULT_OK:
                item = WorkshopItem(workshop_id=wid, missing=True)
            else:
                item = WorkshopItem(
                    workshop_id=wid,
                    title=entry.get("title", "") or "",
                    description=entry.get("description", "") or "",
                    time_updated=entry.get("time_updated"),
                    file_size=int(entry["file_size"]) if entry.get("file_size") else None,
                    preview_url=entry.get("preview_url", "") or "",
                    tags=[t.get("tag", "") for t in entry.get("tags", []) if t.get("tag")],
                )
            found[wid] = item
            if cache:
                cache.put(item)

    if cache:
        cache.save()
    log.info("Workshop lookup returned %d item(s)", len(found))
    return found


def attach_to_mods(mods: list[Mod], items: dict[str, WorkshopItem]) -> int:
    """Copy the Workshop metadata onto the mods. Returns how many were matched."""
    matched = 0
    for mod in mods:
        if not mod.workshop_id:
            continue
        item = items.get(mod.workshop_id)
        if item is None:
            continue
        matched += 1
        mod.workshop_missing = item.missing
        if item.missing:
            continue
        mod.workshop_title = item.title
        mod.workshop_updated = item.time_updated
        mod.workshop_description = item.description
        mod.workshop_preview = item.preview_url
    return matched


def stated_incompatibilities(description: str, known_titles: dict[str, str]) -> list[str]:
    """Find mods named right after an incompatibility phrase in a description.

    `known_titles` maps a lowercased mod title to its mod id. Only names that
    match an installed mod are returned, which keeps the noise down: an author
    warning about a mod you do not have is not your problem.
    """
    if not description:
        return []
    hits: list[str] = []
    for pattern in _INCOMPAT_PATTERNS:
        for match in pattern.finditer(description):
            window = description[match.end() : match.end() + 160].lower()
            for title, mod_id in known_titles.items():
                if len(title) < 5:
                    continue  # too short to match reliably
                if title in window and mod_id not in hits:
                    hits.append(mod_id)
    return hits
