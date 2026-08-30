"""Point the tool at a server ini and let it do the matching.

Give it a path, and it reads the server's two lines, compares them to what is
installed here, and shows the gap in the three shapes that need three different
actions: items to subscribe to, mods to tick, mods to untick. Confirm, and it
subscribes through Steam, waits for the download, saves the selection and starts
a scan.

Two things it deliberately does not do. It never unsubscribes, because a mod
this server does not want may be one another server does, and losing it costs a
download nobody asked for. And it never writes into a save: the selection is the
tool's own file, and putting that order into the game stays behind the Apply
screen, where the backup and the confirmation live.
"""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from . import store
from .serverlist import ServerDiff, compare, read_server_list
from .steam import WorkshopItem
from .tui import RETRO_CSS as _RETRO
from .tui import Plain

_SERVER_CSS = """
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
#hint {
    color: #8a8a8a;
    height: auto;
    padding: 0 2;
}
#path {
    background: #000000;
    color: #b4b4b4;
    border: solid #ffffff;
    height: 3;
    margin: 0 2;
}
#reportbox {
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
"""

# How many names a section prints before it just gives a count. Long enough to
# recognise what is about to happen, short enough that the summary above it does
# not scroll off the screen.
NAMES_SHOWN = 14


def _section(title: str, names: list[str], note: str = "") -> list[str]:
    if not names:
        return []
    lines = ["", f"  {title} ({len(names)})"]
    if note:
        lines.append(f"    {note}")
    for name in names[:NAMES_SHOWN]:
        lines.append(f"      {name}")
    if len(names) > NAMES_SHOWN:
        lines.append(f"      ... and {len(names) - NAMES_SHOWN} more")
    return lines


class ServerListScreen(Screen):
    """Read a server ini, show the gap, and close it on confirmation."""

    CSS = _RETRO + _SERVER_CSS

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q,Q", "back", "Back"),
        Binding("g,G", "go", "Match"),
    ]

    def __init__(
        self,
        mods: list,
        selection_path: Path | None = None,
        steam_sdk: Path | None = None,
        suggested: Path | None = None,
    ) -> None:
        super().__init__()
        self.mods = list(mods)
        self.selection_path = selection_path
        self.steam_sdk = steam_sdk
        self.suggested = suggested
        self.diff: ServerDiff | None = None
        self.titles: dict[str, WorkshopItem] = {}

    # ------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        yield Plain("SERVER LIST", id="headline")
        yield Plain(
            "Type the path to a server ini, or to a file holding just its "
            "Mods= and WorkshopItems= lines.",
            id="subhead",
        )
        yield Input(placeholder="path to the file", id="path")
        yield Plain("ENTER reads the file, 'g' matches this machine to it, ESC goes back", id="hint")
        with VerticalScroll(id="reportbox"):
            yield Plain(self._welcome(), id="report")
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        box = self.query_one("#path", Input)
        if self.suggested is not None and Path(self.suggested).is_file():
            box.value = str(self.suggested)
        box.focus()

    def _welcome(self) -> str:
        return "\n".join(
            [
                "",
                "  Nothing is read until you press ENTER, and nothing changes until",
                "  you confirm on a list where Cancel is the highlighted entry.",
                "",
                "  What this does, in order:",
                "",
                "    1. subscribes to the Workshop items you do not have",
                "    2. waits for Steam to finish downloading them",
                "    3. saves the server's list as your selection, in its order",
                "    4. starts a scan so the new mods are picked up",
                "",
                "  It never unsubscribes and never writes into a save.",
                "",
            ]
        )

    def say(self, text: str) -> None:
        self.query_one("#report", Static).update(text)

    def note(self, text: str) -> None:
        self.query_one("#footer", Static).update(text)

    # ------------------------------------------------------------ reading --

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.read_file(event.value.strip().strip('"'))

    def read_file(self, raw: str) -> None:
        if not raw:
            self.note("no path given")
            return
        path = Path(raw).expanduser()
        if not path.is_file():
            self.say(
                "\n".join(
                    [
                        "",
                        "  No file there.",
                        "",
                        f"  Looked for: {path}",
                        "",
                        "  Copy the server's ini onto this machine, or export the two",
                        "  lines from the server and point at that file.",
                        "",
                    ]
                )
            )
            self.note("file not found")
            return

        server = read_server_list(path)
        if not server:
            self.say(
                "\n".join(
                    [
                        "",
                        "  That file has no Mods= line and no WorkshopItems= line.",
                        "",
                        f"  Read: {path}",
                        "",
                        "  A server ini has both. If you pasted from a terminal, the",
                        "  lines may have been wrapped: they must each be on one line.",
                        "",
                    ]
                )
            )
            self.note("nothing to read in that file")
            return

        saved = store.load_selection(self.selection_path)
        self.diff = compare(server, self.mods, selected=saved)
        self.render_diff()
        self.fetch_titles()

    def render_diff(self) -> None:
        diff = self.diff
        if diff is None:
            return
        server = diff.server
        lines = [
            "",
            f"  server list: {len(server.mod_ids)} mod(s), "
            f"{len(server.workshop_ids)} Workshop item(s)",
            f"  this machine: {len(self.mods)} mod(s) installed",
        ]

        if diff.matched:
            lines += [
                "",
                "  This machine already matches that server exactly.",
                "",
                "  Every mod it lists is installed, every one is ticked, and nothing",
                "  else is. There is nothing to do.",
                "",
            ]
            self.say("\n".join(lines))
            self.note("already matching")
            return

        lines += _section(
            "to subscribe",
            [self._label(item) for item in diff.to_subscribe],
            "Steam downloads these in the background once you confirm",
        )
        lines += _section(
            "to tick",
            diff.to_enable,
            "installed here already, just not in your selection",
        )
        lines += _section(
            "to untick",
            diff.to_disable,
            "kept on disk and still subscribed, only deselected",
        )
        lines += _section(
            "listed by the server but not installed",
            diff.not_installed,
            "these should appear once the downloads above finish"
            if diff.to_subscribe
            else "no Workshop item on the server's list explains these",
        )
        if diff.unchanged:
            lines += ["", f"  already right ({len(diff.unchanged)})"]
        lines.append("")
        self.say("\n".join(lines))
        self.note("'g' to match this machine to that list")

    def _label(self, item_id: str) -> str:
        found = self.titles.get(item_id)
        if found is not None and found.title:
            return f"{item_id}  {found.title}"
        return item_id

    @work(thread=True, exclusive=True)
    def fetch_titles(self) -> None:
        """Put names on the Workshop numbers, when the network allows it.

        A confirmation listing twenty bare item ids is a confirmation nobody can
        actually check. This is best effort on purpose: the ids alone are enough
        to subscribe, so a lookup that fails costs a nicer display and nothing
        else.
        """
        diff = self.diff
        if diff is None or not diff.to_subscribe:
            return
        try:
            from .steam import WorkshopCache, fetch_items

            cache = WorkshopCache(store.default_steam_cache_path())
            found = fetch_items(list(diff.to_subscribe), cache=cache)
        except Exception as exc:  # noqa: BLE001 - display only, never fatal
            from logging import getLogger

            getLogger(__name__).info("Could not name the Workshop items: %s", exc)
            return
        if not found:
            return
        self.app.call_from_thread(self._titles_arrived, found)

    def _titles_arrived(self, found: dict) -> None:
        self.titles = dict(found)
        self.render_diff()

    # ------------------------------------------------------------- acting --

    def action_go(self) -> None:
        diff = self.diff
        if diff is None:
            self.note("read a file first: ENTER on the path")
            return
        if diff.matched:
            self.note("nothing to do, this machine already matches")
            return
        if diff.to_subscribe:
            from .browse_screen import SubscribeScreen
            from .steamsdk import find_library

            targets = [
                self.titles.get(item) or WorkshopItem(workshop_id=item)
                for item in diff.to_subscribe
            ]
            self.app.push_screen(
                SubscribeScreen(targets, find_library(self.steam_sdk)),
                self._after_subscribe,
            )
        else:
            self.app.push_screen(ConfirmAlignScreen(diff), self._after_confirm)

    def _after_subscribe(self, outcome) -> None:
        # False means the user backed out at the confirmation, so the selection
        # is left alone too: they asked for none of it.
        if not outcome:
            self.note("nothing was changed")
            return
        self._save_selection()
        self._rescan()

    def _after_confirm(self, confirmed) -> None:
        if not confirmed:
            self.note("nothing was changed")
            return
        self._save_selection()
        self._rescan()

    def _save_selection(self) -> None:
        diff = self.diff
        if diff is None:
            return
        store.save_selection(diff.selection, self.selection_path)

    def _rescan(self) -> None:
        from .tui import ScanScreen

        self.app.switch_screen(ScanScreen(then="manage"))

    def action_back(self) -> None:
        self.app.pop_screen()


