"""Command line entry point.

A plain run scans, writes an HTML report, and prints nothing but progress and a
short count summary. The findings themselves belong in the report, not in the
terminal scrollback.

  pzmodmanager                     scan and write the HTML report
  pzmodmanager --open              same, and open the report in the browser
  pzmodmanager --tui               interactive interface with a main menu
  pzmodmanager --order <file>      take the load order into account
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import store
from .settings import Settings, default_settings_path
from .logs import setup_logging
from .models import Severity
from .pipeline import ScanOptions, run_scan
from .report import (
    counts_by_severity,
    print_console_report,
    print_summary,
    print_top_mods,
    write_html,
    write_json,
)

DEFAULT_HTML = "pzmodmanager-report.html"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pzmodmanager",
        description=(
            "Scan installed Project Zomboid mods and report what overlaps: missing "
            "dependencies, overwritten files, redefined script objects."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  pzmodmanager\n"
            "  pzmodmanager --tui\n"
            '  pzmodmanager --path "D:/SteamLibrary/steamapps/workshop/content/108600" --open\n'
            '  pzmodmanager --order "%%USERPROFILE%%/Zomboid/Lua/saved_modlists.txt"\n'
        ),
    )

    scan = parser.add_argument_group("where to look for mods")
    scan.add_argument(
        "--path", action="append", default=[], metavar="FOLDER",
        help="extra folder to scan (repeatable): workshop/content/108600, "
             "Zomboid/mods, or a single mod folder",
    )
    scan.add_argument(
        "--no-auto", action="store_true",
        help="do not probe the usual Steam and Zomboid locations",
    )
    scan.add_argument(
        "--build", default="42", metavar="NN",
        help="target build, which decides the media branches read (default: 42)",
    )
    scan.add_argument(
        "--no-scripts", action="store_true",
        help="skip media/scripts parsing (faster on a very large mod set)",
    )

    workshop = parser.add_argument_group("steam workshop")
    workshop.add_argument(
        "--no-steam", action="store_true",
        help="do not query the Steam Workshop (no network access at all)",
    )
    workshop.add_argument(
        "--steam-cache", metavar="FILE",
        help="where Workshop answers are cached between runs",
    )
    workshop.add_argument(
        "--refresh-steam", action="store_true",
        help="ignore the Workshop cache and query Steam again",
    )

    order = parser.add_argument_group("load order")
    order.add_argument(
        "--order", metavar="FILE",
        help="file describing the order: saved_modlists.txt, a server .ini, or a text list",
    )
    order.add_argument(
        "--list-name", metavar="NAME",
        help="which list to use inside saved_modlists.txt (default: the longest one)",
    )
    order.add_argument(
        "--only-enabled", action="store_true",
        help="only analyse mods present in the load order",
    )

    out = parser.add_argument_group("output")
    out.add_argument("--tui", action="store_true", help="interactive interface")
    out.add_argument(
        "--html", metavar="FILE",
        help=f"path of the HTML report (default: {DEFAULT_HTML})",
    )
    out.add_argument("--no-html", action="store_true", help="do not write an HTML report")
    out.add_argument(
        "--embed-images", action="store_true",
        help="embed each mod's local poster in the report instead of linking the "
             "Workshop preview; needs Pillow, and makes a bigger but offline file",
    )
    out.add_argument(
        "--font", metavar="FILE",
        help="TTF or OTF to embed in the report for headings and figures; "
             "pixter-granular.ttf next to the tool is picked up automatically",
    )
    out.add_argument(
        "--open", dest="open_report", action="store_true",
        help="open the HTML report in the browser once it is written",
    )
    out.add_argument("--json", metavar="FILE", help="also export the results as JSON")
    out.add_argument("--quiet", action="store_true", help="no progress lines, only the summary")
    out.add_argument(
        "--print-findings", action="store_true",
        help="also print every finding to the console (off by default)",
    )
    out.add_argument(
        "--min-severity", choices=[s.label for s in Severity], default="info",
        help="drop findings below this severity",
    )
    out.add_argument(
        "--fail-on", choices=[s.label for s in Severity if s.weight > 0] + ["none"],
        default="none",
        help="exit with code 1 if a finding reaches this severity (for scripting)",
    )

    manage = parser.add_argument_group("mod selection")
    manage.add_argument(
        "--manage", action="store_true",
        help="open the mod manager, scanning first if there is nothing saved",
    )
    manage.add_argument(
        "--enable", action="append", default=[], metavar="ID",
        help="add a mod, and its dependencies, to the saved selection (repeatable)",
    )
    manage.add_argument(
        "--disable", action="append", default=[], metavar="ID",
        help="remove a mod from the saved selection (repeatable)",
    )
    manage.add_argument(
        "--export-ini", metavar="FILE",
        help="write the Mods= and WorkshopItems= lines for a server ini",
    )
    manage.add_argument(
        "--export-links", metavar="FILE",
        help="write the Workshop page of every selected mod",
    )
    manage.add_argument(
        "--print-links", action="store_true",
        help="print the Workshop page of every selected mod",
    )
    manage.add_argument(
        "--print-order", action="store_true",
        help="print the resolved load order, dependencies first",
    )
    manage.add_argument(
        "--selection", metavar="FILE", help="where the selection is stored",
    )

    steamsdk = parser.add_argument_group("steam client (unsubscribing)")
    steamsdk.add_argument(
        "--steam-sdk", metavar="PATH",
        help="the Steamworks redistributable, steam_api64.dll, or the folder "
             "holding it; needed only to change subscriptions",
    )
    steamsdk.add_argument(
        "--steam-check", action="store_true",
        help="report what the Steam bridge can do and change nothing",
    )
    steamsdk.add_argument(
        "--add", action="append", default=[], metavar="ID_OR_URL",
        help="subscribe to a Workshop item, by id or by its page address "
             "(repeatable); Steam downloads it in the background afterwards",
    )
    steamsdk.add_argument(
        "--unsubscribe", action="append", default=[], metavar="ID",
        help="unsubscribe from a mod id or Workshop id (repeatable)",
    )
    steamsdk.add_argument(
        "--unsubscribe-unselected", action="store_true",
        help="unsubscribe from every installed mod that is not in the selection",
    )
    steamsdk.add_argument(
        "--yes", action="store_true",
        help="skip the typed confirmation; nothing is unsubscribed without this "
             "or an answer at the prompt",
    )

    logging_group = parser.add_argument_group("logging and data")
    logging_group.add_argument(
        "--data-dir", metavar="FOLDER",
        help="where the saved scan, the selection, the Workshop cache and the log "
             "are kept; settings.json stays in the per-user location",
    )
    logging_group.add_argument("--log", metavar="FILE", help="path of the log file")
    logging_group.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"], default="info",
        help="how much detail goes into the log (default: info)",
    )
    logging_group.add_argument(
        "--state", metavar="FILE",
        help="where the last scan is saved so a later run can reopen it",
    )
    return parser


_SEV_BY_LABEL = {s.label: s for s in Severity}

# Which ScanOptions field each command line option decides. Used to tell the
# interface what to keep pinned, so everything else can be reread from the
# settings between two scans instead of being frozen at launch.
_OPTION_TO_FIELD = {
    "path": "extra_paths",
    "no_auto": "use_defaults",
    "build": "build",
    "no_scripts": "parse_scripts",
    "order": "order_path",
    "only_enabled": "only_enabled",
    "no_steam": "use_steam",
    "steam_sdk": "steam_sdk",
}


def scan_option_overrides(given: set[str]) -> set[str]:
    """The ScanOptions fields the user pinned by typing an option."""
    return {field for option, field in _OPTION_TO_FIELD.items() if option in given}


def _force_utf8_console() -> None:
    """The Windows console defaults to cp1252, which chokes on box characters."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def explicitly_given(argv: list[str] | None) -> set[str]:
    """Which options the user actually typed.

    Saved settings act as defaults, so a stored value must not be mistaken for a
    command line choice. Comparing against a parse of an empty argument list is
    the reliable way to tell them apart.
    """
    parser = build_parser()
    typed = vars(parser.parse_args(argv if argv is not None else sys.argv[1:]))
    blank = vars(parser.parse_args([]))
    return {name for name, value in typed.items() if value != blank.get(name)}


