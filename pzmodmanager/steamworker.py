"""The child process that actually touches the Steamworks library.

Started by steambridge, never by you. It reads one JSON request, does exactly
that one thing, writes a JSON answer, and exits. It has its own stdout, its own
working directory, and its own lifetime, which is the whole point: the library
can print, stall or fall over without any of that reaching the interface.

Everything here is deliberately dull. No interface, no state, no retries. If
something goes wrong the answer carries the reason as text, and the parent
decides what to say about it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _reporter(progress_path: Path):
    """Append one progress line, flushed, so the parent sees it straight away."""

    def say(message: str) -> None:
        try:
            with progress_path.open("a", encoding="utf-8") as handle:
                handle.write(message.replace("\n", " ") + "\n")
                handle.flush()
        except OSError:
            pass

    return say


def _do(action: str, library: Path, items: list[int], say) -> dict:
    from .steamsdk import SteamSDK, SteamSDKError, find_library

    found = find_library(library)
    if found is None:
        return {
            "ok": False,
            "error": (
                f"No Steam library at {library}. Point at steam_api64.dll itself, "
                "or at the folder holding it."
            ),
        }

    say("opening the Steam library...")
    sdk = SteamSDK(found)
    try:
        sdk.open()
    except SteamSDKError as exc:
        return {"ok": False, "error": str(exc), "diagnostics": sdk.diagnostics.lines()}

    answer: dict = {"ok": True, "error": ""}
    try:
        if action in ("list", "check"):
            say("reading the subscription list...")
            answer["subscribed"] = sdk.subscribed_items()
            answer["diagnostics"] = sdk.diagnostics.lines()

        elif action in ("unsubscribe", "subscribe"):
            before = sdk.subscribed_items()
            answer["before"] = len(before)
            say(f"Steam reports {len(before)} subscription(s)")
            if action == "unsubscribe":
                done, failed = sdk.unsubscribe(items, progress=say)
            else:
                done, failed = sdk.subscribe(items)
            after = sdk.subscribed_items()
            answer["done"] = done
            answer["failed"] = failed
            answer["after"] = len(after)
            answer["subscribed"] = after

        else:
            answer = {"ok": False, "error": f"Unknown action: {action}"}

    except SteamSDKError as exc:
        answer = {"ok": False, "error": str(exc), "diagnostics": sdk.diagnostics.lines()}
    except Exception as exc:  # a foreign DLL can fail in ways nobody predicted
        answer = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            sdk.close()
        except Exception:
            pass
    return answer


def main(argv: list[str]) -> int:
    """argv is [request.json, response.json, progress.txt]."""
    if len(argv) < 3:
        return 2
    request_path, response_path, progress_path = (Path(a) for a in argv[:3])
    say = _reporter(progress_path)

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        answer = {"ok": False, "error": f"unreadable request: {exc}"}
    else:
        answer = _do(
            str(request.get("action", "")),
            Path(request.get("library", "")),
            [int(i) for i in request.get("items", [])],
            say,
        )

    try:
        response_path.write_text(json.dumps(answer), encoding="utf-8")
    except OSError:
        return 3
    return 0 if answer.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
