"""Checks every rule against the synthetic mod tree.

Run: python tests/test_pzmodmanager.py
No framework needed: the script checks itself and exits 1 if anything fails.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_fixture import build  # noqa: E402
from pzmodmanager.builds import select_branch, version_tuple  # noqa: E402
from pzmodmanager.logs import setup_logging  # noqa: E402
from pzmodmanager import store  # noqa: E402
from pzmodmanager import selection as sel  # noqa: E402
from pzmodmanager.pipeline import ScanOptions, run_scan  # noqa: E402
from pzmodmanager.report import render_html, to_dict  # noqa: E402
from pzmodmanager.models import Finding, Severity  # noqa: E402
from pzmodmanager.posters import find_poster, pillow_available, poster_blocks  # noqa: E402
from pzmodmanager import steamsdk  # noqa: E402
from pzmodmanager import cli  # noqa: E402
from pzmodmanager.scripts import parse_script_text  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"[{'OK  ' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def test_script_parser() -> None:
    text = """
    module Base
    {
        item Axe
        {
            Weight = 3,
            DisplayName = Axe,
        }
        recipe Make Bandage
        {
            RippedSheets,
            Result: Bandage,
        }
        vehicle CarNormal
        {
            mechanicType = 1,
            engine
            {
                loops = 3,
            }
            part Engine
            {
                category = engine,
            }
        }
    }
    module Brita
    {
        item M16
        {
            Weight = 4, /* comment */
        }
    }
    """
    objects = parse_script_text(text, "scripts/test.txt")
    fqns = {(o.kind, o.fqn) for o in objects}
    check(("item", "Base.Axe") in fqns, "parser: item Base.Axe recognised")
    check(("recipe", "Base.Make Bandage") in fqns, "parser: multi-word recipe name recognised")
    check(("vehicle", "Base.CarNormal") in fqns, "parser: vehicle recognised")
    check(("item", "Brita.M16") in fqns, "parser: second module handled")
    check(
        not any(o.fqn.endswith(".Engine") for o in objects),
        "parser: depth-3 nested blocks are not reported",
    )
    check(
        not any(o.kind == "weight" for o in objects),
        "parser: key=value properties are not mistaken for blocks",
    )


def test_branch_selection() -> None:
    branches = ["common", "42.12", "42.15", "42.19"]
    check(select_branch(branches, "42.20")[0] == "42.19",
          "branch: build 42.20 falls back to the newest branch at or below it")
    check(select_branch(branches, "42.15")[0] == "42.15",
          "branch: an exact version match wins")
    check(select_branch(branches, "42")[0] == "42.19",
          "branch: a bare major picks the newest branch in that line")
    check(select_branch(["41", "42"], "42")[0] == "42",
          "branch: the 41 branch is not chosen for a build 42 target")
    picked, warning = select_branch(["41"], "42")
    check(picked == "41" and warning is not None,
          "branch: a 41-only mod is selected but flagged")
    check(version_tuple("42.19") == (42, 19), "branch: version parsing")


def _refs(spec: dict) -> dict:
    """Build a mod index from {id: (requires, incompatible)}."""
    refs = [
        sel.ModRef(mod_id=mid, name=mid, workshop_id=str(1000 + i),
                   requires=list(req), incompatible=list(inc))
        for i, (mid, (req, inc)) in enumerate(spec.items())
    ]
    return sel.index_by_key(refs)


def test_selection() -> None:
    by_key = _refs({
        "App": (["Lib"], []),
        "Lib": (["Core"], []),
        "Core": ([], []),
        "Solo": ([], ["App"]),
        "Orphan": (["Absent"], []),
    })

    closed, missing = sel.dependency_closure(by_key, {"app"})
    check(closed == {"app", "lib", "core"},
          f"selection: dependencies pulled in transitively (got {sorted(closed)})")
    check(missing == [], "selection: nothing reported missing when all are installed")

    _, missing = sel.dependency_closure(by_key, {"orphan"})
    check(missing == ["Absent"], "selection: a dependency absent from disk is reported")

    check(sel.dependents_of(by_key, "core", {"app", "lib", "core"}) == ["Lib"],
          "selection: direct dependents are found")

    ordered, cycle = sel.topological_order(by_key, {"app", "lib", "core"})
    check(not cycle, "selection: no cycle in a clean graph")
    check(ordered.index("Core") < ordered.index("Lib") < ordered.index("App"),
          f"selection: dependencies come first (got {ordered})")

    problems = sel.validate(by_key, {"app", "solo"}, [])
    kinds = {p.kind for p in problems}
    check("dependency_not_selected" in kinds,
          "selection: a missing dependency is flagged")
    check("declared_incompatibility" in kinds,
          "selection: a declared incompatibility between two selected mods is flagged")
    check(not sel.validate(by_key, {"core"}, []),
          "selection: a self-contained mod raises nothing")

    # A conflict between mods that are not both selected is not a problem.
    finding = Finding(rule="collision_lua_client", severity=Severity.HIGH,
                      title="clash", detail="", mods=["App", "Solo"])
    check(not [p for p in sel.validate(by_key, {"app", "lib", "core"}, [finding])
               if p.kind == "collision_lua_client"],
          "selection: a finding about an unselected mod is ignored")
    check([p for p in sel.validate(by_key, {"app", "lib", "core", "solo"}, [finding])
           if p.kind == "collision_lua_client"],
          "selection: a finding about two selected mods applies")

    cyclic = _refs({"A": (["B"], []), "B": (["A"], [])})
    _, cycle = sel.topological_order(cyclic, {"a", "b"})
    check(sorted(cycle) == ["A", "B"], "selection: a dependency cycle is detected")
    check(any(p.kind == "dependency_cycle" for p in sel.validate(cyclic, {"a", "b"}, [])),
          "selection: the cycle is reported as a problem")

    links = sel.export_links(by_key, ["Core", "App"])
    check("filedetails/?id=1002" in links, "links: the Workshop page is exported")
    check("Core" in links and "App" in links, "links: every selected mod appears")
    local = _refs({"Hand": ([], [])})
    local["hand"].workshop_id = None
    check("no Workshop page" in sel.export_links(local, ["Hand"]),
          "links: a hand-installed mod is listed separately")

    ini = sel.export_server_ini(by_key, ["Core", "Lib", "App"])
    check("Mods=Core;Lib;App" in ini, f"export: Mods line in load order (got {ini!r})")
    check("WorkshopItems=" in ini, "export: WorkshopItems line present")
    check(len(ini.strip().splitlines()) == 2, "export: exactly two lines")


def test_cli_routing(tmp: Path) -> None:
    """--manage opens the interface; the export options stay headless."""
    from pzmodmanager import cli
    import pzmodmanager.tui as tuimod

    seen: dict = {}

    def fake_run_tui(options, log_path=None, report_path=None, store_path=None,
                     selection_path=None, open_manager=False, steam_sdk=None,
                     settings=None, settings_path=None, cli_overrides=None):
        seen.update(opened=True, open_manager=open_manager, steam_sdk=steam_sdk,
                    cli_overrides=cli_overrides,
                    settings=settings)
        return None

    # cli.main reconfigures logging, which would otherwise redirect the rest of
    # this run away from the log file the later checks read.
    real = tuimod.run_tui
    tuimod.run_tui = fake_run_tui
    try:
        state = str(tmp / "routing.json")

        seen.clear()
        cli.main(["--manage", "--state", state, "--steam-sdk", str(tmp)])
        check(seen.get("opened") is True, "cli: --manage opens the interface")
        check(seen.get("open_manager") is True, "cli: --manage lands on the manager")
        check(seen.get("steam_sdk") == tmp,
              "cli: --steam-sdk reaches the interface, so unsubscribing works there")

        seen.clear()
        cli.main(["--tui", "--state", state])
        check(seen.get("opened") is True, "cli: --tui opens the interface")
        check(seen.get("open_manager") is False, "cli: --tui alone starts at the menu")

        seen.clear()
        cli.main(["--print-order", "--no-auto", "--state", state])
        check(not seen.get("opened"), "cli: --print-order stays headless")
    finally:
        tuimod.run_tui = real
        setup_logging(tmp / "test.log", "debug")


def test_settings(tmp: Path) -> None:
    """Settings persist, and a typed argument still beats a saved value."""
    from pzmodmanager import cli
    from pzmodmanager.settings import Settings

    path = tmp / "settings.json"
    check(Settings.load(path) == Settings(), "settings: defaults when there is no file")

    saved = Settings(steam_sdk="/some/sdk", build="42.15", use_steam=False,
                     extra_paths=["/a", "/b"])
    saved.save(path)
    back = Settings.load(path)
    check(back.build == "42.15" and back.steam_sdk == "/some/sdk",
          "settings: values survive a round trip")
    check(back.use_steam is False, "settings: a saved false stays false")
    check(back.extra_paths == ["/a", "/b"], "settings: path lists survive")

    parser = cli.build_parser()
    options = cli.options_from_args(parser.parse_args([]), back, cli.explicitly_given([]))
    check(options.build == "42.15", "settings: a saved value is used when nothing is typed")
    check(options.use_steam is False, "settings: a saved false is respected")
    check(options.steam_sdk == Path("/some/sdk"),
          "settings: the SDK path reaches the scan")

    argv = ["--build", "42.19"]
    options = cli.options_from_args(parser.parse_args(argv), back, cli.explicitly_given(argv))
    check(options.build == "42.19", "settings: a typed argument overrides the saved value")

    bad = tmp / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    check(Settings.load(bad) == Settings(),
          "settings: an unreadable file falls back to defaults rather than failing")


def test_subscription_crosscheck(mods) -> None:
    """Comparing the disk against Steam, and staying quiet when Steam is absent."""
    from pzmodmanager.analyzers import analyze

    installed = sorted({m.workshop_id for m in mods if m.workshop_id})
    check(len(installed) > 2, "crosscheck: the fixture has workshop ids to compare")

    pretend = [w for w in installed[:-2]] + ["9999999"]
    findings, _ = analyze(mods, subscribed_ids=pretend)
    rules = {f.rule for f in findings}
    check("installed_but_not_subscribed" in rules,
          "crosscheck: files with no subscription are flagged")
    check("subscribed_but_not_installed" in rules,
          "crosscheck: a subscription with no files is flagged")

    orphan = next(f for f in findings if f.rule == "installed_but_not_subscribed")
    check(len(orphan.mods) == 2,
          f"crosscheck: both orphans are named (got {len(orphan.mods)})")
    check("still loading" in orphan.detail or "keeps loading" in orphan.detail,
          "crosscheck: the finding explains that the mod is still active")

    findings, _ = analyze(mods, subscribed_ids=None)
    check(not [f for f in findings if "subscri" in f.rule],
          "crosscheck: nothing is claimed when Steam was never asked")


def test_steam_output_capture() -> None:
    """The SDK writes from C, which would otherwise land on top of the interface."""
    import ctypes
    import logging

    from pzmodmanager.steamsdk import steam_output_to_log

    records: list[str] = []

    class Catcher(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("pzmodmanager.steamsdk")
    handler = Catcher()
    logger.addHandler(handler)
    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError:
        logger.removeHandler(handler)
        check(True, "steam: C level capture not testable on this platform, skipped")
        return
    try:
        with steam_output_to_log():
            libc.printf(b"NOISE FROM C\n")
            libc.fflush(None)
    finally:
        logger.removeHandler(handler)

    check(any("NOISE FROM C" in r for r in records),
          "steam: C level output is captured into the log")
    check(True, "steam: and the terminal is handed back afterwards")


def test_steam_bridge(tmp: Path) -> None:
    """The bridge cannot be exercised without Steam, so test that it refuses well.

    Every failure here has to explain itself, because this is the one part of the
    tool that could not be run where it was written.
    """
    check(steamsdk.find_library(tmp / "nothing-here") is None,
          "steam: a missing library is reported as missing, not guessed at")

    diag = steamsdk.diagnose(tmp / "nothing-here")
    check(not diag.ok, "steam: an absent SDK leaves the bridge unusable")
    check(any("Steamworks SDK" in note for note in diag.notes),
          "steam: the diagnosis says what to download")
    check(any("not found" in line for line in diag.lines()),
          "steam: the report names what is missing")

    # A real file that is not the SDK must fail on the symbols, not on a crash.
    fake = tmp / "libsteam_api.so"
    fake.write_bytes(b"not a library")
    diag = steamsdk.diagnose(fake)
    check(not diag.ok, "steam: a file that is not the SDK is refused")
    check(bool(diag.notes), "steam: and the refusal comes with a reason")
    check(not (Path.cwd() / "steam_appid.txt").exists(),
          "steam: no steam_appid.txt is left behind after a failed attempt")
    check("SteamAppId" not in os.environ,
          "steam: the environment is left as it was found")

    check(steamsdk.PZ_APP_ID == "108600", "steam: the app id is Project Zomboid's")
    check(len(steamsdk.UGC_ACCESSOR_NAMES) > 5,
          "steam: several UGC accessor versions are probed, not one guessed")


def test_workshop_input() -> None:
    """Reading what a user pastes, and what an author wrote in a description."""
    from pzmodmanager.steam import (
        item_url,
        mod_ids_in_description,
        parse_workshop_ids,
        search_url,
    )

    check(parse_workshop_ids("2392709985") == ["2392709985"],
          "paste: a bare id")
    check(parse_workshop_ids(
        "https://steamcommunity.com/sharedfiles/filedetails/?id=2392709985"
    ) == ["2392709985"], "paste: a full item link")
    check(parse_workshop_ids(
        "https://steamcommunity.com/sharedfiles/filedetails/?id=2392709985&searchtext=x"
    ) == ["2392709985"], "paste: a link with extra query parameters")
    check(parse_workshop_ids("2392709985, 3728775267\n111222333")
          == ["2392709985", "3728775267", "111222333"],
          "paste: several at once, in the order given")
    check(parse_workshop_ids("2392709985 2392709985") == ["2392709985"],
          "paste: the same id twice is one entry")
    check(parse_workshop_ids("inventory tetris") == [],
          "paste: a name yields nothing rather than a guessed id")
    check(parse_workshop_ids("") == [], "paste: empty input is not an error")
    # A workshop URL contains 108600 in some forms; it must not become an id.
    check("108600" not in parse_workshop_ids(
        "https://steamcommunity.com/workshop/browse/?appid=108600&searchtext=car"),
        "paste: an app id in a search link is not mistaken for an item")

    described = (
        "Adds cars.\n"
        "Workshop ID: 2392709985\n"
        "Mod ID: KI5Vehicles\n"
        "Mod IDs: PackA, PackB and PackC\n"
        "mod id = Legacy (build 41 only)\n"
    )
    found = mod_ids_in_description(described)
    check("KI5Vehicles" in found, "description: the mod id is read")
    check("PackB" in found, "description: a comma separated list is split")
    check("PackC" in found, "description: 'and' is treated as a separator too")
    check("Legacy" in found and "(build" not in found,
          "description: trailing prose after an id is dropped")
    check(mod_ids_in_description("") == [],
          "description: nothing claimed is not an error")
    check(mod_ids_in_description("no ids here at all") == [],
          "description: prose with no declaration yields nothing")

    check("id=123" in item_url("123"), "links: an item page is built from the id")
    check("searchtext=inventory+tetris" in search_url("inventory tetris"),
          "links: a Workshop search is built from the typed text")


def test_browse_screen() -> None:
    """What the Add mods screen says about an item it has looked up."""
    from pzmodmanager.browse_screen import BrowseScreen, _size, _updated
    from pzmodmanager.steam import WorkshopItem

    here = WorkshopItem(workshop_id="111", title="Already Here")
    coming = WorkshopItem(workshop_id="222", title="Subscribed")
    fresh = WorkshopItem(workshop_id="333", title="Brand New")
    gone = WorkshopItem(workshop_id="444", title="Removed", missing=True)

    screen = BrowseScreen(installed={"111"}, subscribed={"111", "222"})
    check(screen.status_of(here) == "already installed",
          "browse: an installed item says so")
    check("not on disk yet" in screen.status_of(coming),
          "browse: subscribed but undownloaded is its own state")
    check(screen.status_of(fresh) == "new", "browse: an unknown item is new")
    check("gone" in screen.status_of(gone),
          "browse: an item removed from the Workshop is flagged")

    # The difference that matters: never asking Steam is not the same as being
    # subscribed to nothing, and the screen must not claim the latter.
    unknown = BrowseScreen(installed={"111"}, subscribed=None)
    check(unknown.status_of(coming) == "new",
          "browse: with no subscription list, nothing is claimed about one")

    check(_size(None) == "unknown size", "browse: an unknown size says unknown")
    check(_size(1_200_000).endswith("MB"), "browse: sizes are human readable")
    check(_updated(None) == "unknown", "browse: an unknown date says unknown")

    from pzmodmanager import tui

    for name, items in (("first run", tui.FIRST_RUN_ITEMS),
                        ("returning", tui.RETURNING_ITEMS)):
        keys = [key for key, _label in items]
        check("browse" in keys, f"menu: the {name} menu offers Add mods")
        check(keys.index("browse") < keys.index("scan"),
              f"menu: Add mods sits above Scan in the {name} menu")


def test_mixed_layout(tmp: Path) -> None:
    """A folder with a root mod.info AND version folders is one mod with two ids.

    Found on a real machine. Hot Brass ships:

        mods/..._Framework/mod.info        id=zHBVCEF      (the Build 41 name)
        mods/..._Framework/42.15/mod.info  id=HBVCEFb42    (the Build 42 name)
        mods/..._TacticalReload/42.16/mod.info  require=HBVCEFb42

    The old classify_layout answered "flat" the moment a root mod.info existed,
    so the scan read the Build 41 id on a Build 42 machine and reported
    HBVCEFb42 as not installed, while it sat in the same Workshop item. A
    critical finding about a dependency that was right there.
    """
    from pzmodmanager.builds import classify_layout
    from pzmodmanager.discovery import discover_all

    check(classify_layout(["42.15"], has_mod_info=True) == "mixed",
          "layout: a root mod.info beside version folders is its own case")
    check(classify_layout([], has_mod_info=True) == "flat",
          "layout: a root mod.info alone is still flat")
    check(classify_layout(["42.15", "common"], has_mod_info=False) == "versioned",
          "layout: version folders alone are still versioned")
    check(classify_layout(["media"], has_mod_info=False) == "unknown",
          "layout: neither is still unknown")

    root = tmp / "mixed" / "steamapps" / "workshop" / "content" / "108600"
    framework = root / "3610677934" / "mods" / "Framework"
    (framework / "42.15").mkdir(parents=True)
    (framework / "mod.info").write_text("name=Framework\nid=zHBVCEF\n", encoding="utf-8")
    (framework / "42.15" / "mod.info").write_text(
        "name=Framework B42\nid=HBVCEFb42\n", encoding="utf-8")
    reload_dir = root / "3610677934" / "mods" / "TacReload" / "42.16"
    reload_dir.mkdir(parents=True)
    (reload_dir / "mod.info").write_text(
        "name=Tac Reload\nid=HBTacReload\nrequire=HBVCEFb42\n", encoding="utf-8")

    def ids_for(build: str) -> list[str]:
        mods, _ = discover_all(extra_paths=[root], use_defaults=False, build=build)
        return sorted(m.mod_id for m in mods)

    check(ids_for("42") == ["HBTacReload", "HBVCEFb42"],
          f"layout: build 42 reads the branch id (got {ids_for('42')})")
    check(ids_for("42.15") == ["HBTacReload", "HBVCEFb42"],
          "layout: a pinned 42.x reads it too")
    check(ids_for("41") == ["HBTacReload", "zHBVCEF"],
          f"layout: build 41 reads the root id instead (got {ids_for('41')})")

    mods, _ = discover_all(extra_paths=[root], use_defaults=False, build="42")
    ids = {m.mod_id for m in mods}
    needed = {r for m in mods for r in m.requires}
    check(not (needed - ids),
          "layout: and the dependency inside the same item is no longer 'missing'")


def test_dependency_typo() -> None:
    """A require= line that needs punctuation stripped is a typo, not a gap.

    Real case: Standardized Vehicle Upgrades declares require=\\tsarslib, with a
    stray backslash, while TsarsLib is installed. Reporting a missing dependency
    there is true and useless.
    """
    from pzmodmanager.analyzers import probable_typo

    by_key = sel.index_by_key([sel.ModRef(mod_id="TsarsLib"), sel.ModRef(mod_id="Brita")])
    check(probable_typo("\\tsarslib", by_key) == "TsarsLib",
          "typo: a stray backslash is seen through")
    check(probable_typo("[Brita]", by_key) == "Brita",
          "typo: so are stray brackets")
    check(probable_typo("tsarslib", by_key) is None,
          "typo: a plain case difference is not a typo, it already matches")
    check(probable_typo("SomethingElse", by_key) is None,
          "typo: an unrelated name is not guessed at")
    check(probable_typo("\\nothing", by_key) is None,
          "typo: and nothing is invented when the cleaned name is unknown either")


def test_problem_filter() -> None:
    """'h' hides the minor problems without hiding the counts.

    A two hundred mod set produces a lot of low findings, mostly typos in other
    people's mod.info, and eighteen of those bury the one critical that actually
    stops the game loading. The filter hides rows, never facts: the footer keeps
    reporting every problem by severity whatever the panel is showing.
    """
    import asyncio

    from pzmodmanager.manager_screen import PROBLEM_VIEWS, ManageScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    mods = [sel.ModRef(mod_id="damnlib", workshop_id="1")]
    mods += [sel.ModRef(mod_id=f"Car{i}", workshop_id=str(100 + i),
                        requires=["\\damnlib"]) for i in range(6)]
    mods.append(sel.ModRef(mod_id="Broken", workshop_id="9", requires=["ReallyGone"]))

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 44)) as pilot:
            screen = ManageScreen(mods, [])
            await app.push_screen(screen)
            await pilot.pause()
            screen.selected = set(screen.by_key)
            screen.refresh_all()
            await pilot.pause()

            from textual.coordinate import Coordinate

            table = screen.query_one("#mods")

            def marked() -> int:
                """Rows carrying the '!' that means 'involved in a problem'."""
                return sum(
                    1
                    for row in range(len(screen.visible_refs))
                    if str(table.get_cell_at(Coordinate(row, 1))).startswith("! ")
                )

            seen["total"] = len(screen.problems)
            seen["views"] = []
            for _ in range(len(PROBLEM_VIEWS) + 1):
                seen["views"].append(
                    (screen._problems_heading(), len(screen.shown_problems()), marked())
                )
                seen["footer"] = str(screen.query_one("#footer").visual)
                await pilot.press("h")
                await pilot.pause()
        return seen

    seen = asyncio.run(run())

    check(seen["total"] == 7,
          f"filter: the set really does have a pile of problems (got {seen['total']})")
    headings = [h for h, _n, _m in seen["views"]]
    counts = [n for _h, n, _m in seen["views"]]
    marks = [m for _h, _n, m in seen["views"]]

    check(counts[0] == 7, "filter: everything is shown to start with")
    check(counts[1] == 1, f"filter: 'h' once hides the low ones (got {counts[1]})")
    check(counts[2] == 1, "filter: 'h' again leaves only the critical")
    check(counts[3] == 7, "filter: and once more brings them all back")
    check("of 7" in headings[1] and "hiding low" in headings[1],
          f"filter: the heading says what is hidden ({headings[1]})")
    check(headings[0] == "PROBLEMS (7)",
          "filter: and says nothing extra when nothing is hidden")
    check("critical 1" in seen["footer"] and "low 6" in seen["footer"],
          f"filter: the footer still counts every problem ({seen['footer'][:60]})")

    # The '!' in the list means "involved in a problem", so it has to mean the
    # problems you are looking at. Every mod flagged by a low typo turns the
    # whole list into exclamation marks, which says nothing; keeping them while
    # hiding the problems themselves would be worse than either.
    # Eight rows, not seven: a typo problem names both the mod that has the
    # broken require= line and the mod it meant, and both are genuinely
    # involved. Six cars plus damnlib plus the one really broken mod.
    check(marks[0] == 8,
          f"filter: with everything shown, every involved mod is marked (got {marks[0]})")
    check(marks[1] == 1,
          f"filter: hiding low clears the markers with it (got {marks[1]})")
    check(marks[2] == 1, "filter: critical only marks only that one")
    check(marks[3] == 8, "filter: and showing everything brings the markers back")
    check(marks[1] < marks[0],
          "filter: hiding problems really does quieten the list")
    check(marks[1] == counts[1],
          "filter: the marked rows and the listed problems agree")


def test_toggle_keeps_the_view_still() -> None:
    """Ticking a box must not throw the list around.

    refresh_table rebuilds every row, and clear() sends the scroll back to the
    top. move_cursor afterwards scrolls only far enough to bring the row into
    view, which parks it on the bottom edge. On a 249 mod list, ticking one box
    jumped the whole view and left you hunting for where you were.

    Searching is different and must keep working: it genuinely changes which
    rows exist, so the old offset is meaningless and the cursor is brought into
    view normally.
    """
    import asyncio

    from pzmodmanager.manager_screen import ManageScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    mods = [sel.ModRef(mod_id=f"Mod{i:03d}", name=f"Mod {i:03d}", workshop_id=str(i))
            for i in range(120)]

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 30)) as pilot:
            screen = ManageScreen(mods, [])
            await app.push_screen(screen)
            await pilot.pause()
            table = screen.query_one("#mods")

            for _ in range(60):
                await pilot.press("down")
            await pilot.pause()
            # Scroll the view without moving the cursor, the way a wheel does,
            # so the cursor sits mid screen. This detail is the whole test: with
            # the cursor already on the bottom edge, scrolling it back into view
            # lands on the same offset by luck and the bug is invisible.
            table.scroll_to(y=table.scroll_y - 12, animate=False, force=True)
            await pilot.pause()
            seen["row_before"] = table.cursor_row
            seen["scroll_before"] = round(table.scroll_y)

            # Count what the toggle actually does to the table. Holding the
            # scroll still was only half the problem: rebuilding 120 rows to
            # change one checkbox repaints the whole list twice per keypress,
            # which reads as a flicker even when nothing ends up moving.
            counts = {"clear": 0, "add": 0, "update": 0}
            real_clear, real_add = table.clear, table.add_row
            real_update = table.update_cell_at

            def counted(name, inner):
                def wrapper(*args, **kwargs):
                    counts[name] += 1
                    return inner(*args, **kwargs)
                return wrapper

            table.clear = counted("clear", real_clear)
            table.add_row = counted("add", real_add)
            table.update_cell_at = counted("update", real_update)

            await pilot.press("x")
            for _ in range(4):
                await pilot.pause()
            seen["row_after"] = table.cursor_row
            seen["scroll_after"] = round(table.scroll_y)
            seen["ticked"] = bool(screen.selected)
            seen["cleared"] = counts["clear"]
            seen["added"] = counts["add"]
            seen["updated"] = counts["update"]

            table.clear, table.add_row = real_clear, real_add
            table.update_cell_at = real_update

            await pilot.press("space")
            for _ in range(4):
                await pilot.pause()
            seen["scroll_after_two"] = round(table.scroll_y)

            screen.query_one("#search").focus()
            await pilot.press(*"Mod 09")
            await pilot.pause()
            seen["rows_filtered"] = len(screen.visible_refs)
            seen["cursor_visible"] = table.cursor_row < len(screen.visible_refs)
        return seen

    seen = asyncio.run(run())

    check(seen["scroll_before"] > 0,
          "still: the test actually scrolled somewhere first")
    check(seen["row_after"] == seen["row_before"],
          "still: toggling keeps the cursor on the same row")
    check(seen["scroll_after"] == seen["scroll_before"],
          f"still: and the list does not move ({seen['scroll_before']} -> "
          f"{seen['scroll_after']})")
    check(seen["ticked"], "still: the toggle did happen, this is not a no-op test")
    check(seen["cleared"] == 0 and seen["added"] == 0,
          f"still: toggling does not rebuild the table "
          f"(cleared {seen['cleared']}, added {seen['added']} of 120 rows)")
    # One cell: the checkbox. No mod here has a problem, so no exclamation mark
    # moves. The upper bound is what matters, not the exact figure: the old code
    # rewrote all 120 rows to change this one character.
    check(0 < seen["updated"] <= 4,
          f"still: it rewrites only the cells that changed "
          f"(got {seen['updated']})")
    check(seen["scroll_after_two"] == seen["scroll_before"],
          "still: unticking does not move it either")
    check(seen["rows_filtered"] > 0 and seen["cursor_visible"],
          "still: searching still lands the cursor on a row that exists")


def test_order_hints() -> None:
    """Load order instructions live in the description, so they get read there.

    require= is the only ordering a mod declares in a way a machine can use, and
    the tool already resolves it. Everything else is prose on the Workshop page:
    "NEEDS TO BE LOADED AFTER ELLIE'S TATTOO PARLOR", or a screenshot of the
    author's own list. That was invisible to the tool and therefore to the user.

    These strings are shortened from real subscriptions. The rejects matter as
    much as the hits: half the pages that say "load order" are only asking you
    to paste yours into a bug report.
    """
    from pzmodmanager.steam import order_hints

    hit = order_hints("[*]NEEDS TO BE LOADED AFTER ELLIE'S TATTOO PARLOR")
    check(hit == ["NEEDS TO BE LOADED AFTER ELLIE'S TATTOO PARLOR"],
          f"hints: an instruction is kept, with its BBCode stripped (got {hit})")
    check(order_hints("[b]The mod must be below any other clothing mod it affects.[/b]"),
          "hints: 'must be below' counts as an instruction")
    check(order_hints("NeatUI Framework must be loaded before any mod that needs it"),
          "hints: so does 'must be loaded before'")

    for noise in [
        "Include your mod list/load order and steps to reproduce in a bug report",
        "Compatibility can depend on load order, versions and game build",
        "Load order doesn't matter, just activate and go.",
        "[h2]Recommended Load Order[/h2]",
        "Mod Load Order Sorter",
        "",
    ]:
        check(order_hints(noise) == [],
              f"hints: nothing claimed for {noise[:44]!r}")

    many = "\n".join(f"You must load it after mod number {i}" for i in range(9))
    check(len(order_hints(many)) == 3,
          "hints: at most three lines, so the panel stays readable")
    long_line = "Put this mod below " + ("everything else " * 40)
    check(len(order_hints(long_line)[0]) <= 200,
          "hints: a very long line is cut rather than flooding the panel")

    for aside in [
        "# check correct mod load order for compatibility with custom vehicles",
        "// remember to load it after the framework",
        "1.4.2 fixed the mod loading before its dependency",
    ]:
        check(order_hints(aside) == [],
              f"hints: a comment or changelog line is not an instruction "
              f"({aside[:40]!r})")

    # A note is NOT a problem, and reaching the manager as one was a mistake:
    # three quoted sentences appeared as three new errors under a heading that
    # says PROBLEMS, and put an exclamation mark on mods with nothing wrong.
    ref = sel.ModRef(mod_id="Skin", name="Skin", workshop_id="42",
                     order_notes=["Load this after Spongie's Customisation"])
    check(sel.validate({"skin": ref}, {"skin"}, []) == [],
          "hints: a note raises no problem and does not touch the problem count")
    notes = sel.order_notes({"skin": ref}, {"skin"})
    check(len(notes) == 1 and notes[0].mod_id == "Skin",
          "hints: it is collected separately, against its mod")
    check("Spongie" in notes[0].lines[0],
          "hints: the author's own words are quoted, not paraphrased")
    check(notes[0].url.endswith("42"),
          "hints: with the page to go and read")
    check(sel.order_notes({"skin": ref}, set()) == [],
          "hints: nothing is said about a mod you did not select")


def test_sort_disturbs_a_working_order_as_little_as_possible() -> None:
    """The sort must respect dependencies AND keep a working order nearly intact.

    This is a real regression, caught by the game's own log. The sort used to
    emit whole waves: everything with no unmet dependency, then everything
    freed by that, and so on. `preferred` could only shuffle within a wave, so
    a mod declaring no require= line always came out ahead of every mod that
    declared one, however late the working order had put it.

    On a real 246 mod set that put DynamicVehicleSnow, which patches vehicle
    skins and declares nothing, a hundred and eighty places ahead of the KI5
    vehicles it patches, which all declare require=damnlib. The game answered
    with 132 "template not found" errors that the previous order did not have.

    Picking one ready mod at a time, always the lowest ranked, fixes it: a mod
    now moves only when a real dependency forces it to.
    """
    lib = sel.ModRef(mod_id="Lib")
    cars = [sel.ModRef(mod_id=f"Car{i}", requires=["Lib"]) for i in range(5)]
    # Declares nothing, and the working order deliberately puts it last.
    patch = sel.ModRef(mod_id="SnowPatch")
    by_key = sel.index_by_key([lib, *cars, patch])
    keys = set(by_key)
    working = ["Lib"] + [c.mod_id for c in cars] + ["SnowPatch"]

    out, cycle = sel.topological_order(by_key, keys, preferred=working)
    check(not cycle, "stable: nothing circular here")
    check(out == working,
          f"stable: a working order that already satisfies every dependency "
          f"comes back unchanged (got {out})")
    check(out.index("SnowPatch") > max(out.index(c.mod_id) for c in cars),
          "stable: the mod with no requirements stays after the mods that have one")

    # A dependency really is enforced when the working order breaks it.
    broken = [c.mod_id for c in cars] + ["SnowPatch", "Lib"]
    fixed, _ = sel.topological_order(by_key, keys, preferred=broken)
    check(fixed.index("Lib") < min(fixed.index(c.mod_id) for c in cars),
          f"stable: a broken order is corrected, not preserved (got {fixed})")
    # SnowPatch lands first here, and that is correct rather than a slip: the
    # cars are all still waiting on Lib, so the only ready mods are Lib and
    # SnowPatch, and SnowPatch is the lower ranked of the two. What a stable
    # sort promises is the lowest ranked AVAILABLE mod, not the lowest ranked.
    check([m for m in fixed if m.startswith("Car")] == [c.mod_id for c in cars],
          f"stable: and the mods that were not forced keep their relative order "
          f"(got {fixed})")

    # With no preferred order at all it must still be a valid topological sort.
    plain, _ = sel.topological_order(by_key, keys)
    check(plain.index("Lib") < min(plain.index(c.mod_id) for c in cars),
          "stable: with nothing to preserve it still puts the library first")
    check(sorted(plain) == sorted(working), "stable: and loses nothing")


def test_order_view() -> None:
    """'o' shows the order that gets exported, instead of hiding it in a file.

    The sequence was already being computed, by topological_order, and the only
    way to see it was to export and open the file. Worse, the panel's "order:
    resolved" line called topological_order without the preferred order while
    the export called it with, so the two could describe different sequences.
    Both now go through resolved_order, and this checks the screen agrees with
    the export down to the row.
    """
    import asyncio

    from textual.coordinate import Coordinate

    from pzmodmanager.manager_screen import ManageScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    # Alphabetically Alpha, Beta, Zeta. By dependency the reverse: Alpha needs
    # Beta needs Zeta. So the two views cannot be confused with each other.
    mods = [
        sel.ModRef(mod_id="Alpha", name="Alpha", requires=["Beta"]),
        sel.ModRef(mod_id="Beta", name="Beta", requires=["Zeta"]),
        sel.ModRef(mod_id="Zeta", name="Zeta"),
        sel.ModRef(mod_id="Lonely", name="Lonely"),
    ]

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 30)) as pilot:
            screen = ManageScreen(mods, [])
            await app.push_screen(screen)
            await pilot.pause()
            table = screen.query_one("#mods")
            names = lambda: [table.get_cell_at(Coordinate(i, 1)).plain
                             for i in range(table.row_count)]  # noqa: E731
            ids = lambda: [table.get_cell_at(Coordinate(i, 2)).plain
                           for i in range(table.row_count)]  # noqa: E731

            seen["alphabetical"] = ids()
            # Drop one, so the view has a mod with no place in the order.
            screen.selected = {"alpha", "beta", "zeta"}
            screen.refresh_all()
            await pilot.pause()

            await pilot.press("o")
            for _ in range(3):
                await pilot.pause()
            seen["ordered"] = ids()
            seen["labels"] = names()
            seen["export"] = screen.resolved_order()[0]
            seen["notice"] = screen.notice

            await pilot.press("o")
            for _ in range(3):
                await pilot.pause()
            seen["back"] = ids()
        return seen

    seen = asyncio.run(run())
    check(seen["alphabetical"] == ["Alpha", "Beta", "Lonely", "Zeta"],
          f"order view: the default list is alphabetical (got {seen['alphabetical']})")
    check(seen["ordered"][:3] == ["Zeta", "Beta", "Alpha"],
          f"order view: 'o' puts dependencies first (got {seen['ordered'][:3]})")
    check(seen["ordered"][:3] == seen["export"],
          "order view: and the rows are exactly what export would write")
    check([n[:3].strip() for n in seen["labels"][:3]] == ["1", "2", "3"],
          f"order view: the places are numbered (got {seen['labels'][:3]})")
    check(seen["ordered"][3] == "Lonely" and seen["labels"][3].startswith("  ."),
          f"order view: an unselected mod sits below, unnumbered "
          f"(got {seen['labels'][3]!r})")
    check("load order" in seen["notice"], "order view: the notice says which view this is")
    check(seen["back"] == seen["alphabetical"],
          "order view: pressing it again goes back to alphabetical")


def test_game_log(tmp: Path) -> None:
    """Reading what the game recorded, instead of only predicting it.

    Everything else in the tool is a prediction from mod.info on disk. The game
    writes down the order it really used and the winner of every contested file:

        LOG  : Mod          f:0> loading ETO_B
        LOG  : Mod          f:0> mod "ETO_B" overrides media/textures/x.png

    That closes the loop, and it answers the one question nothing else can: a
    mod quietly losing its files to a later one produces no error and nothing on
    screen. On a real 246 mod session this found 976 contested files and one mod
    that had lost 555 of them.

    The shapes below are copied from a real console.txt, spacing included,
    because that spacing is what the patterns match on.
    """
    from pzmodmanager import gamelog

    text = "\n".join([
        "LOG  : General      f:0> Loading Mods",
        "LOG  : Mod          f:0> loading AlicesMultiWear",
        'LOG  : Mod          f:0> mod "AlicesMultiWear" overrides media/textures/bag.png',
        'LOG  : Mod          f:0> mod "AlicesMultiWear" overrides media/textures/hat.png',
        "LOG  : Mod          f:0> loading ETO_B",
        'LOG  : Mod          f:0> mod "ETO_B" overrides media/textures/bag.png',
        "LOG  : Mod          f:0> loading Quiet",
        "LOG  : General      f:0> Loading Scripts",
        'LOG  : General      f:0> ERROR: template "DAMN85stepVan" not found B',
        'LOG  : General      f:0> ERROR: template "DAMN86fordE150" not found B',
        'ERROR: General      f:0 at ImportedSkeleton.collectBoneFrames  > '
        'Could not find bone index for node name: "Bip01_HeadNub"',
        'ERROR: General      f:0 at ImportedSkeleton.collectBoneFrames  > '
        'Could not find bone index for node name: "Bip01_L_Toe0Nub"',
    ])
    record = gamelog.parse(text, source="console.txt")

    check(record.loaded == ["AlicesMultiWear", "ETO_B", "Quiet"],
          f"log: the load order is read in order (got {record.loaded})")
    check(record.override_count == 3,
          f"log: every override line is counted (got {record.override_count})")
    check(len(record.contested) == 1 and record.contested[0].path.endswith("bag.png"),
          "log: only the file two mods supply is contested")
    check(record.contested[0].winner == "ETO_B",
          "log: the winner is the one loaded last, which is how the game works")
    check(record.contested[0].losers == ["AlicesMultiWear"],
          "log: and the earlier one is recorded as losing it")
    check(record.losses() == [("AlicesMultiWear", "ETO_B", 1)],
          f"log: the loss is reported as a pair (got {record.losses()})")

    # Two shapes, four lines. This grouping is the difference between a screen
    # that says "7 problems" and one that says "6113 errors".
    check(len(record.errors) == 2,
          f"log: errors collapse to their shapes (got {len(record.errors)})")
    check(record.error_total == 4, "log: while the true count is kept")
    bones = next(g for g in record.errors if "bone" in g.shape)
    check(bones.count == 2 and len(bones.subjects) == 2,
          "log: with the varying part collected, so you can see what it is about")
    check(bones.samples and "Bip01_HeadNub" in bones.samples[0],
          "log: and a real line kept as an example")

    # The comparison that matters: is the exported order the applied order?
    check(record.disagreements(["AlicesMultiWear", "ETO_B", "Quiet"]) == [],
          "log: an order that matches the game reports no disagreement")
    moved = record.disagreements(["ETO_B", "AlicesMultiWear", "Quiet"])
    check(len(moved) == 2,
          f"log: a swapped pair is reported, both halves (got {moved})")
    check(record.disagreements(["Ghost", "ETO_B"]) == [],
          "log: a mod the log never saw is skipped rather than shifting the rest")

    # It must survive a file that is not a game log at all.
    empty = gamelog.parse("", source="x")
    check(empty.loaded == [] and empty.errors == [] and empty.contested == [],
          "log: an empty file parses to nothing rather than failing")
    junk = gamelog.parse("hello\nERROR: something odd\n", source="x")
    check(len(junk.errors) == 1, "log: a stray error line still groups")

    missing = tmp / "nope" / "console.txt"
    check(gamelog.read(missing) is None,
          "log: a file that is not there returns nothing, and does not raise")

    # And the screen, driven for real, including a log with brackets in it.
    import asyncio

    from textual.coordinate import Coordinate

    from pzmodmanager.gamelog_screen import VIEWS, GameLogScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    hostile = tmp / "console.txt"
    hostile.write_text(
        text + "\n"
        'LOG  : Mod          f:0> loading [B42]Bracket Mod\n'
        'LOG  : Mod          f:0> mod "[B42]Bracket Mod" overrides media/textures/bag.png\n',
        encoding="utf-8",
    )

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(120, 40)) as pilot:
            screen = GameLogScreen(hostile, predicted=["AlicesMultiWear", "ETO_B"])
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            table = screen.query_one("#log-table")
            seen["views"] = {}
            for name in VIEWS:
                seen["views"][name] = [
                    [table.get_cell_at(Coordinate(r, c)).plain
                     for c in range(len(table.columns))]
                    for r in range(min(4, table.row_count))
                ]
                await pilot.press("tab")
                await pilot.pause()
            seen["foot"] = screen.query_one("#log-foot").render().plain

            # And with no log at all, which is the state on a machine that has
            # never launched the game since installing this.
            blank = GameLogScreen(tmp / "not-here.txt")
            await app.push_screen(blank)
            await pilot.pause()
            seen["blank"] = blank.query_one("#log-notes").render().plain
        return seen

    shown = asyncio.run(run())
    order_rows = shown["views"]["order"]
    check(any("[B42]Bracket Mod" in row[1] for row in order_rows),
          f"log: a mod id full of brackets is printed, not parsed (got {order_rows})")
    check(shown["views"]["errors"][0][0] == "2",
          f"log: the errors view leads with the commonest "
          f"(got {shown['views']['errors'][0]})")
    check(any("loses" not in r and r for r in shown["views"]["overrides"][0]),
          "log: the overrides view has rows")
    check("mod(s) loaded" in shown["foot"], "log: the footer counts the session")
    check("NO GAME LOG" in shown["blank"],
          "log: with no log it explains itself rather than showing an empty table")


def test_apply_to_save(tmp: Path) -> None:
    """Writing the load order into a save. The only write in the whole tool.

    Build 42 keeps the order inside the save, in its own mods.txt, and that file
    is what the game reads: on a real machine its sequence matched the order the
    game logged, mod for mod. Exporting a text file next to the report never
    reached the game at all, which is the gap this closes.

    Two properties carry the safety, and both are checked below on a file shaped
    exactly like a real one, brace for brace.

      * it REORDERS and never adds or removes. The mod set of a save is part of
        that save; changing it can break a world with items in the ground. A
        different set is refused and the file is left byte for byte identical.
      * it copies the file first, and restoring is one call, because the game
        has no undo for this.
    """
    from pzmodmanager import savegame

    original = (
        "VERSION = 1,\n"
        "\n"
        "mods\n"
        "{\n"
        "    mod = ZombieBuddy,\n"
        "    mod = AlicesMultiWear,\n"
        "    mod = ETO_B,\n"
        "}\n"
        "\n"
        "maps\n"
        "{\n"
        "}\n"
    )
    folder = tmp / "Saves" / "Apocalypse" / "2026-08-30_03-32-14"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "mods.txt"
    target.write_text(original, encoding="utf-8")

    save = savegame.read_save(folder)
    check(save is not None and save.mods == ["ZombieBuddy", "AlicesMultiWear", "ETO_B"],
          f"save: the order is read out of the save (got {save.mods if save else None})")
    check(save.label == "Apocalypse/2026-08-30_03-32-14",
          "save: named the way the player would recognise it")
    check(savegame.read_save(tmp / "nowhere") is None,
          "save: a folder with no mods.txt is not a save, and does not raise")

    # A different set of mods must be refused, and change nothing.
    for wrong in (["ZombieBuddy", "AlicesMultiWear", "Ghost"],
                  ["ZombieBuddy", "AlicesMultiWear"]):
        proposal = savegame.plan(save, wrong)
        check(not proposal.safe, f"save: {wrong} is not the same set, so not safe")
        check(proposal.refusal, "save: and the refusal says which mods differ")
        done, message, backup = savegame.apply(save, wrong)
        check(not done and message.startswith("refused"),
              f"save: the write is refused (got {message})")
        check(backup is None, "save: with no backup taken, because nothing was risked")
        check(target.read_text(encoding="utf-8") == original,
              "save: and the file is byte for byte what it was")

    # A real reordering.
    wanted = ["ETO_B", "ZombieBuddy", "AlicesMultiWear"]
    proposal = savegame.plan(save, wanted)
    check(proposal.safe, "save: the same mods in a new order is safe")
    check(len(proposal.moves) == 3,
          f"save: every mod that moves is listed (got {proposal.moves})")
    done, message, backup = savegame.apply(save, wanted)
    check(done, f"save: the write goes through (got {message})")
    check(backup is not None and backup.is_file(),
          "save: a copy of the old file is kept beside it")
    check(backup.read_text(encoding="utf-8") == original,
          "save: and the copy is the file exactly as it was")

    after = savegame.read_save(folder)
    check(after.mods == wanted, f"save: the new order is in the file (got {after.mods})")
    check(sorted(after.mods) == sorted(save.mods),
          "save: with the same mods, none gained, none lost")
    text = target.read_text(encoding="utf-8")
    check(text.startswith("VERSION = 1,") and text.rstrip().endswith("}"),
          "save: the version line and the maps block survive untouched")
    check(text.count("mod = ") == 3 and "    mod = " in text,
          "save: the game's own indentation is kept, not reinvented")

    # Applying the same order twice must be a no-op, not a second backup.
    before_count = len(after.backups)
    done, message, backup = savegame.apply(after, wanted)
    check(done and "nothing to do" in message,
          f"save: re-applying the same order does nothing (got {message})")
    check(len(savegame.read_save(folder).backups) == before_count,
          "save: and does not pile up another backup")

    # The undo.
    restored, message = savegame.restore(after, after.backups[0])
    check(restored, f"save: a backup restores (got {message})")
    check(target.read_text(encoding="utf-8") == original,
          "save: back to exactly the original bytes")

    # And the screen, with Cancel first because a stray ENTER must do nothing.
    import asyncio

    from pzmodmanager.apply_screen import ApplyScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 40)) as pilot:
            screen = ApplyScreen(wanted)
            screen.saves = [savegame.read_save(folder)]
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.saves = [savegame.read_save(folder)]
            screen.redraw()
            await pilot.pause()
            seen["stage"] = screen.stage
            await pilot.press("enter")          # choose the save
            for _ in range(3):
                await pilot.pause()
            seen["after_pick"] = screen.stage
            choices = screen.query_one("#apply-choices")
            seen["first"] = choices.get_option_at_index(0).id
            seen["ids"] = [choices.get_option_at_index(i).id
                           for i in range(choices.option_count)]
            seen["highlighted"] = choices.highlighted
            seen["notes"] = screen.query_one("#apply-notes").render().plain
            await pilot.press("enter")          # takes Cancel, so writes nothing
            for _ in range(3):
                await pilot.pause()
            seen["file"] = target.read_text(encoding="utf-8")
        return seen

    shown = asyncio.run(run())
    check(shown["after_pick"] == "confirm",
          "save: choosing a save moves to the confirmation, it does not write")
    check(shown["first"] == "cancel" and shown["highlighted"] == 0,
          f"save: Cancel is first and highlighted (got {shown['ids']})")
    check("write" in shown["ids"], "save: writing is offered, just not by default")
    check("THIS WRITES INSIDE YOUR SAVE" in shown["notes"],
          "save: the warning is on the screen, not only in the docs")
    check("backup" in shown["notes"] or "copy" in shown["notes"],
          "save: and it says a copy is taken first")
    check(shown["file"] == original,
          "save: pressing ENTER on the default wrote nothing at all")

    # The common case, which the strict rule refuses and should not simply
    # stop at: a save lists the mods that were active last time it ran, and the
    # selection has moved on. Three variants switched off are still in the save;
    # one mod switched on is not. Narrowing keeps the save's set exactly.
    target.write_text(original, encoding="utf-8")
    save = savegame.read_save(folder)
    moved_on = ["ETO_B", "NewMod", "ZombieBuddy"]      # AlicesMultiWear dropped
    proposal = savegame.plan(save, moved_on)
    check(not proposal.safe, "narrow: the strict rule still refuses this")
    check(proposal.shared == 2, f"narrow: two mods are shared (got {proposal.shared})")

    fitted = proposal.fitted
    check(sorted(fitted) == sorted(save.mods),
          f"narrow: the set is exactly the save's, none added or lost (got {fitted})")
    check(fitted.index("ETO_B") < fitted.index("ZombieBuddy"),
          f"narrow: the shared mods take the order's sequence (got {fitted})")
    check(fitted[1] == "AlicesMultiWear",
          f"narrow: a mod the order never mentions keeps its exact place "
          f"(was index 1, got {fitted})")
    check("NewMod" not in fitted,
          "narrow: and a mod the save does not have is not smuggled in")

    done, message, backup = savegame.apply(save, fitted)
    check(done, f"narrow: the narrowed list writes through the same door ({message})")
    check(savegame.read_save(folder).mods == fitted, "narrow: and lands in the file")
    savegame.restore(savegame.read_save(folder), backup)

    # The screen must offer it rather than only saying no.
    async def refused() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 44)) as pilot:
            screen = ApplyScreen(moved_on)
            await app.push_screen(screen)
            await pilot.pause()
            screen.saves = [savegame.read_save(folder)]
            screen.redraw()
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(3):
                await pilot.pause()
            choices = screen.query_one("#apply-choices")
            seen["ids"] = [choices.get_option_at_index(i).id
                           for i in range(choices.option_count)]
            seen["highlighted"] = choices.highlighted
            seen["notes"] = screen.query_one("#apply-notes").render().plain
        return seen

    offered = asyncio.run(refused())
    check("fit" in offered["ids"],
          f"narrow: the screen offers it instead of stopping (got {offered['ids']})")
    check("write" not in offered["ids"],
          "narrow: while the strict write stays off the table")
    check(offered["ids"][0] == "cancel" and offered["highlighted"] == 0,
          "narrow: Cancel is still first and still highlighted")
    check("adding and removing none" in offered["notes"]
          or "gains none" in offered["notes"],
          "narrow: and the screen says the set does not change")


def test_order_pins(tmp: Path) -> None:
    """Ordering the user states by hand, because most of it is stated nowhere.

    require= says a mod must be PRESENT, not that it must come first, and the
    rest lives in prose on a Workshop page that no tool can safely act on. A pin
    is the user writing that ordering down once, in a form the sort can use.

    Three things have to hold. It must change the exported order, since a
    constraint that does not reach the file is decoration. It must survive a
    restart. And it must refuse a pin that closes a loop at the moment it is
    made, because accepting one produces an order nothing can satisfy and the
    only symptom appears somewhere else entirely.
    """
    import asyncio

    from pzmodmanager.manager_screen import ManageScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    pins_file = tmp / "pins" / "load-order-pins.json"
    # Alphabetical order is Alpha, Beta, Zeta, and nothing requires anything, so
    # any change to the sequence can only come from a pin.
    mods = [sel.ModRef(mod_id=n, name=n) for n in ("Alpha", "Beta", "Zeta")]

    check(store.load_pins(pins_file) == [],
          "pins: no file means no pins, not a crash")

    async def run(first_visit: bool) -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 34)) as pilot:
            screen = ManageScreen(mods, [], pins_path=pins_file)
            await app.push_screen(screen)
            await pilot.pause()
            screen.selected = {"alpha", "beta", "zeta"}
            screen.refresh_all()
            await pilot.pause()
            seen["loaded"] = list(screen.pins)
            seen["before"] = screen.resolved_order()[0]

            if first_visit:
                table = screen.query_one("#mods")
                # Zeta is the last row; pin it to load before Alpha, the first.
                table.move_cursor(row=2)
                await pilot.pause()
                await pilot.press("b")
                await pilot.pause()
                seen["half"] = screen.pin_anchor
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("b")
                for _ in range(3):
                    await pilot.pause()
                seen["pins"] = list(screen.pins)
                seen["after"] = screen.resolved_order()[0]
                seen["notice"] = screen.notice

                # Now the opposite pin, which would close the loop.
                await pilot.press("b")           # holds Alpha
                table.move_cursor(row=2)
                await pilot.pause()
                await pilot.press("b")           # Alpha before Zeta
                for _ in range(3):
                    await pilot.pause()
                seen["after_loop"] = list(screen.pins)
                seen["loop_notice"] = screen.notice
        return seen

    made = asyncio.run(run(True))
    again = asyncio.run(run(False))

    check(made["loaded"] == [], "pins: the first visit starts with none")
    check(made["before"] == ["Alpha", "Beta", "Zeta"],
          f"pins: unpinned, the order is the plain one (got {made['before']})")
    check(made["half"] == "Zeta",
          f"pins: the first press holds the mod, it does not act (got {made['half']})")
    check(made["pins"] == [("Zeta", "Alpha")],
          f"pins: the second press records the pair (got {made['pins']})")
    # The property, not a full sequence. Beta is under no constraint at all, so
    # where it lands is the tie break's business and asserting it would be
    # testing an accident. What a pin promises is exactly one thing.
    check(made["after"].index("Zeta") < made["after"].index("Alpha"),
          f"pins: and the order really changes (got {made['after']})")
    check(made["before"].index("Zeta") > made["before"].index("Alpha"),
          "pins: which is the opposite of where they sat without it")
    check(made["after_loop"] == [("Zeta", "Alpha")],
          f"pins: a pin that closes a loop is refused (got {made['after_loop']})")
    check("refused" in made["loop_notice"] and "loop" in made["loop_notice"],
          f"pins: and says so rather than failing quietly ({made['loop_notice']})")

    check(pins_file.is_file(), "pins: they are written to disk straight away")
    check(again["loaded"] == [("Zeta", "Alpha")],
          f"pins: and are still there next time (got {again['loaded']})")
    check(again["before"].index("Zeta") < again["before"].index("Alpha"),
          f"pins: shaping the order from the moment the screen opens "
          f"(got {again['before']})")

    # A pin naming something that is not here does nothing, and does not throw.
    by_key = sel.index_by_key(mods)
    keys = set(by_key)
    check(sel.pin_edges(by_key, keys, [("Ghost", "Alpha")]) == [],
          "pins: one naming an uninstalled mod is ignored, not an error")
    check(sel.pin_edges(by_key, {"alpha"}, [("Zeta", "Alpha")]) == [],
          "pins: one whose mods are not both selected does nothing")
    check(sel.pin_edges(by_key, keys, [("Alpha", "Alpha")]) == [],
          "pins: a mod cannot be pinned before itself")

    # The screen that lists them, driven for real. ENTER is the interesting part:
    # a DataTable with a row cursor swallows it and turns it into RowSelected, so
    # a Binding("enter", ...) never fires. That was a silent dead key.
    from pzmodmanager.manager_screen import PinsScreen

    async def review() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 40)) as pilot:
            listed = [("Zeta", "Alpha"), ("Beta", "[b]Ghost[/b]")]
            screen = PinsScreen(listed, sel.index_by_key(mods))
            app.push_screen(screen, lambda value: seen.update(kept=value))
            await pilot.pause()
            await pilot.pause()
            table = screen.query_one("#pins-table")
            seen["rows"] = table.row_count
            seen["hostile"] = table.get_cell_at(Coordinate(1, 2)).plain
            seen["status"] = table.get_cell_at(Coordinate(1, 3)).plain
            await pilot.press("enter")
            await pilot.pause()
            seen["marked"] = set(screen.dropped)
            await pilot.press("enter")
            await pilot.pause()
            seen["unmarked"] = set(screen.dropped)
            await pilot.press("enter")
            await pilot.press("s")
            for _ in range(3):
                await pilot.pause()
        return seen

    from textual.coordinate import Coordinate

    shown = asyncio.run(review())
    check(shown["rows"] == 2, "pins: the review screen lists them all")
    check(shown["hostile"] == "[b]Ghost[/b]",
          f"pins: a mod id full of brackets is printed, not parsed "
          f"(got {shown['hostile']!r})")
    check("not installed" in shown["status"],
          f"pins: a pin pointing at nothing says so ({shown['status']})")
    check(shown["marked"] == {0}, "pins: ENTER marks a pin for removal")
    check(shown["unmarked"] == set(), "pins: and pressing it again puts it back")
    check(shown["kept"] == [("Beta", "[b]Ghost[/b]")],
          f"pins: saving keeps exactly what was not marked (got {shown['kept']})")

    # "before everything" and "after everything" are real instructions, and
    # writing them as one pair per mod would be 240 lines that go stale the
    # moment a mod is added. The anchor stands for the rest of the selection.
    anchored = sel.topological_order(
        by_key, keys, pins=[("Zeta", sel.EVERYTHING), (sel.EVERYTHING, "Alpha")]
    )[0]
    check(anchored[0] == "Zeta" and anchored[-1] == "Alpha",
          f"pins: one anchor pin puts a mod first or last (got {anchored})")
    check(sorted(anchored) == sorted(mods_ids := [m.mod_id for m in mods
                                                 if m.key in keys]),
          f"pins: and loses nothing on the way ({len(anchored)} of {len(mods_ids)})")
    check(sel.pin_edges(by_key, keys, [(sel.EVERYTHING, sel.EVERYTHING)]) == [],
          "pins: an anchor against itself asks for nothing and gets nothing")

    # And a loop that does get in anyway must be reported, not hidden.
    problems = sel.validate(by_key, keys, [], pins=[("Alpha", "Beta"), ("Beta", "Alpha")])
    cycles = [p for p in problems if p.kind == "dependency_cycle"]
    check(len(cycles) == 1, "pins: a loop made of pins is reported as a problem")
    check("pins" in cycles[0].fix_hint,
          f"pins: and the advice names the pins ({cycles[0].fix_hint})")


def test_restore_scanned() -> None:
    """'r' puts back what the scan recorded, and says where that came from.

    The key used to be 'o', labelled "from the load order", which promised the
    wrong thing twice over. It never touched the order, only which mods are on;
    and what it restores is a snapshot frozen at scan time, so a list saved in
    game afterwards is not in it. Both of those have to be visible in the notice
    or the key is a trap.

    The empty case matters as much: with no mod list to read, the scan leaves
    every mod marked enabled, so restoring would tick all of them. That is a
    silent wrong answer, and it must refuse instead.
    """
    import asyncio

    from pzmodmanager.manager_screen import ManageScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    mods = [
        sel.ModRef(mod_id="Alpha", name="Alpha", was_enabled=True),
        sel.ModRef(mod_id="Beta", name="Beta", was_enabled=True),
        sel.ModRef(mod_id="Gamma", name="Gamma", was_enabled=False),
    ]
    real = store.StoredScan(
        has_order=True,
        order_source="/home/leo/Zomboid/Lua/saved_modlists.txt",
        saved_at=1_700_000_000.0,
    )
    orderless = store.StoredScan(has_order=False)

    async def run(scan) -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 30)) as pilot:
            screen = ManageScreen(mods, [], scan=scan)
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.press("n")          # clear it first, so 'r' has work to do
            await pilot.pause()
            seen["after_none"] = set(screen.selected)
            await pilot.press("r")
            for _ in range(3):
                await pilot.pause()
            seen["restored"] = set(screen.selected)
            seen["notice"] = screen.notice
        return seen

    good = asyncio.run(run(real))
    empty = asyncio.run(run(orderless))

    check(good["after_none"] == set(), "restore: 'n' really emptied the selection")
    check(good["restored"] == {"alpha", "beta"},
          f"restore: 'r' puts back exactly what the scan had on "
          f"(got {sorted(good['restored'])})")
    check("saved_modlists.txt" in good["notice"],
          f"restore: the notice names the file it came from ({good['notice']})")
    check("scanned" in good["notice"],
          f"restore: and when that was, so a newer in game list is not assumed "
          f"({good['notice']})")
    check(empty["restored"] == set(),
          "restore: with no mod list to read it changes nothing")
    check("nothing to restore" in empty["notice"],
          f"restore: and says why rather than ticking everything "
          f"({empty['notice']})")


def test_one_dependency_resolver() -> None:
    """The three places that ask "is this dependency installed" must agree.

    They did not, and that is the whole point of this test. The same question
    was answered by three functions written at different times: the scan's
    rules, the manager's problem panel, and the dependency closure behind the
    'd' key and the footer. A fix went into one, the other two carried on
    calling the same mod missing, and each round of that looked like a brand
    new bug to whoever was reading the screen.

    They now share resolve_requirement. This drives all three over the same mod
    set and demands the same answer from each, so a fourth copy cannot quietly
    appear and drift.
    """
    from pzmodmanager.analyzers import probable_typo
    from pzmodmanager.selection import dependency_closure, resolve_requirement

    refs = [
        sel.ModRef(mod_id="damnlib", workshop_id="3171167894"),
        sel.ModRef(mod_id="tsarslib", workshop_id="3402491515"),
        sel.ModRef(mod_id="KI5trailers", workshop_id="3330403100",
                   requires=["\\damnlib"]),
        sel.ModRef(mod_id="SVU3Core", workshop_id="3403490889",
                   requires=["\\tsarslib"]),
        sel.ModRef(mod_id="Orphan", workshop_id="1", requires=["ReallyGone"]),
    ]
    by_key = sel.index_by_key(refs)
    everything = set(by_key)

    check(resolve_requirement("damnlib", by_key) == "damnlib",
          "resolver: an exact id resolves to itself")
    check(resolve_requirement("\\damnlib", by_key) == "damnlib",
          "resolver: a stray backslash resolves to the real mod")
    check(resolve_requirement("ReallyGone", by_key) is None,
          "resolver: a genuinely absent mod resolves to nothing")
    check(resolve_requirement("", by_key) is None,
          "resolver: an empty require= entry is not a dependency")
    check(probable_typo("damnlib", by_key) is None,
          "resolver: an exact match is not reported as a typo")

    # 1. the closure, which feeds the 'd' key and the footer's "missing from disk"
    _closed, missing = dependency_closure(by_key, everything)
    check(missing == ["ReallyGone"],
          f"agree: the closure calls only the absent mod missing (got {missing})")
    pulled, _ = dependency_closure(by_key, everything - {"damnlib"})
    check("damnlib" in pulled,
          "agree: and pressing 'd' pulls in a mod named by a typo")

    # 2. the manager panel
    problems = sel.validate(by_key, everything)
    absent = [p for p in problems if p.kind == "dependency_not_installed"]
    check(len(absent) == 1 and "ReallyGone" in absent[0].message,
          f"agree: the panel calls only the same one missing (got {len(absent)})")
    check(sum(1 for p in problems if p.kind == "dependency_typo") == 2,
          "agree: and reports the two typos as typos")

    # 3. the scan's rules
    check(probable_typo("\\tsarslib", by_key) == "tsarslib",
          "agree: the scan's rules resolve it the same way")

    # 4. the sort, which is where the answer actually becomes a load order.
    #
    # This one was missed the first time, and the miss was invisible: the panel
    # said "order: resolved" because the panel asks validate, while the sort
    # compared the raw require= string and quietly built no edge at all. On a
    # real set that meant damnlib landing after the hundred vehicles that need
    # it, in an exported file nobody had reason to re-read.
    ordered, cycle = sel.topological_order(by_key, everything)
    check(not cycle, "agree: nothing here is circular, so the sort must succeed")
    check(ordered.index("damnlib") < ordered.index("KI5trailers"),
          f"agree: the sort puts a library before a mod that names it with a "
          f"typo (got {ordered})")
    check(ordered.index("tsarslib") < ordered.index("SVU3Core"),
          "agree: and does it for every such pair, not one lucky case")

    # The four answers, side by side, on every requirement in the set.
    for ref in refs:
        for required in ref.requires:
            resolved = resolve_requirement(required, by_key) is not None
            in_closure = required not in missing
            in_panel = not any(
                p.kind == "dependency_not_installed" and required in p.message
                for p in problems
            )
            target = resolve_requirement(required, by_key)
            in_sort = target is None or (
                ordered.index(by_key[target].mod_id) < ordered.index(ref.mod_id)
            )
            check(resolved == in_closure == in_panel and in_sort,
                  f"agree: all four say the same about {required!r}")


def test_ids_are_clean_before_anything_compares_them(tmp: Path) -> None:
    """The structural fix, and the reason it exists.

    Five separate places compared a mod id that an author typed by hand. Four of
    them remembered to see through a stray character and one did not, each time a
    different one, and each miss was silent and looked like a brand new bug:

      * the scan's rules called an installed library missing;
      * the manager's panel repeated it after the scan was fixed;
      * the dependency closure said "missing from disk" in the footer;
      * the SORT built no edge at all, so damnlib loaded after the hundred
        vehicles needing it, while the panel cheerfully said "order resolved";
      * the INCOMPATIBILITY check reported two mods as fine when the game itself
        refuses to load them together, which is the worst failure available: a
        false all clear.

    Remembering at each comparison is the thing that kept failing. So the
    cleaning moved to the parser, and the invariant below is what a sixth site
    now inherits for free: by the time anything can compare an id, there is
    nothing left to see through.
    """
    from pzmodmanager.modinfo import build_mod, clean_mod_id

    check(clean_mod_id("\\damnlib") == "damnlib", "clean: a leading backslash goes")
    check(clean_mod_id('  "TombBodyTex" ') == "TombBodyTex", "clean: so do quotes")
    check(clean_mod_id("[Ghost],") == "Ghost", "clean: brackets and trailing commas too")
    check(clean_mod_id("") == "" and clean_mod_id("\\\\") == "",
          "clean: junk on its own leaves nothing, rather than a phantom id")

    folder = tmp / "hostile" / "NastyMod"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mod.info").write_text(
        "id=NastyMod\n"
        "name=Nasty Mod\n"
        "require=\\damnlib\n"
        "require=  tsarslib  \n"
        'incompatible="TombBodyTex";[TombBodyCustom],\n',
        encoding="utf-8",
    )
    mod = build_mod(folder / "mod.info", "local", None)

    check(mod.requires == ["damnlib", "tsarslib"],
          f"clean: the parser hands out clean requirements (got {mod.requires})")
    check(mod.incompatible == ["TombBodyTex", "TombBodyCustom"],
          f"clean: and clean incompatibilities (got {mod.incompatible})")
    check(mod.requires_raw == ["\\damnlib", "tsarslib"],
          f"clean: while keeping what the author typed (got {mod.requires_raw})")

    junk = set("\\/\"'`[](){}<>,;:")
    for field_name in ("requires", "incompatible"):
        for value in getattr(mod, field_name):
            check(not (set(value) & junk) and value == value.strip(),
                  f"clean: nothing in {field_name} carries junk ({value!r})")

    # The whole point: a mod declaring incompatible=\Other must be reported
    # against Other. This is the case the game refuses to run and the tool
    # called fine, on a real machine, in a screenshot.
    faces = sel.ModRef(mod_id="SPNCCFaces", incompatible_raw=["\\TombBodyTex"],
                       incompatible=["TombBodyTex"])
    tex = sel.ModRef(mod_id="TombBodyTex")
    by_key = sel.index_by_key([faces, tex])
    clashes = [
        p for p in sel.validate(by_key, set(by_key), [])
        if p.kind == "declared_incompatibility"
    ]
    check(len(clashes) == 1,
          f"clean: a backslashed incompatible= is reported (got {len(clashes)})")
    check(clashes[0].severity is Severity.CRITICAL,
          "clean: as critical, because the game will not load the pair")

    # And the scan's own rule, which is a different function over different data.
    from pzmodmanager.analyzers import AnalysisContext, rule_declared_incompatibility
    from pzmodmanager.models import Mod

    a = Mod(mod_id="SPNCCFaces", name="Faces", root=folder,
            incompatible=["TombBodyTex"], incompatible_raw=["\\TombBodyTex"])
    b = Mod(mod_id="TombBodyTex", name="Tex", root=folder)
    ctx = AnalysisContext(mods=[a, b])
    found = rule_declared_incompatibility(ctx)
    check(len(found) == 1 and "TombBodyTex" in found[0].evidence,
          f"clean: the scan's rule agrees with the manager's (got {found})")


def test_validate_knows_typos() -> None:
    """The manager's own dependency check, which is NOT the scan's.

    This is the lesson of the session written down. probable_typo was added to
    analyzers.py, the scan's rules, and the manager kept reporting the same
    dependency as missing because validate() carries a second, independent copy
    of that check. Two code paths answering the same question is a bug waiting
    for one of them to be fixed alone, so both are exercised here.

    Taken from a real machine: Standardized Vehicle Upgrades declares
    require=\\tsarslib, and Tsar's Common Library is installed, declaring
    id=tsarslib. A stray backslash produced a critical about a mod sitting on
    disk.
    """
    refs = [
        sel.ModRef(mod_id="TsarsLib", workshop_id="3402491515"),
        sel.ModRef(mod_id="SVU3Core", workshop_id="3403490889",
                   requires=["\\tsarslib"]),
        sel.ModRef(mod_id="SVU3V", workshop_id="3304582091",
                   requires=["\\tsarslib", "\\SVU3Core"]),
        sel.ModRef(mod_id="Needy", workshop_id="9", requires=["ReallyAbsent"]),
    ]
    by_key = sel.index_by_key(refs)

    problems = sel.validate(by_key, {"tsarslib", "svu3core", "svu3v"})
    kinds = {p.kind for p in problems}
    check("dependency_not_installed" not in kinds,
          "validate: a typo is no longer reported as a missing mod")
    check(kinds == {"dependency_typo"},
          f"validate: every one of them is a typo instead (got {kinds})")
    check(all(p.severity is Severity.LOW for p in problems),
          "validate: and none of them is critical any more")
    check(any("TsarsLib" in p.message for p in problems),
          "validate: the message names the mod actually installed")

    # The typo target being installed but unticked is a selection problem.
    problems = sel.validate(by_key, {"svu3core"})
    kinds = {p.kind for p in problems}
    check("dependency_not_selected" in kinds,
          "validate: an unselected typo target is a selection problem")
    check(all(p.kind != "dependency_typo" for p in problems),
          "validate: and not dismissed as a harmless typo")

    # A genuinely absent mod must stay critical. This is the check that stops
    # the typo rule from quietly swallowing real problems.
    problems = sel.validate(by_key, {"needy"})
    check(any(p.kind == "dependency_not_installed" and p.severity is Severity.CRITICAL
              for p in problems),
          "validate: a genuinely missing mod is still critical")

    # The scan's rules and the manager must agree about the same mod set.
    from pzmodmanager.analyzers import probable_typo

    check(probable_typo("\\tsarslib", by_key) == "TsarsLib",
          "validate: the scan's rules and the manager share one typo check")


def test_multi_mod_items() -> None:
    """One Workshop item can install several mods, and that changes unsubscribing.

    "42.20 | Every Texture Optimized" installs ETO_B and ETO_P, two variants you
    choose between. Enabling is per mod. Subscribing is per ITEM, and Steam
    cannot remove part of one.

    The bug this guards was real and destructive: deselecting ETO_P made the
    manager offer to unsubscribe from the item, which would have deleted ETO_B,
    the variant explicitly kept, while the confirmation screen listed only
    ETO_P. The tool would have destroyed something you told it to keep.
    """
    from pzmodmanager.selection import unsubscribe_plan

    refs = [
        sel.ModRef(mod_id="ETO_B", name="Well Balanced", workshop_id="3119788162"),
        sel.ModRef(mod_id="ETO_P", name="Max Performance", workshop_id="3119788162"),
        sel.ModRef(mod_id="Solo", name="Solo", workshop_id="999"),
        sel.ModRef(mod_id="Local", name="Local", workshop_id=None),
    ]
    by_key = sel.index_by_key(refs)

    safe, held = unsubscribe_plan(by_key, {"eto_b", "local"})
    check([t.workshop_id for t in safe] == ["999"],
          "multi mod: only the item with nothing kept can be unsubscribed")
    check(len(held) == 1 and held[0].workshop_id == "3119788162",
          "multi mod: the shared item is held back")
    check(held[0].keeping == ["ETO_B"] and held[0].dropping == ["ETO_P"],
          "multi mod: and it says which half you kept and which you dropped")
    kept_ids = {r.workshop_id for k, r in by_key.items() if k in {"eto_b", "local"}}
    check(not (kept_ids & {t.workshop_id for t in safe}),
          "multi mod: no mod you kept can be removed by what would be unsubscribed")

    safe, held = unsubscribe_plan(by_key, {"local"})
    check(sorted(t.workshop_id for t in safe) == ["3119788162", "999"],
          "multi mod: dropping every variant releases the whole item")
    check(safe[0].mod_ids == ["ETO_B", "ETO_P"],
          "multi mod: and the confirmation names both mods it would remove")
    check(not held, "multi mod: nothing is held back then")

    safe, held = unsubscribe_plan(by_key, set(by_key))
    check(not safe and not held,
          "multi mod: keeping everything unsubscribes from nothing")

    # A mod installed by hand has no item behind it and must never appear.
    check(all(t.workshop_id for t in unsubscribe_plan(by_key, set())[0]),
          "multi mod: a hand installed mod is never an unsubscribe target")

    # The scanner has to see both mods in the first place.
    from pzmodmanager.steam import mod_ids_in_description

    found = mod_ids_in_description(
        "Workshop ID: 3119788162\nMod ID: ETO_B\nMod ID: ETO_P\n"
    )
    check(found == ["ETO_B", "ETO_P"],
          "multi mod: several Mod ID lines are all read from the description")


def _unsubscribe_screen_with(mods):
    """An unsubscribe screen showing both a target and a held back item."""
    from pzmodmanager.unsubscribe_screen import UnsubscribeScreen

    by_key = sel.index_by_key(mods)
    all_dropped, _ = sel.unsubscribe_plan(by_key, set())
    _, held = sel.unsubscribe_plan(by_key, {mods[-1].key})
    return lambda: UnsubscribeScreen(all_dropped, Path("/tmp/none"), held=held)


def test_hostile_text_everywhere() -> None:
    """Every screen, fed text full of square brackets, must survive.

    This bug class has now bitten three times, in three disguises, and each
    disguise looked like a different bug:

      * a ticked row rendered as nothing, because "[x]" is a valid style tag
        and Rich swallowed it silently;
      * a Workshop description took the whole screen down with a MarkupError,
        because Steam descriptions are full of BBCode like [B]...[/B];
      * a mod named with brackets crashed the table itself, because DataTable
        runs plain strings through Text.from_markup inside its own drawing.

    Two widgets parse markup and they look nothing alike in the code, so the
    fix is at the widget rather than at each of the hundred call sites: Plain
    for every Static, cell() for every table cell. This test is what keeps that
    true, by driving all six screens with text designed to break them.
    """
    import asyncio

    from pzmodmanager.browse_screen import BrowseScreen, SubscribeScreen
    from pzmodmanager.manager_screen import ManageScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.settings_screen import SettingsScreen
    from pzmodmanager.steam import WorkshopItem
    from pzmodmanager.tui import ModCheckApp, Plain, ResultsScreen, cell
    from pzmodmanager.unsubscribe_screen import UnsubscribeScreen

    from rich.text import Text
    from textual.widgets import Static

    check(issubclass(Plain, Static), "markup: Plain is a Static, so queries still match")
    check(isinstance(cell("[x]"), Text), "markup: cell() hands back a Text object")
    check(str(cell("[B]bold[/B]")) == "[B]bold[/B]",
          "markup: and the brackets survive it intact")

    # No Static may be built raw: the whole point is that no call site has to
    # remember. Reading the source is the only way to check the rule holds.
    import re

    for name in ("tui", "browse_screen", "manager_screen",
                 "settings_screen", "unsubscribe_screen"):
        module = __import__(f"pzmodmanager.{name}", fromlist=["x"])
        source = Path(module.__file__).read_text(encoding="utf-8")
        raw = re.findall(r"(?<![\w.])Static\((?!\s*\))", source)
        check(not raw, f"markup: {name}.py builds no bare Static ({len(raw)} found)")
        # Markup written into a string is the other half of the same mistake:
        # with parsing off it shows as literal "[b]" on screen, which is how
        # the manager's headings started printing their own tags. Read the
        # syntax tree rather than the raw text, so an index like found[i] and a
        # docstring explaining this rule are not mistaken for style tags.
        import ast

        tree = ast.parse(source)
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                if isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        tags: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                tags += re.findall(r"\[/?(?:b|i|dim|bold|red|green|yellow)\]", node.value)
        check(not tags, f"markup: {name}.py spells no style tags into text ({tags[:3]})")

    BAD = "[B]bold[/B] [unclosed [/] [] [x] [#f00] [/color]"
    mods = [
        sel.ModRef(mod_id="Mod[1]", name=BAD, workshop_id="111",
                   requires=["[dep]"], incompatible=[BAD]),
        sel.ModRef(mod_id="Plain", name="Plain", workshop_id="222"),
    ]
    finding = Finding(rule="r", severity=Severity.HIGH, title=BAD, detail=BAD,
                      mods=["Mod[1]"], evidence=[BAD], advice=BAD)
    items = [
        WorkshopItem(workshop_id="1", title=BAD, description=BAD, tags=["Build 42"]),
        WorkshopItem(workshop_id="2", title="[/]", description="[not closed", tags=[]),
    ]

    async def drive(make) -> str:
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        try:
            async with app.run_test(size=(120, 44)) as pilot:
                screen = make()
                await app.push_screen(screen)
                await pilot.pause()
                if isinstance(screen, BrowseScreen):
                    screen.lookup_finished(items, [], {})
                    await pilot.pause()
                for _ in range(4):
                    await pilot.press("down")
                    await pilot.pause()
                for key in ("x", "space"):
                    await pilot.press(key)
                    await pilot.pause()
            return ""
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"

    screens = [
        ("Add mods", lambda: BrowseScreen(installed_mods=mods, build="42")),
        ("the manager", lambda: ManageScreen(mods, [finding])),
        ("the results", lambda: ResultsScreen(
            store.StoredScan(mods=mods, findings=[finding], mod_count=2))),
        ("settings", lambda: SettingsScreen(Settings(steam_sdk="C:\\a [b]\\c"))),
        ("subscribe", lambda: SubscribeScreen(items, Path("/tmp/none"))),
        ("unsubscribe", _unsubscribe_screen_with(mods)),
    ]
    for label, make in screens:
        failure = asyncio.run(drive(make))
        check(not failure, f"markup: {label} survives text full of brackets ({failure})")


def test_menu_greying() -> None:
    """An entry is offered only when there is something behind it.

    The trap: a scan that found no mods is saved exactly like one that found a
    hundred, so a file existing is not the same as there being anything to open.
    Judging by the file left the menu offering "Last results" and "Manage mods"
    onto empty screens.
    """
    import asyncio

    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    check(store.StoredScan().has_results is False,
          "menu: an empty scan reports nothing to show")
    check(store.StoredScan(mods=[sel.ModRef(mod_id="A")]).has_results is True,
          "menu: a scan with mods reports something to show")
    check(store.StoredScan(mod_count=9).has_mods is False,
          "menu: a count with no mod records is still nothing to manage")

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(110, 44)) as pilot:
            await pilot.pause()

            def state() -> dict:
                menu = app.screen.query_one("#menu")
                return {o.id: bool(o.disabled) for o in menu._options}

            for label, stored in (
                ("none", None),
                ("empty", store.StoredScan(mod_count=0, mods=[], findings=[])),
                ("full", store.StoredScan(mod_count=1, mods=[sel.ModRef(mod_id="A")])),
            ):
                app.stored = stored
                app.screen.refresh_menu()
                await pilot.pause()
                seen[label] = state()
                seen[label + "_footer"] = app.screen._footer_text()
        return seen

    seen = asyncio.run(run())

    check(seen["none"]["results"] and seen["none"]["manage"],
          "menu: with no scan, Results and Manage mods are greyed")
    check(not seen["none"]["browse"] and not seen["none"]["scan"],
          "menu: Add mods and Scan stay available with no scan")
    check(seen["empty"]["results"] and seen["empty"]["manage"],
          "menu: a scan that found nothing greys them too")
    check("nothing to open" in seen["empty_footer"],
          "menu: and the footer says why, instead of leaving you guessing")
    check(not seen["full"]["results"] and not seen["full"]["manage"],
          "menu: a scan with mods opens both")


def test_reset_actions(tmp: Path) -> None:
    """Clearing the tool's own state, twice on purpose.

    The regression here is subtle and was found by driving the real screen. The
    first ENTER armed the action and redrew the table; redrawing clears the rows,
    which snaps the cursor to the first one, which fires a highlight for a
    different row, which disarmed what had just been armed. Every press ended up
    one out of step. Arming now rewrites a single cell instead.
    """
    import asyncio
    import shutil

    from pzmodmanager.settings import Settings
    from pzmodmanager.settings_screen import ROWS, SettingsScreen
    from pzmodmanager.tui import ModCheckApp

    data = tmp / "resetstate"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    names = [name for name, _l, _k, _h in ROWS]
    check("reset_scan" in names and "reset_all" in names,
          "reset: the settings screen offers the reset actions")

    previous = store._data_dir
    store.set_data_dir(data)
    try:
        store.save(store.StoredScan(mod_count=7), store.default_store_path())
        store.save_selection(["A"], store.default_selection_path())
        store.default_steam_cache_path().write_text("{}", encoding="utf-8")
        (data / "previews").mkdir(exist_ok=True)
        (data / "previews" / "a.img").write_bytes(b"x")

        async def run() -> dict:
            seen: dict = {}
            live = Settings(build="42.15", data_dir=str(data))
            app = ModCheckApp(ScanOptions(), settings=live, cli_overrides=set())
            app.stored = store.load(store.default_store_path())
            async with app.run_test(size=(110, 44)) as pilot:
                screen = SettingsScreen(live, data / "settings.json")
                await app.push_screen(screen)
                await pilot.pause()
                table = screen.query_one("#rows")

                table.move_cursor(row=names.index("reset_scan"))
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                seen["armed"] = screen.armed
                seen["file_after_one"] = store.default_store_path().is_file()

                await pilot.press("enter")
                await pilot.pause()
                seen["file_after_two"] = store.default_store_path().is_file()
                seen["app_forgot"] = app.stored is None

                # Arming then walking away must leave the action undone.
                table.move_cursor(row=names.index("reset_selection"))
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                seen["armed_second"] = screen.armed
                table.move_cursor(row=names.index("reset_cache"))
                await pilot.pause()
                seen["disarmed"] = screen.armed
                seen["selection_kept"] = store.default_selection_path().is_file()

                table.move_cursor(row=names.index("reset_all"))
                await pilot.pause()
                await pilot.press("enter")
                await pilot.press("enter")
                await pilot.pause()
                seen["build_after_all"] = screen.settings.build
                seen["previews_left"] = len(list((data / "previews").glob("*.img")))
            return seen

        seen = asyncio.run(run())
    finally:
        store.set_data_dir(previous)

    check(seen.get("armed") == "reset_scan",
          "reset: one ENTER arms the action and it stays armed")
    check(seen.get("file_after_one") is True,
          "reset: and one ENTER alone deletes nothing")
    check(seen.get("file_after_two") is False,
          "reset: the second ENTER does it")
    check(seen.get("app_forgot") is True,
          "reset: the menu is told too, so it stops offering Last results")
    check(seen.get("armed_second") == "reset_selection",
          "reset: a second action arms the same way")
    check(seen.get("disarmed") is None,
          "reset: moving to another row disarms it")
    check(seen.get("selection_kept") is True,
          "reset: and the action it was armed for did not happen")
    check(seen.get("build_after_all") == "42",
          "reset: 'everything' puts the settings back to their defaults")
    check(seen.get("previews_left") == 0,
          "reset: and clears the cached preview images")


def test_browse_concerns() -> None:
    """What can honestly be said about an item before a single byte is downloaded.

    Two sources with very different standing. The Steam build tag is picked from
    a fixed list and is trustworthy. Everything read out of the description is
    prose an author typed, and is treated as a hint, never as a finding.
    """
    from pzmodmanager.browse_screen import BrowseScreen
    from pzmodmanager.steam import WorkshopItem, build_tags, requires_in_description

    check(build_tags(["Build 42", "Clothing/Armor"]) == ["42"],
          "tags: the build is read from the Steam tags")
    check(build_tags(["Build 41", "Build 42"]) == ["41", "42"],
          "tags: a mod supporting both is recognised as both")
    check(build_tags(["Items"]) == [],
          "tags: no build tag yields nothing, not a guess")
    check(requires_in_description("Required mods: A, B and C") == ["A", "B", "C"],
          "prose: a requires line is split into names")
    check(requires_in_description("This mod requires nothing. Enjoy!") == [],
          "prose: 'requires nothing' is not read as a dependency")

    installed = [
        sel.ModRef(mod_id="Brita_Armor", workshop_id="111"),
        sel.ModRef(mod_id="NeedsFramework", workshop_id="222",
                   requires=["MagicFramework"]),
        sel.ModRef(mod_id="PickyMod", workshop_id="333", incompatible=["RivalMod"]),
    ]
    screen = BrowseScreen(installed_mods=installed, build="42")

    def kinds(item):
        return [k for k, _t in screen.concerns(item)]

    def texts(item):
        return " ".join(t for _k, t in screen.concerns(item))

    clean = WorkshopItem(workshop_id="901", title="Nice", tags=["Build 42"],
                         description="Mod ID: NiceMod")
    check(screen.worst(clean) == "",
          "concerns: a plain Build 42 mod raises nothing")

    old = WorkshopItem(workshop_id="900", title="Old", tags=["Build 41"])
    check(screen.worst(old) == "conflict", "concerns: a Build 41 only mod is a conflict")
    check("Build 41" in texts(old), "concerns: and the message names the build")

    untagged = WorkshopItem(workshop_id="902", title="Mystery", tags=["Items"])
    check(screen.worst(untagged) == "warning",
          "concerns: no build tag is a warning, not a conflict")

    duplicate = WorkshopItem(workshop_id="903", title="Reupload", tags=["Build 42"],
                             description="Mod ID: Brita_Armor")
    check(screen.worst(duplicate) == "conflict",
          "concerns: an item claiming an installed mod id is a conflict")
    check("111" in texts(duplicate),
          "concerns: and it names the Workshop item that already has that id")

    fills = WorkshopItem(workshop_id="904", title="Framework", tags=["Build 42"],
                         description="Mod ID: MagicFramework")
    check("note" in kinds(fills) and screen.worst(fills) == "",
          "concerns: filling a missing dependency is good news, not a problem")
    check("NeedsFramework" in texts(fills),
          "concerns: and it says which of your mods was waiting for it")

    refused = WorkshopItem(workshop_id="905", title="Rival", tags=["Build 42"],
                           description="Mod ID: RivalMod")
    check(screen.worst(refused) == "conflict",
          "concerns: an installed mod declaring this incompatible is a conflict")

    needy = WorkshopItem(workshop_id="906", title="Addon", tags=["Build 42"],
                         description="Mod ID: Addon\nRequires: SomeLibrary")
    check(screen.worst(needy) == "warning",
          "concerns: a dependency read from prose is a warning, since prose lies")

    gone = WorkshopItem(workshop_id="907", title="Gone", tags=["Build 42"], missing=True)
    check(screen.worst(gone) == "conflict",
          "concerns: an item removed from the Workshop is a conflict")

    # Targeting 41 must not turn every 41 mod into a conflict.
    on41 = BrowseScreen(installed_mods=installed, build="41")
    check(on41.worst(old) == "", "concerns: a Build 41 mod is fine when you target 41")
    check(on41.worst(clean) == "conflict",
          "concerns: and a Build 42 mod is the problem instead")


def test_browse_screen_live() -> None:
    """Mount the screen for real and press the keys, rather than calling methods.

    Two things only a mounted screen can catch. A CSS property Textual does not
    know kills the app at startup and nothing before this notices. And a key
    binding does not fire while a text box has focus, because a letter typed into
    an Input is text, not a shortcut: that is why pressing 's' to search only put
    an 's' in the box. ENTER decides now, by what you typed.
    """
    import asyncio

    from pzmodmanager import browse_screen as bs
    from pzmodmanager.browse_screen import BrowseScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.steam import WorkshopItem
    from pzmodmanager.tui import ModCheckApp

    async def run() -> dict:
        seen: dict = {}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(120, 40)) as pilot:
            screen = BrowseScreen()
            await app.push_screen(screen)
            await pilot.pause()
            seen["mounted"] = True

            table = screen.query_one("#results")
            seen["h_scrollbar"] = table.styles.scrollbar_size_horizontal
            seen["bar_colour"] = str(table.styles.scrollbar_color)
            seen["bar_active"] = str(table.styles.scrollbar_color_active)

            first = WorkshopItem(workshop_id="1", title="Looked up first",
                                 tags=["Build 42"])
            screen.lookup_finished([first], [], {})
            await pilot.pause()
            item = WorkshopItem(
                workshop_id="3783094058", title="Vanilla Outfits Expanded",
                file_size=6_900_000, description="Mod ID: VanillaOutfitsExpanded",
                tags=["Build 42"],
            )
            screen.lookup_finished([item], [], {"3783094058": None})
            await pilot.pause()
            seen["order"] = [i.workshop_id for i in screen.results]
            seen["cursor_row"] = screen.query_one("#results").cursor_row
            seen["focus_after_lookup"] = app.focused.id

            await pilot.press("space")
            await pilot.pause()
            seen["marked_space"] = set(screen.chosen)
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            seen["marked_x"] = set(screen.chosen)
            # What the cell actually draws, not what was handed to it. A raw
            # "[x]" is stored fine and renders as nothing, so checking the
            # stored value would have passed while the column stayed empty.
            from io import StringIO

            from rich.console import Console
            from textual.coordinate import Coordinate

            def drawn(value) -> str:
                console = Console(file=StringIO(), width=12, no_color=True)
                console.print(value)
                return console.file.getvalue().strip()

            cells = screen.query_one("#results")
            seen["ticked_cell"] = drawn(cells.get_cell_at(Coordinate(0, 0)))
            await pilot.press("X")
            await pilot.pause()
            seen["after_upper_x"] = set(screen.chosen)

            opened: list[str] = []
            real_open, bs.webbrowser.open = bs.webbrowser.open, opened.append
            try:
                screen.query_one("#find").focus()
                await pilot.press(*"brita")
                await pilot.press("enter")
                await pilot.pause()
            finally:
                bs.webbrowser.open = real_open
            seen["searched"] = opened[0] if opened else ""
        return seen

    seen = asyncio.run(run())

    check(seen.get("mounted") is True,
          "live browse: the screen mounts, so its CSS is valid")
    check(seen.get("h_scrollbar") == 0,
          "live browse: the results table has no horizontal scrollbar")
    check("74" in seen.get("bar_colour", ""),
          f"live browse: scrollbars are grey, not the theme blue (got {seen.get('bar_colour')})")
    check("138" in seen.get("bar_active", ""),
          "live browse: a dragged scrollbar stays grey too")
    check(seen.get("focus_after_lookup") == "results",
          "live browse: focus lands on the results, so SPACE reaches the screen")
    check(seen.get("marked_space") == {"3783094058"},
          "live browse: SPACE marks the highlighted row")
    check(seen.get("marked_x") == {"3783094058"},
          "live browse: 'x' marks it too")
    check(seen.get("after_upper_x") == set(),
          "live browse: 'X' works as well, so caps lock is not a wall")
    check(seen.get("ticked_cell") == "[x]",
          f"live browse: a ticked row really draws [x] (drew {seen.get('ticked_cell')!r})")
    check("searchtext=brita" in seen.get("searched", ""),
          "live browse: ENTER on a name searches Steam instead of doing nothing")
    check(seen.get("order") == ["3783094058", "1"],
          "live browse: the newest lookup goes to the top of the list")
    check(seen.get("cursor_row") == 0,
          "live browse: and the cursor lands on it, so the panel shows it")


def test_stored_subscriptions(tmp: Path) -> None:
    """The saved scan must remember what Steam said, including having said nothing."""
    path = tmp / "sub.json"
    scan = store.StoredScan(mod_count=3, subscribed_ids=["1", "2"])
    store.save(scan, path)
    back = store.load(path)
    check(back is not None and back.subscribed_ids == ["1", "2"],
          "store: the subscription list survives a round trip")

    silent = store.StoredScan(mod_count=3, subscribed_ids=None)
    store.save(silent, tmp / "none.json")
    back = store.load(tmp / "none.json")
    check(back is not None and back.subscribed_ids is None,
          "store: never having asked Steam stays None, not an empty list")

    empty = store.StoredScan(mod_count=3, subscribed_ids=[])
    store.save(empty, tmp / "empty.json")
    back = store.load(tmp / "empty.json")
    check(back is not None and back.subscribed_ids == [],
          "store: subscribed to nothing is kept apart from never having asked")


def test_data_dir(tmp: Path) -> None:
    """The state files can be moved, and settings.json deliberately cannot.

    Anything that says where files go has to be findable without knowing where
    files go. settings.json is that file, so it stays in the per-user location
    while everything it points at is free to move. If this ever changes, the tool
    will have nowhere to read its own configuration from.
    """
    from pzmodmanager.logs import default_log_path
    from pzmodmanager.settings import Settings, default_settings_path

    fixed_settings = default_settings_path()
    target = tmp / "chosen"

    store.set_data_dir(target)
    try:
        check(store.state_dir() == target, "data dir: the state folder moves")
        check(store.default_store_path().parent == target,
              "data dir: the saved scan follows")
        check(store.default_selection_path().parent == target,
              "data dir: the selection follows")
        check(store.default_steam_cache_path().parent == target,
              "data dir: the Workshop cache follows")
        check(default_log_path().parent == target, "data dir: the log follows")
        check(default_settings_path() == fixed_settings,
              "data dir: settings.json stays put, or nothing could find it")
        check(store.config_dir() != target,
              "data dir: the config folder is not the data folder")

        # A round trip through the file, since this is what the screen writes.
        saved = Settings(data_dir=str(target))
        path = tmp / "s.json"
        saved.save(path)
        check(Settings.load(path).data_dir_path == target,
              "data dir: the setting survives being saved and read back")
    finally:
        # A module level override would leak into every later test.
        store.set_data_dir(None)

    check(store.state_dir() != target, "data dir: clearing it restores the default")
    check(Settings().data_dir == "", "data dir: empty by default, nothing moves")


def test_settings_are_live() -> None:
    """Nothing the settings screen can change may be cached at launch.

    The regression this guards: both the scan options and the Steam library path
    were read once in __init__. Changing the SDK path on the settings screen then
    did nothing at all, the manager still reported "library not found" on a path
    that was right, and a rescan after any settings change silently reran the old
    values. The interface said "run a new scan for this to take effect", and that
    was untrue.
    """
    from pzmodmanager.settings import Settings
    from pzmodmanager.tui import ModCheckApp

    live = Settings(steam_sdk="", build="42", use_steam=True)
    app = ModCheckApp(ScanOptions(build="42"), settings=live, cli_overrides=set())

    check(app.steam_sdk is None, "live: no SDK reported before one is set")
    live.steam_sdk = str(Path("/somewhere/win64/steam_api64.dll"))
    check(app.steam_sdk is not None and "win64" in str(app.steam_sdk),
          "live: setting the SDK path is visible immediately, with no relaunch")

    check(app.scan_options.build == "42", "live: the scan starts from the saved build")
    live.build = "42.15"
    check(app.scan_options.build == "42.15",
          "live: changing the build reaches the next scan")
    live.use_steam = False
    check(app.scan_options.use_steam is False,
          "live: toggling the Workshop lookup reaches the next scan")
    check(app.scan_options.steam_sdk is not None,
          "live: the scan gets the SDK path too, for the subscription check")

    # What was typed on the command line must still win for the whole session.
    pinned = ModCheckApp(
        ScanOptions(build="41", steam_sdk=Path("/from/cli")),
        settings=Settings(build="42", steam_sdk="/from/settings"),
        steam_sdk=Path("/from/cli"),
        cli_overrides={"build", "steam_sdk"},
    )
    check(pinned.scan_options.build == "41",
          "live: --build still beats the saved setting")
    check(str(pinned.steam_sdk) == str(Path("/from/cli")),
          "live: --steam-sdk still beats the saved setting")
    check(pinned.scan_options.use_defaults is True,
          "live: an option you did not type is still read from the settings")


def test_nothing_is_blue() -> None:
    """The interface is monochrome, and Textual's default theme is not.

    $primary is #0178D4 and it surfaces wherever a widget is focused: the border
    of an OptionList, of an Input, selection highlights, progress bars, and in
    widgets this project never names in its own stylesheet. Chasing it one
    selector at a time is the same losing game the scrollbars were, where seven
    separate properties each had to be set or the accent showed through.

    So the theme itself is replaced, once. This checks the replacement is in
    force and that every border actually drawn on every screen is a colour the
    interface uses.
    """
    import asyncio

    from pzmodmanager.apply_screen import ApplyScreen
    from pzmodmanager.gamelog_screen import GameLogScreen
    from pzmodmanager.manager_screen import ManageScreen, PinsScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.settings_screen import SettingsScreen
    from pzmodmanager.tui import MONOCHROME, ModCheckApp

    # Black, white, and the greys the stylesheet uses. Nothing else.
    allowed = {(0, 0, 0), (255, 255, 255), (180, 180, 180),
               (74, 74, 74), (106, 106, 106), (138, 138, 138), (26, 26, 26)}

    async def run() -> dict:
        seen: dict = {"stray": [], "theme": ""}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(120, 44)) as pilot:
            seen["theme"] = app.theme
            seen["blue"] = [
                key for key, value in app.theme_variables.items()
                if isinstance(value, str) and value.lower().startswith("#0178d4")
            ]
            mods = [sel.ModRef(mod_id=f"M{i}", name=f"Mod {i}") for i in range(5)]
            screens = [
                ManageScreen(mods, []),
                PinsScreen([("A", "B")], sel.index_by_key(mods)),
                ApplyScreen(["M0"]),
                GameLogScreen(Path("/nowhere/console.txt")),
                SettingsScreen(Settings(), None),
            ]
            for screen in screens:
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.pause()
                for widget in screen.query("*"):
                    for edge in (widget.styles.border_top, widget.styles.border_left):
                        # An empty border type means nothing is drawn, whatever
                        # placeholder colour sits beside it.
                        if not edge or not edge[0] or edge[1] is None:
                            continue
                        if tuple(edge[1].rgb) not in allowed:
                            seen["stray"].append(
                                (type(screen).__name__, widget.id or type(widget).__name__,
                                 edge[0], edge[1].hex)
                            )
                app.pop_screen()
                await pilot.pause()
        return seen

    shown = asyncio.run(run())
    check(shown["theme"] == MONOCHROME.name,
          f"colour: the monochrome theme is the one in force (got {shown['theme']})")
    check(not shown["blue"],
          f"colour: no theme variable still holds the default blue "
          f"(got {shown['blue']})")
    check(not shown["stray"],
          f"colour: every border drawn on every screen is monochrome "
          f"(got {shown['stray'][:4]})")


def test_key_hints_match_bindings() -> None:
    """Every key named in the on screen help must be a key that is bound.

    They had drifted: the help said 'Q', 'R', 'M' and 'D' while the bindings were
    lower case, so the keys shown did nothing if you took them literally.
    """
    import re

    from pzmodmanager import manager_screen, settings_screen, tui, unsubscribe_screen

    for module in (tui, manager_screen, settings_screen, unsubscribe_screen):
        source = Path(module.__file__).read_text(encoding="utf-8")
        bound = set()
        for keys in re.findall(r'Binding\("([^"]+)"', source):
            bound.update(k.strip() for k in keys.split(","))
        # Drop f-string expressions first. A pluralisation like {'S' if count > 1
        # else ''} is quoted the same way a key hint is, and is not one.
        prose = re.sub(r"\{[^{}]*\}", "", source)
        # A key hint is 'x' followed by what it does, or spelled out as press 'x'.
        shown = set(re.findall(r"[Pp]ress '([A-Za-z0-9])'", prose))
        shown |= set(re.findall(r"'([A-Za-z0-9])' (?:opens|exports|adds|all|none|from|to|manages|searches|unsubscribes|clears|restores|hides|toggles)", prose))
        # A screen may legitimately name no letter keys: the unsubscribe screen
        # only uses arrows, ENTER and ESC, on purpose.
        for key in sorted(shown):
            check(key in bound,
                  f"keys: '{key}' shown in {Path(module.__file__).name} is really bound")


def test_steam_child_process(tmp: Path) -> None:
    """The Steam work must happen in a child process, and must never hang us.

    This is the regression test for the freeze: the library used to be called
    from the interface's own thread, and its output redirection took the screen
    down with it. These checks are about the shape of the escape route, not about
    Steam, which cannot be present here.
    """
    import time

    from pzmodmanager import steambridge

    command, env = steambridge.worker_command(
        tmp / "req.json", tmp / "resp.json", tmp / "prog.txt"
    )
    check(steambridge.WORKER_FLAG in command,
          "bridge: the child is started in worker mode")
    check(str(tmp / "req.json") in command,
          "bridge: the request file is handed to the child")
    check("PYTHONPATH" in env or getattr(sys, "frozen", False),
          "bridge: the child is told where the package lives")

    # No SDK anywhere: the answer must be a refusal, produced by a child that
    # started, ran and exited on its own.
    started = time.monotonic()
    answer = steambridge.list_subscriptions(tmp / "no-sdk-here")
    check(not answer.usable, "bridge: a missing library gives an unusable answer")
    check(not answer.timed_out, "bridge: and it is a refusal, not a timeout")
    check("steam_api" in answer.error or "No Steam library" in answer.error,
          "bridge: the refusal says what was looked for")
    check(time.monotonic() - started < 60,
          "bridge: a refusal comes back quickly rather than waiting out the deadline")

    check(steambridge.UNSUBSCRIBE_BASE > 0
          and steambridge.UNSUBSCRIBE_PER_ITEM > 0,
          "bridge: the unsubscribe deadline grows with the number of items")

    # The worker must be unreachable through the ordinary option parser, so a
    # typo can never put a user into it.
    parser = cli.build_parser()
    help_text = parser.format_help()
    check(steambridge.WORKER_FLAG not in help_text,
          "bridge: worker mode is not offered as a user option")

    source = (Path(__file__).resolve().parent.parent
              / "pzmodmanager" / "unsubscribe_screen.py").read_text(encoding="utf-8")
    check("steam_output_to_log" not in source,
          "bridge: the interface never redirects its own terminal for Steam")
    pipeline_source = (Path(__file__).resolve().parent.parent
                       / "pzmodmanager" / "pipeline.py").read_text(encoding="utf-8")
    check("steambridge" in pipeline_source,
          "bridge: the scan reads subscriptions through the child process")


def test_store(tmp: Path) -> None:
    path = tmp / "state.json"
    check(store.load(path) is None, "store: nothing to load on a first run")


def test_logging(tmp: Path) -> None:
    log_path = setup_logging(tmp / "test.log", "debug")
    check(log_path is not None and log_path.is_file(), "logging: log file created")
    return log_path


def close_log_handlers() -> None:
    """Detach and close every file handler the tool's logging set up."""
    import logging

    for logger in [logging.getLogger()] + [
        logging.getLogger(name) for name in list(logging.root.manager.loggerDict)
    ]:
        for handler in list(getattr(logger, "handlers", [])):
            try:
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)



