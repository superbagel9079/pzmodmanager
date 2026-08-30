"""Writing the load order into a save, with the guard rails that deserves.

This is the only screen in the tool that changes a file belonging to the game.
So it is deliberately slower than it needs to be: pick the save, read what the
write would do, then confirm on a list whose first and highlighted entry is
Cancel. A stray ENTER does nothing.

The write itself refuses anything that is not a pure reordering, and takes a
timestamped copy first. Restoring that copy is on this screen too, because an
undo you have to go and find is not an undo.
"""

from __future__ import annotations

from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, OptionList
from textual.widgets.option_list import Option

from . import savegame
from .tui import RETRO_CSS as _RETRO
from .tui import Plain, cell, panel as _panel

# How many moved mods the confirmation lists before summarising. Enough to
# recognise the change, few enough that nobody scrolls past the warning.
MOVES_SHOWN = 12


class ApplyScreen(ModalScreen):
    """Pick a save, look at what would change, then decide."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q,Q", "cancel", "Cancel"),
        Binding("r,R", "restore", "Restore a backup"),
    ]

    CSS = _RETRO + """
    ApplyScreen { align: center middle; background: #000000; }
    #apply-box {
        width: 100;
        height: auto;
        max-height: 88%;
        border: solid #b4b4b4;
        background: #000000;
        color: #b4b4b4;
        padding: 1 2;
    }
    #apply-saves, #apply-choices {
        height: auto;
        max-height: 14;
        background: #000000;
        color: #b4b4b4;
    }
    #apply-notes { height: auto; color: #b4b4b4; }
    OptionList > .option-list--option-highlighted {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #ffffff;
        color: #000000;
        text-style: bold;
    }
    DataTable > .datatable--header {
        background: #000000; color: #ffffff; text-style: bold;
    }
    DataTable > .datatable--hover { background: #000000; }
    """

    def __init__(self, ordered: list[str]) -> None:
        super().__init__()
        self.ordered = list(ordered)
        self.saves: list[savegame.SaveGame] = []
        self.chosen: savegame.SaveGame | None = None
        self.plan: savegame.Plan | None = None
        self.stage = "pick"
        self.notice = ""

    # ------------------------------------------------------------------ view --

    def compose(self) -> ComposeResult:
        with Container(id="apply-box"):
            with VerticalScroll():
                yield Plain("", id="apply-notes")
                yield DataTable(id="apply-saves", cursor_type="row", zebra_stripes=False)
                yield OptionList(id="apply-choices")

    def on_mount(self) -> None:
        table = self.query_one("#apply-saves", DataTable)
        table.add_column("SAVE", width=44)
        table.add_column("MODS", width=6)
        table.add_column("LAST PLAYED")
        self.saves = savegame.find_saves()
        self.redraw()

    def redraw(self) -> None:
        notes = self.query_one("#apply-notes", Plain)
        table = self.query_one("#apply-saves", DataTable)
        choices = self.query_one("#apply-choices", OptionList)
        choices.clear_options()

        if not self.saves:
            table.display = False
            choices.display = False
            notes.update(
                _panel([
                    ("", False), ("NO SAVE FOUND", True), ("", False),
                    ("  The load order lives inside a save, and there is not one", False),
                    ("  on this machine that the tool can see. Start a game once", False),
                    ("  and come back.", False), ("", False),
                ])
            )
            return

        if self.stage == "pick":
            table.display = True
            choices.display = False
            table.clear()
            for save in self.saves:
                table.add_row(cell(save.label), cell(str(len(save.mods))), cell(save.when))
            notes.update(
                _panel([
                    ("", False),
                    ("APPLY THE ORDER TO A SAVE", True),
                    ("", False),
                    ("  Build 42 keeps the load order inside the save, in its own", False),
                    ("  mods.txt. Pick one with ENTER to see exactly what would", False),
                    ("  change. Nothing is written until you confirm after that.", False),
                    ("", False),
                    (f"  the order to apply has {len(self.ordered)} mod(s)", False),
                    (f"  {self.notice}" if self.notice else "", False),
                    ("", False),
                ])
            )
            table.focus()
            return

        # The confirmation.
        table.display = False
        choices.display = True
        assert self.plan is not None
        plan = self.plan
        lines: list[tuple[str, bool]] = [
            ("", False),
            (f"APPLY TO {plan.save.label}", True),
            ("", False),
        ]
        if not plan.safe:
            lines += [
                ("  THE TWO LISTS ARE NOT THE SAME", True),
                ("", False),
                ("  A save records the mods that were active the last time it", False),
                ("  ran, and your selection has moved on since. Adding or", False),
                ("  removing a mod in a save is not something this tool will do:", False),
                ("  a world with items in the ground can break either way.", False),
                ("", False),
                (f"  {plan.refusal}", False),
                ("", False),
            ]
            fitted = plan.fitted_moves
            if plan.shared and fitted:
                lines += [
                    (f"  It can still resequence the {len(plan.save.mods)} mod(s) this "
                     "save has.", True),
                    ("", False),
                    (f"  {plan.shared} of them are in your order and take its", False),
                    ("  sequence. The other "
                     f"{len(plan.save.mods) - plan.shared} keep the exact place they", False),
                    ("  already occupy, so nothing drifts around them. The save", False),
                    ("  keeps every mod it has, and gains none.", False),
                    ("", False),
                    (f"  {len(fitted)} mod(s) would move:", False),
                ]
                for mod_id, was, now in fitted[:MOVES_SHOWN]:
                    lines.append((f"      {mod_id}: {was + 1} -> {now + 1}", False))
                if len(fitted) > MOVES_SHOWN:
                    lines.append((f"      ... {len(fitted) - MOVES_SHOWN} more", False))
                lines += [
                    ("", False),
                    ("  THIS WRITES INSIDE YOUR SAVE.", True),
                    ("  A timestamped copy of the current mods.txt is made first,", False),
                    ("  in the same folder, and 'r' on this screen puts it back.", False),
                    ("", False),
                ]
            else:
                lines += [
                    ("  Match your selection to the save, or start a new game.", False),
                    ("", False),
                ]
        elif not plan.moves:
            lines += [
                ("  Nothing to do. This save is already in exactly this order.", False),
                ("", False),
            ]
        else:
            lines += [
                (f"  {len(plan.moves)} of {len(plan.ordered)} mod(s) would move.", False),
                ("  The same mods, in a different sequence. None added, none", False),
                ("  removed.", False),
                ("", False),
            ]
            for mod_id, was, now in plan.moves[:MOVES_SHOWN]:
                lines.append((f"      {mod_id}: {was + 1} -> {now + 1}", False))
            if len(plan.moves) > MOVES_SHOWN:
                lines.append((f"      ... {len(plan.moves) - MOVES_SHOWN} more", False))
            lines += [
                ("", False),
                ("  THIS WRITES INSIDE YOUR SAVE.", True),
                ("  A timestamped copy of the current mods.txt is made first,", False),
                ("  in the same folder, and 'r' on this screen puts it back.", False),
                ("  The game has no undo of its own for this.", False),
                ("", False),
            ]
        backups = plan.save.backups
        if backups:
            lines.append((f"  {len(backups)} backup(s) here, newest {backups[0].name}", False))
        if self.notice:
            lines += [("", False), (f"  {self.notice}", False)]
        lines.append(("", False))
        notes.update(_panel(lines))

        options = [Option("Cancel, change nothing", id="cancel")]
        if plan.safe and plan.moves:
            options.append(Option("Write this order into the save", id="write"))
        elif not plan.safe and plan.shared and plan.fitted_moves:
            options.append(
                Option(
                    f"Resequence the {len(plan.save.mods)} mod(s) this save has, "
                    "adding and removing none",
                    id="fit",
                )
            )
        if backups:
            options.append(Option(f"Restore {backups[0].name}", id="restore"))
        options.append(Option("Back to the save list", id="back"))
        choices.add_options(options)
        # Cancel first and highlighted: a stray ENTER must do nothing.
        choices.highlighted = 0
        choices.focus()

    # --------------------------------------------------------------- actions --

    def on_data_table_row_selected(self, event) -> None:
        index = event.cursor_row
        if not (0 <= index < len(self.saves)):
            return
        self.chosen = self.saves[index]
        self.plan = savegame.plan(self.chosen, self.ordered)
        self.stage = "confirm"
        self.notice = ""
        self.redraw()

    def on_option_list_option_selected(self, event) -> None:
        choice = event.option.id
        if choice == "cancel":
            self.dismiss(None)
        elif choice == "back":
            self.stage = "pick"
            self.notice = ""
            self.redraw()
        elif choice == "write":
            self.write_it()
        elif choice == "fit":
            self.write_it(narrowed=True)
        elif choice == "restore":
            self.action_restore()

    def write_it(self, narrowed: bool = False) -> None:
        """Write, either the order as it stands or narrowed to this save.

        The narrowed list is built from the save's own mods, so it passes the
        same set check as any other write. There is no second, weaker path into
        savegame.apply: there is one door, and this walks through it.
        """
        if self.chosen is None or self.plan is None:
            return
        wanted = self.plan.fitted if narrowed else self.ordered
        done, message, _backup = savegame.apply(self.chosen, wanted)
        self.notice = message
        # Re-read, so the screen shows the file as it now is rather than as it
        # was when this screen opened.
        fresh = savegame.read_save(self.chosen.path)
        if fresh is not None:
            self.chosen = fresh
            self.saves = [fresh if s.path == fresh.path else s for s in self.saves]
            self.plan = savegame.plan(fresh, self.ordered)
        if done:
            self.dismiss(message)
            return
        self.redraw()

    def action_restore(self) -> None:
        if self.chosen is None:
            return
        backups = self.chosen.backups
        if not backups:
            self.notice = "no backup here to restore"
            self.redraw()
            return
        _done, message = savegame.restore(self.chosen, backups[0])
        fresh = savegame.read_save(self.chosen.path)
        if fresh is not None:
            self.chosen = fresh
            self.plan = savegame.plan(fresh, self.ordered)
        self.notice = message
        self.redraw()

    def action_cancel(self) -> None:
        self.dismiss(None)
