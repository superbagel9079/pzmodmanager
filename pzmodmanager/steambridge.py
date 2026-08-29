"""Talking to Steam from a separate process, and why that is the only safe way.

The first version called the Steamworks library straight from the interface's
worker thread. It froze the screen, and the reason is worth writing down so
nobody puts it back.

The Steam library prints to file descriptors 1 and 2 from C. Python cannot
intercept that by reassigning sys.stdout, so the old code pointed the descriptors
themselves at a temporary file while Steam ran. That works in a plain console.
Inside a full screen interface it is fatal: Textual draws the screen by writing
to descriptor 1, so for as long as the redirect was up, every frame the interface
drew went into the temporary file instead of the terminal. Nothing was hung. The
picture had simply stopped being delivered, which looks exactly like a freeze and
lasts as long as Steam takes to answer.

Running Steam in a child process fixes the whole class of problem at once:

  * the child owns its own descriptors, so whatever the library prints goes to a
    log file and never near the terminal the interface is drawing on;
  * a child can be given a deadline and killed, so a slow or wedged Steam client
    can no longer hang the tool for ever;
  * the child claims app id 108600 and writes steam_appid.txt in its own
    temporary directory, so nothing is left in the user's folders;
  * if the library crashes, and a foreign DLL loaded by ctypes certainly can,
    it takes the child down and not the tool.

The parent sends a small JSON request, the child writes a JSON answer, and
progress lines are appended to a third file that the parent tails while it waits.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# How long to give Steam before deciding it is not coming back. Initialising can
# genuinely take a few seconds on a cold client; a minute and a half is patience,
# not optimism.
LIST_TIMEOUT = 90.0
CHECK_TIMEOUT = 90.0
# Unsubscribing is one call per item plus a settle period, so the budget grows
# with the list rather than being one number that is wrong at both ends.
UNSUBSCRIBE_BASE = 90.0
UNSUBSCRIBE_PER_ITEM = 1.5

POLL_SECONDS = 0.15

WORKER_FLAG = "--steam-worker"


@dataclass
class BridgeAnswer:
    """What the child managed to do, in terms the caller can act on."""

    ok: bool = False
    error: str = ""
    timed_out: bool = False
    subscribed: list[int] = field(default_factory=list)
    done: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    before: int | None = None
    after: int | None = None
    diagnostics: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.ok and not self.error


def worker_command(request: Path, response: Path, progress: Path) -> tuple[list[str], dict]:
    """Build the command that runs this same program in worker mode.

    Frozen, sys.executable is the tool itself and the flag is enough. From
    source it is the Python interpreter, which needs to be told where the package
    lives, because the child's working directory is a temporary folder rather
    than the project.
    """
    tail = [WORKER_FLAG, str(request), str(response), str(progress)]
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        # PyInstaller leaves markers in the environment saying "this process has
        # already unpacked the bundle". Inherited by the child they are a lie,
        # and it would look for files that are not there yet.
        for name in [n for n in env if n.startswith("_PYI") or n.startswith("_MEIPASS")]:
            env.pop(name, None)
        return [sys.executable, *tail], env

    package_parent = Path(__file__).resolve().parent.parent
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{package_parent}{os.pathsep}{existing}" if existing else str(package_parent)
    )
    return [sys.executable, "-m", "pzmodmanager", *tail], env


def _drain(handle, progress) -> None:
    """Report whatever the child has appended since the last look."""
    if progress is None:
        return
    for line in handle.readlines():
        line = line.strip()
        if line:
            progress(line)


def run_action(
    action: str,
    library: Path,
    item_ids: list[int] | None = None,
    timeout: float | None = None,
    progress=None,
) -> BridgeAnswer:
    """Run one Steam action in a child process and wait for it, with a deadline.

    `progress` is called with each line the child reports, as it happens rather
    than in a lump at the end, so a long unsubscribe still looks alive.
    """
    items = [int(i) for i in (item_ids or [])]
    if timeout is None:
        if action == "unsubscribe":
            timeout = UNSUBSCRIBE_BASE + UNSUBSCRIBE_PER_ITEM * len(items)
        elif action == "check":
            timeout = CHECK_TIMEOUT
        else:
            timeout = LIST_TIMEOUT

    with tempfile.TemporaryDirectory(prefix="pzmm-steam-") as workspace:
        room = Path(workspace)
        request = room / "request.json"
        response = room / "response.json"
        progress_file = room / "progress.txt"
        output = room / "steam-output.txt"

        request.write_text(
            json.dumps({"action": action, "library": str(library), "items": items}),
            encoding="utf-8",
        )
        progress_file.touch()

        command, env = worker_command(request, response, progress_file)
        log.info("Steam worker: %s (timeout %.0fs)", action, timeout)

        creation = 0
        if sys.platform.startswith("win"):
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            sink = output.open("wb")
        except OSError:
            sink = subprocess.DEVNULL

        try:
            child = subprocess.Popen(
                command,
                cwd=str(room),          # steam_appid.txt lands here, not in your folders
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,            # Steam's own printing, kept off the terminal
                stderr=subprocess.STDOUT,
                creationflags=creation,
            )
        except OSError as exc:
            if sink is not subprocess.DEVNULL:
                sink.close()
            log.warning("Could not start the Steam worker: %s", exc)
            return BridgeAnswer(error=f"Could not start the Steam helper: {exc}")

        answer = BridgeAnswer()
        deadline = time.monotonic() + timeout
        with progress_file.open("r", encoding="utf-8", errors="replace") as tail:
            while True:
                _drain(tail, progress)
                if child.poll() is not None:
                    break
                if time.monotonic() > deadline:
                    log.warning("Steam worker timed out after %.0fs, killing it", timeout)
                    child.kill()
                    child.wait(timeout=10)
                    answer.timed_out = True
                    answer.error = (
                        f"Steam did not answer within {int(timeout)} seconds. The "
                        "client may be starting, updating, or asking for something "
                        "in its own window."
                    )
                    break
                time.sleep(POLL_SECONDS)
            _drain(tail, progress)

        if sink is not subprocess.DEVNULL:
            sink.close()
        _log_steam_output(output)

        if answer.timed_out:
            return answer

        if not response.is_file():
            code = child.returncode
            answer.error = (
                f"The Steam helper stopped without answering (exit code {code}). "
                "The log has whatever it printed."
            )
            log.warning("Steam worker produced no answer, exit code %s", code)
            return answer

        try:
            payload = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            answer.error = f"The Steam helper's answer could not be read: {exc}"
            return answer

    answer.ok = bool(payload.get("ok"))
    answer.error = payload.get("error", "")
    answer.subscribed = [int(i) for i in payload.get("subscribed", [])]
    answer.done = [int(i) for i in payload.get("done", [])]
    answer.failed = [int(i) for i in payload.get("failed", [])]
    answer.before = payload.get("before")
    answer.after = payload.get("after")
    answer.diagnostics = list(payload.get("diagnostics", []))
    return answer


def _log_steam_output(path: Path) -> None:
    """Put whatever Steam printed in the log, which is where it is useful."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return
    for line in text.splitlines():
        if line.strip():
            log.info("steam: %s", line.strip())


# ------------------------------------------------------------- convenience --


def list_subscriptions(library: Path, progress=None) -> BridgeAnswer:
    return run_action("list", library, progress=progress)


def unsubscribe(library: Path, item_ids: list[int], progress=None) -> BridgeAnswer:
    return run_action("unsubscribe", library, item_ids, progress=progress)


def subscribe(library: Path, item_ids: list[int], progress=None) -> BridgeAnswer:
    return run_action("subscribe", library, item_ids, progress=progress)


def check(library: Path, progress=None) -> BridgeAnswer:
    return run_action("check", library, progress=progress)