class ConfirmAlignScreen(Screen):
    """The confirmation for the case where nothing needs downloading.

    It exists so that the ticking and unticking is never a side effect of
    reading a file. When there are items to subscribe to, the subscribe screen
    is the confirmation and this one is skipped.
    """

    CSS = _RETRO + _SERVER_CSS + """
#choice-area {
    height: auto;
    align: center middle;
    padding: 1 0 0 0;
}
#choice-box {
    border: solid #b4b4b4;
    width: 44;
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
OptionList > .option-list--option-highlighted {
    background: #ffffff;
    color: #000000;
    text-style: bold;
}
"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q,Q", "cancel", "Cancel"),
    ]

    def __init__(self, diff: ServerDiff) -> None:
        super().__init__()
        self.diff = diff

    def compose(self) -> ComposeResult:
        yield Plain("CHANGE THE SELECTION", id="headline")
        yield Plain(
            "Nothing to download. This only changes which mods are ticked.",
            id="subhead",
        )
        with VerticalScroll(id="reportbox"):
            yield Plain(self._body(), id="report")
        with Container(id="choice-area"):
            with Container(id="choice-box"):
                yield OptionList(id="choice")
        yield Plain("", id="footer")

    def _body(self) -> str:
        lines = [""]
        lines += _section("will be ticked", self.diff.to_enable)
        lines += _section("will be unticked", self.diff.to_disable)
        lines += [
            "",
            "  No mod is deleted, no subscription is touched, and nothing is",
            "  written into a save. Only the tool's own selection changes, and",
            "  reading another list replaces it again.",
            "",
        ]
        return "\n".join(lines)

    def on_mount(self) -> None:
        choice = self.query_one("#choice", OptionList)
        moved = len(self.diff.to_enable) + len(self.diff.to_disable)
        choice.add_options(
            [
                Option("  Cancel, change nothing  ".center(34), id="cancel"),
                Option(f"  Change {moved} mod(s)  ".center(34), id="go"),
            ]
        )
        choice.highlighted = 0
        choice.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id == "go")

    def action_cancel(self) -> None:
        self.dismiss(False)
