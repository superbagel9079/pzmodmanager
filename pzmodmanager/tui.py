"""Interactive interface, in the spirit of an old terminal game.

Deliberately monochrome: severity is carried by an ASCII marker rather than a
colour, so the display stays readable on any terminal, including black and white
ones and colour schemes that remap the palette. The selected line is shown in
inverse video, like an old menu.

Three screens: the main menu, the scan with its live progress, and the results.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import DataTable, Log, OptionList, Static
from textual.widgets.option_list import Option

from . import store
from .models import Finding, Severity
from .pipeline import ScanOptions, ScanResult, run_scan
from .report import SEVERITY_ORDER, counts_by_severity, write_html
from .store import StoredScan

# Written with escaped backslashes rather than raw strings: a raw string cannot
# end with a backslash, and several of these lines do.
BANNER = "\n".join(
    [
        "                               _                                             ",
        "                              | |                                            ",
        "  _ __ _____ __ ___   ___   __| |_ __ ___   __ _ _ __   __ _  __ _  ___ _ __ ",
        " | '_ \\_  / '_ ` _ \\ / _ \\ / _` | '_ ` _ \\ / _` | '_ \\ / _` |/ _` |/ _ \\ '__|",
        " | |_) / /| | | | | | (_) | (_| | | | | | | (_| | | | | (_| | (_| |  __/ |   ",
        " | .__/___|_| |_| |_|\\___/ \\__,_|_| |_| |_|\\__,_|_| |_|\\__,_|\\__, |\\___|_|   ",
        " | |                                                          __/ |          ",
        " |_|                                                         |___/           ",
    ]
)

AUTHOR = "by superbagel9079"

# Menu entries. The wording changes once a scan exists: a first run offers a
# greyed out "Results" and a plain "Scan", a later run offers "Last results" and
# "Rescan". Labels are padded to a common width and centred by hand, because the
# highlight bar is as wide as the option and every entry must line up.
FIRST_RUN_ITEMS = [
    ("results", "Results"),
    ("manage", "Manage mods"),
    ("browse", "Add mods"),
    ("gamelog", "Game log"),
    ("scan", "Scan"),
    ("settings", "Settings"),
    ("quit", "Quit"),
]
RETURNING_ITEMS = [
    ("results", "Last results"),
    ("manage", "Manage mods"),
    ("browse", "Add mods"),
    ("gamelog", "Game log"),
    ("scan", "Rescan"),
    ("settings", "Settings"),
    ("quit", "Quit"),
]
_MENU_WIDTH = max(
    len(label) for _, label in FIRST_RUN_ITEMS + RETURNING_ITEMS
) + 4

# Severity reads from the number of exclamation marks, not from a colour.
MARKER = {
    Severity.CRITICAL: "[!!!]",
    Severity.HIGH: "[!! ]",
    Severity.MEDIUM: "[!  ]",
    Severity.LOW: "[ . ]",
    Severity.INFO: "[   ]",
}

# Shared look, applied to every screen.
# Textual's default theme is blue: $primary is #0178D4, and it comes out as the
# focus border on an OptionList or an Input, in progress bars, in selection
# highlights, and in widgets this project never names. Chasing it one selector
# at a time is the same losing game as the scrollbars below, so the theme itself
# is replaced. One definition, and nothing can leak a colour the interface does
# not use.
MONOCHROME = Theme(
    name="pzmodmanager",
    primary="#ffffff",
    secondary="#b4b4b4",
    accent="#ffffff",
    foreground="#b4b4b4",
    background="#000000",
    surface="#000000",
    panel="#000000",
    success="#b4b4b4",
    warning="#b4b4b4",
    error="#b4b4b4",
    dark=True,
)

RETRO_CSS = """
/* Scrollbars are their own family of colours, seven of them, and any one left
   alone shows the theme through. The theme itself is monochrome now (see
   MONOCHROME above), so this is belt and braces rather than the fix, but it
   also sets the greys the interface actually wants rather than white. */