def test_crash_folders_are_not_the_current_order(tmp: Path) -> None:
    """A crash copy must never be mistaken for the player's current save.

    When the game dies it copies the save folder beside itself with `_crash` on
    the end, and it writes that copy *after* the original. So on a machine that
    has crashed even once, the most recently modified mods.txt on disk belongs to
    a dead save. Picking a save by date therefore hands back the mod list as it
    was at the moment of the crash, and presents it as the current load order.

    That is not staleness, it is the wrong save, and it is silent: the list looks
    plausible, so nothing warns you. On a real machine this picked
    `2026-08-30_03-32-14_crash` over the save that was actually being played.

    The fix is to stop guessing. The game writes down which save it last opened,
    in latestSave.ini, so that answer is used first and the date is only a
    fallback. Both halves are checked here, plus a malformed marker, because a
    fallback that crashes is not a fallback.
    """
    from pzmodmanager import discovery, loadorder, savegame

    user = tmp / "ZomboidCrash"
    saves = user / "Saves" / "Apocalypse"

    def make(name: str, mods: list[str], when: float) -> Path:
        folder = saves / name
        folder.mkdir(parents=True, exist_ok=True)
        body = "".join(f"    mod = {m},\n" for m in mods)
        target = folder / savegame.MODS_FILE
        target.write_text(f"VERSION = 1,\n\nmods\n{{\n{body}}}\n\nmaps\n{{\n}}\n",
                          encoding="utf-8")
        os.utime(target, (when, when))
        return folder

    # The save actually being played, then the crash copy written after it. The
    # ordering of these two timestamps is the whole point of the test.
    played = make("2026-08-30_15-12-12", ["ZombieBuddy", "damnlib", "ETO_B"], 1_000_000)
    crashed = make("2026-08-30_03-32-14_crash", ["ZombieBuddy", "OldMod"], 2_000_000)
    make("2026-08-30_03-32-14", ["ZombieBuddy", "OldMod"], 500_000)

    check(savegame.is_crash_save(crashed), "crash: the _crash copy is recognised")
    check(not savegame.is_crash_save(played), "crash: a real save is not")

    original = discovery.default_user_folder
    try:
        discovery.default_user_folder = lambda: user

        # No marker yet: the date decides, and the crash copy is the newest.
        # Without the fix this returns the crash folder, which is the bug.
        picked = loadorder.default_order_candidates()
        check(picked and not savegame.is_crash_save(picked[0].parent),
              f"crash: a crash copy is never offered as the load order "
              f"(got {picked[0].parent.name if picked else None})")

        # Now the game's own answer, which points at neither of the newest two.
        (user / savegame.LATEST_SAVE_FILE).write_text(
            "2026-08-30_15-12-12\nApocalypse\n", encoding="utf-8")
        check(savegame.latest_save_folder() == played,
              "crash: latestSave.ini names the save the game last opened")
        picked = loadorder.default_order_candidates()
        check(picked and picked[0].parent == played,
              f"crash: the game's own answer wins over the file dates "
              f"(got {picked[0].parent.name if picked else None})")

        listed = savegame.find_saves()
        check(all(not savegame.is_crash_save(s.path) for s in listed),
              "crash: the apply screen is not offered a crash copy to write into")
        check(listed and listed[0].path == played,
              "crash: the save being played is offered first")
        check(len(savegame.find_saves(include_crash=True)) == len(listed) + 1,
              "crash: the copies are still reachable when explicitly asked for")

        # A marker that lies, points outside, or is truncated must fall back
        # rather than raise, and must still refuse the crash copy.
        for bad in ("", "only-one-line\n", "..\n..\n", "nope\nApocalypse\n"):
            (user / savegame.LATEST_SAVE_FILE).write_text(bad, encoding="utf-8")
            check(savegame.latest_save_folder() is None,
                  f"crash: a malformed marker is ignored rather than trusted ({bad!r})")
            picked = loadorder.default_order_candidates()
            check(picked and not savegame.is_crash_save(picked[0].parent),
                  "crash: the fallback still refuses the crash copy")
    finally:
        discovery.default_user_folder = original



