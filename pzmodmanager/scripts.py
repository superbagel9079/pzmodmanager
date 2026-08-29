"""Parser for media/scripts/*.txt files.

Engine format:

    module Base
    {
        item Axe
        {
            Weight = 3,
            ...
        }
        recipe Make Bandage    /* a recipe name may contain spaces */
        {
            ...
        }
    }

The contents of a block are not interpreted: only the (kind, module.name) pairs
matter, because that is what the engine indexes and what the last mod loaded
overwrites.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import ScriptObject

# Block kinds where a redefinition by two different mods is most often a real
# in-game problem. Other kinds are reported at a lower severity. This list
# changes between builds: it is used to weight, never to filter.
SIGNIFICANT_KINDS = {
    "item",
    "vehicle",
    "recipe",
    "craftrecipe",
    "uniquerecipe",
    "evolvedrecipe",
    "fixing",
    "entity",
}

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"//[^\n]*")


def strip_comments(text: str) -> str:
    text = _COMMENT_BLOCK.sub(" ", text)
    text = _COMMENT_LINE.sub(" ", text)
    return text


def _header_of(buffer: str) -> tuple[str, str] | None:
    """Extract (kind, name) from the text preceding an opening brace.

    A block header is the last non-empty line before the brace, and that line
    never contains an `=` (otherwise it is a property of the parent block).
    """
    chunk = buffer.split(",")[-1]
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    if not lines:
        return None
    header = lines[-1]
    if "=" in header:
        return None
    header = re.sub(r"\s+", " ", header).strip()
    parts = header.split(" ", 1)
    kind = parts[0].strip()
    name = parts[1].strip() if len(parts) > 1 else ""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", kind):
        return None
    return kind, name


def parse_script_text(text: str, rel_path: str) -> list[ScriptObject]:
    """Brace parser. Returns the objects declared at depth 2."""
    text = strip_comments(text)
    objects: list[ScriptObject] = []
    stack: list[tuple[str, str]] = []
    buffer: list[str] = []
    current_module = ""

    for char in text:
        if char == "{":
            header = _header_of("".join(buffer))
            buffer = []
            if header is None:
                stack.append(("?", ""))
                continue
            kind, name = header
            stack.append((kind, name))
            depth = len(stack)
            if depth == 1 and kind.lower() == "module":
                current_module = name
            elif depth == 2 and name:
                objects.append(
                    ScriptObject(
                        kind=kind.lower(),
                        module=current_module,
                        name=name,
                        source_file=rel_path,
                    )
                )
        elif char == "}":
            buffer = []
            if stack:
                popped = stack.pop()
                if not stack and popped[0].lower() == "module":
                    current_module = ""
        else:
            buffer.append(char)
            # Guard rail: a growing buffer means we are reading block contents,
            # not a header. Truncate it so memory does not blow up.
            if len(buffer) > 4096:
                buffer = buffer[-256:]

    return objects


def parse_script_file(path: Path, rel_path: str) -> list[ScriptObject]:
    from .modinfo import read_text_tolerant

    return parse_script_text(read_text_tolerant(path), rel_path)
