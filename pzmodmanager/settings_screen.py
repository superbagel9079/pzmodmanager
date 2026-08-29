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
    ("data_dir", "Data folder", "path",
     "Where the last scan, your selection, the Workshop cache and the log are "
     "kept. Empty means the usual per-user folder. Takes effect on the next launch."),
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

    # Actions rather than values. ENTER arms one, ENTER again carries it out, so
    # a stray keypress on a list you are scrolling through cannot wipe anything.
    ("reset_scan", "Clear the last scan", "action",
     "Forgets the saved results. The menu goes back to offering Scan rather than "
     "Last results. Your mods are untouched."),
    ("reset_selection", "Clear the saved selection", "action",
     "Forgets which mods you ticked in the manager. It starts again from what the "
     "load order had enabled."),
    ("reset_cache", "Clear the Workshop cache", "action",
     "Throws away the cached Workshop answers and preview images. The next scan "
     "asks Steam again, which is slower once."),
    ("reset_all", "Reset everything above", "action",
     "Every setting back to its default, and all of the above cleared. Nothing "
     "outside this tool is touched: no mod, no save, no server file."),
]

RESET_ACTIONS = {"reset_scan", "reset_selection", "reset_cache", "reset_all"}

# Settings that only take effect the next time mods are read from disk. Saying so
# at the moment of the change beats leaving you to wonder why nothing moved.
NEEDS_RESCAN = {
    "steam_sdk", "build", "use_defaults", "extra_paths",
    "order_path", "use_steam", "parse_scripts", "only_enabled",
}
RESCAN_NOTE = "  ..  run a new scan for this to take effect"

# The data folder is resolved once, at startup, because the log file is opened
# before anything else happens. A rescan cannot move it; only a relaunch can.
NEEDS_RESTART = {"data_dir"}
RESTART_NOTE = "  ..  restart the tool for this to take effect"

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


def _note(name: str) -> str:
    """What the user has to do for this change to actually apply."""
    if name in NEEDS_RESTART:
        return RESTART_NOTE
    return RESCAN_NOTE if name in NEEDS_RESCAN else ""


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
        # Which action is one keypress from happening. Cleared by any movement.
        self.armed: str | None = None
        self.value_column = None

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
        # Kept, so one cell can be rewritten without redrawing the whole table.
        self.value_column = table.add_column("VALUE")
        self.query_one("#value", Input).display = False
        self.refresh_rows()
        self.refresh_detected()
        table.focus()

    def refresh_rows(self) -> None:
        table = self.query_one("#rows", DataTable)
        position = table.cursor_row
        table.clear()
        for name, label, kind, _help in ROWS:
            if kind == "action":
                value = (
                    "ENTER again to confirm" if self.armed == name else "ENTER to do it"
                )
            else:
                value = self.settings.describe(name)
            table.add_row(label, value, key=name)
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
        from .store import config_dir, state_dir

        lines.append(f"  Data folder {state_dir()}")
        lines.append(f"  Settings    {config_dir()}  (this one never moves)")
        self.query_one("#detected", Static).update("\n".join(lines))

    # --------------------------------------------------------------- editing --

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Moving to another row disarms a pending action.

        The comparison matters. Redrawing the table moves the cursor back onto
        the same row, which fires this event again, so disarming on any highlight
        at all disarmed the action the instant it was armed: the first ENTER
        appeared to do nothing and the second armed it, one press out of step for
        ever.
        """
        if self.armed is None:
            self.show_help()
            return
        key = getattr(event.row_key, "value", None)
        if key is not None and key != self.armed:
            self.armed = None
            self.notice = ""
            self.call_after_refresh(self.refresh_rows)
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
        if kind == "action":
            self.run_action(name, label)
            return
        if kind == "bool":
            setattr(self.settings, name, not getattr(self.settings, name))
            self.save("toggled " + label.lower() + _note(name))
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
        self.save(f"saved {name}" + _note(name))

    def run_action(self, name: str, label: str) -> None:
        """Two presses: the first arms, the second acts.

        Arming rewrites one cell rather than redrawing the table, and that is not
        a micro-optimisation. Redrawing clears the rows, which snaps the cursor to
        the first one, which fires a highlight for a different row, which disarmed
        the action a moment after it was armed. The first ENTER then looked like
        it had done nothing and every press was one out of step.
        """
        if self.armed != name:
            self.armed = name
            self.notice = f"{label.lower()}: press ENTER again to confirm"
            table = self.query_one("#rows", DataTable)
            if self.value_column is not None:
                try:
                    table.update_cell(name, self.value_column, "ENTER again to confirm")
                except Exception:
                    pass
            self.query_one("#footer", Static).update(self.notice)
            return
        self.armed = None
        self.notice = self.perform_reset(name)
        self.refresh_rows()
        self.refresh_detected()

    def perform_reset(self, name: str) -> str:
        """Delete this tool's own state. Nothing of the game's is touched."""
        from . import store
        from .posters import preview_cache_dir

        def remove(path: Path) -> bool:
            try:
                if path.is_file():
                    path.unlink()
                    return True
            except OSError as exc:
                log_note.append(str(exc))
            return False

        log_note: list[str] = []
        done: list[str] = []

        if name in ("reset_scan", "reset_all"):
            if remove(store.default_store_path()):
                done.append("last scan")
            # The menu reads this, so it has to be told, not just the file.
            self.app.stored = None
        if name in ("reset_selection", "reset_all"):
            if remove(store.default_selection_path()):
                done.append("selection")
        if name in ("reset_cache", "reset_all"):
            if remove(store.default_steam_cache_path()):
                done.append("Workshop cache")
            folder = preview_cache_dir()
            removed = 0
            try:
                for entry in folder.glob("*.img"):
                    try:
                        entry.unlink()
                        removed += 1
                    except OSError:
                        pass
            except OSError:
                pass
            if removed:
                done.append(f"{removed} preview image(s)")
        if name == "reset_all":
            self.settings = Settings()
            self.settings.save(self.settings_path)
            self.app.settings = self.settings
            done.append("every setting")

        if log_note:
            return f"partly done: {log_note[0]}"
        if not done:
            return "nothing to clear, it was already empty"
        return "cleared: " + ", ".join(done)

    def action_clear_value(self) -> None:
        row = self.current_row()
        if row is None:
            return
        name, label, kind, _help = row
        if kind == "action":
            self.run_action(name, label)
            return
        if kind == "bool":
            return
        setattr(self.settings, name, [] if kind == "paths" else "")
        self.save(f"cleared {label.lower()}" + _note(name))

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
