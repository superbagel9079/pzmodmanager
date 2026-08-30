"""Reading what the game itself recorded.

Everything else in this tool is a prediction. It reads mod.info off the disk,
works out an order, and says which mod should win a contested file. The game
knows. It writes down, every launch, the exact order it loaded the mods in and
the winner of every file that more than one mod supplies:

    LOG  : Mod          f:0> loading ETO_B
    LOG  : Mod          f:0> mod "ETO_B" overrides media/textures/vehicles/stepvan_1.png

So this module closes the loop. Predict before the run, verify after it, from
the same data. A disagreement between the two is worth more than either alone:
it means the order the tool exported is not the order the game applied.

Nothing here writes anything. It reads one text file.

Two things worth knowing about the file. It is rewritten at every launch, so it
describes the last session and not necessarily the mod list as it stands now.
And it is large, a couple of megabytes with two hundred mods, which is why the
errors are grouped by shape rather than listed: six thousand error lines turn
out to be seven distinct problems.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONSOLE_NAME = "console.txt"

# "LOG  : Mod          f:0> loading ETO_B"
_LOADING = re.compile(r"^LOG\s*:\s*Mod\s+f:\d+>\s*loading\s+(.+?)\s*$")
# 'LOG  : Mod          f:0> mod "ETO_B" overrides media/textures/x.png'
_OVERRIDE = re.compile(r'mod\s+"([^"]+)"\s+overrides\s+(\S+)')

# How many characters of an error line to keep once it is generalised.
SHAPE_WIDTH = 130
# How many example lines to keep per group. Enough to recognise the problem,
# few enough that a group of six thousand does not fill the screen.
SAMPLES_PER_GROUP = 3

# Files every mod has one of. The game logs them as overrides like any other,
# but two mods having their own icon is not a collision.
PER_MOD_FILES = {"icon.png", "preview.png", "poster.png", "mod.info"}


def default_console_path() -> Path | None:
    """Where the game writes its log, if that folder exists on this machine."""
    from .discovery import default_user_folder

    folder = default_user_folder()
    if folder is None:
        return None
    candidate = folder / CONSOLE_NAME
    return candidate if candidate.is_file() else None


def archived_logs(limit: int = 12) -> list[Path]:
    """Previous sessions, newest first.

    console.txt is overwritten at every launch; the game keeps the older ones
    under Logs/. Useful when the interesting run was not the last one.
    """
    from .discovery import default_user_folder

    folder = default_user_folder()
    if folder is None:
        return []
    found: list[Path] = []
    logs = folder / "Logs"
    if not logs.is_dir():
        return []
    try:
        for entry in logs.rglob("*DebugLog*.txt"):
            if entry.is_file():
                found.append(entry)
    except OSError as exc:
        log.warning("Could not walk %s: %s", logs, exc)
        return []
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def generalise(line: str) -> str:
    """Turn one error line into the shape it shares with its siblings.

    Six thousand lines saying a different bone name was not found are one
    problem, not six thousand. Quoted text and numbers are what varies, so both
    are blanked and the rest is what identifies the group.
    """
    text = re.sub(r'"[^"]*"', '"..."', line)
    text = re.sub(r"'[^']*'", "'...'", text)
    text = re.sub(r"\bf:\d+\b", "f:N", text)
    text = re.sub(r"\b\d+\b", "N", text)
    text = re.sub(r"^.*?ERROR:?\s*", "", text)
    return text.strip()[:SHAPE_WIDTH]


@dataclass
class ErrorGroup:
    """One distinct problem, and how many times the game hit it."""

    shape: str
    count: int
    samples: list[str] = field(default_factory=list)
    # The varying part, collected: bone names, template names, and so on. This
    # is what tells you whether a group is about mods you actually have.
    subjects: list[str] = field(default_factory=list)


@dataclass
class Contested:
    """A file more than one mod supplies, and who ended up owning it."""

    path: str
    claimants: list[str]

    @property
    def winner(self) -> str:
        """The last mod to claim it. In Project Zomboid, later wins."""
        return self.claimants[-1] if self.claimants else ""

    @property
    def losers(self) -> list[str]:
        return self.claimants[:-1]


@dataclass
class GameLog:
    """One session, as the game recorded it."""

    source: str = ""
    # How many launches this file holds, and which one is reported. The game
    # appends to console.txt rather than replacing it, so a file can describe
    # several runs and reporting them together doubles every number.
    sessions: int = 1
    loaded: list[str] = field(default_factory=list)
    contested: list[Contested] = field(default_factory=list)
    errors: list[ErrorGroup] = field(default_factory=list)
    override_count: int = 0
    line_count: int = 0

    @property
    def position(self) -> dict[str, int]:
        """Mod id, lowercased, to the place the game loaded it at."""
        return {m.strip().lower(): i for i, m in enumerate(self.loaded)}

    @property
    def error_total(self) -> int:
        return sum(g.count for g in self.errors)

    def losses(self) -> list[tuple[str, str, int]]:
        """(loser, winner, how many files) worst first.

        The one number in here that maps to something a player notices. A mod
        losing five hundred files to another has effectively been switched off,
        without an error and without anyone saying so.
        """
        pairs: Counter = Counter()
        for item in self.contested:
            for loser in item.losers:
                pairs[(loser, item.winner)] += 1
        return [(a, b, n) for (a, b), n in pairs.most_common()]

    def disagreements(self, predicted: list[str]) -> list[tuple[str, int, int]]:
        """Mods the game loaded somewhere other than where an order put them.

        Returns (mod id, place in `predicted`, place in the log). Only mods
        present in both, since a mod that was not loaded has no place to
        compare. The result is the honest measure of whether an exported order
        actually reached the game.
        """
        here = self.position
        out: list[tuple[str, int, int]] = []
        shared = [m for m in predicted if m.strip().lower() in here]
        for rank, mod_id in enumerate(shared):
            actual = here[mod_id.strip().lower()]
            out.append((mod_id, rank, actual))
        # Compare ranks within the shared set, so mods the log has and the
        # prediction does not cannot shift everything by a constant.
        actual_rank = {
            mod_id: i
            for i, mod_id in enumerate(sorted(shared, key=lambda m: here[m.strip().lower()]))
        }
        return [
            (mod_id, rank, actual_rank[mod_id])
            for mod_id, rank, _ in out
            if actual_rank[mod_id] != rank
        ]


def parse(text: str, source: str = "") -> GameLog:
    """Read a console.txt and report its most recent launch.

    The game appends to this file rather than replacing it, so it can hold
    several runs. Reporting them together is not just untidy, it is wrong: two
    launches of the same 246 mods came out as 492 mods loaded and twice the
    contested files, which reads as a catastrophe rather than a repeat.

    The boundary needs no marker from the game. A run loads each mod once, so
    the moment a mod is announced that this run has already loaded, a new run
    has begun. Everything accumulated so far is dropped and counting restarts.

    Never raises: a truncated log still parses.
    """
    result = GameLog(source=source)
    claims: dict[str, list[str]] = defaultdict(list)
    shapes: dict[str, ErrorGroup] = {}
    lines = text.splitlines()
    result.line_count = len(lines)
    seen: set[str] = set()

    def restart() -> None:
        result.sessions += 1
        result.loaded.clear()
        result.override_count = 0
        claims.clear()
        shapes.clear()
        seen.clear()

    for line in lines:
        stripped = line.strip()

        loading = _LOADING.match(stripped)
        if loading:
            mod_id = loading.group(1)
            if mod_id.strip().lower() in seen:
                restart()
            seen.add(mod_id.strip().lower())
            result.loaded.append(mod_id)
            continue

        override = _OVERRIDE.search(stripped)
        if override:
            mod_id, path = override.group(1), override.group(2)
            result.override_count += 1
            if mod_id not in claims[path]:
                claims[path].append(mod_id)
            continue

        if "ERROR" not in stripped:
            continue
        shape = generalise(stripped)
        if not shape:
            continue
        group = shapes.get(shape)
        if group is None:
            group = shapes[shape] = ErrorGroup(shape=shape, count=0)
        group.count += 1
        if len(group.samples) < SAMPLES_PER_GROUP:
            group.samples.append(stripped[:220])
        for quoted in re.findall(r'"([^"]+)"', stripped):
            if quoted not in group.subjects:
                group.subjects.append(quoted)

    # Every mod ships its own icon.png and preview.png, and the game logs each
    # one as an override. They are not a shared file two mods fight over, so
    # counting them made 177 mods look like they were in conflict over an icon.
    result.contested = [
        Contested(path=path, claimants=mods)
        for path, mods in claims.items()
        if len(mods) > 1 and path not in PER_MOD_FILES
    ]
    result.contested.sort(key=lambda c: (-len(c.claimants), c.path))
    result.errors = sorted(shapes.values(), key=lambda g: -g.count)
    log.info(
        "Game log read: %d mods loaded, %d overrides, %d contested file(s), "
        "%d error line(s) in %d shape(s)",
        len(result.loaded),
        result.override_count,
        len(result.contested),
        result.error_total,
        len(result.errors),
    )
    return result


def read(path: Path) -> GameLog | None:
    """Parse a log file, or return None if it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not read the game log %s: %s", path, exc)
        return None
    return parse(text, source=str(path))
