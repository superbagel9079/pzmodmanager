"""Build version folders inside a mod.

Build 42 mods are laid out like this:

    mods/CleanUI/common/media/...      shared, always applied, no mod.info
    mods/CleanUI/42.12/mod.info + media/
    mods/CleanUI/42.13/mod.info + media/
    ...
    mods/CleanUI/42.19/mod.info + media/

Each version folder carries its own mod.info. The game picks the folder matching
the running game version, falling back to the closest lower one, and merges it
with common/. So a client on 42.20 loads CleanUI's 42.19 branch plus common.

Older mods use the flat Build 41 layout instead, with mod.info and media/ sitting
directly in the mod folder.

Getting this right matters: treating each version folder as a separate mod turns
one ordinary mod into six mods sharing an id, which reads as a critical conflict
when nothing is wrong at all.
"""

from __future__ import annotations

import re

COMMON_DIR = "common"

# "41", "42", "42.19", "42.20.1" are all valid; "media", "foo" are not.
_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")

# A bare major such as "42" used as a target means "the newest 42.x available".
_OPEN_ENDED = 1_000_000


def is_version_dir(name: str) -> bool:
    """True for a build or version folder name, common/ included."""
    return name == COMMON_DIR or bool(_VERSION_RE.match(name))


def version_tuple(name: str) -> tuple[int, ...]:
    """Turn '42.19' into (42, 19). Raises ValueError on anything else."""
    if not _VERSION_RE.match(name):
        raise ValueError(f"not a version folder: {name}")
    return tuple(int(part) for part in name.split("."))


def target_tuple(target: str) -> tuple[int, ...]:
    """Turn a --build value into a comparable tuple.

    A bare major becomes an open-ended upper bound, so --build 42 selects the
    newest 42.x branch a mod ships rather than only an exact '42' folder.
    """
    parts = version_tuple(target)
    if len(parts) == 1:
        return (parts[0], _OPEN_ENDED)
    return parts


def _padded(value: tuple[int, ...], length: int) -> tuple[int, ...]:
    return value + (0,) * (length - len(value))


def compare(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare two version tuples of possibly different length."""
    size = max(len(a), len(b))
    pa, pb = _padded(a, size), _padded(b, size)
    return (pa > pb) - (pa < pb)


def select_branch(names: list[str], target: str) -> tuple[str | None, str | None]:
    """Pick the version folder the game would use.

    Returns (selected folder name, warning or None). `names` is the list of
    subfolder names of the mod; common/ is handled separately by the caller and
    is ignored here.
    """
    versions = [n for n in names if n != COMMON_DIR and _VERSION_RE.match(n)]
    if not versions:
        return None, None

    wanted = target_tuple(target)
    wanted_major = wanted[0]

    def key(name: str) -> tuple[int, ...]:
        return version_tuple(name)

    # The game only loads a branch for its own major line. Restrict to it first.
    same_major = [n for n in versions if key(n)[0] == wanted_major]
    eligible = [n for n in same_major if compare(key(n), wanted) <= 0]
    if eligible:
        return max(eligible, key=key), None

    if same_major:
        # Every branch is newer than the target: the mod targets a later patch.
        lowest = min(same_major, key=key)
        return lowest, (
            f"no branch at or below build {target}; using {lowest}, "
            "which was published for a later version"
        )

    # Nothing in this major line at all: the mod does not support this build.
    highest = max(versions, key=key)
    return highest, (
        f"no branch for build {wanted_major}; the newest available is {highest}"
    )


def classify_layout(subdir_names: list[str], has_mod_info: bool) -> str:
    """Say which layout a mod folder uses.

    Four answers, not three. The one that was missing is 'mixed': a folder with
    a mod.info at the top AND version subfolders that each have their own. That
    is one mod supporting both builds from a single folder, and the two files
    can declare different ids on purpose.

    Hot Brass is the case that taught this. Its root mod.info says id=zHBVCEF,
    the Build 41 name, while 42.15/mod.info says id=HBVCEFb42. Reading the root
    because it exists reported the Build 41 id on a Build 42 machine, so the
    mod that requires HBVCEFb42 was told its dependency was not installed, when
    it was sitting in the same Workshop item.
    """
    versioned = any(is_version_dir(name) for name in subdir_names)
    if has_mod_info and versioned:
        return "mixed"
    if has_mod_info:
        return "flat"
    if versioned:
        return "versioned"
    return "unknown"
