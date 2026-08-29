"""Builds a synthetic mod tree reproducing every kind of conflict.

Usage: python tests/make_fixture.py /tmp/pzfixture
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def write(path: Path, content: str = "-- placeholder\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_poster(path: Path) -> None:
    """A tiny valid PNG, so the poster code has something real to chew on.

    Written by hand rather than with Pillow: the fixture must build even where
    Pillow is not installed.
    """
    import struct
    import zlib

    width = height = 8
    raw = b"".join(b"\x00" + bytes([40, 90, 130] * width) for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def mod(root: Path, wsid: str, mod_id: str, name: str, **info) -> Path:
    d = root / wsid / "mods" / mod_id
    lines = [f"name={name}", f"id={mod_id}", "poster=poster.png"]
    for key, value in info.items():
        if isinstance(value, (list, tuple)):
            if value:
                lines.append(f"{key}=" + ";".join(value))
        else:
            lines.append(f"{key}={value}")
    write(d / "mod.info", "\n".join(lines) + "\n")
    return d


def build(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    content = dest / "steamapps" / "workshop" / "content" / "108600"
    content.mkdir(parents=True)

    INV_LUA = "media/lua/client/ISUI/ISInventoryPage.lua"

    # 1. Two inventory mods replacing the same vanilla file (Build 41 layout)
    a = mod(content, "1001", "InventoryTetris", "Inventory Tetris")
    write_poster(a / "poster.png")
    write(a / INV_LUA, "-- Tetris version\n")
    write(a / "media/lua/client/TetrisUtil.lua")
    write(a / "media/textures/ui/grid.png", "x")

    b = mod(content, "1002", "BetterSorting", "Better Sorting")
    write(b / INV_LUA, "-- Better Sorting version\n")
    write(b / "media/lua/client/SortingUtil.lua")

    # 2. A Build 42 mod with common/ and 42/ branches, plus a texture clash with 1001
    c = mod(content, "1003", "KI5Vehicles", "KI5 Vehicle Pack", require=["BaseVehicles"])
    write(c / "common/media/textures/ui/grid.png", "y")
    write(
        c / "42/media/scripts/vehicles/ki5.txt",
        """
module Base
{
    vehicle CarNormal
    {
        mechanicType = 1,
        engine
        {
            loops = 3,
        }
    }
    item Wheel
    {
        Weight = 10,
    }
}
""",
    )
    # 41 branch: must NOT collide with another mod's 42 branch
    write(c / "41/media/scripts/vehicles/legacy.txt", "module Base\n{\n item Old\n {\n }\n}\n")

    # 3. A mod redefining the same script objects as KI5
    d = mod(content, "1004", "VehicleRebalance", "Vehicle Rebalance")
    write(
        d / "42/media/scripts/rebalance.txt",
        """
/* rebalance patch */
module Base
{
    vehicle CarNormal
    {
        mechanicType = 2,
    }
    item Wheel
    {
        Weight = 4,   // lighter
    }
    recipe Make Reinforced Wheel
    {
        Wheel,
        Result: Wheel,
    }
}
""",
    )

    # 4. Missing dependency (BaseVehicles is installed nowhere) -> already via 1003
    # 5. Declared incompatibility
    e = mod(content, "1005", "HardcoreZombies", "Hardcore Zombies",
            incompatible=["BetterSorting"])
    write(e / "media/lua/server/ZombieTweaks.lua")

    # 6. Duplicate id
    f1 = mod(content, "1006", "DuplicateMod", "Duplicate Mod (copy A)")
    write(f1 / "media/lua/shared/Dup.lua")
    f2 = mod(content, "1007", "DuplicateMod", "Duplicate Mod (copy B)")
    write(f2 / "media/lua/shared/Dup.lua")

    # 7. A clean mod, no overlap at all
    g = mod(content, "1008", "QuietMod", "Quiet Mod")
    write(g / "media/lua/client/QuietOnly.lua")

    # 8b. A Build 42 versioned mod: common/ plus one folder per game version,
    #     each carrying its own mod.info. This is ONE mod, not five.
    for v in ["42.12", "42.15", "42.19"]:
        write(content / f"1009/mods/VersionedMod/{v}/mod.info",
              f"name=Versioned Mod\nid=VersionedMod\nmodversion={v}\n")
        write(content / f"1009/mods/VersionedMod/{v}/media/lua/client/Versioned_{v}.lua")
    write(content / "1009/mods/VersionedMod/common/media/lua/shared/VersionedShared.lua")

    # 8c. A mod that only ships a Build 41 branch: no folder for build 42.
    write(content / "1010/mods/LegacyOnlyMod/41/mod.info",
          "name=Legacy Only Mod\nid=LegacyOnlyMod\n")
    write(content / "1010/mods/LegacyOnlyMod/41/media/lua/client/Legacy.lua")

    # 8. A local mod, installed by hand
    local = dest / "Zomboid" / "mods" / "MyLocalMod"
    write(local / "mod.info", "name=My Local Mod\nid=MyLocalMod\nrequire=QuietMod\n")
    write(local / "media/lua/client/ISUI/ISInventoryPage.lua", "-- third version!\n")

    # Load order: the dependency is placed AFTER the mod that uses it
    order = dest / "modlist.txt"
    order.write_text(
        "\n".join(
            [
                "InventoryTetris",
                "BetterSorting",
                "MyLocalMod",
                "VehicleRebalance",
                "KI5Vehicles",
                "HardcoreZombies",
                "DuplicateMod",
                "MissingMod",
                "QuietMod",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Fixture created in {dest}")
    print(f"  workshop : {content}")
    print(f"  local    : {dest / 'Zomboid' / 'mods'}")
    print(f"  order    : {order}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pzfixture"))