def options_from_args(args, settings: Settings | None = None, given: set[str] | None = None) -> ScanOptions:
    settings = settings or Settings()
    given = given or set()

    cache: Path | None = None
    if not args.refresh_steam:
        cache = (
            Path(args.steam_cache).expanduser()
            if args.steam_cache
            else store.default_steam_cache_path()
        )

    def pick(option: str, from_args, from_settings):
        return from_args if option in given else from_settings

    extra = [Path(p).expanduser() for p in args.path] or [
        Path(p).expanduser() for p in settings.extra_paths
    ]
    sdk = (
        Path(args.steam_sdk).expanduser()
        if "steam_sdk" in given and args.steam_sdk
        else settings.steam_sdk_path
    )
    return ScanOptions(
        extra_paths=extra,
        use_defaults=pick("no_auto", not args.no_auto, settings.use_defaults),
        build=pick("build", args.build, settings.build),
        parse_scripts=pick("no_scripts", not args.no_scripts, settings.parse_scripts),
        order_path=(
            Path(args.order).expanduser()
            if "order" in given and args.order
            else settings.order_path_or_none
        ),
        list_name=args.list_name,
        only_enabled=pick("only_enabled", args.only_enabled, settings.only_enabled),
        use_steam=pick("no_steam", not args.no_steam, settings.use_steam),
        steam_cache=cache,
        steam_sdk=sdk,
    )


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    # Worker mode is checked before argparse, deliberately. It is not a user
    # facing option: the tool starts a copy of itself this way to keep the Steam
    # library in a process of its own, and it must not appear in --help or be
    # reachable by a typo that argparse would try to be helpful about.
    if raw and raw[0] == "--steam-worker":
        from .steamworker import main as worker_main

        return worker_main(raw[1:])

    _force_utf8_console()
    args = build_parser().parse_args(argv)

    # Order matters here. The data folder decides where the log, the saved scan,
    # the selection and the Workshop cache go, so it has to be settled before
    # anything works out a path. Logging used to start first, which would have
    # put the log in the old place every time.
    settings_path = default_settings_path()
    settings = Settings.load(settings_path)
    given = explicitly_given(argv)
    store.set_data_dir(
        Path(args.data_dir).expanduser()
        if "data_dir" in given and args.data_dir
        else settings.data_dir_path
    )

    log_path = setup_logging(Path(args.log) if args.log else None, args.log_level)

    state_path = Path(args.state).expanduser() if args.state else store.default_store_path()
    selection_path = (
        Path(args.selection).expanduser() if args.selection else store.default_selection_path()
    )

    if args.steam_check:
        return run_steam_check(args, settings)

    # --manage opens the interface. The other selection options are the headless
    # route, and only they should bypass it.
    if args.add:
        return run_add(args, settings)

    headless_manager = bool(
        args.enable
        or args.unsubscribe
        or args.unsubscribe_unselected
        or args.disable
        or args.export_ini
        or args.export_links
        or args.print_order
        or args.print_links
    )
    if headless_manager and not args.tui:
        return run_manager(args, state_path, selection_path, log_path)

    if args.tui or args.manage:
        from .tui import run_tui

        scan_options = options_from_args(args, settings, given)
        run_tui(
            scan_options,
            log_path,
            Path(args.html) if "html" in given and args.html else Path(settings.report_path),
            state_path,
            selection_path,
            open_manager=args.manage,
            steam_sdk=scan_options.steam_sdk,
            settings=settings,
            settings_path=settings_path,
            # Which ScanOptions fields came from the command line. The interface
            # rebuilds everything else from the settings before each scan, so a
            # change made on the settings screen is actually used, and only what
            # you typed stays pinned for the session.
            cli_overrides=scan_option_overrides(given),
        )
        return 0

    from rich.console import Console

    console = Console()

    def progress(message: str) -> None:
        if not args.quiet:
            console.print(f"  [dim]·[/dim] {message}")

    if not args.quiet:
        console.print("[bold]pzmodmanager[/bold]")
        console.print()

    result = run_scan(options_from_args(args, settings, given), progress=progress)

    if not result.ok:
        console.print("\n[red]No mod found.[/red]")
        console.print(
            'Point the tool at a folder with --path, for example:\n'
            '  pzmodmanager --path "C:/Program Files (x86)/Steam/steamapps/workshop/content/108600"'
        )
        if log_path:
            console.print(f"\nLog: {log_path}")
        return 2

    threshold = _SEV_BY_LABEL[args.min_severity]
    result.findings = [f for f in result.findings if f.severity.weight >= threshold.weight]

    html_path: Path | None = None
    if not args.no_html:
        html_path = write_html(
            Path(args.html or DEFAULT_HTML),
            result,
            log_path,
            Path(args.font).expanduser() if args.font else None,
            args.embed_images,
        )
    json_path: Path | None = None
    if args.json:
        json_path = write_json(Path(args.json), result)

    store.save(store.from_result(result, html_path), state_path)

    print_summary(console, result)
    print_top_mods(console, result)

    if args.print_findings:
        console.print()
        print_console_report(result.findings, result.ctx, console)

    console.print()
    if html_path:
        console.print(f"Report : {html_path.resolve()}")
    if json_path:
        console.print(f"JSON   : {json_path.resolve()}")
    if log_path:
        console.print(f"Log    : {log_path}")

    if html_path and args.open_report:
        webbrowser.open(html_path.resolve().as_uri())

    if args.fail_on != "none":
        limit = _SEV_BY_LABEL[args.fail_on]
        counts = counts_by_severity(result.findings)
        if any(counts[s] for s in counts if s.weight >= limit.weight):
            return 1
    return 0