def test_focus_never_greys_the_background() -> None:
    """Focusing a widget must not wash the black behind it.

    Textual paints a focused widget with `background-tint: $foreground 5%`, and
    it does so in eighteen places across its own widgets: Input, DataTable,
    OptionList, Tree, Select, Log, Button and the rest. A tint composites on top
    of the background rather than replacing it, so a panel that already says
    `background: #000000` obeys that rule and still turns grey the instant it
    takes focus. That is why the search box and the mod list looked lighter than
    the screen around them while the stylesheet insisted they were black.

    This is the third family of leaking colour, after the blue accent and the
    seven scrollbar properties, and it is fixed the same way: once, in the `*`
    rule, rather than widget by widget. So the check is the same shape too. It
    walks every screen, focuses everything that can hold focus, and demands that
    no widget anywhere carries a tint with any opacity at all.

    Measured on the real thing before the fix: '#search' and '#mods' both came
    back #B4B4B4 at 5%.
    """
    import asyncio

    from pzmodmanager.apply_screen import ApplyScreen
    from pzmodmanager.gamelog_screen import GameLogScreen
    from pzmodmanager.manager_screen import ManageScreen, PinsScreen
    from pzmodmanager.settings import Settings
    from pzmodmanager.settings_screen import SettingsScreen
    from pzmodmanager.tui import ModCheckApp

    async def run() -> dict:
        seen: dict = {"tinted": [], "focused": 0, "checked": 0}
        app = ModCheckApp(ScanOptions(), settings=Settings(), cli_overrides=set())
        async with app.run_test(size=(120, 44)) as pilot:
            mods = [sel.ModRef(mod_id=f"M{i}", name=f"Mod {i}") for i in range(5)]
            screens = [
                None,  # the menu, reached by pushing nothing
                ManageScreen(mods, []),
                PinsScreen([("A", "B")], sel.index_by_key(mods)),
                ApplyScreen(["M0"]),
                GameLogScreen(Path("/nowhere/console.txt")),
                SettingsScreen(Settings(), None),
            ]

            def sweep(screen, label: str) -> None:
                for widget in screen.query("*"):
                    tint = widget.styles.background_tint
                    seen["checked"] += 1
                    if tint is not None and tint.a:
                        seen["tinted"].append(
                            (label, widget.id or type(widget).__name__, tint.hex)
                        )

            for screen in screens:
                if screen is not None:
                    await app.push_screen(screen)
                    await pilot.pause()
                await pilot.pause()
                here = app.screen
                label = type(here).__name__
                sweep(here, label)
                # Nothing is tinted at rest, so focus is where it would show.
                for widget in here.query("*"):
                    if not widget.focusable:
                        continue
                    widget.focus()
                    await pilot.pause()
                    seen["focused"] += 1
                    sweep(here, f"{label} (focus {widget.id or type(widget).__name__})")
                if screen is not None:
                    app.pop_screen()
                    await pilot.pause()
        return seen

    shown = asyncio.run(run())
    check(shown["focused"] > 0,
          f"tint: something was actually focused, or this checks nothing "
          f"(focused {shown['focused']})")
    check(shown["checked"] > 50,
          f"tint: every screen was walked (got {shown['checked']} widget states)")
    check(not shown["tinted"],
          f"tint: no widget washes its background when focused "
          f"(got {shown['tinted'][:4]})")


