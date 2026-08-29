"""Unsubscribing, as two full screens rather than a box floating over the list.

This is the only thing the tool does that reaches outside itself and cannot be
undone from here, so it gets the whole screen: the count as the headline, every
item listed rather than a truncated sample, and a menu that starts on Cancel.
Nothing is one keystroke away.

The run itself is a second screen, because unsubscribing a hundred items takes
time and a frozen interface with no explanation is its own kind of failure.
"""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .selection import ModRef
from .tui import RETRO_CSS

_SCREEN_CSS = """
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
#listbox {
    height: 1fr;
    margin: 0 2;
    border: heavy #b4b4b4;
    background: #000000;
    padding: 0 1;
    scrollbar-background: #000000;
    scrollbar-color: #4a4a4a;
}
#warning {
    color: #b4b4b4;
    height: auto;
    padding: 1 2 0 2;
}
#choice-area {
    height: auto;
    align: center middle;
    padding: 1 0 0 0;
}
#choice-box {
    border: solid #b4b4b4;
    width: 40;
    height: auto;
    padding: 0 1;
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
RichLog {
    border: heavy #b4b4b4;
    background: #000000;
    color: #b4b4b4;
    height: 1fr;
    margin: 0 2;
    scrollbar-background: #000000;
    scrollbar-color: #4a4a4a;
}
"""