def run_steam_check(args, settings: Settings | None = None) -> int:
    """Say exactly what the Steam bridge finds, and touch nothing."""
    from rich.console import Console

    from .steambridge import check
    from .steamsdk import find_library, platform_dll_names

    console = Console()
    console.print("[bold]Steam bridge check[/bold]\n")
    configured = Path(args.steam_sdk).expanduser() if args.steam_sdk else None
    if configured is None and settings is not None:
        configured = settings.steam_sdk_path

    library = find_library(configured)
    console.print(f"  configured   {configured or 'nothing set'}")
    console.print(f"  library      {library or 'not found'}")
    if library is None:
        console.print(
            f"\n[yellow]Point --steam-sdk, or the Steam SDK setting, at "
            f"{platform_dll_names()[0]} itself or at the folder holding it. In the "
            "SDK archive that is sdk/redistributable_bin/win64.[/yellow]"
        )
        return 1

    console.print("  asking Steam, in a separate process...")
    answer = check(library, progress=lambda line: console.print(f"    [dim]{line}[/dim]"))
    for line in answer.diagnostics:
        console.print(f"  {line}")
    if not answer.usable:
        console.print(f"\n[yellow]{answer.error}[/yellow]")
        console.print(
            "The bridge needs the Steamworks redistributable and a running, "
            "logged in Steam client."
        )
        return 1
    console.print(f"  subscribed   {len(answer.subscribed)} item(s) visible")
    console.print("\n[green]The bridge works. Subscriptions can be changed.[/green]")
    return 0


