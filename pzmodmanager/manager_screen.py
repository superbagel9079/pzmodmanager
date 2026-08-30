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


class ManageScreen(Screen):
    """Pick the mods to run, with dependencies handled and conflicts flagged."""

    CSS = _RETRO + _OWN_CSS

    BINDINGS = [
        Binding("escape", "back", "Menu"),
        Binding("space", "toggle", "Toggle"),
        Binding("x,X", "toggle", "Toggle"),
        Binding("a,A", "select_all", "All"),
        Binding("n,N", "select_none", "None"),
        Binding("o,O", "select_from_order", "From order"),
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
    ) -> None:
        super().__init__()
        self.selection_path = selection_path
        self.steam_sdk = steam_sdk
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
        self.notice = ""

    # ------------------------------------------------------------------ view --

    def compose(self) -> ComposeResult:
        yield Plain(
            "'x' or SPACE toggles, '/' searches, 'a' all, 'n' none, "
            "'o' from the load order\n"
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
            # Start from whatever the scanned load order had enabled.
            self.selected = {r.key for r in self.refs if r.was_enabled}
            self.notice = "starting from the mods the scan found enabled"
        self.refresh_all()
        table.focus()

    # --------------------------------------------------------------- content --

    def _matches(self, ref: ModRef) -> bool:
        if not self.filter_text:
            return True
        needle = self.filter_text.lower()
        return needle in ref.mod_id.lower() or needle in (ref.name or "").lower()

    def refresh_all(self) -> None:
        self.problems = validate(self.by_key, self.selected, self.findings)
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
        previous = table.cursor_row
        # Where the list was scrolled to, not just which row was highlighted.
        # clear() sends the scroll back to the top, and move_cursor afterwards
        # only scrolls far enough to bring the row into view, which parks it on
        # the bottom edge. Ticking a box then threw the whole list around.
        before = table.scroll_offset
        rows_before = len(self.visible_refs)
        table.clear()
        self.visible_refs = [r for r in self.refs if self._matches(r)]
        # The same problems the panel is showing, so the marker and the panel
        # never disagree. With every mod flagged by a low typo, a list of two
        # hundred exclamation marks says nothing at all, and hiding the problems
        # while keeping their markers would be the worst of both.
        flagged = {m.strip().lower() for p in self.shown_problems() for m in p.mods}
        for index, ref in enumerate(self.visible_refs):
            mark = CHECKED if ref.key in self.selected else UNCHECKED
            name = ref.name or ref.mod_id
            if ref.key in flagged and ref.key in self.selected:
                name = f"! {name}"
            table.add_row(cell(mark), cell(name), cell(ref.mod_id), key=str(index))
        if self.visible_refs:
            table.move_cursor(row=min(previous, len(self.visible_refs) - 1))
            # Put the view back exactly where it was, but only when the list is
            # the same length. Ticking a box cannot change the number of rows,
            # so the old offset is still right. Searching does change it, and
            # there the cursor should be brought into view normally.
            #
            # It has to be scheduled, not called. move_cursor does not scroll
            # immediately after a rebuild: it queues _scroll_cursor_into_view
            # with call_after_refresh, so anything done here and now is undone a
            # moment later. Queueing behind it is what actually holds.
            if rows_before == len(self.visible_refs):
                table.call_after_refresh(
                    table.scroll_to, x=before.x, y=before.y, animate=False, force=True
                )
        self.query_one("#footer", Static).update(
            summarise(self.by_key, self.selected, self.problems)
            + (f"   {self.notice}" if self.notice else "")
        )

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
        ordered, cycle = topological_order(self.by_key, self.selected)
        add(f"  order      {'has a cycle' if cycle else 'resolved'}")

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

    def action_select_from_order(self) -> None:
        self.selected = {r.key for r in self.refs if r.was_enabled}
        self.notice = "reset to the load order found by the scan"
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
        ordered, cycle = topological_order(
            self.by_key,
            self.selected,
            preferred=[r.mod_id for r in sorted(
                (r for r in self.refs if r.order_index is not None),
                key=lambda r: r.order_index,
            )],
        )
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
