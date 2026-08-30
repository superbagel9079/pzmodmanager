"""The mod manager screen.

A list of every installed mod with a checkbox, a search box, and a panel that
revalidates the whole selection on every keystroke. Selecting a mod pulls in what
it requires, because a selection missing a dependency is never what anyone meant.
Everything else is reported rather than decided: an incompatibility between two
mods you want is your call, not the tool's.

Nothing here writes to the game. The output is the load order and the two lines a
server ini needs.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, Static

from . import store
from .posters import pillow_available, poster_blocks
from .tui import RETRO_CSS as _RETRO
from .tui import Plain, cell, panel as _panel
from .selection import (
    ModRef,
    Problem,
    dependency_closure,
    dependents_of,
    export_links,
    export_server_ini,
    export_text,
    index_by_key,
    order_notes,
    pin_edges,
    summarise,
    unsubscribe_plan,
    topological_order,
    validate,
)

# Wrapped in a Text object when written to the table: Rich would otherwise read
# "[x]" as a markup tag and drop it, leaving an empty column.
CHECKED = "[x]"
UNCHECKED = "[ ]"

# What the problems panel shows, cycled with 'h'. A mod set of two hundred
# throws a lot of low findings, mostly typos in other people's mod.info, and
# eighteen of those bury the one critical that actually stops the game loading.
# The counts in the footer always stay honest: this hides rows, not facts.
PROBLEM_VIEWS = [
    ("everything", 0),
    ("hiding low", 2),
    ("critical only", 4),
]

# How many load order notes the panel quotes before summarising the rest. A full
# selection of two hundred mods turns up around thirty of them.
NOTES_SHOWN = 8


_OWN_CSS = """
#manage-body {
    height: 1fr;
    padding: 0 2;
}
#mods {
    width: 3fr;
    height: 100%;
    border: solid #b4b4b4;
    background: #000000;
    color: #b4b4b4;
    overflow-x: hidden;
    scrollbar-background: #000000;
    scrollbar-color: #4a4a4a;
}
#sidebox {
    width: 2fr;
    height: 100%;
    border: solid #b4b4b4;
    background: #000000;
    padding: 0 1;
    scrollbar-background: #000000;
    scrollbar-color: #4a4a4a;
}
#side {
    color: #b4b4b4;
    height: auto;
}
#poster {
    height: auto;
    padding: 1 0 0 1;
}
#search {
    background: #000000;
    color: #b4b4b4;
    border: solid #4a4a4a;
    margin: 0 2;
    height: 3;
}
#search:focus {
    border: solid #ffffff;
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


