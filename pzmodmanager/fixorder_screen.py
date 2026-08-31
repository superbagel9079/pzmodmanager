"""One screen that takes the order all the way to the game.

Everything this screen does was already possible: scan, tick, export, copy,
apply. What made it hard was that the tool asked you to think in its steps
rather than in yours. You do not want a scan, an export and a file copy. You
want the game to load your mods in the right order.

So this is one entry, one screen, one confirmation. It works out the order, it
works out which of the three destinations exist on this machine, it says what
writing to each would change, and it writes only the ones you leave ticked.
Everything it does can still be done the long way on the other screens, which
are unchanged: this adds a short path, it does not close the long one.

The safety is not relaxed for being quick. Every write takes a timestamped copy
first, the confirmation lists what will happen with Cancel highlighted, and a
destination that would change nothing is offered unticked.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from . import destinations as dst
from . import store
from .selection import index_by_key, topological_order, validate
from .tui import RETRO_CSS as _RETRO
from .tui import MARKER, Plain

_FIX_CSS = """
#headline {
    color: #ffffff;
    text-style: bold;
    height: auto;
    padding: 1 2 0 2;
}
#subhead {
    color: #8a8a8a;
    height: auto;
    padding: 0 2 1 2;
}
#summary {
    height: auto;
    margin: 0 2;
    border: heavy #b4b4b4;
    background: #000000;
    padding: 0 1;
}
#where {
    height: 1fr;
    margin: 0 2;
    border: heavy #b4b4b4;
    background: #000000;
    padding: 0 1;
}
#footer {
    color: #8a8a8a;
    height: auto;
    padding: 0 2 1 2;
}
#choice-area {
    height: auto;
    align: center middle;
    padding: 1 0 0 0;
}
#choice-box {
    border: solid #b4b4b4;
    width: 46;
    height: auto;
    padding: 0 1;
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
OptionList > .option-list--option-highlighted {
    background: #ffffff;
    color: #000000;
    text-style: bold;
}
"""

# Three states, and all three keep the brackets so the column always lines up.
# A row with no brackets at all read as a missing checkbox rather than as a
# destination that cannot be written, which is exactly how it was reported.
TICKED = "[x]"
UNTICKED = "[ ]"
BLOCKED = "[-]"


class FixOrderScreen(Screen):
    """Compute the order, then write it wherever it is wanted."""

    CSS = _RETRO + _FIX_CSS

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q,Q", "back", "Back"),
        Binding("a,A", "apply", "Apply"),
    ]
    # SPACE is deliberately NOT a Binding here. Some Textual versions give
    # OptionList its own space binding that fires OptionSelected, and a screen
    # level binding on the same key then toggled a second time in the same
    # keypress. Two toggles look exactly like none, which is how this was
    # reported: the cross never appeared. Handling it in on_key instead means
    # the event reaches this screen only when the list did not already act on
    # it, so the destination flips exactly once either way.

    def __init__(
        self,
        mods: list,
        findings: list | None = None,
        selection_path: Path | None = None,
        server_ini: str = "",
    ) -> None:
        super().__init__()
        self.mods = list(mods)
        self.findings = list(findings or [])
        self.selection_path = selection_path
        self.server_ini = server_ini
        self.by_key = index_by_key(self.mods)
        self.keys: set[str] = set()
        self.ordered: list[str] = []
        self.cycle: list[str] = []
        self.problems: list = []
        self.destinations: list[dst.Destination] = []
        self.results: list[dst.WriteResult] = []

    # ------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        yield Plain("FIX MY LOAD ORDER", id="headline")
        yield Plain(
            "Nothing is written until you confirm, and every write is backed up first.",
            id="subhead",
        )
        yield Plain("", id="summary")
        with VerticalScroll(id="where"):
            yield OptionList(id="rows")
        with Container(id="choice-area"):
            with Container(id="choice-box"):
                yield OptionList(id="choice")
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        self.compute()
        self.redraw()
        self.query_one("#rows", OptionList).focus()

    # ------------------------------------------------------------ working --

    def compute(self) -> None:
        """Work out the order and where it could go. Reads only."""
        saved = store.load_selection(self.selection_path)
        if saved:
            self.keys = {s.strip().lower() for s in saved} & set(self.by_key)
        else:
            self.keys = {r.key for r in self.by_key.values() if r.was_enabled}

        preferred = [
            r.mod_id
            for r in sorted(
                (r for r in self.by_key.values() if r.order_index is not None),
                key=lambda r: r.order_index,
            )
        ]
        pins = store.load_pins()
        self.ordered, self.cycle = topological_order(
            self.by_key, self.keys, preferred=preferred, pins=pins
        )
        self.problems = validate(self.by_key, self.keys, self.findings, pins=pins)
        self.destinations = dst.plan_destinations(
            self.ordered,
            self.by_key,
            self.keys,
            self.mods,
            server_ini=self.server_ini,
        )

    def _summary_lines(self) -> str:
        satisfied, late = self._dependency_counts()
        worst = {}
        for problem in self.problems:
            worst[problem.severity] = worst.get(problem.severity, 0) + 1
        lines = [
            "",
            f"  {len(self.ordered)} mod(s) selected out of {len(self.mods)} installed",
            f"  {satisfied} dependency(ies) satisfied, {late} loaded too late, "
            f"{len(self.cycle)} in a cycle",
        ]
        if worst:
            counted = "  ".join(
                f"{MARKER[sev]} {count}" for sev, count in sorted(
                    worst.items(), key=lambda pair: -pair[0].weight
                )
            )
            lines.append(f"  {counted}")
        else:
            lines.append("  nothing to report on this selection")
        lines.append("")
        return "\n".join(lines)

    def _dependency_counts(self) -> tuple[int, int]:
        from .selection import resolve_requirement

        place = {mod_id.lower(): i for i, mod_id in enumerate(self.ordered)}
        satisfied = late = 0
        for key in self.keys:
            ref = self.by_key[key]
            for raw in (getattr(ref, "requires_raw", None) or ref.requires):
                target = resolve_requirement(raw, self.by_key)
                if target is None or target not in place or key not in place:
                    continue
                if place[target] < place[key]:
                    satisfied += 1
                else:
                    late += 1
        return satisfied, late

    # ------------------------------------------------------------ drawing --

    def redraw(self) -> None:
        self.query_one("#summary", Static).update(self._summary_lines())

        rows = self.query_one("#rows", OptionList)
        previous = rows.highlighted
        rows.clear_options()

        # One option per destination, and none of them disabled. The first
        # version put the file path on its own disabled row and disabled any
        # destination that could not be written, which on a machine where only
        # one of the three applied left a single reachable row: the arrow keys
        # then did nothing at all and the screen looked frozen. A row you can
        # move to and be told why it is unavailable beats a row you cannot
        # reach.
        options = []
        for dest in self.destinations:
            if not dest.available:
                mark = BLOCKED
            elif dest.chosen:
                mark = TICKED
            else:
                mark = UNTICKED
            head = f" {mark}  {dest.label.ljust(18)} {dest.detail if dest.available else dest.reason}"
            second = dest.where or (dest.reason if dest.available else "")
            body = head if not second else f"{head}\n          {second}"
            options.append(Option(body, id=dest.key))
        rows.add_options(options)
        rows.highlighted = min(previous or 0, max(len(options) - 1, 0))

        choice = self.query_one("#choice", OptionList)
        choice.clear_options()
        count = sum(1 for d in self.destinations if d.chosen and d.available)
        choice.add_options(
            [
                Option("  Cancel, change nothing  ".center(36), id="cancel"),
                Option(
                    f"  Apply to {count} destination(s)  ".center(36),
                    id="go",
                    disabled=count == 0,
                ),
            ]
        )
        choice.highlighted = 0

        self.query_one("#footer", Static).update(
            "UP and DOWN move, SPACE or ENTER ticks, 'a' applies, ESC goes back"
            if not self.results
            else "done, ESC to go back"
        )

    # ------------------------------------------------------------- acting --

    def on_key(self, event) -> None:
        """SPACE ticks the highlighted row, once, whatever Textual does with it."""
        if event.key != "space":
            return
        rows = self.query_one("#rows", OptionList)
        if not rows.has_focus:
            return
        event.stop()
        event.prevent_default()
        self.action_toggle()

    def action_toggle(self) -> None:
        rows = self.query_one("#rows", OptionList)
        index = rows.highlighted
        if index is None:
            return
        option = rows.get_option_at_index(index)
        for dest in self.destinations:
            if dest.key != option.id:
                continue
            if not dest.available:
                # Reachable but not writable. Saying why beats a key that does
                # nothing and leaves you guessing which of the two it was.
                self.query_one("#footer", Static).update(
                    f"{dest.label}: {dest.reason}"
                )
                return
            dest.chosen = not dest.chosen
            self.redraw()
            return

    def action_apply(self) -> None:
        wanted = [d for d in self.destinations if d.chosen and d.available]
        if not wanted:
            self.query_one("#footer", Static).update("nothing ticked, so nothing to do")
            return
        self.app.push_screen(ConfirmFixScreen(wanted), self._confirmed)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "choice":
            if event.option.id == "go":
                self.action_apply()
            else:
                self.action_back()
        else:
            self.action_toggle()

    def _confirmed(self, confirmed) -> None:
        if not confirmed:
            self.query_one("#footer", Static).update("nothing was changed")
            return
        self.results = [
            dst.apply_destination(dest, self.ordered)
            for dest in self.destinations
            if dest.chosen and dest.available
        ]
        self.show_results()

    def show_results(self) -> None:
        lines = [""]
        for result in self.results:
            mark = "done  " if result.ok else "failed"
            lines.append(f"  {mark}  {result.key.ljust(8)} {result.message}")
            if result.backup is not None:
                lines.append(f"          the old file is {result.backup.name}")
        lines += [
            "",
            "  Nothing else was touched. No mod was deleted and no subscription",
            "  was changed.",
            "",
        ]
        self.query_one("#summary", Static).update("\n".join(lines))
        rows = self.query_one("#rows", OptionList)
        rows.clear_options()
        rows.add_options([Option("  ESC to go back", disabled=True)])
        choice = self.query_one("#choice", OptionList)
        choice.clear_options()
        choice.add_options([Option("  Back  ".center(36), id="cancel")])
        choice.highlighted = 0
        self.query_one("#headline", Static).update("DONE")
        self.query_one("#footer", Static).update("ESC to go back")

    def action_back(self) -> None:
        self.app.pop_screen()


class ConfirmFixScreen(Screen):
    """The one confirmation, listing exactly what is about to change."""

    CSS = _RETRO + _FIX_CSS

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q,Q", "cancel", "Cancel"),
    ]

    def __init__(self, chosen: list[dst.Destination]) -> None:
        super().__init__()
        self.chosen = chosen

    def compose(self) -> ComposeResult:
        yield Plain("ABOUT TO WRITE", id="headline")
        yield Plain(
            "These files belong to the game and to your server, not to this tool.",
            id="subhead",
        )
        with VerticalScroll(id="where"):
            yield Plain(self._body(), id="body")
        with Container(id="choice-area"):
            with Container(id="choice-box"):
                yield OptionList(id="choice")
        yield Plain("", id="footer")

    def _body(self) -> str:
        lines = [""]
        for dest in self.chosen:
            lines.append(f"  {dest.label}")
            lines.append(f"      {dest.where}")
            lines.append(f"      {dest.detail}")
            lines.append("")
        lines += [
            "  Each one is copied to a timestamped file beside itself before it",
            "  is changed, and each is written whole or not at all.",
            "",
        ]
        if any(d.key == "server" for d in self.chosen):
            lines += [
                "  The server ini keeps every other setting it has. Only the",
                "  Mods= and WorkshopItems= lines are replaced, and the server",
                "  has to be restarted to read them.",
                "",
            ]
        if any(d.key == "rules" for d in self.chosen):
            lines += [
                "  The in-game list still needs one click: open the mods screen",
                "  and press Sort. Nothing outside the game can press it.",
                "",
            ]
        return "\n".join(lines)

    def on_mount(self) -> None:
        choice = self.query_one("#choice", OptionList)
        choice.add_options(
            [
                Option("  Cancel, change nothing  ".center(36), id="cancel"),
                Option(
                    f"  Write {len(self.chosen)} file(s)  ".center(36), id="go"
                ),
            ]
        )
        choice.highlighted = 0
        choice.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id == "go")

    def action_cancel(self) -> None:
        self.dismiss(False)