def run_add(args, settings: Settings | None = None) -> int:
    """Subscribe to Workshop items named on the command line."""
    from rich.console import Console

    from .steam import (
        WorkshopCache,
        fetch_items,
        item_url,
        mod_ids_in_description,
        parse_workshop_ids,
    )
    from .steambridge import subscribe as bridge_subscribe
    from .steamsdk import find_library

    console = Console()
    ids: list[str] = []
    for entry in args.add:
        found = parse_workshop_ids(entry)
        if not found:
            console.print(f"[red]Not a Workshop id or link: {entry}[/red]")
            return 2
        ids += [i for i in found if i not in ids]

    console.print(f"[bold]Looking up {len(ids)} Workshop item(s)[/bold]\n")
    items = fetch_items(ids, cache=WorkshopCache(store.default_steam_cache_path()))
    for wid in ids:
        item = items.get(wid)
        if item is None:
            console.print(f"  [yellow]{wid}  Steam returned nothing about this[/yellow]")
            continue
        console.print(f"  {item.title or '(no title)'}")
        console.print(f"    {item_url(wid)}")
        claimed = mod_ids_in_description(item.description)
        if claimed:
            console.print(f"    [dim]description claims mod id(s): {', '.join(claimed[:5])}[/dim]")
        if item.missing:
            console.print("    [yellow]no longer on the Workshop[/yellow]")

    configured = Path(args.steam_sdk).expanduser() if args.steam_sdk else None
    if configured is None and settings is not None:
        configured = settings.steam_sdk_path
    library = find_library(configured)
    if library is None:
        console.print(
            "\n[red]No Steamworks library found, so nothing was subscribed.[/red]\n"
            "Set the Steam SDK in the settings, or pass --steam-sdk."
        )
        return 2

    console.print(f"\n[bold]Subscribing to {len(ids)} item(s)[/bold]")
    if not args.yes and sys.stdin.isatty():
        try:
            answer = input("Type YES to go ahead, anything else to cancel: ")
        except (EOFError, KeyboardInterrupt):
            console.print("\nCancelled.")
            return 0
        if answer.strip() != "YES":
            console.print("Cancelled, nothing was changed.")
            return 0
    elif not args.yes:
        console.print("[red]Not a terminal. Pass --yes if you really mean it.[/red]")
        return 2

    answer = bridge_subscribe(
        library, [int(i) for i in ids],
        progress=lambda line: console.print(f"  [dim]{line}[/dim]"),
    )
    if not answer.usable:
        console.print(f"\n[red]{answer.error}[/red]")
        return 2
    console.print()
    for item in answer.done:
        console.print(f"[green]added[/green]     {item}")
    for item in answer.failed:
        console.print(f"[yellow]not added[/yellow] {item}")
    console.print(
        "\nSteam downloads these in the background. Nothing is on disk yet, so "
        "run a scan once it has finished."
    )
    return 0