class ExportScreen(ModalScreen):
    """Shows the generated ini lines and says where they were written."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Back"),
        Binding("q,Q", "dismiss_screen", "Back"),
    ]

    CSS = _RETRO + """
    ExportScreen {
        align: center middle;
        background: #000000;
    }
    #export-box {
        width: 88;
        height: auto;
        max-height: 80%;
        border: solid #b4b4b4;
        background: #000000;
        color: #b4b4b4;
        padding: 1 2;
    }
    """

    def __init__(self, body) -> None:
        super().__init__()
        self.body = body

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Plain(self.body, id="export-text"), id="export-box")

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class PinsScreen(ModalScreen):
    """The ordering constraints stated by hand, and a way to drop them.

    Removing one is the only destructive thing here, and it destroys nothing but
    a line the user typed. It is applied when the screen closes, so ESC leaves
    everything exactly as it was.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q,Q", "cancel", "Cancel"),
        # ENTER is not a binding here. A DataTable with a row cursor takes it
        # first and turns it into RowSelected, so a binding for it never fires;
        # the handler below is what actually runs. Declaring both would risk
        # toggling twice the day Textual stops swallowing it.
        Binding("d,D", "drop", "Remove"),
        Binding("s,S", "save", "Save and close"),
    ]

    CSS = _RETRO + """
    PinsScreen {
        align: center middle;
        background: #000000;
    }
    #pins-box {
        width: 96;
        height: auto;
        max-height: 80%;
        border: solid #b4b4b4;
        background: #000000;
        color: #b4b4b4;
        padding: 1 2;
    }
    #pins-table {
        height: auto;
        max-height: 24;
        background: #000000;
        color: #b4b4b4;
    }
    """

    def __init__(self, pins: list[tuple[str, str]], by_key: dict[str, ModRef]) -> None:
        super().__init__()
        self.pins = list(pins)
        self.by_key = by_key
        self.dropped: set[int] = set()

    def compose(self) -> ComposeResult:
        with Container(id="pins-box"):
            yield Plain(
                "LOAD ORDER PINS\n\n"
                "Ordering you stated by hand, treated exactly like a declared\n"
                "requirement when the order is worked out.\n\n"
                "ENTER or 'd' marks a pin for removal or puts it back, 's'\n"
                "saves and closes, ESC closes and changes nothing.",
                id="pins-help",
            )
            yield DataTable(id="pins-table", cursor_type="row", zebra_stripes=False)
            yield Plain("", id="pins-footer")

    def on_mount(self) -> None:
        table = self.query_one("#pins-table", DataTable)
        table.add_column("", width=3)
        table.add_column("LOADS FIRST", width=34)
        table.add_column("THEN", width=34)
        table.add_column("STATUS")
        self.redraw()
        table.focus()

    def status_of(self, before: str, after: str) -> str:
        """Whether a pin can do anything, which is not the same as being saved."""
        missing = [
            mod_id
            for mod_id in (before, after)
            if mod_id.strip().lower() not in self.by_key
        ]
        if missing:
            return f"not installed: {', '.join(missing)}"
        return ""

    def redraw(self) -> None:
        table = self.query_one("#pins-table", DataTable)
        row = table.cursor_row
        table.clear()
        for index, (before, after) in enumerate(self.pins):
            mark = "[-]" if index in self.dropped else "   "
            table.add_row(
                cell(mark), cell(before), cell(after),
                cell(self.status_of(before, after)),
            )
        if self.pins:
            table.move_cursor(row=min(row, len(self.pins) - 1))
        kept = len(self.pins) - len(self.dropped)
        self.query_one("#pins-footer", Static).update(
            Text(
                f"{len(self.pins)} pin(s), {len(self.dropped)} marked for removal, "
                f"{kept} would remain"
                if self.dropped
                else f"{len(self.pins)} pin(s)"
            )
        )

    def on_data_table_row_selected(self, event) -> None:
        """ENTER on a row. See the note next to the bindings."""
        self.action_drop()

    def action_drop(self) -> None:
        table = self.query_one("#pins-table", DataTable)
        index = table.cursor_row
        if not (0 <= index < len(self.pins)):
            return
        self.dropped.symmetric_difference_update({index})
        self.redraw()

    def action_save(self) -> None:
        self.dismiss([p for i, p in enumerate(self.pins) if i not in self.dropped])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ManageScreen(Screen):
    """Pick the mods to run, with dependencies handled and conflicts flagged."""

    CSS = _RETRO + _OWN_CSS

    BINDINGS = [
        Binding("escape", "back", "Menu"),
        Binding("space", "toggle", "Toggle"),
        Binding("x,X", "toggle", "Toggle"),
        Binding("a,A", "select_all", "All"),
        Binding("n,N", "select_none", "None"),
        Binding("r,R", "restore_scanned", "Scanned list"),
        Binding("o,O", "toggle_order_view", "Order view"),
        Binding("b,B", "pin", "Pin before"),
        Binding("v,V", "view_pins", "Pins"),
        Binding("d,D", "add_dependencies", "Deps"),
        Binding("w,W", "open_workshop", "Workshop"),
        Binding("p,P", "open_problem_link", "Problem link"),
        Binding("e,E", "export", "Export"),
        Binding("u,U", "unsubscribe", "Unsubscribe"),
        Binding("h,H", "cycle_problems", "Filter problems"),
        Binding("slash", "focus_search", "Search"),
    ]

    def __init__(
        self,
        refs: list[ModRef],
        findings,
        export_dir: Path | None = None,
        selection_path: Path | None = None,
        steam_sdk: Path | None = None,
        scan=None,
        pins_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.selection_path = selection_path
        self.pins_path = pins_path or store.default_pins_path()
        # Ordering the user stated by hand, as (loads first, loads second) mod
        # ids. Kept as ids rather than keys so the file reads the way the mods
        # are actually named.
        self.pins: list[tuple[str, str]] = store.load_pins(self.pins_path)
        # The first half of a pin being built, while waiting for the second.
        self.pin_anchor: str | None = None
        self.steam_sdk = steam_sdk
        # Where the enabled flags came from, and when. Without a mod list the
        # scan leaves every mod marked enabled, which would make 'r' silently
        # tick all 253 boxes and look like a bug rather than an empty source.
        self.has_order = bool(getattr(scan, "has_order", False))
        self.order_source = str(getattr(scan, "order_source", "") or "")
        self.scan_label = str(getattr(scan, "saved_label", "") or "")
        # One row per mod id. Two folders sharing an id are one selectable mod,
        # and the duplicate itself is already reported as a finding.
        self.by_key = index_by_key(list(refs))
        self.refs = sorted(
            self.by_key.values(), key=lambda r: (r.name or r.mod_id).lower()
        )
        self.findings = findings
        self.export_dir = Path(export_dir or Path.cwd())
        self.selected: set[str] = set()
        self.filter_text = ""
        self.visible_refs: list[ModRef] = []
        self.problems: list[Problem] = []
        self.problem_view = 0
        self.order_view = False
        self.notice = ""
        # What is actually drawn in the table right now, one tuple per row. Kept
        # so a refresh can rewrite only the cells whose text changed instead of
        # emptying the table and filling it again.
        self.drawn_rows: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------------ view --

    def compose(self) -> ComposeResult:
        yield Plain(
            "'x' or SPACE toggles, '/' searches, 'a' all, 'n' none, "
            "'r' restores the list found at the scan\n"
            "'o' shows the load order that will be exported, numbered\n"
            "'b' on a mod, then 'b' on another, pins the first to load before it; "
            "'v' lists your pins\n"
            "'d' adds dependencies, 'w' opens this mod on the Workshop, 'p' opens the "
            "first problem link\n"
            "'e' exports, 'u' unsubscribes the deselected mods from Steam\n"
            "'h' hides the minor problems, ESC returns",
            id="hint",
        )
        yield Input(placeholder="search...", id="search")
        with Horizontal(id="manage-body"):
            yield DataTable(id="mods", cursor_type="row", zebra_stripes=False)
            yield VerticalScroll(
                Plain("", id="poster"), Plain("", id="side"), id="sidebox"
            )
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        table = self.query_one("#mods", DataTable)
        table.add_column("ON", width=3)
        table.add_column("MOD", width=40)
        table.add_column("ID")
        saved = store.load_selection(self.selection_path)
        if saved:
            known = {s.strip().lower() for s in saved} & set(self.by_key)
            self.selected = known
            self.notice = f"restored a saved selection of {len(known)} mod(s)"
        else:
            # Start from whatever the scan recorded as enabled.
            self.selected = {r.key for r in self.refs if r.was_enabled}
            self.notice = f"starting from the mods {self.scan_origin}"
        self.refresh_all()
        table.focus()

    # --------------------------------------------------------------- content --

    def _matches(self, ref: ModRef) -> bool:
        if not self.filter_text:
            return True
        needle = self.filter_text.lower()
        return needle in ref.mod_id.lower() or needle in (ref.name or "").lower()

    def refresh_all(self) -> None:
        self.problems = validate(
            self.by_key, self.selected, self.findings, pins=self.pins
        )
        self.refresh_table()
        self.refresh_side()

    def refresh_table(self) -> None:
        table = self.query_one("#mods", DataTable)
        # A refresh can be asked for before on_mount has run, for instance by a
        # screen callback arriving early. Creating the columns here keeps that
        # from raising instead of drawing.
        if not table.columns:
            table.add_column("ON", width=3)
            table.add_column("MOD", width=40)
            table.add_column("ID")
        base, positions = self._listing()
        wanted = [r for r in base if self._matches(r)]
        # The same problems the panel is showing, so the marker and the panel
        # never disagree. With every mod flagged by a low typo, a list of two
        # hundred exclamation marks says nothing at all, and hiding the problems
        # while keeping their markers would be the worst of both.
        flagged = {m.strip().lower() for p in self.shown_problems() for m in p.mods}
        rows = [self._row_for(ref, flagged, positions) for ref in wanted]
        # Ticking a box changes at most a handful of cells: the box itself, the
        # boxes of any dependency it pulled in, and the exclamation marks the new
        # problem list moved. The rows themselves are the same rows, in the same
        # order. Rewriting only what changed is the difference between one small
        # repaint and two full ones, which is what the flicker was.
        same_rows = [r.key for r in wanted] == [r.key for r in self.visible_refs]
        self.visible_refs = wanted
        if same_rows and table.row_count == len(rows):
            self._patch_rows(table, rows)
        else:
            self._rebuild_rows(table, rows)
        self.drawn_rows = rows
        self.query_one("#footer", Static).update(
            summarise(self.by_key, self.selected, self.problems)
            + (f"   {self.notice}" if self.notice else "")
        )

    @property
    def scan_origin(self) -> str:
        """"enabled in <file>, read <when>", as much of it as is known.

        The date is not decoration. A list saved in game after the scan is not
        in here, and the only way to notice that is to be told how old this one
        is.
        """
        parts = ["the scan found enabled"]
        if self.order_source:
            parts.append(f"in {Path(self.order_source).name}")
        if self.scan_label:
            parts.append(f"(scanned {self.scan_label})")
        return " ".join(parts)

    def resolved_order(self) -> tuple[list[str], list[str]]:
        """The load order this selection would export, and any cycle in it.

        One computation with three readers: the panel's "order" line, the order
        view, and the export itself. They used to be two. The panel called
        topological_order without `preferred` while the export called it with, so
        the panel could report on a sequence that was not the one written out.
        """
        return topological_order(
            self.by_key,
            self.selected,
            pins=self.pins,
            preferred=[
                r.mod_id
                for r in sorted(
                    (r for r in self.refs if r.order_index is not None),
                    key=lambda r: r.order_index,
                )
            ],
        )

    def _listing(self) -> tuple[list[ModRef], dict[str, int]]:
        """The rows to draw and, in order view, the position of each.

        Order view puts the selected mods in load order and numbers them, then
        the rest alphabetically underneath. Showing only the selection would be
        tidier, but then unticking a mod would make its row vanish under the
        cursor. Here it moves down into the unnumbered part instead, which is
        also where you go to tick something new.
        """
        if not self.order_view:
            return list(self.refs), {}
        ordered, _cycle = self.resolved_order()
        keys = [k for k in (m.strip().lower() for m in ordered) if k in self.by_key]
        positions = {key: index + 1 for index, key in enumerate(keys)}
        rest = [r for r in self.refs if r.key not in positions]
        return [self.by_key[k] for k in keys] + rest, positions

    def _row_for(
        self, ref: ModRef, flagged: set[str], positions: dict[str, int]
    ) -> tuple[str, str, str]:
        """The three cells a mod occupies, as plain strings so they compare."""
        mark = CHECKED if ref.key in self.selected else UNCHECKED
        name = ref.name or ref.mod_id
        if ref.key in flagged and ref.key in self.selected:
            name = f"! {name}"
        if self.order_view:
            place = positions.get(ref.key)
            name = f"{place:>3}  {name}" if place else f"  .  {name}"
        return (mark, name, ref.mod_id)

    def _patch_rows(self, table: DataTable, rows: list[tuple[str, str, str]]) -> None:
        """Rewrite only the cells that differ, leaving cursor and scroll alone.

        Nothing here empties the table, so there is no scroll reset to undo and
        no cursor to put back. That is the whole point: the old code had to
        restore both, and the restoring was itself a second visible repaint.
        """
        for index, row in enumerate(rows):
            was = self.drawn_rows[index] if index < len(self.drawn_rows) else None
            for column, value in enumerate(row):
                if was is not None and was[column] == value:
                    continue
                table.update_cell_at(
                    Coordinate(index, column), cell(value), update_width=False
                )

    def _rebuild_rows(self, table: DataTable, rows: list[tuple[str, str, str]]) -> None:
        """Draw the table from scratch, for when the set of rows really changed.

        Searching is the case that lands here. The cursor is kept in range and
        scrolled into view normally, because the row it used to sit on may not
        exist any more.
        """
        previous = table.cursor_row
        table.clear()
        for index, row in enumerate(rows):
            table.add_row(*(cell(value) for value in row), key=str(index))
        if rows:
            table.move_cursor(row=min(previous, len(rows) - 1))

    def refresh_poster(self, current) -> None:
        """Draw the highlighted mod's poster, if there is one to draw."""
        widget = self.query_one("#poster", Static)
        if current is None:
            widget.update("")
            return
        # Small on purpose: the panel also has to show the problems.
        art = poster_blocks(
            Path(current.poster_path) if current.poster_path else None,
            width=22,
            max_rows=10,
        )
        if art is not None:
            widget.update(art)
        elif not pillow_available():
            widget.update(Text("install Pillow to see mod posters here", style="dim"))
        else:
            widget.update(Text("no poster shipped with this mod", style="dim"))

    def shown_problems(self) -> list[Problem]:
        """The problems the panel is currently showing, worst first."""
        _label, floor = PROBLEM_VIEWS[self.problem_view]
        return [p for p in self.problems if p.severity.weight >= floor]

    def _problems_heading(self) -> str:
        label, _floor = PROBLEM_VIEWS[self.problem_view]
        shown = len(self.shown_problems())
        if shown == len(self.problems):
            return f"PROBLEMS ({shown})"
        return f"PROBLEMS ({shown} of {len(self.problems)}, {label})"

    def action_cycle_problems(self) -> None:
        self.problem_view = (self.problem_view + 1) % len(PROBLEM_VIEWS)
        label, _floor = PROBLEM_VIEWS[self.problem_view]
        hidden = len(self.problems) - len(self.shown_problems())
        self.notice = (
            f"showing {label}" + (f", {hidden} hidden" if hidden else "")
        )
        self.refresh_table()
        self.refresh_side()

    def refresh_side(self) -> None:
        rule = "-" * 34
        # (text, bold). Nothing here is markup, so nothing here can be misread
        # as markup: mod names and problem messages arrive full of brackets.
        lines: list[tuple[str, bool]] = [("", False), ("SELECTION", True), ("", False)]
        add = lambda line: lines.append((line, False))  # noqa: E731
        add(f"  selected   {len(self.selected)}")
        add(f"  installed  {len(self.by_key)}")
        _ordered, cycle = self.resolved_order()
        add(f"  order      {'has a cycle' if cycle else 'resolved'}")
        add(f"  view       {'load order' if self.order_view else 'alphabetical'}")
        if self.pins:
            # Two counts, because they answer different questions: how many you
            # have written down, and how many are actually shaping this order. A
            # pin whose mods are not both selected does nothing, silently.
            active = len(pin_edges(self.by_key, self.selected, self.pins))
            suffix = "" if active == len(self.pins) else f" ({active} in effect)"
            add(f"  pins       {len(self.pins)}{suffix}, press 'v'")
        if self.pin_anchor:
            add("")
            add(f"  PINNING    {self.pin_anchor} loads first")
            add("             highlight the mod it comes before, press 'b'")
            add("             'v' cancels")

        current = self.current_ref()
        self.refresh_poster(current)
        if current:
            lines += [("", False), (rule, False), ("", False),
                      (current.name or current.mod_id, True)]
            add(f"  id        {current.mod_id}")
            if current.workshop_url:
                add(f"  workshop  {current.workshop_id}")
                add(f"  link      {current.workshop_url}")
                add("            press 'w' to open it in Steam")
            add(f"  source    {current.source or 'unknown'}")
            if current.requires:
                add(f"  requires  {', '.join(current.requires)}")
            if current.incompatible:
                add(f"  conflicts {', '.join(current.incompatible)}")
            breaks = dependents_of(self.by_key, current.key, self.selected)
            if breaks and current.key in self.selected:
                add(f"  needed by {', '.join(breaks)}")
            if current.order_notes:
                # Quoted, never acted on. The tool can order what require= says;
                # this is the part of the page it can only point at.
                add("  order     its Workshop page says where to put it:")
                for note in current.order_notes:
                    add(f"            {note}")

        lines += [("", False), (rule, False), ("", False),
                  (self._problems_heading(), True), ("", False)]
        shown = self.shown_problems()
        if not self.problems:
            add("  Nothing blocking this selection.")
        elif not shown:
            _label, _floor = PROBLEM_VIEWS[self.problem_view]
            add(f"  Nothing at this level. {len(self.problems)} hidden.")
            add("  Press 'h' to show them.")
        for problem in shown[:20]:
            add(f"  [{problem.severity.label}] {problem.message}")
            if problem.fix_hint:
                add(f"      {problem.fix_hint}")
            for label, url in problem.links:
                add(f"      {label}:")
                add(f"      {url}")
            add("")
        if len(shown) > 20:
            add(f"  ... {len(shown) - 20} more")

        # Its own block, with its own count, below the problems and outside
        # their total. These are quotations from a Workshop page, not defects:
        # listing them as problems made three sentences look like three new
        # errors and put an exclamation mark on mods with nothing wrong. Below
        # rather than above because a large selection produces a lot of them,
        # and they must never push the actual problems off the top.
        notes = order_notes(self.by_key, self.selected)
        if notes:
            lines += [("", False), (rule, False), ("", False),
                      (f"LOAD ORDER NOTES ({len(notes)})", True), ("", False)]
            add("  Not problems, and not counted as any. These pages say where")
            add("  to place the mod, in words no tool can turn into an order.")
            add("")
            for note in notes[:NOTES_SHOWN]:
                add(f"  {note.mod_id}")
                for line in note.lines:
                    add(f"      {line}")
                if note.url:
                    add(f"      {note.url}")
                add("")
            if len(notes) > NOTES_SHOWN:
                add(f"  ... {len(notes) - NOTES_SHOWN} more")

        self.query_one("#side", Static).update(_panel(lines))

    def current_ref(self) -> ModRef | None:
        table = self.query_one("#mods", DataTable)
        index = table.cursor_row
        if 0 <= index < len(self.visible_refs):
            return self.visible_refs[index]
        return None

    # --------------------------------------------------------------- actions --

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.refresh_side()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#mods", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_toggle(self) -> None:
        ref = self.current_ref()
        if ref is None:
            return
        if ref.key in self.selected:
            broken = dependents_of(self.by_key, ref.key, self.selected)
            self.selected.discard(ref.key)
            if broken:
                verb = "still needs it" if len(broken) == 1 else "still need it"
                self.notice = f"dropped {ref.mod_id}; {', '.join(broken)} {verb}"
            else:
                self.notice = f"dropped {ref.mod_id}"
        else:
            before = set(self.selected)
            closed, missing = dependency_closure(self.by_key, self.selected | {ref.key})
            self.selected = closed
            pulled = sorted(closed - before - {ref.key})
            parts = [f"added {ref.mod_id}"]
            if pulled:
                names = ", ".join(self.by_key[k].mod_id for k in pulled)
                parts.append(f"with its dependencies: {names}")
            if missing:
                parts.append(f"missing from disk: {', '.join(missing)}")
            self.notice = "; ".join(parts)
        self.refresh_all()

    def action_select_all(self) -> None:
        self.selected = set(self.by_key)
        self.notice = "selected everything installed"
        self.refresh_all()

    def action_select_none(self) -> None:
        self.selected = set()
        self.notice = "cleared the selection"
        self.refresh_all()

    def action_pin(self) -> None:
        """Say by hand that one mod loads before another. Two presses of 'b'.

        The first press holds the mod that must come first, the second names the
        one that follows. Two presses rather than a dialog because the mods have
        to be found in the list anyway, and the list already has a search box.

        A pin that would close a loop is refused outright, with both mods named.
        Accepting it would produce an order no sorting can satisfy, and the only
        symptom would be a cycle warning somewhere else entirely.
        """
        ref = self.current_ref()
        if ref is None:
            return
        if self.pin_anchor is None:
            self.pin_anchor = ref.mod_id
            self.notice = (
                f"{ref.mod_id} will load first: highlight the mod it must come "
                "before and press 'b' again ('v' to cancel)"
            )
            self.refresh_side()
            return

        first, second = self.pin_anchor, ref.mod_id
        self.pin_anchor = None
        if first.strip().lower() == second.strip().lower():
            self.notice = "a mod cannot load before itself; nothing was pinned"
            self.refresh_all()
            return
        if (first, second) in self.pins:
            self.notice = f"already pinned: {first} loads before {second}"
            self.refresh_all()
            return

        candidate = self.pins + [(first, second)]
        _ordered, cycle = topological_order(
            self.by_key, self.selected, pins=candidate
        )
        if cycle:
            self.notice = (
                f"refused: {first} before {second} would close a loop with "
                f"{', '.join(cycle[:4])}"
            )
            self.refresh_all()
            return

        self.pins = candidate
        store.save_pins(self.pins, self.pins_path)
        self.notice = f"pinned: {first} loads before {second}"
        self.refresh_all()

    def action_view_pins(self) -> None:
        """List the pins, and cancel a half finished one on the way in."""
        if self.pin_anchor is not None:
            self.pin_anchor = None
            self.notice = "pin cancelled"
            self.refresh_all()
            return
        self.app.push_screen(PinsScreen(self.pins, self.by_key), self._after_pins)

    def _after_pins(self, kept: list[tuple[str, str]] | None) -> None:
        if kept is None or kept == self.pins:
            return
        removed = len(self.pins) - len(kept)
        self.pins = kept
        store.save_pins(self.pins, self.pins_path)
        self.notice = f"removed {removed} pin(s)"
        self.refresh_all()

    def action_toggle_order_view(self) -> None:
        """Swap between the alphabetical list and the order that gets exported.

        Worth being clear about what this is not: it does not choose an order,
        it shows the one already being computed. Until now that sequence only
        existed inside the exported file, so the first chance to see it was
        after writing it out.
        """
        self.order_view = not self.order_view
        if self.order_view:
            _ordered, cycle = self.resolved_order()
            self.notice = (
                "load order view: dependencies before dependents, unselected "
                "mods below"
            )
            if cycle:
                self.notice += f"; {len(cycle)} mod(s) are in a loop and sit at the end"
        else:
            self.notice = "alphabetical view"
        self.refresh_all()

    def action_restore_scanned(self) -> None:
        """Put the ticks back to what the scan recorded as enabled.

        Not the current state of the game. The scan read a mod list once and
        froze it; anything saved in game since then is invisible until the next
        scan. Saying so in the notice is the point: the old wording called this
        "the load order", which promised something it never did. It restores
        which mods are on, never the sequence they load in.
        """
        if not self.has_order:
            self.notice = (
                "the scan found no mod list to read, so there is nothing to "
                "restore; the selection is unchanged"
            )
            self.refresh_all()
            return
        self.selected = {r.key for r in self.refs if r.was_enabled}
        self.notice = f"restored the {len(self.selected)} mod(s) {self.scan_origin}"
        self.refresh_all()

    def action_add_dependencies(self) -> None:
        before = set(self.selected)
        closed, missing = dependency_closure(self.by_key, self.selected)
        self.selected = closed
        added = len(closed - before)
        if added:
            self.notice = f"pulled in {added} dependenc{'y' if added == 1 else 'ies'}"
        else:
            self.notice = "no dependency was missing"
        if missing:
            self.notice += f"; not installed: {', '.join(missing)}"
        self.refresh_all()

    def action_open_workshop(self) -> None:
        ref = self.current_ref()
        if ref is None:
            return
        url = ref.steam_client_url
        if not url:
            self.notice = f"{ref.mod_id} is a local mod, it has no Workshop page"
        else:
            # steam:// opens the item in the Steam client, where Unsubscribe is.
            webbrowser.open(url)
            self.notice = f"opened the Workshop page for {ref.mod_id}"
        self.refresh_table()

    def action_open_problem_link(self) -> None:
        """Open the first link of the first problem, usually the useful one."""
        for problem in self.problems:
            if problem.links:
                label, url = problem.links[0]
                webbrowser.open(url)
                self.notice = f"opened: {label}"
                self.refresh_table()
                return
        self.notice = "no problem has a link to open"
        self.refresh_table()

    def action_unsubscribe(self) -> None:
        """Hand over to the full screen, which does the confirming."""
        from .steamsdk import find_library
        from .unsubscribe_screen import UnsubscribeScreen

        # Grouped by Workshop item, because that is the unit Steam removes. A
        # deselected mod sharing an item with one you kept is NOT a target:
        # unsubscribing would delete the one you kept, and the old code did
        # exactly that while listing only the mod you dropped.
        targets, held = unsubscribe_plan(self.by_key, self.selected)
        if not targets:
            if held:
                names = ", ".join(h.workshop_id for h in held[:3])
                self.notice = (
                    f"nothing can be unsubscribed: item(s) {names} also hold mods "
                    "you kept, and Steam cannot remove part of an item"
                )
            else:
                self.notice = "nothing deselected that came from the Workshop"
            self.refresh_table()
            return

        # Read from the app, not from the copy handed over when this screen was
        # built. The settings screen can set the SDK path after the manager is
        # already open, and looking at the stale copy is what made this report
        # "library not found" on a path that was right.
        configured = getattr(self.app, "steam_sdk", None) or self.steam_sdk
        self.app.push_screen(
            UnsubscribeScreen(targets, find_library(configured), held=held),
            self._after_unsubscribe,
        )

    def _after_unsubscribe(self, changed: bool | None) -> None:
        """A rescan after unsubscribing, so the manager is never stale.

        Worth knowing what it will and will not show. Steam does not delete the
        files until it next shuts down, so the mods are still on disk and the
        rescan still finds them. What changes is that their subscription is gone,
        which the scan now compares, so they come back flagged as installed but
        no longer subscribed, and therefore still loading.
        """
        if not changed:
            self.notice = "nothing was unsubscribed"
            self.refresh_table()
            return
        from .tui import ScanScreen

        self.app.switch_screen(ScanScreen(then="manage"))

    def action_export(self) -> None:
        ordered, cycle = self.resolved_order()
        ini = export_server_ini(self.by_key, ordered)
        written: list[str] = []
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            ini_path = self.export_dir / "pzmodmanager-server.ini.txt"
            ini_path.write_text(ini, encoding="utf-8")
            written.append(str(ini_path))
            list_path = self.export_dir / "pzmodmanager-modlist.txt"
            list_path.write_text(export_text(ordered), encoding="utf-8")
            written.append(str(list_path))
            links_path = self.export_dir / "pzmodmanager-workshop-links.txt"
            links_path.write_text(export_links(self.by_key, ordered), encoding="utf-8")
            written.append(str(links_path))
        except OSError as exc:
            written.append(f"could not write the files: {exc}")

        store.save_selection(ordered, self.selection_path)

        blocking = [p for p in self.problems if p.severity.weight >= 3]
        body_lines: list[tuple[str, bool]] = [("", False), ("SERVER INI LINES", True), ("", False)]
        body_lines += [("  " + line, False) for line in ini.strip().splitlines()]
        body_lines += [("", False), ("WRITTEN TO", True), ("", False)]
        body_lines += [(f"  {path}", False) for path in written]
        if cycle:
            body_lines += [
                ("", False), ("WARNING", True), ("", False),
                ("  These mods require each other in a loop, so no order", False),
                ("  can satisfy them all: " + ", ".join(cycle), False),
            ]
        if blocking:
            body_lines += [("", False),
                           (f"{len(blocking)} PROBLEM(S) STILL OPEN", True), ("", False)]
            body_lines += [(f"  {p.message}", False) for p in blocking[:12]]
            body_lines += [("", False),
                           ("  Exported anyway. The selection is yours to make.", False)]
        body_lines += [("", False), ("-" * 34, False), ("", False),
                       ("  ESC to go back", False)]
        self.app.push_screen(ExportScreen(_panel(body_lines)))
        self.notice = f"exported {len(ordered)} mod(s)"
        self.refresh_table()

    def action_back(self) -> None:
        self.app.pop_screen()
