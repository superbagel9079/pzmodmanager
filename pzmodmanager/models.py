"""Data model.

Two objects only:
  - Mod     : everything we know about an installed mod (metadata + file index)
  - Finding : one observation produced by an analysis rule
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class Severity(enum.Enum):
    """Severity scale, most serious first."""

    CRITICAL = ("critical", 4, "#c0392b")
    HIGH = ("high", 3, "#e67e22")
    MEDIUM = ("medium", 2, "#d4a017")
    LOW = ("low", 1, "#3498db")
    INFO = ("info", 0, "#7f8c8d")

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def weight(self) -> int:
        return self.value[1]

    @property
    def color(self) -> str:
        return self.value[2]

    def __lt__(self, other: "Severity") -> bool:
        return self.weight < other.weight


@dataclass
class ScriptObject:
    """An object declared in media/scripts/*.txt (item, vehicle, recipe...)."""

    kind: str  # "item", "vehicle", "recipe", "fixing", "craftRecipe"...
    module: str  # "Base", "Brita", ...
    name: str  # "Axe", "M16"...
    source_file: str  # path relative to the mod

    @property
    def fqn(self) -> str:
        """Fully qualified name, the way the engine indexes it."""
        return f"{self.module}.{self.name}" if self.module else self.name


@dataclass
class Mod:
    """One mod installed on disk."""

    mod_id: str
    name: str
    root: Path  # folder holding mod.info
    source: str = "unknown"  # "workshop" or "local"
    workshop_id: str | None = None
    description: str = ""
    author: str = ""
    poster: str = ""
    mod_version: str = ""

    requires: list[str] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)
    pack: list[str] = field(default_factory=list)
    tiledefs: list[str] = field(default_factory=list)

    # Build 42 layout: a mod ships one folder per game version plus common/.
    layout: str = "flat"  # "flat" (Build 41 style) or "versioned"
    info_path: Path | None = None  # where mod.info was actually read
    branch: str | None = None  # version folder in use, e.g. "42.19"
    available_branches: list[str] = field(default_factory=list)
    branch_warning: str | None = None
    media_roots: list[Path] = field(default_factory=list)
    build_targets: list[str] = field(default_factory=list)  # ["common", "42.19"]

    # Filled in by the steam module when Workshop lookups are enabled
    workshop_title: str = ""
    workshop_updated: int | None = None
    workshop_missing: bool = False
    workshop_description: str = ""
    workshop_preview: str = ""

    # File index: normalised relative path -> real paths on disk
    assets: dict[str, list[Path]] = field(default_factory=dict)
    script_objects: list[ScriptObject] = field(default_factory=list)

    raw: dict[str, list[str]] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)

    # Filled in by the loadorder module
    enabled: bool = True
    order_index: int | None = None

    @property
    def key(self) -> str:
        """Case-insensitive comparison key."""
        return self.mod_id.strip().lower()

    @property
    def display(self) -> str:
        if self.workshop_id:
            return f"{self.name} [{self.mod_id} / WS {self.workshop_id}]"
        return f"{self.name} [{self.mod_id}]"

    @property
    def workshop_url(self) -> str | None:
        """The Workshop page, for a browser. None for a hand-installed mod."""
        if not self.workshop_id:
            return None
        return (
            "https://steamcommunity.com/sharedfiles/filedetails/?id="
            f"{self.workshop_id}"
        )

    @property
    def lua_files(self) -> list[str]:
        return [p for p in self.assets if p.startswith("lua/")]

    def __hash__(self) -> int:
        return hash((self.mod_id, str(self.root)))


@dataclass
class Finding:
    """One observation: a rule fired on one or more mods."""

    rule: str  # technical rule identifier
    severity: Severity
    title: str  # single readable line
    detail: str  # plain-language explanation
    mods: list[str] = field(default_factory=list)  # mod_ids involved
    evidence: list[str] = field(default_factory=list)  # files / objects at fault
    winner: str | None = None  # mod_id that wins, when load order is known
    advice: str = ""

    @property
    def sort_key(self) -> tuple:
        return (-self.severity.weight, self.rule, self.title)