* {
    scrollbar-background: #000000;
    scrollbar-background-hover: #000000;
    scrollbar-background-active: #000000;
    scrollbar-color: #4a4a4a;
    scrollbar-color-hover: #6a6a6a;
    scrollbar-color-active: #8a8a8a;
    scrollbar-corner-color: #000000;
}
Screen {
    background: #000000;
    color: #b4b4b4;
}
#hint {
    color: #6a6a6a;
    padding: 0 2;
    height: auto;
}
#bannerbox {
    height: auto;
    padding: 2 0 0 0;
}
#banner {
    color: #ffffff;
    text-style: bold;
    width: auto;
    height: auto;
}
#author {
    color: #8a8a8a;
    content-align: center top;
    height: auto;
    padding: 0 0 1 0;
}
#footer {
    color: #6a6a6a;
    content-align: center bottom;
    height: auto;
    padding: 1 0 1 0;
    dock: bottom;
}
"""


def _centered(lines: list[str]) -> str:
    width = max(len(line) for line in lines)
    return "\n".join(line.ljust(width) for line in lines)


def panel(lines: list[tuple[str, bool]]) -> Text:
    """Build a block of text where some lines are bold, as styled Text.

    The alternative, spelling "[b]...[/b]" into the string and letting the
    widget parse it, only works while every other character on that panel is
    also safe to parse. It never is: these panels print mod names, finding
    titles and Workshop text, and a mod called "[41/42.20MP] Hot Brass" is a
    name, not markup. Emphasis belongs to the span, not to the string.
    """
    text = Text()
    for index, (line, bold) in enumerate(lines):
        if index:
            text.append("\n")
        text.append(line, style="bold" if bold else "")
    return text


def cell(value) -> Text:
    """Anything on its way into a table cell.

    DataTable has the same trap as Static and it is easy to miss, because the
    two look nothing alike in the code: a plain string handed to add_row goes
    through Text.from_markup, so a mod named "Brackets [in] the title" raises a
    MarkupError from inside the table's own drawing code. Text objects are
    passed through untouched, so nothing reaches a cell as a str.
    """
    return value if isinstance(value, Text) else Text("" if value is None else str(value))


class Plain(Static):
    """A Static that never reads its content as markup.

    Nothing this interface shows is markup. What it shows is its own literal
    text, or text that came from a mod or from the Workshop, and that second
    kind is full of square brackets: Steam descriptions carry BBCode like
    [B]...[/B], mod ids and Windows paths have brackets in them, and a checkbox
    is literally [x]. Textual reads every one of those as a style tag. The
    failures are not subtle in the same way twice, which is what made this
    costly: [x] silently rendered as nothing, while a Workshop description took
    the whole screen down with a MarkupError.

    So the rule is the widget, not the caller: no Static here parses markup, and
    no future line of text has to remember to escape itself.
    """

    def __init__(self, content="", **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(content, **kwargs)


class BannerHeader(Plain):
    """The ASCII title, centred as a block rather than line by line."""

    def __init__(self) -> None:
        super().__init__(BANNER, id="banner")


# --------------------------------------------------------------------------- #
# Main menu
# --------------------------------------------------------------------------- #


class MenuScreen(Screen):
    CSS = RETRO_CSS + """
    #menu-area {
        height: 1fr;
        align: center middle;
    }
    #menu-box {
        border: solid #b4b4b4;
        width: 28;
        height: auto;
        padding: 1 1;
        align: center top;
    }
    OptionList {
        background: #000000;
        color: #8a8a8a;
        border: none;
        padding: 0;
        width: auto;
        height: auto;
        scrollbar-size: 0 0;
    }
    OptionList > .option-list--option {
        background: #000000;
        color: #8a8a8a;
    }
    OptionList > .option-list--option-hover {
        background: #000000;
        color: #b4b4b4;
    }
    OptionList > .option-list--option-highlighted {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }
    OptionList:focus > .option-list--option-highlighted {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }
    OptionList > .option-list--option-disabled {
        color: #4a4a4a;
    }
    """

    BINDINGS = [
        Binding("q,Q", "quit_app", "Quit"),
        Binding("escape", "quit_app", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Plain(
            "Use arrow keys to move, ENTER to select\nPress 'q' to quit",
            id="hint",
        )
        yield Container(BannerHeader(), id="bannerbox")
        yield Plain(AUTHOR, id="author")
        with Container(id="menu-area"):
            with Container(id="menu-box"):
                yield OptionList(id="menu")
        yield Plain("mod compatibility checker for Project Zomboid", id="footer")

    def on_mount(self) -> None:
        self.query_one("#bannerbox", Container).styles.align_horizontal = "center"
        self.refresh_menu()
        self.query_one("#menu", OptionList).focus()

    def on_screen_resume(self, event) -> None:
        # Coming back from a scan may have unlocked the Results entry.
        self.refresh_menu(keep_position=True)
        self.query_one("#menu", OptionList).focus()

    def refresh_menu(self, keep_position: bool = False) -> None:
        menu = self.query_one("#menu", OptionList)
        previous = menu.highlighted if keep_position else None
        menu.clear_options()
        scan = self.app.stored
        # What is behind each entry, rather than whether a file happens to exist.
        # A scan that found nothing is saved like any other, and judging by the
        # file alone offered "Last results" onto an empty screen.
        has_results = bool(scan and scan.has_results)
        has_mods = bool(scan and scan.has_mods)
        items = RETURNING_ITEMS if has_results else FIRST_RUN_ITEMS
        menu.add_options(
            [
                Option(
                    label.center(_MENU_WIDTH),
                    id=key,
                    disabled=(
                        (key == "results" and not has_results)
                        or (key == "manage" and not has_mods)
                    ),
                )
                for key, label in items
            ]
        )
        if previous is not None:
            menu.highlighted = min(previous, len(items) - 1)
        else:
            # A first run starts on Scan, a later run on the results already
            # there. Found by key: a hardcoded index breaks the moment an entry
            # is added above it, and does so silently.
            if has_results:
                menu.highlighted = 0
            else:
                keys = [key for key, _label in items]
                menu.highlighted = keys.index("scan") if "scan" in keys else 0
        self.query_one("#footer", Static).update(self._footer_text())

    def _footer_text(self) -> str:
        scan = self.app.stored
        if scan is None:
            return "mod compatibility checker for Project Zomboid"
        if not scan.has_results:
            return (
                f"last scan {scan.saved_label} found no mods, so there is nothing "
                "to open. Check the folders under Settings, then scan again."
            )
        return (
            f"last scan {scan.saved_label}   "
            f"{scan.mod_count} mods   {len(scan.findings)} findings"
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        choice = event.option.id
        if choice == "scan":
            self.app.push_screen(ScanScreen())
        elif choice == "results" and self.app.stored is not None:
            self.app.push_screen(ResultsScreen(self.app.stored))
        elif choice == "manage" and self.app.stored is not None:
            from .manager_screen import ManageScreen

            self.app.push_screen(
                ManageScreen(
                    self.app.stored.mods,
                    self.app.stored.findings,
                    export_dir=self.app.report_path.parent,
                    selection_path=self.app.selection_path,
                    steam_sdk=self.app.steam_sdk,
                    scan=self.app.stored,
                )
            )
        elif choice == "browse":
            from .browse_screen import BrowseScreen

            scan = self.app.stored
            self.app.push_screen(
                BrowseScreen(
                    subscribed=set(scan.subscribed_ids)
                    if scan and scan.subscribed_ids is not None else None,
                    steam_cache=self.app.scan_options.steam_cache,
                    installed_mods=scan.mods if scan else [],
                    build=self.app.scan_options.build,
                )
            )
        elif choice == "gamelog":
            from .gamelog_screen import GameLogScreen
            from .selection import index_by_key, topological_order

            # The order the tool would export, so the screen can say whether it
            # is the order the game actually applied. Built from the saved
            # selection when there is one, so the comparison is against what the
            # user chose rather than everything installed.
            predicted: list[str] = []
            scan = self.app.stored
            if scan is not None and scan.has_mods:
                by_key = index_by_key(list(scan.mods))
                saved = store.load_selection(self.app.selection_path)
                keys = (
                    {s.strip().lower() for s in saved} & set(by_key)
                    if saved
                    else {r.key for r in by_key.values() if r.was_enabled}
                )
                predicted, _cycle = topological_order(
                    by_key, keys, pins=store.load_pins()
                )
            self.app.push_screen(GameLogScreen(predicted=predicted))
        elif choice == "settings":
            from .settings_screen import SettingsScreen

            self.app.push_screen(
                SettingsScreen(self.app.settings, self.app.settings_path)
            )
        elif choice == "quit":
            self.app.exit()

    def action_quit_app(self) -> None:
        self.app.exit()


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #


class ScanScreen(Screen):
    CSS = RETRO_CSS + """
    #scan-area {
        height: 1fr;
        padding: 0 4;
    }
    #scan-title {
        color: #ffffff;
        text-style: bold;
        height: auto;
        padding: 1 0 0 0;
    }
    Log {
        border: solid #b4b4b4;
        background: #000000;
        color: #b4b4b4;
        height: 1fr;
        scrollbar-background: #000000;
        scrollbar-color: #4a4a4a;
    }
    """

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, then: str = "results") -> None:
        super().__init__()
        # "results" after a plain scan, "manage" when a rescan was triggered from
        # the manager, so the user lands back where they were.
        self.then = then

    def compose(self) -> ComposeResult:
        yield Plain("Scanning. This can take a moment on a large mod set.", id="hint")
        with Container(id="scan-area"):
            yield Plain("SCAN IN PROGRESS", id="scan-title")
            yield Log(id="progress", highlight=False)
        yield Plain("ESC to go back to the menu", id="footer")

    def on_mount(self) -> None:
        self.run_scan_worker()

    def append(self, message: str) -> None:
        self.query_one("#progress", Log).write_line(message)

    @work(thread=True, exclusive=True)
    def run_scan_worker(self) -> None:
        app = self.app

        def progress(message: str) -> None:
            app.call_from_thread(self.append, f"  {message}")

        try:
            result = run_scan(app.scan_options, progress=progress)
        except Exception as exc:  # a crashed scan must not kill the interface
            app.call_from_thread(self.append, "")
            app.call_from_thread(self.append, f"  Scan failed: {exc}")
            app.call_from_thread(
                self.append, "  See the log file for the full traceback."
            )
            return

        app.call_from_thread(self.scan_finished, result)

    def scan_finished(self, result: ScanResult) -> None:
        if not result.ok:
            self.append("")
            self.append("  No mod found. Check the Settings screen.")
            return
        report = None
        try:
            report = write_html(self.app.report_path, result, self.app.log_path)
            self.append(f"  Report written to {report}")
        except OSError as exc:
            self.append(f"  Could not write the report: {exc}")
        self.app.report_written = report

        scan = store.from_result(result, report)
        saved = store.save(scan, self.app.store_path)
        if saved:
            self.append("  Saved, so the next launch can offer these results again.")
        self.app.stored = scan

        self.append("")
        if self.then == "manage":
            from .manager_screen import ManageScreen

            self.append("  Reopening the manager with the new scan...")
            self.app.switch_screen(
                ManageScreen(
                    scan.mods,
                    scan.findings,
                    export_dir=self.app.report_path.parent,
                    selection_path=self.app.selection_path,
                    steam_sdk=self.app.steam_sdk,
                    scan=scan,
                )
            )
        else:
            self.append("  Opening results...")
            self.app.switch_screen(ResultsScreen(scan))


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


class ResultsScreen(Screen):
    CSS = RETRO_CSS + """
    #summary {
        color: #8a8a8a;
        padding: 0 2;
        height: auto;
    }
    #body {
        height: 1fr;
        padding: 0 2;
    }
    #list {
        width: 3fr;
        height: 100%;
        border: solid #b4b4b4;
        background: #000000;
        color: #b4b4b4;
        overflow-x: hidden;
        scrollbar-background: #000000;
        scrollbar-color: #4a4a4a;
    }
    #detailbox {
        width: 2fr;
        height: 100%;
        border: solid #b4b4b4;
        background: #000000;
        padding: 0 1;
        scrollbar-background: #000000;
        scrollbar-color: #4a4a4a;
    }
    #detail {
        color: #b4b4b4;
        height: auto;
    }
    DataTable > .datatable--cursor {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }
    DataTable > .datatable--header {
        background: #000000;
        color: #ffffff;
        text-style: bold;
    }
    DataTable > .datatable--hover {
        background: #000000;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "Menu"),
        Binding("0", "filter_all", "All"),
        Binding("1", "filter_critical", "Critical"),
        Binding("2", "filter_high", "High"),
        Binding("3", "filter_medium", "Medium"),
        Binding("4", "filter_low", "Low"),
        Binding("5", "filter_info", "Info"),
        Binding("r,R", "open_report", "Report"),
        Binding("m,M", "manage", "Manage"),
    ]

    def __init__(self, scan: StoredScan) -> None:
        super().__init__()
        self.scan = scan
        self.severity_filter: Severity | None = None
        self.rows: list[Finding] = []

    def compose(self) -> ComposeResult:
        yield Plain(
            "Arrow keys to move, 1-5 to filter by severity, 0 for all\n"
            "'r' opens the HTML report, 'm' manages the mods, ESC returns to the menu",
            id="hint",
        )
        yield Plain("", id="summary")
        with Horizontal(id="body"):
            yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
            yield VerticalScroll(Plain("", id="detail"), id="detailbox")
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        self.refresh_rows()
        self.query_one("#list", DataTable).focus()

    def _summary(self) -> str:
        scan = self.scan
        counts = counts_by_severity(scan.findings)
        breakdown = "   ".join(
            f"{MARKER[s]} {s.label} {counts[s]}" for s in SEVERITY_ORDER if counts[s]
        )
        order = (
            "load order known"
            if scan.has_order
            else "no load order, winners cannot be named"
        )
        head = (
            f"{scan.mod_count} mods   {scan.file_count} files   "
            f"{len(scan.findings)} findings   {order}   scanned {scan.saved_label}"
        )
        return f"{head}\n{breakdown}" if breakdown else head

    def refresh_rows(self) -> None:
        table = self.query_one("#list", DataTable)
        if not table.columns:
            table.add_columns("SEV.", "FINDING")
        table.clear()
        self.rows = [
            f
            for f in self.scan.findings
            if self.severity_filter is None or f.severity is self.severity_filter
        ]
        for index, finding in enumerate(self.rows):
            table.add_row(
                cell(MARKER[finding.severity]), cell(finding.title), key=str(index)
            )
        self.query_one("#summary", Static).update(self._summary())
        report = getattr(self.app, "report_written", None) or self.scan.report_path
        self.query_one("#footer", Static).update(
            f"'r' opens the report: {report}" if report else "no report written"
        )
        if self.rows:
            self.show_detail(self.rows[0])
        else:
            self.query_one("#detail", Static).update(
                "\n  Nothing at this severity.\n\n  Press '0' to see everything again."
            )

    def show_detail(self, finding: Finding) -> None:
        rule = "-" * 34
        lines: list[tuple[str, bool]] = [
            ("", False),
            (f"{MARKER[finding.severity]}  {finding.severity.label.upper()}", False),
            (finding.title, True),
            (rule, False),
            ("", False),
            (finding.detail, False),
            ("", False),
            (f"Rule   {finding.rule}", False),
            (f"Mods   {', '.join(dict.fromkeys(finding.mods)) or '-'}", False),
        ]
        if finding.winner:
            lines.append((f"Wins   {finding.winner}", False))
        if finding.advice:
            lines += [("", False), (rule, False), ("", False), (finding.advice, False)]
        if finding.evidence:
            lines += [("", False), (rule, False), ("", False),
                      (f"{len(finding.evidence)} item(s)", True), ("", False)]
            lines += [(f"  {item}", False) for item in finding.evidence]
        self.query_one("#detail", Static).update(panel(lines))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            index = int(event.row_key.value)
        except (TypeError, ValueError):
            return
        if 0 <= index < len(self.rows):
            self.show_detail(self.rows[index])

    def _set_filter(self, severity: Severity | None) -> None:
        self.severity_filter = severity
        self.refresh_rows()

    def action_filter_all(self) -> None:
        self._set_filter(None)

    def action_filter_critical(self) -> None:
        self._set_filter(Severity.CRITICAL)

    def action_filter_high(self) -> None:
        self._set_filter(Severity.HIGH)

    def action_filter_medium(self) -> None:
        self._set_filter(Severity.MEDIUM)

    def action_filter_low(self) -> None:
        self._set_filter(Severity.LOW)

    def action_filter_info(self) -> None:
        self._set_filter(Severity.INFO)

    def action_open_report(self) -> None:
        report = getattr(self.app, "report_written", None) or self.scan.report_path
        if report and Path(report).is_file():
            webbrowser.open(Path(report).resolve().as_uri())
            return
        # Silently doing nothing is the worst of the three options here.
        self.query_one("#footer", Static).update(
            f"the report is not there any more: {report}" if report
            else "no report was written for this scan"
        )

    def action_manage(self) -> None:
        from .manager_screen import ManageScreen

        self.app.push_screen(
            ManageScreen(
                self.scan.mods,
                self.scan.findings,
                export_dir=self.app.report_path.parent,
                selection_path=self.app.selection_path,
                steam_sdk=self.app.steam_sdk,
                scan=self.scan,
            )
        )

    def action_back(self) -> None:
        self.app.pop_screen()


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