def _confirm_unsubscribe(console, targets: list[tuple[str, str]], assume_yes: bool) -> bool:
    """One grouped confirmation showing everything that would be unsubscribed."""
    console.print()
    console.print("[bold]These mods would be unsubscribed from your Steam account:[/bold]\n")
    for mod_id, workshop_id in targets:
        console.print(f"  {mod_id}   (Workshop {workshop_id})")
    console.print(
        f"\n[yellow]{len(targets)} item(s). Steam deletes the local files once it "
        "next shuts down, so any server or save relying on them loses them too."
        "[/yellow]"
    )
    if assume_yes:
        console.print("[dim]--yes given, proceeding without asking.[/dim]")
        return True
    if not sys.stdin.isatty():
        console.print(
            "[red]Not a terminal, so nothing was changed. Pass --yes if you really "
            "mean it.[/red]"
        )
        return False
    try:
        answer = input("\nType UNSUBSCRIBE to go ahead, anything else to cancel: ")
    except (EOFError, KeyboardInterrupt):
        console.print("\nCancelled.")
        return False
    if answer.strip() != "UNSUBSCRIBE":
        console.print("Cancelled, nothing was changed.")
        return False
    return True


def run_unsubscribe(args, by_key, selected: set[str], console) -> int:
    """Resolve what to unsubscribe, confirm once, then do it and verify."""
    from .steambridge import unsubscribe as bridge_unsubscribe
    from .steamsdk import find_library

    targets: list[tuple[str, str]] = []
    unknown: list[str] = []

    def add(ref) -> None:
        if ref.workshop_id and (ref.mod_id, ref.workshop_id) not in targets:
            targets.append((ref.mod_id, ref.workshop_id))

    for wanted in args.unsubscribe:
        key = wanted.strip().lower()
        ref = by_key.get(key)
        if ref is None:
            # Might be a Workshop id rather than a mod id.
            ref = next(
                (r for r in by_key.values() if r.workshop_id == wanted.strip()), None
            )
        if ref is None:
            unknown.append(wanted)
        else:
            add(ref)

    if args.unsubscribe_unselected:
        for key, ref in by_key.items():
            if key not in selected:
                add(ref)

    for name in unknown:
        console.print(f"[red]Unknown mod, cannot unsubscribe: {name}[/red]")
    if unknown:
        return 2
    if not targets:
        console.print("Nothing to unsubscribe.")
        return 0

    configured = Path(args.steam_sdk).expanduser() if args.steam_sdk else None
    if configured is None:
        configured = Settings.load().steam_sdk_path
    library = find_library(configured)
    if library is None:
        console.print(
            "\n[red]No Steamworks library found, so nothing was changed.[/red]\n"
            "Download the Steamworks SDK and pass --steam-sdk, then try "
            "--steam-check first."
        )
        return 2

    if not _confirm_unsubscribe(console, targets, args.yes):
        return 0

    console.print()
    answer = bridge_unsubscribe(
        library,
        [int(w) for _, w in targets],
        progress=lambda line: console.print(f"  [dim]{line}[/dim]"),
    )
    if not answer.usable:
        console.print(f"\n[red]{answer.error}[/red]")
        console.print("Run --steam-check for a fuller diagnosis.")
        return 2
    done, failed = answer.done, answer.failed

    by_workshop = {w: m for m, w in targets}
    console.print()
    for item in done:
        console.print(f"[green]unsubscribed[/green] {by_workshop.get(str(item), item)}")
    for item in failed:
        console.print(
            f"[yellow]still subscribed[/yellow] {by_workshop.get(str(item), item)}"
        )
    if failed:
        console.print(
            "\nSteam may simply not have caught up. Check the Workshop page, and "
            "look at the log."
        )
    return 0


