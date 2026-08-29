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
        shown |= set(re.findall(r"'([A-Za-z0-9])' (?:opens|exports|adds|all|none|from|to|manages|searches|unsubscribes|clears)", prose))
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
        test_data_dir(tmp)
        test_settings_are_live()
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
