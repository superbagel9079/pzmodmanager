"""Settings that persist between runs.

Everything here used to be a command line option you had to retype. It is kept
in one small JSON file next to the log and the saved scan, so the interface can
edit it and the next launch remembers.

Command line arguments still win. A saved setting is a default, not a lock: if
you pass --build on the command line, that is what runs, and the saved value is
left alone. Only the settings screen writes this file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

SETTINGS_NAME = "settings.json"
SETTINGS_VERSION = 1


@dataclass
class Settings:
    """What the tool remembers about how you want it to run."""

    steam_sdk: str = ""          # folder or file holding steam_api64.dll
    build: str = "42"            # target game version, decides the media branch
    use_defaults: bool = True    # probe the usual Steam and Zomboid locations
    parse_scripts: bool = True   # read media/scripts, slower on a big mod set
    use_steam: bool = True       # look mods up on the Workshop web API
    extra_paths: list[str] = None  # extra folders to scan
    order_path: str = ""         # load order file, empty means look for one
    report_path: str = "pzmodmanager-report.html"
    only_enabled: bool = False   # ignore mods absent from the load order

    def __post_init__(self) -> None:
        if self.extra_paths is None:
            self.extra_paths = []

    # ------------------------------------------------------------------ io --

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Read the settings, falling back to the defaults on any trouble."""
        source = Path(path) if path else default_settings_path()
        if not source.is_file():
            return cls()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings %s: %s", source, exc)
            return cls()
        if payload.get("version") != SETTINGS_VERSION:
            log.info("Settings file is from another version, starting fresh")
            return cls()

        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in payload.get("settings", {}).items() if k in known}
        try:
            settings = cls(**values)
        except TypeError as exc:
            log.warning("Settings file has unusable values: %s", exc)
            return cls()
        log.info("Settings loaded from %s", source)
        return settings

    def save(self, path: Path | None = None) -> Path | None:
        target = Path(path) if path else default_settings_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {"version": SETTINGS_VERSION, "settings": asdict(self)}, indent=2
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Could not save the settings: %s", exc)
            return None
        log.info("Settings saved to %s", target)
        return target

    # -------------------------------------------------------------- helpers --

    @property
    def steam_sdk_path(self) -> Path | None:
        return Path(self.steam_sdk).expanduser() if self.steam_sdk else None

    @property
    def order_path_or_none(self) -> Path | None:
        return Path(self.order_path).expanduser() if self.order_path else None

    def describe(self, name: str) -> str:
        """The value as the settings screen should show it."""
        value = getattr(self, name)
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, list):
            return f"{len(value)} folder(s)" if value else "none"
        return str(value) if value else "not set"


def default_settings_path() -> Path:
    from .store import state_dir

    return state_dir() / SETTINGS_NAME
