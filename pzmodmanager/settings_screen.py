"""The settings screen: what the tool looks at, and where it writes.

Replaces the read only Locations screen. Every row is editable: ENTER on a
toggle flips it, ENTER on a path or a value opens a field under the list. The
Steam SDK folder lives here, so the bridge no longer needs an option retyped on
every launch.

Changes are saved as soon as you make them. What was detected on this machine is
shown underneath, read only, because it is the thing you usually want to copy
into a field above.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from .settings import Settings
from .tui import RETRO_CSS

# name, label, kind, help
ROWS = [
    ("steam_sdk", "Steam SDK", "path",
     "steam_api64.dll itself, or the folder holding it. Both work. "
     "Changing this only takes effect on the next scan."),
    ("build", "Target build", "text",
     "42 means the newest 42.x branch each mod ships. 42.15 pins it to that client."),
    ("use_defaults", "Auto-detect locations", "bool",
     "Probe the usual Steam libraries and the Zomboid folder."),
    ("extra_paths", "Extra scan folders", "paths",
     "Additional folders to scan, one per line."),
    ("order_path", "Load order file", "path",
     "A server ini, saved_modlists.txt, or a plain list. Empty means look for one."),
    ("use_steam", "Steam Workshop lookup", "bool",
     "Fetch titles, update dates and descriptions from the public Workshop API."),
    ("parse_scripts", "Parse item scripts", "bool",
     "Read media/scripts to find redefined items. Slower on a large mod set."),
    ("only_enabled", "Only analyse enabled mods", "bool",
     "Ignore mods absent from the load order."),
    ("report_path", "Report file", "path",
     "Where the HTML report is written."),
]

# Settings that only take effect the next time mods are read from disk. Saying so
# at the moment of the change beats leaving you to wonder why nothing moved.
NEEDS_RESCAN = {
    "steam_sdk", "build", "use_defaults", "extra_paths",
    "order_path", "use_steam", "parse_scripts", "only_enabled",
}
RESCAN_NOTE = "  ..  run a new scan for this to take effect"

_OWN_CSS = """
#settings-area {
    height: 1fr;
    padding: 0 2;
}
#rows {
    height: 1fr;
    border: heavy #b4b4b4;
    background: #000000;
    color: #b4b4b4;
    overflow-x: hidden;
    scrollbar-background: #000000;
    scrollbar-color: #4a4a4a;
}
#editor {
    height: auto;
    margin: 0 2;
}
#editor-label {
    color: #ffffff;
    height: auto;
    padding: 1 0 0 1;
}
#editor-help {
    color: #6a6a6a;
    height: auto;
    padding: 0 0 0 1;
}
#value {
    background: #000000;
    color: #b4b4b4;
    border: solid #ffffff;
    height: 3;
}
#detected {
    height: auto;
    color: #6a6a6a;
    padding: 1 2 0 2;
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