def run_manager(args, state_path: Path, selection_path: Path, log_path: Path | None) -> int:
    """Headless selection work: enable, disable, order, export."""
    from rich.console import Console

    from .selection import (
        dependency_closure,
        export_links,
        export_server_ini,
        index_by_key,
        topological_order,
        validate,
    )

    console = Console()

    scan = store.load(state_path)
    if scan is None or not scan.mods:
        console.print("[yellow]No saved scan yet, running one first.[/yellow]\n")
        result = run_scan(
            options_from_args(args, Settings.load(), explicitly_given(None)),
            progress=lambda m: console.print(f"  [dim]·[/dim] {m}"),
        )
        if not result.ok:
            console.print("\n[red]No mod found.[/red]")
            return 2
        scan = store.from_result(result, None)
        store.save(scan, state_path)
        console.print()

    by_key = index_by_key(scan.mods)
    saved = store.load_selection(selection_path)
    if saved:
        selected = {s.strip().lower() for s in saved} & set(by_key)
    else:
        selected = {r.key for r in scan.mods if r.was_enabled} or set(by_key)

    for mod_id in args.enable:
        key = mod_id.strip().lower()
        if key not in by_key:
            console.print(f"[red]Unknown mod, cannot enable: {mod_id}[/red]")
            return 2
        before = set(selected)
        selected, missing = dependency_closure(by_key, selected | {key})
        pulled = sorted(selected - before - {key})
        console.print(f"enabled {by_key[key].mod_id}")
        for extra in pulled:
            console.print(f"  pulled in {by_key[extra].mod_id}")
        for absent in missing:
            console.print(f"  [yellow]required but not installed: {absent}[/yellow]")

    # Disables run after enables, so an explicit disable always wins over a
    # dependency that an enable pulled in. The resulting gap is reported rather
    # than silently filled back.
    for mod_id in args.disable:
        key = mod_id.strip().lower()
        if key not in selected:
            console.print(f"[yellow]{mod_id} was not selected[/yellow]")
            continue
        selected.discard(key)
        console.print(f"disabled {by_key[key].mod_id if key in by_key else mod_id}")

    preferred = [
        r.mod_id
        for r in sorted(
            (r for r in scan.mods if r.order_index is not None),
            key=lambda r: r.order_index,
        )
    ]
    ordered, cycle = topological_order(by_key, selected, preferred=preferred)
    problems = validate(by_key, selected, scan.findings)

    if args.enable or args.disable:
        store.save_selection(ordered, selection_path)

    if args.print_order:
        console.print()
        for position, mod_id in enumerate(ordered, start=1):
            console.print(f"{position:3}. {mod_id}")

    if args.unsubscribe or args.unsubscribe_unselected:
        return run_unsubscribe(args, by_key, selected, console)

    if args.print_links:
        console.print()
        for mod_id in ordered:
            ref = by_key.get(mod_id.strip().lower())
            if ref and ref.workshop_url:
                console.print(f"{ref.workshop_url}   {ref.mod_id}")
            elif ref:
                console.print(f"{'local mod, no page':52}   {ref.mod_id}")

    if args.export_links:
        target = Path(args.export_links).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(export_links(by_key, ordered), encoding="utf-8")
            console.print(f"\nWorkshop links written to {target.resolve()}")
        except OSError as exc:
            console.print(f"\n[red]Could not write {target}: {exc}[/red]")
            return 2

    if args.export_ini:
        blocking = [p for p in problems if p.severity is Severity.CRITICAL]
        if blocking:
            console.print(
                f"\n[yellow]Warning: exporting with {len(blocking)} critical "
                "problem(s) unresolved. The selection is yours to make, but this "
                "list will not load cleanly.[/yellow]"
            )
        target = Path(args.export_ini).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(export_server_ini(by_key, ordered), encoding="utf-8")
            console.print(f"\nServer ini lines written to {target.resolve()}")
        except OSError as exc:
            console.print(f"\n[red]Could not write {target}: {exc}[/red]")
            return 2

    console.print(
        f"\n{len(selected)} of {len(by_key)} mods selected, "
        f"{len(problems)} problem(s)"
    )
    if cycle:
        console.print(f"[red]Dependency cycle: {', '.join(cycle)}[/red]")
    for problem in problems[:10]:
        console.print(f"  [{problem.severity.label}] {problem.message}")
    if len(problems) > 10:
        console.print(f"  ... {len(problems) - 10} more, open --manage to see them all")
    if log_path:
        console.print(f"\nLog: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