class ModCheckApp(App):
    """Holds the scan settings and the last result; the screens do the rest."""

    CSS = RETRO_CSS

    def __init__(
        self,
        options: ScanOptions,
        log_path: Path | None = None,
        report_path: Path | None = None,
        store_path: Path | None = None,
        selection_path: Path | None = None,
        open_manager: bool = False,
        steam_sdk: Path | None = None,
        settings=None,
        settings_path: Path | None = None,
        cli_overrides=None,
    ) -> None:
        super().__init__()
        self.selection_path = selection_path
        self.open_manager = open_manager
        from .settings import Settings

        self.settings = settings or Settings()
        self.settings_path = settings_path
        # What the command line actually said, kept apart from the saved
        # settings. A typed option must keep winning for the whole session, and
        # everything else must be read fresh, because the settings screen can
        # change any of it between two scans.
        self.cli_options = options
        self.cli_overrides = set(cli_overrides or ())
        self._steam_sdk_override = steam_sdk if "steam_sdk" in self.cli_overrides else None
        self.log_path = log_path
        self.report_path = Path(report_path or "pzmodmanager-report.html")
        self.store_path = store_path
        # A previous run leaves its results behind, which is what turns the
        # first-run menu into one offering "Last results" and "Rescan".
        self.stored: StoredScan | None = store.load(store_path)
        self.report_written: Path | None = None

    # ------------------------------------------------------------- live state --
    #
    # These two used to be plain attributes set once in __init__, and that was a
    # bug with two faces: a scan started after a settings change still ran the
    # values from launch, and the unsubscribe screen still looked for the Steam
    # library where it was at launch, which is why it reported none after the
    # path had just been filled in. Nothing may cache what the settings screen
    # can change. Both are read at the moment they are used.

    @property
    def steam_sdk(self) -> Path | None:
        """Where the Steam library is, as of right now."""
        if self._steam_sdk_override is not None:
            return self._steam_sdk_override
        return self.settings.steam_sdk_path

    @property
    def scan_options(self) -> ScanOptions:
        """The options a scan started now should use.

        Built from the saved settings every time, then overwritten by whatever
        was typed on the command line, so a typed option still wins without
        freezing the rest alongside it.
        """
        options = ScanOptions(
            extra_paths=[Path(p).expanduser() for p in self.settings.extra_paths],
            use_defaults=self.settings.use_defaults,
            build=self.settings.build,
            parse_scripts=self.settings.parse_scripts,
            order_path=self.settings.order_path_or_none,
            list_name=self.cli_options.list_name,
            only_enabled=self.settings.only_enabled,
            use_steam=self.settings.use_steam,
            steam_cache=self.cli_options.steam_cache,
            steam_sdk=self.settings.steam_sdk_path,
        )
        for name in self.cli_overrides:
            setattr(options, name, getattr(self.cli_options, name))
        return options

    def on_mount(self) -> None:
        self.title = "pzmodmanager"
        self.register_theme(MONOCHROME)
        self.theme = MONOCHROME.name
        self.push_screen(MenuScreen())
        if self.open_manager and self.stored is not None:
            from .manager_screen import ManageScreen

            self.push_screen(
                ManageScreen(
                    self.stored.mods,
                    self.stored.findings,
                    export_dir=self.report_path.parent,
                    selection_path=self.selection_path,
                    steam_sdk=self.steam_sdk,
                    scan=self.stored,
                )
            )


def run_tui(
    options: ScanOptions,
    log_path: Path | None = None,
    report_path: Path | None = None,
    store_path: Path | None = None,
    selection_path: Path | None = None,
    open_manager: bool = False,
    steam_sdk: Path | None = None,
    settings=None,
    settings_path: Path | None = None,
    cli_overrides=None,
) -> None:
    ModCheckApp(
        options,
        log_path,
        report_path,
        store_path,
        selection_path,
        open_manager,
        steam_sdk,
        settings,
        settings_path,
        cli_overrides,
    ).run()