class SettingsScreen(Screen):
    """Edit and save what the tool uses, without retyping options every time."""

    CSS = RETRO_CSS + _OWN_CSS

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q,Q", "back", "Back"),
        Binding("d,D", "clear_value", "Clear"),
    ]

    def __init__(self, settings: Settings, settings_path: Path | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.settings_path = settings_path
        self.editing: str | None = None
        self.notice = ""

    # ------------------------------------------------------------------ view --

    def compose(self) -> ComposeResult:
        yield Static(
            "Arrow keys to move, ENTER to change a value, 'd' to clear one\n"
            "Saved straight away, but read by the next scan: change something here,"
            " then run Scan. ESC returns to the menu.",
            id="hint",
        )
        with Container(id="settings-area"):
            yield DataTable(id="rows", cursor_type="row", zebra_stripes=False)
        with Container(id="editor"):
            yield Static("", id="editor-label")
            yield Static("", id="editor-help")
            yield Input(id="value")
        yield Static("", id="detected")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        table.add_column("SETTING", width=26)
        table.add_column("VALUE")
        self.query_one("#value", Input).display = False
        self.refresh_rows()
        self.refresh_detected()
        table.focus()

    def refresh_rows(self) -> None:
        table = self.query_one("#rows", DataTable)
        position = table.cursor_row
        table.clear()
        for name, label, _kind, _help in ROWS:
            table.add_row(label, self.settings.describe(name), key=name)
        if ROWS:
            table.move_cursor(row=min(position, len(ROWS) - 1))
        self.query_one("#footer", Static).update(
            self.notice or f"settings file: {self.settings_path or 'default location'}"
        )
        self.show_help()

    def current_row(self):
        table = self.query_one("#rows", DataTable)
        index = table.cursor_row
        if 0 <= index < len(ROWS):
            return ROWS[index]
        return None

    def show_help(self) -> None:
        row = self.current_row()
        if row and not self.editing:
            _name, label, _kind, help_text = row
            self.query_one("#editor-label", Static).update(label)
            self.query_one("#editor-help", Static).update(help_text)

    def refresh_detected(self) -> None:
        from .discovery import default_user_folder, workshop_dirs
        from .steamsdk import find_library

        lines = ["DETECTED ON THIS MACHINE"]
        found = workshop_dirs()
        lines.append(
            f"  Workshop    {found[0] if found else 'not found'}"
            + (f"  (+{len(found) - 1} more)" if len(found) > 1 else "")
        )
        user = default_user_folder()
        lines.append(f"  Game folder {user if user else 'not found'}")
        library = find_library(self.settings.steam_sdk_path)
        if library:
            lines.append(f"  Steam SDK   {library}")
        elif self.settings.steam_sdk:
            lines.append(
                "  Steam SDK   not found at that path (give the dll, or its folder)"
            )
        else:
            lines.append("  Steam SDK   not set, subscriptions cannot be read or changed")
        self.query_one("#detected", Static).update("\n".join(lines))

    # --------------------------------------------------------------- editing --

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_help()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # The table consumes ENTER itself, so the edit hangs off its own event
        # rather than a screen level binding that would never fire.
        self.action_edit()

    def action_edit(self) -> None:
        row = self.current_row()
        if row is None:
            return
        name, label, kind, _help = row
        if kind == "bool":
            setattr(self.settings, name, not getattr(self.settings, name))
            self.save(
                "toggled " + label.lower()
                + (RESCAN_NOTE if name in NEEDS_RESCAN else "")
            )
            return

        self.editing = name
        field = self.query_one("#value", Input)
        current = getattr(self.settings, name)
        field.value = "\n".join(current) if isinstance(current, list) else str(current)
        field.display = True
        self.query_one("#editor-label", Static).update(f"{label}  (ENTER to save, ESC to drop)")
        field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.editing is None:
            return
        name = self.editing
        kind = next(k for n, _l, k, _h in ROWS if n == name)
        text = event.value.strip()
        if kind == "paths":
            setattr(self.settings, name, [p for p in (s.strip() for s in text.splitlines()) if p])
        else:
            setattr(self.settings, name, text)
        self.stop_editing()
        self.save(f"saved {name}" + (RESCAN_NOTE if name in NEEDS_RESCAN else ""))

    def action_clear_value(self) -> None:
        row = self.current_row()
        if row is None:
            return
        name, label, kind, _help = row
        if kind == "bool":
            return
        setattr(self.settings, name, [] if kind == "paths" else "")
        self.save(
            f"cleared {label.lower()}" + (RESCAN_NOTE if name in NEEDS_RESCAN else "")
        )

    def stop_editing(self) -> None:
        self.editing = None
        field = self.query_one("#value", Input)
        field.display = False
        field.value = ""
        self.query_one("#rows", DataTable).focus()

    def save(self, message: str) -> None:
        saved = self.settings.save(self.settings_path)
        self.notice = message if saved else f"{message}, but the file could not be written"
        # The app keeps the live copy, so a scan started next uses the new values.
        self.app.settings = self.settings
        self.refresh_rows()
        self.refresh_detected()

    def action_back(self) -> None:
        if self.editing is not None:
            self.stop_editing()
            self.refresh_rows()
            return
        self.dismiss(self.settings)