def main() -> int:
    test_script_parser()
    test_branch_selection()
    test_selection()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        log_path = test_logging(tmp)
        test_store(tmp)
        test_settings(tmp)
        test_steam_bridge(tmp)
        test_workshop_input()
        test_browse_screen()
        test_mixed_layout(tmp)
        test_dependency_typo()
        test_problem_filter()
        test_toggle_keeps_the_view_still()
        test_order_hints()
        test_sort_disturbs_a_working_order_as_little_as_possible()
        test_order_view()
        test_game_log(tmp)
        test_apply_to_save(tmp)
        test_crash_folders_are_not_the_current_order(tmp)
        test_order_pins(tmp)
        test_restore_scanned()
        test_one_dependency_resolver()
        test_ids_are_clean_before_anything_compares_them(tmp)
        test_validate_knows_typos()
        test_multi_mod_items()
        test_hostile_text_everywhere()
        test_menu_greying()
        test_reset_actions(tmp)
        test_browse_concerns()
        test_browse_screen_live()
        test_stored_subscriptions(tmp)
        test_data_dir(tmp)
        test_settings_are_live()
        test_nothing_is_blue()
        test_focus_never_greys_the_background()
        test_key_hints_match_bindings()
        test_steam_child_process(tmp)
        test_steam_output_capture()
        test_cli_routing(tmp)


        fixture = tmp / "fixture"
        build(fixture)

        options = ScanOptions(
            extra_paths=[
                fixture / "steamapps/workshop/content/108600",
                fixture / "Zomboid/mods",
            ],
            use_defaults=False,
            order_path=fixture / "modlist.txt",
            use_steam=False,
        )

        steps: list[str] = []
        result = run_scan(options, progress=steps.append)

        check(
            any("Searching for the game folder" in s for s in steps),
            "pipeline: emits a progress message before searching",
        )
        check(
            any("Indexing mod files" in s for s in steps),
            "pipeline: emits a progress message while indexing",
        )
        check(result.duration >= 0, "pipeline: reports a duration")
        check(result.file_count > 0, "pipeline: counts the indexed files")

        rules = [f.rule for f in result.findings]
        by_rule: dict[str, list] = {}
        for finding in result.findings:
            by_rule.setdefault(finding.rule, []).append(finding)

        check(len(result.mods) == 11, f"discovery: 11 mods expected, {len(result.mods)} found")

        versioned = next((m for m in result.mods if m.mod_id == "VersionedMod"), None)
        check(versioned is not None, "layout: the versioned mod is found")
        if versioned:
            check(versioned.layout == "versioned", "layout: recognised as versioned")
            check(versioned.branch == "42.19",
                  f"layout: branch 42.19 selected (got {versioned.branch})")
            check(versioned.build_targets == ["common", "42.19"],
                  f"layout: common and the branch are both loaded (got {versioned.build_targets})")
        check(
            not any(f.rule == "duplicate_id" and "VersionedMod" in f.mods
                    for f in result.findings),
            "layout: version folders do not look like duplicate mod ids",
        )
        check("no_branch_for_build" in [f.rule for f in result.findings],
              "rule: a mod with no branch for this build is flagged")
        check(
            any(m.source == "local" for m in result.mods),
            "discovery: the manually installed mod is seen",
        )
        check(
            any(m.workshop_id == "1003" for m in result.mods),
            "discovery: the Workshop id is taken from the path",
        )

        check("missing_dependency" in rules, "rule: missing dependency")
        check("duplicate_id" in rules, "rule: duplicate mod id")
        check("declared_incompatibility" in rules, "rule: declared incompatibility")
        check("dependency_loaded_late" in rules, "rule: dependency loaded too late")
        check("mod_not_installed" in rules, "rule: listed mod is not installed")
        check("collision_lua_client" in rules, "rule: client Lua collision")
        check("script_object_collision" in rules, "rule: script object collision")
        check("collision_texture" in rules, "rule: texture collision")

        lua = by_rule["collision_lua_client"][0]
        check(
            set(lua.mods) == {"InventoryTetris", "BetterSorting", "MyLocalMod"},
            "lua collision: all three mods are listed",
        )
        check(
            lua.winner == "MyLocalMod",
            f"lua collision: the winner is the one loaded last (got {lua.winner})",
        )

        # No false positives
        check(
            not any(
                "QuietMod" in f.mods and f.rule.startswith("collision")
                for f in result.findings
            ),
            "false positive: the clean mod appears in no collision",
        )
        check(
            not any("Base.Old" in f.evidence for f in result.findings),
            "false positive: the 41/ branch is not compared against the 42/ branch",
        )
        check(
            not any(
                f.rule.startswith("collision") and len(set(f.mods)) < 2
                for f in result.findings
            ),
            "false positive: no mod collides with itself",
        )

        kinds = {
            f.title.split('"')[1] for f in by_rule["script_object_collision"]
        }
        check(
            kinds == {"item", "vehicle"},
            f"script collision: item and vehicle expected, got {sorted(kinds)}",
        )

        html = render_html(result, log_path)
        check(html.startswith("<!doctype html>"), "HTML report: complete document")
        check("prefers-color-scheme" in html, "HTML report: light and dark themes")
        check("Mod compatibility report" in html, "HTML report: titled")
        check(str(log_path) in html, "HTML report: mentions the log file")
        # Posters: the fixture ships one, so it must be found and drawable.
        poster_mod = next((m for m in result.ctx.mods if m.mod_id == "InventoryTetris"), None)
        if poster_mod:
            found = find_poster(poster_mod)
            check(found is not None and found.name == "poster.png",
                  "poster: the mod poster is located next to mod.info")
            if pillow_available() and found:
                art = poster_blocks(found, width=10, max_rows=4)
                check(art is not None and "\u2580" in str(art),
                      "poster: rendered as half blocks for the terminal")
            check(poster_blocks(None) is None,
                  "poster: a mod with no image renders nothing rather than failing")

        embedded = render_html(result, log_path, embed_images=True)
        check("data:image/jpeg;base64," in embedded,
              "HTML report: posters embed when asked for an offline report")
        check("class='thumb'" in html, "HTML report: the inventory has a thumbnail column")

        check("steamcommunity.com/sharedfiles/filedetails/?id=" in html,
              "HTML report: mod names link to the Workshop")
        check("target='_blank'" in html, "HTML report: links open in a new tab")
        local_mod = next((m for m in result.ctx.mods if m.source == "local"), None)
        check(local_mod is not None and local_mod.workshop_url is None,
              "HTML report: a hand-installed mod has no link")
        check(len(html) > 5000, "HTML report: not empty")

        payload = to_dict(result)
        check(len(payload["findings"]) == len(result.findings), "JSON export: all findings")
        check(payload["files_indexed"] == result.file_count, "JSON export: file count")

        # Everything the user sees must be English.
        allowed = set("…“”‘’·")
        accented = [c for c in html if ord(c) > 127 and c not in allowed]
        check(not accented, f"language: no accented characters left in the report {accented[:5]}")

        # Em and en dashes are banned everywhere the user can see them.
        dashes = [c for c in html if c in "—–"]
        check(not dashes, "style: no em or en dash in the report")
        for source in sorted(Path("pzmodmanager").glob("*.py")) + [Path("README.md")]:
            if not source.is_file():
                continue
            text = source.read_text(encoding="utf-8")
            check(
                "—" not in text and "–" not in text and "mdash" not in text,
                f"style: no em dash in {source.name}",
            )

        saved_scan = store.from_result(result, None)
        check(len(saved_scan.mods) == len(result.ctx.mods),
              "store: the mod list is kept alongside the findings")
        store.save(saved_scan, tmp / "roundtrip.json")
        reloaded = store.load(tmp / "roundtrip.json")
        check(reloaded is not None and len(reloaded.mods) == len(saved_scan.mods),
              "store: a saved scan reloads with its mods")
        if reloaded:
            original = {m.mod_id: m.requires for m in saved_scan.mods}
            back = {m.mod_id: m.requires for m in reloaded.mods}
            check(original == back, "store: dependencies survive the round trip")

        store.save_selection(["A", "B"], tmp / "sel.json")
        check(store.load_selection(tmp / "sel.json") == ["A", "B"],
              "store: a selection round trips")

        test_subscription_crosscheck(result.mods)

        log_text = log_path.read_text(encoding="utf-8")
        check("Scan starting" in log_text, "logging: the scan start is recorded")
        check("Discovery finished" in log_text, "logging: discovery is recorded")
        check("Analysis finished" in log_text, "logging: analysis is recorded")

        # Windows refuses to delete an open file, and the log handler still holds
        # test.log, so the temporary folder cannot be cleaned up until logging
        # lets go of it. On Linux the unlink would simply have succeeded and this
        # would never have shown up.
        close_log_handlers()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All checks pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
