"""What the game recorded, as a screen.

The rest of the tool predicts. This reports. The two are worth putting side by
side: a mod the tool ordered first and the game loaded fortieth means the order
that was exported never reached the game.

Three sections, in the order they answer questions people actually ask:

  1. errors, grouped by shape, because six thousand lines are seven problems;
  2. who lost files to whom, which is the silent failure nothing warns about;
  3. how the order the game applied compares to the one the tool would export.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable

from . import gamelog
from .tui import RETRO_CSS as _RETRO
from .tui import Plain, cell, panel as _panel

# The three views, cycled with TAB. Kept small on purpose: this screen answers
# questions, it is not a log viewer.
VIEWS = ["errors", "overrides", "order"]

_OWN_CSS = """
#log-hint { padding: 1 2 0 2; }
#log-body { height: 1fr; padding: 0 2; }
#log-table {
    height: auto;
    background: #000000;
    color: #b4b4b4;
}
#log-notes { color: #b4b4b4; height: auto; padding: 1 0; }
#log-foot { padding: 0 2 1 2; }
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
DataTable > .datatable--hover { background: #000000; }
"""


class GameLogScreen(Screen):
    """Read the game's own log and say what it found."""

    CSS = _RETRO + _OWN_CSS

    BINDINGS = [
        Binding("escape", "back", "Menu"),
        Binding("q,Q", "back", "Menu"),
        Binding("tab", "next_view", "Next view"),
        Binding("e,E", "show_errors", "Errors"),
        Binding("o,O", "show_overrides", "Overrides"),
        Binding("l,L", "show_order", "Order"),
    ]

    def __init__(self, path: Path | None = None, predicted: list[str] | None = None) -> None:
        super().__init__()
        self.path = path
        # The order the tool would export, so the two can be compared. None when
        # there is no scan to compare against, which is not an error.
        self.predicted = list(predicted or [])
        self.record: gamelog.GameLog | None = None
        self.view = 0
        self.notice = ""

    # ------------------------------------------------------------------ view --

    def compose(self) -> ComposeResult:
        yield Plain(
            "TAB or 'e' 'o' 'l' switch between the errors, the overridden files "
            "and the load order\nESC returns",
            id="log-hint",
        )
        with VerticalScroll(id="log-body"):
            yield Plain("", id="log-notes")
            yield DataTable(id="log-table", cursor_type="row", zebra_stripes=False)
        yield Plain("", id="log-foot")

    def on_mount(self) -> None:
        source = self.path or gamelog.default_console_path()
        if source is None:
            self.notice = "no game log found"
        else:
            self.record = gamelog.read(Path(source))
            if self.record is None:
                self.notice = f"could not read {source}"
        self.redraw()
        self.query_one("#log-table", DataTable).focus()

    # --------------------------------------------------------------- content --

    def _columns(self, table: DataTable, spec: list[tuple[str, int | None]]) -> None:
        table.clear(columns=True)
        for label, width in spec:
            table.add_column(label, width=width)

    def redraw(self) -> None:
        table = self.query_one("#log-table", DataTable)
        notes = self.query_one("#log-notes", Plain)
        record = self.record

        if record is None:
            self._columns(table, [("", None)])
            notes.update(
                _panel([
                    ("", False),
                    ("NO GAME LOG", True),
                    ("", False),
                    ("  The game writes console.txt into its data folder every", False),
                    ("  time it starts. Launch the game once, quit, and come back.", False),
                    ("", False),
                    (f"  {self.notice}", False),
                ])
            )
            self.query_one("#log-foot", Plain).update(Text(""))
            return

        name = VIEWS[self.view]
        if name == "errors":
            self._draw_errors(table, notes, record)
        elif name == "overrides":
            self._draw_overrides(table, notes, record)
        else:
            self._draw_order(table, notes, record)

        self.query_one("#log-foot", Plain).update(
            Text(
                f"{len(record.loaded)} mod(s) loaded   "
                f"{record.override_count} override(s)   "
                f"{len(record.contested)} contested file(s)   "
                f"{record.error_total} error line(s) in {len(record.errors)} shape(s)"
                + (f"   {self.notice}" if self.notice else "")
            )
        )

    def _draw_errors(self, table, notes, record) -> None:
        self._columns(table, [("COUNT", 8), ("DISTINCT", 9), ("PROBLEM", None)])
        for group in record.errors:
            table.add_row(
                cell(str(group.count)),
                cell(str(len(group.subjects)) if group.subjects else ""),
                cell(group.shape),
            )
        notes.update(
            _panel([
                ("", False),
                ("ERRORS, GROUPED BY SHAPE", True),
                ("", False),
                ("  Grouped because they repeat. Quoted names and numbers are", False),
                ("  what varies, so DISTINCT is how many different things hit", False),
                ("  the same problem: one shape with thousands of lines and", False),
                ("  twenty six subjects is one problem, not thousands.", False),
                ("", False),
                (f"  read from {record.source}", False),
                ("", False),
            ])
        )

    def _draw_overrides(self, table, notes, record) -> None:
        self._columns(table, [("FILES", 7), ("LOSES", 40), ("TO", None)])
        losses = record.losses()
        for loser, winner, count in losses:
            table.add_row(cell(str(count)), cell(loser), cell(winner))
        notes.update(
            _panel([
                ("", False),
                ("FILES LOST TO A LATER MOD", True),
                ("", False),
                ("  In Project Zomboid the mod loaded last wins a file both", False),
                ("  supply. The loser's version is simply not used, with no", False),
                ("  error and nothing on screen. This is the game's own record", False),
                ("  of who won, not a guess from the order.", False),
                ("", False),
                ("  A large number here is not automatically wrong. A texture", False),
                ("  optimiser is meant to be overwritten, and a patch is meant", False),
                ("  to overwrite. A mod losing most of its files to something", False),
                ("  unrelated is the case worth looking at.", False),
                ("", False),
            ])
        )

    def _draw_order(self, table, notes, record) -> None:
        self._columns(table, [("PLACE", 7), ("MOD", None)])
        for index, mod_id in enumerate(record.loaded):
            table.add_row(cell(str(index + 1)), cell(mod_id))

        lines: list[tuple[str, bool]] = [
            ("", False), ("THE ORDER THE GAME APPLIED", True), ("", False)
        ]
        if not self.predicted:
            lines += [
                ("  No scan to compare this against. Run one, then come back", False),
                ("  and this will also say where the two disagree.", False),
            ]
        else:
            here = record.position
            shared = [m for m in self.predicted if m.strip().lower() in here]
            lines.append(
                (f"  {len(shared)} of your {len(self.predicted)} selected mod(s) "
                 "appear in this log", False)
            )
            if len(shared) < len(self.predicted) * 0.8:
                lines += [
                    ("", False),
                    ("  This log is from a session with a different mod list.", False),
                    ("  It describes the last launch, not what is selected now,", False),
                    ("  so comparing the two positions would be meaningless.", False),
                ]
            else:
                moved = record.disagreements(self.predicted)
                lines += [
                    ("", False),
                    (f"  {len(moved)} of them sit somewhere other than where", False),
                    ("  the tool would have put them.", False),
                ]
                if not moved:
                    lines.append(
                        ("  The exported order is the order the game applied.", False)
                    )
                for mod_id, want, got in moved[:12]:
                    lines.append((f"      {mod_id}: {want + 1} -> {got + 1}", False))
                if len(moved) > 12:
                    lines.append((f"      ... {len(moved) - 12} more", False))
        lines.append(("", False))
        notes.update(_panel(lines))

    # --------------------------------------------------------------- actions --

    def action_next_view(self) -> None:
        self.view = (self.view + 1) % len(VIEWS)
        self.redraw()

    def action_show_errors(self) -> None:
        self.view = 0
        self.redraw()

    def action_show_overrides(self) -> None:
        self.view = 1
        self.redraw()

    def action_show_order(self) -> None:
        self.view = 2
        self.redraw()

    def action_back(self) -> None:
        self.app.pop_screen()