class UnsubscribeScreen(Screen):
    """Confirm, on the whole screen, with Cancel selected to start with."""

    CSS = RETRO_CSS + _SCREEN_CSS

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q,Q", "cancel", "Cancel"),
    ]

    def __init__(self, targets: list[ModRef], library: Path | None) -> None:
        super().__init__()
        self.targets = targets
        self.library = library

    def compose(self) -> ComposeResult:
        yield Static(
            "Arrow keys to choose, ENTER to act, ESC to go back without changing anything",
            id="hint",
        )
        if self.library is None:
            yield Static("STEAM LIBRARY NOT FOUND", id="headline")
            yield Static(
                "Nothing can be unsubscribed until the Steamworks SDK is in place.",
                id="subhead",
            )
            yield VerticalScroll(Static(self._missing_body(), id="list"), id="listbox")
            yield Static("", id="warning")
        else:
            count = len(self.targets)
            yield Static(
                f"UNSUBSCRIBE {count} MOD{'S' if count > 1 else ''} FROM STEAM",
                id="headline",
            )
            yield Static(
                f"Every deselected mod that came from the Workshop. "
                f"Steam ID currently logged in decides whose account this touches.",
                id="subhead",
            )
            yield VerticalScroll(Static(self._list_body(), id="list"), id="listbox")
            yield Static(self._warning_body(), id="warning")

        with Container(id="choice-area"):
            with Container(id="choice-box"):
                yield OptionList(id="choice")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        choice = self.query_one("#choice", OptionList)
        if self.library is None:
            choice.add_options([Option("  Back  ".center(30), id="cancel")])
        else:
            count = len(self.targets)
            choice.add_options(
                [
                    # Cancel first and highlighted: the dangerous option should
                    # never be the one a stray Enter lands on.
                    Option("  Cancel, change nothing  ".center(30), id="cancel"),
                    Option(f"  Unsubscribe {count} mod(s)  ".center(30), id="go"),
                ]
            )
        choice.highlighted = 0
        choice.focus()
        self.query_one("#footer", Static).update(
            f"library: {self.library}" if self.library else "no library"
        )

    def _list_body(self) -> str:
        lines = [""]
        width = max((len(r.mod_id) for r in self.targets), default=10)
        for ref in self.targets:
            lines.append(f"  {ref.mod_id.ljust(width)}   Workshop {ref.workshop_id}")
        lines.append("")
        return "\n".join(lines)

    def _missing_body(self) -> str:
        from .steamsdk import platform_dll_names

        return "\n".join(
            [
                "",
                f"  The tool needs {platform_dll_names()[0]} from the Steamworks SDK.",
                "",
                "  Inside the SDK archive it is at:",
                "",
                "    sdk/redistributable_bin/win64/steam_api64.dll",
                "",
                "  Copy it next to this tool, or start with --steam-sdk pointing at",
                "  that win64 folder.",
                "",
                "  Then check it with:  pzmodmanager --steam-check",
                "",
            ]
        )

    def _warning_body(self) -> str:
        return "\n".join(
            [
                "  Steam removes the local files once it next shuts down, and until",
                "  then the game still loads them. On the machine that also feeds a",
                "  server, these mods go for you too, and a save that relied on them",
                "  loses whatever they placed in the world.",
                "",
                "  Deselecting is reversible. This is not.",
            ]
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "go" and self.library is not None:
            self.app.push_screen(
                UnsubscribeRunScreen(self.targets, self.library), self._run_finished
            )
        else:
            self.dismiss(False)

    def _run_finished(self, changed: bool | None) -> None:
        """Pass the outcome back to the manager rather than losing it here."""
        self.dismiss(bool(changed))

    def action_cancel(self) -> None:
        self.dismiss(False)


class UnsubscribeRunScreen(Screen):
    """Does the work in a worker, showing each step, then the verified result."""

    CSS = RETRO_CSS + _SCREEN_CSS

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q,Q", "back", "Back")]

    def __init__(self, targets: list[ModRef], library: Path) -> None:
        super().__init__()
        self.targets = targets
        self.library = library
        self.finished = False

    def compose(self) -> ComposeResult:
        yield Static("Talking to Steam. Do not close Steam while this runs.", id="hint")
        yield Static("UNSUBSCRIBING", id="headline")
        yield Static(f"{len(self.targets)} item(s)", id="subhead")
        yield RichLog(id="progress", wrap=True, markup=False, highlight=False)
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self.run_worker_job()

    def append(self, message: str) -> None:
        self.query_one("#progress", RichLog).write(message)

    @work(thread=True, exclusive=True)
    def run_worker_job(self) -> None:
        """Hand the work to a child process and narrate what it reports.

        Nothing in this thread touches the Steam library directly. It printed to
        the terminal from C, over the top of the interface, and it could stall
        with no way back. The child has its own descriptors and its own deadline.
        """
        from .steambridge import unsubscribe

        app = self.app

        def progress(message: str) -> None:
            app.call_from_thread(self.append, f"  {message}")

        answer = unsubscribe(
            self.library, [int(r.workshop_id) for r in self.targets], progress=progress
        )
        if not answer.usable:
            app.call_from_thread(self.finish_with_error, answer.error)
            return
        app.call_from_thread(
            self.finish,
            answer.done,
            answer.failed,
            answer.before or 0,
            answer.after or 0,
        )

    def finish(self, done: list[int], failed: list[int], before: int, after: int) -> None:
        names = {str(r.workshop_id): r.mod_id for r in self.targets}
        self.append("")
        for item in done:
            self.append(f"  gone      {names.get(str(item), item)}")
        for item in failed:
            self.append(f"  still on  {names.get(str(item), item)}")
        self.append("")
        self.append(f"  subscriptions: {before} before, {after} after")
        if failed:
            self.append("")
            self.append(
                "  Steam may not have caught up yet. Check the Workshop page for "
                "the ones still listed."
            )
        self.append("")
        self.append("  Files disappear when Steam next shuts down. Until then the")
        self.append("  game still loads them.")
        self.finished = True
        self.query_one("#headline", Static).update("DONE")
        self.query_one("#footer", Static).update("ESC to go back to the manager")

    def finish_with_error(self, message: str) -> None:
        self.append("")
        self.append(f"  Failed: {message}")
        self.append("  Nothing was changed. Run --steam-check for a fuller diagnosis.")
        self.finished = True
        self.query_one("#headline", Static).update("FAILED")
        self.query_one("#footer", Static).update("ESC to go back to the manager")

    def action_back(self) -> None:
        self.dismiss(self.finished)
