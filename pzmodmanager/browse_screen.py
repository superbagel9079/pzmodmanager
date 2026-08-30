"""Finding mods on the Workshop and subscribing to them.

The other direction from the manager, which only ever works with what is already
on your disk. Here you name something that is not installed yet, look at what
Steam says about it, and add it.

**On searching, honestly.** Looking a mod up by its id needs no key and no
account: `GetPublishedFileDetails` is public, and it is the same call the scan
already uses. Searching the whole Workshop by name is a different endpoint,
`IPublishedFileService/QueryFiles`, and that one wants a Steam Web API key. The
key is free and anyone can create one, but it is a step this tool does not
currently ask anybody to take, and it could not be tested where this was written.

So the screen does the part that certainly works, and hands the rest to Steam:
type a name in the box and press ENTER, and Steam's own Workshop search opens in
your browser with that text. Copy the address of anything you like, paste it back
here, and you get the full card and a one keypress subscribe. It is one more step
than an in-tool search, and it uses the real search rather than an approximation.

The box decides which of the two you meant, rather than asking you to pick a
different key for each: text with an id in it is looked up, text without one is
searched for.

**What subscribing does and does not do.** It tells Steam you want the item.
Steam then downloads it in its own time, in the background, and nothing appears
on disk at the moment you press the key.

That is why adding does not simply rescan afterwards. A scan run at that moment
walks the same folders as before and finds exactly nothing new, which looks like
the tool did nothing. So it watches the Workshop folder for the ids it just
subscribed to, says how many have landed, and starts the scan when they are
there. There is a deadline and an escape, because a download can be queued behind
a game update, or Steam can be asking for something in its own window, and an
interface that waits for ever is its own kind of failure.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from rich.text import Text

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .steam import (
    WorkshopItem,
    build_tags,
    item_url,
    mod_ids_in_description,
    parse_workshop_ids,
    requires_in_description,
    search_url,
)
from .tui import RETRO_CSS, Plain, cell

_BROWSE_CSS = """
#browse-body {
    height: 1fr;
    padding: 0 2;
}
#results {
    width: 3fr;
    border: heavy #b4b4b4;
    background: #000000;
    color: #b4b4b4;
    /* The columns are sized to fit, so a horizontal bar only ever showed as an
       empty stripe under the last row. */
    scrollbar-size-horizontal: 0;
}
#detail-image {
    height: auto;
    padding: 1 0 0 1;
}
#detail {
    width: 2fr;
    border: heavy #b4b4b4;
    background: #000000;
    color: #b4b4b4;
    padding: 0 1;
    margin: 0 0 0 1;
}
#find {
    background: #000000;
    color: #b4b4b4;
    border: solid #ffffff;
    height: 3;
    margin: 0 2;
}
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
}
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
RichLog {
    border: heavy #b4b4b4;
    background: #000000;
    color: #b4b4b4;
    height: 1fr;
    margin: 0 2;
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


# How long to watch the Workshop folder for a download before giving up and
# scanning with whatever arrived. Generous, because a large mod behind a game
# update can genuinely take this long, and ESC ends the wait at any moment.
WAIT_SECONDS = 300.0
WAIT_POLL_SECONDS = 2.0


def _wrap(text: str, width: int) -> list[str]:
    """Break a message into lines that fit, without pulling in textwrap."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# Wrapped in a Text object before it reaches the table. Rich reads "[x]" as a
# markup tag opening the style "x", renders nothing, and leaves the cell empty,
# which looks exactly like a tick that did not register. "[ ]" is not a valid tag
# so it survives untouched, which is why only the ticked state disappeared and
# the bug looked like the key was doing nothing.
CHECKED = Text("[x]")
UNCHECKED = Text("[ ]")


def _size(value) -> str:
    if not value:
        return "unknown size"
    number = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} GB"


def _updated(value) -> str:
    if not value:
        return "unknown"
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return "unknown"


class BrowseScreen(Screen):
    """Look a Workshop item up by id or link, then subscribe to it."""

    CSS = RETRO_CSS + _BROWSE_CSS

    BINDINGS = [
        Binding("escape", "back", "Menu"),
        Binding("space", "toggle", "Mark"),
        Binding("x,X", "toggle", "Mark"),
        Binding("a,A", "add", "Add marked"),
        Binding("s,S", "search_steam", "Search on Steam"),
        Binding("w,W", "open_page", "Workshop page"),
        Binding("c,C", "clear_results", "Clear"),
        Binding("r,R", "rescan", "Rescan"),
        Binding("slash", "focus_find", "Find"),
    ]

    def __init__(
        self,
        installed: set[str] | None = None,
        subscribed: set[str] | None = None,
        steam_cache: Path | None = None,
        installed_mods=None,
        build: str = "42",
    ) -> None:
        super().__init__()
        # What the last scan knew, so an item already on disk or already
        # subscribed says so instead of looking like a fresh find. The full mod
        # records, not just the ids, because checking an item against your set
        # needs to read what your mods require and refuse.
        self.installed_mods = list(installed_mods or [])
        self.by_mod_id = {m.mod_id.strip().lower(): m for m in self.installed_mods}
        self.build = build or "42"
        self.installed = set(installed or ()) | {
            m.workshop_id for m in self.installed_mods if m.workshop_id
        }
        self.subscribed = set(subscribed) if subscribed is not None else None
        self.steam_cache = steam_cache
        self.results: list[WorkshopItem] = []
        # Workshop id -> the downloaded preview, or None once we have tried and
        # failed. Absent means not attempted yet.
        self.previews: dict[str, Path | None] = {}
        self.chosen: set[str] = set()
        self.notice = ""
        self.added_recently = False

    # ------------------------------------------------------------------ view --

    def compose(self) -> ComposeResult:
        yield Plain(
            "Type in the box and press ENTER: an id or link is looked up, "
            "anything else is searched for on Steam\n"
            "'x' or SPACE marks, 'a' adds the marked ones, 'w' opens the page, "
            "'c' clears, ESC returns   ('!' means read the panel before adding)",
            id="hint",
        )
        yield Input(
            placeholder="workshop link or id to look up, or a name to search for...",
            id="find",
        )
        with Horizontal(id="browse-body"):
            yield DataTable(id="results", cursor_type="row", zebra_stripes=False)
            yield VerticalScroll(
                Plain("", id="detail-image"),
                Plain("", id="detail-text"),
                id="detail",
            )
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_column("ADD", width=5)
        table.add_column("TITLE", width=40)
        table.add_column("WORKSHOP ID", width=13)
        table.add_column("STATUS")
        self.refresh_table()
        self.query_one("#find", Input).focus()

    def status_of(self, item: WorkshopItem) -> str:
        if item.missing:
            return "gone from the Workshop"
        if item.workshop_id in self.installed:
            return "already installed"
        if self.subscribed is not None and item.workshop_id in self.subscribed:
            return "subscribed, not on disk yet"
        return "new"

    def concerns(self, item: WorkshopItem) -> list[tuple[str, str]]:
        """What is worth knowing about this item before subscribing to it.

        Everything here is computed from two sources with very different
        standing, and the wording keeps them apart. The Steam tags are picked
        from a fixed list and can be trusted. The description is prose an author
        typed, so what is read out of it is a hint to check, and the panel says
        so rather than dressing a guess up as a finding.

        The real dependency and conflict graph is only knowable once mod.info is
        on disk. This is the part that can be known before, and it is worth
        having: a Build 41 mod or a duplicate id is exactly the thing you want to
        catch before a download, not after.
        """
        out: list[tuple[str, str]] = []
        target = (self.build or "42").split(".")[0]
        builds = build_tags(item.tags)

        if item.missing:
            out.append(("conflict",
                        "This item is gone from the Workshop. Subscribing brings "
                        "nothing down."))
        if builds and target not in builds:
            if target == "42" and builds == ["41"]:
                out.append(("conflict",
                            "Tagged Build 41 only. Build 42 changed the mod folder "
                            "layout and a great deal of the Lua API, so this will "
                            "very probably not load."))
            else:
                out.append(("conflict",
                            f"Tagged Build {' and '.join(builds)}, and you are "
                            f"targeting Build {target}."))
        elif not builds:
            out.append(("warning",
                        "No build tag at all, so there is no telling which version "
                        "of the game it was made for."))

        claimed = mod_ids_in_description(item.description)
        for name in claimed:
            existing = self.by_mod_id.get(name.strip().lower())
            if existing is None:
                continue
            if existing.workshop_id and existing.workshop_id != item.workshop_id:
                out.append(("conflict",
                            f"Mod id {name} is already installed, from Workshop item "
                            f"{existing.workshop_id}. Two folders with the same id is "
                            "a critical problem: the game loads one and ignores the "
                            "other, without saying which."))
            else:
                out.append(("note", f"Mod id {name} is already installed."))

        # The good news case: something you already have is missing a dependency
        # that this item claims to provide.
        lowered = {c.strip().lower() for c in claimed}
        for mod in self.installed_mods:
            for needed in mod.requires:
                if needed.strip().lower() in lowered and needed.strip().lower() not in self.by_mod_id:
                    out.append(("note",
                                f"{mod.mod_id} requires {needed}, which is not "
                                "installed. This item claims to provide it."))
            for refused in mod.incompatible:
                if refused.strip().lower() in lowered:
                    out.append(("conflict",
                                f"{mod.mod_id}, which you have installed, declares "
                                f"{refused} incompatible."))

        for needed in requires_in_description(item.description):
            if needed.strip().lower() not in self.by_mod_id:
                out.append(("warning",
                            f"The description says it requires {needed}, which does "
                            "not match anything installed. Read from prose, so check "
                            "the page."))
        return out

    def worst(self, item: WorkshopItem) -> str:
        kinds = {kind for kind, _text in self.concerns(item)}
        if "conflict" in kinds:
            return "conflict"
        if "warning" in kinds:
            return "warning"
        return ""

    def refresh_table(self) -> None:
        table = self.query_one("#results", DataTable)
        position = table.cursor_row
        table.clear()
        for item in self.results:
            mark = CHECKED if item.workshop_id in self.chosen else UNCHECKED
            flag = {"conflict": "! ", "warning": ". "}.get(self.worst(item), "  ")
            title = item.title or "(no title returned)"
            table.add_row(
                cell(mark),
                cell(flag + title[:38]),
                cell(item.workshop_id),
                cell(self.status_of(item)),
                key=item.workshop_id,
            )
        if self.results:
            table.move_cursor(row=min(max(position, 0), len(self.results) - 1))
        self.refresh_detail()
        self.refresh_footer()

    def refresh_footer(self) -> None:
        if self.notice:
            self.query_one("#footer", Static).update(self.notice)
            return
        marked = len(self.chosen)
        if not self.results:
            text = "nothing looked up yet"
        else:
            text = f"{len(self.results)} result(s), {marked} marked to add"
        self.query_one("#footer", Static).update(text)

    def current(self) -> WorkshopItem | None:
        table = self.query_one("#results", DataTable)
        index = table.cursor_row
        if 0 <= index < len(self.results):
            return self.results[index]
        return None

    def refresh_image(self, item: WorkshopItem | None) -> None:
        """Draw the Workshop preview as half blocks, or say why there is none."""
        panel = self.query_one("#detail-image", Static)
        if item is None:
            panel.update("")
            return

        from .posters import pillow_available, poster_blocks

        path = self.previews.get(item.workshop_id)
        if path is not None:
            blocks = poster_blocks(path, width=28, max_rows=10)
            if blocks is not None:
                panel.update(blocks)
                return
        if not pillow_available():
            panel.update("  (no picture: Pillow is not installed)")
        elif item.workshop_id not in self.previews:
            panel.update("  (fetching the picture...)")
        else:
            panel.update("  (this item has no preview image)")

    def refresh_detail(self) -> None:
        item = self.current()
        self.refresh_image(item)
        panel = self.query_one("#detail-text", Static)
        if item is None:
            panel.update(
                "\n".join(
                    [
                        "",
                        "  Nothing selected.",
                        "",
                        "  Paste a Workshop link or an id above.",
                        "",
                        "  To find something by name, type the name and press ENTER.",
                        "  Steam's own Workshop search opens in your browser, and you",
                        "  paste the link of anything you like back into the box.",
                        "",
                        "  Searching from inside the tool needs a Steam Web API key,",
                        "  which this tool does not ask you for.",
                        "",
                    ]
                )
            )
            return

        lines = ["", f"  {item.title or '(no title)'}", ""]
        lines.append(f"    id        {item.workshop_id}")
        lines.append(f"    updated   {_updated(item.time_updated)}")
        lines.append(f"    size      {_size(item.file_size)}")
        lines.append(f"    status    {self.status_of(item)}")
        builds = build_tags(item.tags)
        lines.append(
            f"    build     {' and '.join(builds) if builds else 'not declared'}"
            f"   (you target {(self.build or '42').split('.')[0]})"
        )
        if item.tags:
            lines.append(f"    tags      {', '.join(item.tags[:6])}")
        lines.append("")
        lines.append(f"    {item_url(item.workshop_id)}")
        lines.append("    press 'w' to open it")
        lines.append("")

        found = self.concerns(item)
        if found:
            conflicts = [t for k, t in found if k == "conflict"]
            warnings = [t for k, t in found if k == "warning"]
            notes = [t for k, t in found if k == "note"]
            lines.append("    BEFORE YOU ADD THIS")
            lines.append("")
            for kind, group in (("[!!]", conflicts), ("[! ]", warnings), ("[ .]", notes)):
                for text in group:
                    wrapped = []
                    line = ""
                    for word in text.split():
                        if len(line) + len(word) + 1 > 46:
                            wrapped.append(line)
                            line = word
                        else:
                            line = f"{line} {word}".strip()
                    wrapped.append(line)
                    lines.append(f"      {kind} {wrapped[0]}")
                    for extra in wrapped[1:]:
                        lines.append(f"           {extra}")
                    lines.append("")

        claimed = mod_ids_in_description(item.description)
        if claimed:
            lines.append("    MOD IDS THE DESCRIPTION CLAIMS")
            for name in claimed[:8]:
                already = "  (already installed)" if name.lower() in {
                    i.lower() for i in self.installed
                } else ""
                lines.append(f"      {name}{already}")
            lines.append("")
            if len(claimed) > 1:
                lines.append(f"    This one item installs {len(claimed)} separate mods.")
                lines.append("    You subscribe to the item as a whole, then enable the")
                lines.append("    ones you want in the manager. Several are often")
                lines.append("    variants meant to be used one at a time, so read the")
                lines.append("    page before enabling them all.")
                lines.append("")
            lines.append("    Written by the author by hand, so treat it as a hint.")
            lines.append("    The real ids are read from mod.info after downloading.")
            lines.append("")

        if item.missing:
            lines.append("    This item is no longer on the Workshop. Subscribing to")
            lines.append("    it will not bring anything down.")
            lines.append("")

        if item.description:
            lines.append("    DESCRIPTION")
            text = " ".join(item.description.split())
            for start in range(0, min(len(text), 700), 52):
                lines.append(f"      {text[start:start + 52]}")
            if len(text) > 700:
                lines.append("      ...")
            lines.append("")
        self.query_one("#detail-text", Static).update("\n".join(lines))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.refresh_detail()

    # --------------------------------------------------------------- looking --

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        ids = parse_workshop_ids(text)
        if not ids:
            # Not an id, so it is a name, so search for it. The 's' binding
            # cannot fire while the box has focus: a letter typed into an Input
            # is text, not a shortcut, and telling you to press it there was
            # advice that could not work.
            self.search_for(text)
            return
        self.notice = f"looking up {len(ids)} item(s)..."
        self.refresh_footer()
        self.lookup(ids)

    @work(thread=True, exclusive=True)
    def lookup(self, ids: list[str]) -> None:
        """The network call, off the interface thread so nothing freezes."""
        from .steam import WorkshopCache, fetch_items

        app = self.app
        cache = WorkshopCache(self.steam_cache) if self.steam_cache else None
        found = fetch_items(ids, cache=cache)
        # Keep the order the user pasted, and say so when Steam knew nothing.
        items = [found[i] for i in ids if i in found]
        unknown = [i for i in ids if i not in found]

        # The pictures come down on the same thread, for the same reason the
        # lookup does: this is the network, and the interface must keep drawing.
        from .posters import fetch_preview

        previews: dict[str, Path | None] = {}
        for item in items:
            previews[item.workshop_id] = (
                fetch_preview(item.preview_url) if item.preview_url else None
            )
        app.call_from_thread(self.lookup_finished, items, unknown, previews)

    def lookup_finished(
        self,
        items: list[WorkshopItem],
        unknown: list[str],
        previews: dict[str, Path | None] | None = None,
    ) -> None:
        self.previews.update(previews or {})
        known = {i.workshop_id for i in self.results}
        fresh = [i for i in items if i.workshop_id not in known]
        # Newest at the top: what you just asked for is what you want to look at,
        # and appending buried it under everything looked up before.
        self.results = fresh + self.results
        added = len(fresh)
        if unknown:
            self.notice = (
                f"{added} added. Steam returned nothing for {', '.join(unknown[:3])}"
                + (" and others" if len(unknown) > 3 else "")
                + ". Check the id, or the item may be private or removed."
            )
        elif added:
            self.notice = f"{added} item(s) found. SPACE to mark, 'a' to add."
        else:
            self.notice = "already in the list."
        self.query_one("#find", Input).value = ""
        self.refresh_table()
        table = self.query_one("#results", DataTable)
        if added:
            table.move_cursor(row=0)
        table.focus()
        self.refresh_detail()

    # ---------------------------------------------------------------- acting --

    def action_focus_find(self) -> None:
        self.query_one("#find", Input).focus()

    def action_toggle(self) -> None:
        item = self.current()
        if item is None:
            return
        if item.workshop_id in self.chosen:
            self.chosen.discard(item.workshop_id)
        else:
            if item.workshop_id in self.installed:
                self.notice = f"{item.title or item.workshop_id} is already installed"
            elif item.missing:
                self.notice = "that item is gone from the Workshop, adding it does nothing"
            else:
                self.notice = ""
            self.chosen.add(item.workshop_id)
        self.refresh_table()

    def action_clear_results(self) -> None:
        self.results = []
        self.chosen = set()
        self.notice = "list cleared"
        self.refresh_table()

    def action_open_page(self) -> None:
        item = self.current()
        if item is None:
            return
        webbrowser.open(item_url(item.workshop_id))
        self.notice = f"opened {item.workshop_id} in the browser"
        self.refresh_footer()

    def search_for(self, text: str) -> None:
        """Hand the text to Steam's own Workshop search."""
        webbrowser.open(search_url(text))
        self.notice = (
            f"Steam is searching for {text!r} in your browser. Copy an item's "
            "address and paste it back here."
        )
        self.refresh_footer()

    def action_search_steam(self) -> None:
        """The same thing from the results table, where the key does reach us."""
        text = self.query_one("#find", Input).value.strip()
        if not text:
            self.notice = "type a name in the box, then press ENTER"
            self.refresh_footer()
            return
        self.search_for(text)

    def action_rescan(self) -> None:
        """Only worth offering once something has actually been added."""
        if not self.added_recently:
            self.notice = "nothing added yet, so there is nothing new to find"
            self.refresh_footer()
            return
        from .tui import ScanScreen

        self.app.switch_screen(ScanScreen(then="manage"))

    def action_add(self) -> None:
        from .steamsdk import find_library

        targets = [i for i in self.results if i.workshop_id in self.chosen]
        if not targets:
            self.notice = "nothing marked. SPACE marks the highlighted item."
            self.refresh_footer()
            return
        configured = getattr(self.app, "steam_sdk", None)
        self.app.push_screen(
            SubscribeScreen(targets, find_library(configured)), self._after_add
        )

    def _after_add(self, outcome) -> None:
        """Rescan on the way back, when the files are actually there.

        The run screen watched the Workshop folder, so by the time it says
        "scan" there is something new to find. Without that wait an automatic
        rescan would walk the same folders as before and report nothing, which
        reads as a broken tool rather than a download still in flight.
        """
        if not outcome:
            self.refresh_table()
            return
        self.added_recently = True
        self.chosen = set()
        if outcome == "scan":
            from .tui import ScanScreen

            self.app.switch_screen(ScanScreen(then="manage"))
            return
        self.notice = (
            "Added, but the files have not arrived yet. Press 'r' to scan once "
            "Steam has finished."
        )
        self.refresh_table()

    def action_back(self) -> None:
        self.app.pop_screen()


class SubscribeScreen(Screen):
    """Confirm on the whole screen, listing everything, starting on Cancel.

    Adding is far less dangerous than removing: nothing of yours is destroyed,
    and unsubscribing again undoes it. It still gets a full list and a Cancel
    default, because a confirmation that behaves differently depending on how
    risky the action is teaches you to stop reading it.
    """

    CSS = RETRO_CSS + _BROWSE_CSS

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q,Q", "cancel", "Cancel"),
    ]

    def __init__(self, targets: list[WorkshopItem], library: Path | None) -> None:
        super().__init__()
        self.targets = targets
        self.library = library

    def compose(self) -> ComposeResult:
        yield Plain(
            "Arrow keys to choose, ENTER to act, ESC to go back without changing anything",
            id="hint",
        )
        if self.library is None:
            yield Plain("STEAM LIBRARY NOT FOUND", id="headline")
            yield Plain(
                "Subscribing goes through the Steam client, so it needs the SDK.",
                id="subhead",
            )
            yield VerticalScroll(Plain(self._missing_body(), id="list"), id="listbox")
        else:
            count = len(self.targets)
            yield Plain(
                f"SUBSCRIBE TO {count} WORKSHOP ITEM{'S' if count > 1 else ''}",
                id="headline",
            )
            yield Plain(
                "Added to the Steam account currently logged in.", id="subhead"
            )
            yield VerticalScroll(Plain(self._list_body(), id="list"), id="listbox")

        with Container(id="choice-area"):
            with Container(id="choice-box"):
                yield OptionList(id="choice")
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        choice = self.query_one("#choice", OptionList)
        if self.library is None:
            choice.add_options([Option("  Back  ".center(34), id="cancel")])
        else:
            count = len(self.targets)
            choice.add_options(
                [
                    Option("  Cancel, change nothing  ".center(34), id="cancel"),
                    Option(f"  Subscribe to {count} item(s)  ".center(34), id="go"),
                ]
            )
        choice.highlighted = 0
        choice.focus()
        self.query_one("#footer", Static).update(
            f"library: {self.library}" if self.library else "no library"
        )

    def _list_body(self) -> str:
        lines = [""]
        total = 0
        for item in self.targets:
            total += int(item.file_size or 0)
            title = (item.title or "(no title)")[:44]
            lines.append(f"  {title.ljust(44)}  {item.workshop_id.rjust(11)}  {_size(item.file_size)}")
        lines += [
            "",
            f"  {len(self.targets)} item(s), about {_size(total)} to download.",
            "",
            "  Steam downloads these in the background, in its own time. Nothing",
            "  appears on disk when you confirm, and the scan will not see them",
            "  until the download finishes.",
            "",
            "  Subscribing is undone by unsubscribing. Nothing here is permanent.",
            "",
        ]
        return "\n".join(lines)

    def _missing_body(self) -> str:
        from .steamsdk import platform_dll_names

        return "\n".join(
            [
                "",
                f"  The tool needs {platform_dll_names()[0]} from the Steamworks SDK.",
                "",
                "  Set it under Settings, as the dll itself or the folder holding it,",
                "  then run a new scan.",
                "",
                "  Until then you can still look items up here, and press 'w' to open",
                "  a page in Steam, where the Subscribe button lives.",
                "",
            ]
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "go" and self.library is not None:
            self.app.push_screen(
                SubscribeRunScreen(self.targets, self.library), self._run_finished
            )
        else:
            self.dismiss(False)

    def _run_finished(self, outcome) -> None:
        self.dismiss(outcome)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SubscribeRunScreen(Screen):
    """Does the subscribing in the child process, showing each step."""

    CSS = RETRO_CSS + _BROWSE_CSS

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q,Q", "back", "Back")]

    def __init__(self, targets: list[WorkshopItem], library: Path) -> None:
        super().__init__()
        self.targets = targets
        self.library = library
        self.changed = False
        self.waiting = False
        self.scan_next = False

    def compose(self) -> ComposeResult:
        yield Plain("Talking to Steam. Do not close Steam while this runs.", id="hint")
        yield Plain("SUBSCRIBING", id="headline")
        yield Plain(f"{len(self.targets)} item(s)", id="subhead")
        yield RichLog(id="progress", wrap=True, markup=False, highlight=False)
        yield Plain("", id="footer")

    def on_mount(self) -> None:
        self.run_worker_job()

    def append(self, message: str) -> None:
        self.query_one("#progress", RichLog).write(message)

    @work(thread=True, exclusive=True)
    def run_worker_job(self) -> None:
        from .steambridge import subscribe

        app = self.app

        def progress(message: str) -> None:
            app.call_from_thread(self.append, f"  {message}")

        answer = subscribe(
            self.library, [int(i.workshop_id) for i in self.targets], progress=progress
        )
        if not answer.usable:
            app.call_from_thread(self.finish_with_error, answer.error)
            return
        app.call_from_thread(
            self.finish, answer.done, answer.failed, answer.before or 0, answer.after or 0
        )

    def wait_for_files(self, ids: list[int]) -> None:
        """Watch the Workshop folder until the new items land, then say so."""
        self.waiting = True
        self.query_one("#headline", Static).update("WAITING FOR STEAM")
        self.query_one("#footer", Static).update(
            "ESC stops waiting and goes back, the download carries on regardless"
        )
        self.watch_worker(ids)

    @work(thread=True, exclusive=False)
    def watch_worker(self, ids: list[int]) -> None:
        import time

        from .discovery import workshop_dirs

        app = self.app
        wanted = {str(i) for i in ids}
        roots = workshop_dirs()

        def landed() -> set[str]:
            found = set()
            for root in roots:
                for item in wanted:
                    folder = Path(root) / item
                    try:
                        if folder.is_dir() and any(folder.iterdir()):
                            found.add(item)
                    except OSError:
                        pass
            return found

        if not roots:
            app.call_from_thread(
                self.stop_waiting,
                "No Workshop folder found on this machine, so there is nothing to "
                "watch. Rescan yourself once Steam has finished.",
                False,
            )
            return

        deadline = time.monotonic() + WAIT_SECONDS
        seen: set[str] = set()
        while self.waiting and time.monotonic() < deadline:
            now = landed()
            if now != seen:
                seen = now
                app.call_from_thread(
                    self.append,
                    f"  {len(seen)} of {len(wanted)} downloaded"
                    + (f": {', '.join(sorted(seen))}" if seen else ""),
                )
            if seen >= wanted:
                app.call_from_thread(
                    self.stop_waiting, "All of them are on disk. Scanning now.", True
                )
                return
            time.sleep(WAIT_POLL_SECONDS)

        if not self.waiting:
            return
        app.call_from_thread(
            self.stop_waiting,
            f"Gave up after {int(WAIT_SECONDS / 60)} minutes with "
            f"{len(seen)} of {len(wanted)} downloaded. Steam may be queued behind "
            "something else. Scanning anyway, so what did arrive is picked up.",
            True,
        )

    def stop_waiting(self, message: str, then_scan: bool) -> None:
        self.waiting = False
        self.append("")
        for line in _wrap(message, 66):
            self.append(f"  {line}")
        self.scan_next = then_scan
        self.query_one("#headline", Static).update("DONE")
        self.query_one("#footer", Static).update(
            "ESC to carry on" + (" (a scan starts)" if then_scan else "")
        )

    def finish(self, done: list[int], failed: list[int], before: int, after: int) -> None:
        names = {str(i.workshop_id): (i.title or i.workshop_id) for i in self.targets}
        self.append("")
        for item in done:
            self.append(f"  added     {names.get(str(item), item)}")
        for item in failed:
            self.append(f"  not added {names.get(str(item), item)}")
        self.append("")
        self.append(f"  subscriptions: {before} before, {after} after")
        if failed:
            self.append("")
            self.append("  Steam may not have caught up yet, or the item may be")
            self.append("  private, removed, or for another game.")
        self.changed = bool(done)
        if done:
            self.append("")
            self.append("  Steam is downloading them now. Watching the Workshop folder")
            self.append("  so the scan starts when they are actually there.")
            self.append("")
            self.wait_for_files(done)
        else:
            self.query_one("#headline", Static).update("NOTHING ADDED")
            self.query_one("#footer", Static).update("ESC to go back")

    def finish_with_error(self, message: str) -> None:
        self.append("")
        self.append(f"  Failed: {message}")
        self.append("  Nothing was changed. Run --steam-check for a fuller diagnosis.")
        self.query_one("#headline", Static).update("FAILED")
        self.query_one("#footer", Static).update("ESC to go back")

    def action_back(self) -> None:
        # Stops the watcher thread on its next look, rather than killing it.
        self.waiting = False
        self.dismiss("scan" if (self.changed and self.scan_next) else self.changed)
