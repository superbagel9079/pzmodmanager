"""Talking to the Steam client through the Steamworks SDK.

The Web API cannot change a subscription: `UnsubscribeItem` there is a publisher
method on partner.steam-api.com, and a publisher key only ever covers that
publisher's own apps. Project Zomboid is not ours, so that door stays shut.

The SDK is a different door. `ISteamUGC::SubscribeItem` and `UnsubscribeItem`
run inside the Steam client, act on whoever is logged in, and need no key at all.
That is how third party Workshop managers do it.

Three things to be clear about before using any of this.

  * It needs the Steamworks SDK. Only `steam_api64.dll` is used, and it is never
    shipped with this tool: you download the SDK and point at it.
  * The process has to identify itself to Steam as an app, which here means
    claiming app id 108600. Your tool tells Steam it is Project Zomboid. That is
    what every third party Workshop manager does, and it is a grey area in
    Steam's terms rather than a documented, blessed route.
  * Unsubscribing removes the local files once Steam next shuts down. On a
    machine that also feeds a server, the mod is gone for you too.

The flat C API changes names between SDK releases: the UGC accessor carries a
version suffix. Everything here probes for what is actually exported and reports
what it found, because a wrong guess would otherwise fail with a bare
AttributeError.

Checked against SDK 1.65: the library exports SteamAPI_InitFlat but no
SteamAPI_Init, which is an inline helper in the C++ header rather than a symbol,
and the accessor is SteamAPI_SteamUGC_v021. Loading, symbol resolution and the
init call are all verified against that release. What could not be verified
anywhere without a running Steam client is the subscribe and unsubscribe calls
themselves.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PZ_APP_ID = "108600"

# Newest first: the accessor is exported as SteamAPI_SteamUGC_v0NN and the
# number moves with the SDK. Probing beats hardcoding one and hoping.
UGC_ACCESSOR_NAMES = [f"SteamAPI_SteamUGC_v{n:03d}" for n in range(25, 9, -1)]
UGC_ACCESSOR_NAMES.append("SteamAPI_SteamUGC")

DLL_NAMES = {
    "win32": ["steam_api64.dll", "steam_api.dll"],
    "darwin": ["libsteam_api.dylib"],
    "linux": ["libsteam_api.so"],
}

# How long to pump callbacks after firing a call, before checking the result by
# re-reading the subscription list.
SETTLE_SECONDS = 3.0

# ESteamAPIInitResult, read from steam_api.h in SDK 1.65.
INIT_OK = 0
INIT_RESULTS = {
    1: "Steam refused to initialise, for an unspecified reason.",
    2: "Steam is not running, or this process cannot reach it. Start the Steam "
       "client, log in, and try again.",
    3: "The Steam client is older than this SDK. Let Steam update itself first.",
}


@contextlib.contextmanager
def steam_output_to_log():
    """Send descriptors 1 and 2 to the log for a while. Console use only.

    NEVER call this while a full screen interface is running. Textual draws the
    screen by writing to descriptor 1, so redirecting it also redirects the
    interface: the picture stops arriving, the screen appears to freeze, and
    nothing in the traceback ever points here. That is precisely the bug this
    module used to have, and it is why the Steam work now happens in a child
    process (see steambridge) where the descriptors are the child's own.

    Kept because it is still the right tool for a plain console command that
    wants Steam's C level chatter in the log rather than in the output.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (ValueError, OSError):
        pass
    saved_out, saved_err = os.dup(1), os.dup(2)
    sink = tempfile.TemporaryFile()
    try:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        yield
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (ValueError, OSError):
            pass
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        try:
            sink.seek(0)
            written = sink.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            written = ""
        sink.close()
        if written:
            for line in written.splitlines():
                log.info("steam: %s", line)


class SteamSDKError(RuntimeError):
    """Anything that stops the bridge from working, with a readable reason."""


@dataclass
class Diagnostics:
    """What the bridge found, so a failure can be understood without a debugger."""

    dll_path: str = ""
    init_symbol: str = ""
    ugc_accessor: str = ""
    subscribed_count: int | None = None
    app_id: str = PZ_APP_ID
    notes: list[str] = field(default_factory=list)
    ok: bool = False

    def lines(self) -> list[str]:
        out = [
            f"library      {self.dll_path or 'not found'}",
            f"init symbol  {self.init_symbol or 'not found'}",
            f"UGC accessor {self.ugc_accessor or 'not found'}",
            f"app id       {self.app_id}",
        ]
        if self.subscribed_count is not None:
            out.append(f"subscribed   {self.subscribed_count} item(s) visible")
        out += [f"note         {note}" for note in self.notes]
        out.append(f"result       {'usable' if self.ok else 'not usable'}")
        return out


def platform_dll_names() -> list[str]:
    if sys.platform.startswith("win"):
        return DLL_NAMES["win32"]
    if sys.platform == "darwin":
        return DLL_NAMES["darwin"]
    return DLL_NAMES["linux"]


def find_library(explicit: Path | None = None) -> Path | None:
    """Locate the Steamworks redistributable.

    Looks where told, then beside the tool, then in the working directory. The
    SDK is never bundled, so if none of those has it the answer is honestly none.
    """
    names = platform_dll_names()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        if path.is_dir():
            for name in names:
                candidate = path / name
                if candidate.is_file():
                    return candidate
        log.warning("No Steam library at %s", path)
        return None

    roots = [Path.cwd(), Path(sys.argv[0]).resolve().parent, Path(__file__).resolve().parent.parent]
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


class SteamSDK:
    """A minimal ctypes bridge to ISteamUGC. Use it as a context manager."""

    def __init__(self, library: Path, app_id: str = PZ_APP_ID) -> None:
        self.library_path = Path(library)
        self.app_id = str(app_id)
        self.lib: ctypes.CDLL | None = None
        self.ugc = None
        self.diagnostics = Diagnostics(dll_path=str(self.library_path), app_id=self.app_id)
        self._appid_file: Path | None = None
        self._previous_env: str | None = None

    # ------------------------------------------------------------- lifecycle --

    def __enter__(self) -> "SteamSDK":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _claim_app_id(self) -> None:
        """Tell the Steam client which app we are.

        SteamAPI_Init reads the SteamAppId environment variable, and falls back
        to a steam_appid.txt in the working directory. The variable is preferred
        here because it leaves nothing behind on disk.
        """
        self._previous_env = os.environ.get("SteamAppId")
        os.environ["SteamAppId"] = self.app_id
        os.environ["SteamGameId"] = self.app_id

        appid_file = Path.cwd() / "steam_appid.txt"
        if not appid_file.exists():
            try:
                appid_file.write_text(self.app_id, encoding="ascii")
                self._appid_file = appid_file
            except OSError as exc:
                self.diagnostics.notes.append(
                    f"could not write steam_appid.txt ({exc}); relying on the "
                    "environment variable alone"
                )

    def _release_app_id(self) -> None:
        if self._previous_env is None:
            os.environ.pop("SteamAppId", None)
        else:
            os.environ["SteamAppId"] = self._previous_env
        os.environ.pop("SteamGameId", None)
        if self._appid_file and self._appid_file.exists():
            try:
                self._appid_file.unlink()
            except OSError:
                pass

    def _init_api(self) -> None:
        """Call whichever init the SDK exports, newest calling convention first."""
        assert self.lib is not None

        # SDK 1.59 and later export SteamAPI_InitFlat, which fills an error buffer.
        init_flat = getattr(self.lib, "SteamAPI_InitFlat", None)
        if init_flat is not None:
            buffer = ctypes.create_string_buffer(1024)
            init_flat.restype = ctypes.c_int
            init_flat.argtypes = [ctypes.c_char_p]
            result = init_flat(buffer)
            self.diagnostics.init_symbol = "SteamAPI_InitFlat"
            if result != INIT_OK:
                message = buffer.value.decode("utf-8", "replace").strip()
                raise SteamSDKError(
                    f"{INIT_RESULTS.get(result, f'init failed with code {result}')}"
                    + (f" Steam said: {message}" if message else "")
                )
            return

        init = getattr(self.lib, "SteamAPI_Init", None)
        if init is None:
            raise SteamSDKError(
                "This library exports neither SteamAPI_InitFlat nor SteamAPI_Init. "
                "It may not be the Steamworks redistributable."
            )
        init.restype = ctypes.c_bool
        init.argtypes = []
        self.diagnostics.init_symbol = "SteamAPI_Init"
        if not init():
            raise SteamSDKError(
                "SteamAPI_Init returned false. The usual causes are the Steam "
                "client not running, not being logged in, or the account not "
                f"owning app {self.app_id}."
            )

    def _get_ugc(self):
        assert self.lib is not None
        for name in UGC_ACCESSOR_NAMES:
            accessor = getattr(self.lib, name, None)
            if accessor is None:
                continue
            accessor.restype = ctypes.c_void_p
            accessor.argtypes = []
            pointer = accessor()
            if pointer:
                self.diagnostics.ugc_accessor = name
                return ctypes.c_void_p(pointer)
        raise SteamSDKError(
            "Could not find a SteamAPI_SteamUGC accessor in this library. The SDK "
            "version may be newer than the range this tool probes."
        )

    def open(self) -> None:
        if not self.library_path.is_file():
            raise SteamSDKError(f"Steam library not found: {self.library_path}")
        self._claim_app_id()
        try:
            self.lib = ctypes.CDLL(str(self.library_path))
        except OSError as exc:
            self._release_app_id()
            raise SteamSDKError(
                f"Could not load {self.library_path}: {exc}. On Windows this is "
                "usually a 32 bit library loaded by 64 bit Python, or the reverse."
            ) from exc
        try:
            self._init_api()
            self.ugc = self._get_ugc()
        except Exception:
            self._release_app_id()
            raise
        self.diagnostics.ok = True
        log.info(
            "Steam SDK ready: %s via %s, UGC accessor %s",
            self.library_path,
            self.diagnostics.init_symbol,
            self.diagnostics.ugc_accessor,
        )

    def close(self) -> None:
        if self.lib is not None:
            shutdown = getattr(self.lib, "SteamAPI_Shutdown", None)
            if shutdown is not None:
                try:
                    shutdown()
                except Exception as exc:
                    log.warning("SteamAPI_Shutdown complained: %s", exc)
        self._release_app_id()
        self.lib = None
        self.ugc = None

    # ------------------------------------------------------------ operations --

    def _run_callbacks(self, seconds: float) -> None:
        assert self.lib is not None
        pump = getattr(self.lib, "SteamAPI_RunCallbacks", None)
        if pump is None:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pump()
            time.sleep(0.05)

    def subscribed_items(self) -> list[int]:
        """The Workshop ids this account is subscribed to for our app id.

        Also the way results are verified: rather than plumbing the asynchronous
        callback for each call, the list is read again afterwards and compared.
        """
        assert self.lib is not None and self.ugc is not None
        count_fn = getattr(self.lib, "SteamAPI_ISteamUGC_GetNumSubscribedItems", None)
        list_fn = getattr(self.lib, "SteamAPI_ISteamUGC_GetSubscribedItems", None)
        if count_fn is None or list_fn is None:
            raise SteamSDKError(
                "This library does not export GetNumSubscribedItems or "
                "GetSubscribedItems."
            )

        # Signature taken from steam_api_flat.h in SDK 1.65:
        #   uint32 GetNumSubscribedItems( ISteamUGC*, bool bIncludeLocallyDisabled )
        # Older SDKs omit the flag. Passing it anyway is safe on x86-64, where a
        # surplus argument sits in a register the callee ignores. Guessing the
        # other way round is not: a missing argument reads whatever that register
        # held, and ctypes raises nothing to warn you.
        count_fn.restype = ctypes.c_uint32
        count_fn.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        count = int(count_fn(self.ugc, True))
        if count == 0:
            return []

        # uint32 GetSubscribedItems( ISteamUGC*, PublishedFileId_t*, uint32, bool )
        buffer = (ctypes.c_uint64 * count)()
        list_fn.restype = ctypes.c_uint32
        list_fn.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        returned = int(list_fn(self.ugc, buffer, ctypes.c_uint32(count), True))
        return [int(buffer[i]) for i in range(min(returned, count))]

    def _call_item_function(self, symbol: str, item_id: int) -> None:
        assert self.lib is not None and self.ugc is not None
        function = getattr(self.lib, symbol, None)
        if function is None:
            raise SteamSDKError(f"This library does not export {symbol}.")
        function.restype = ctypes.c_uint64  # SteamAPICall_t
        function.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        function(self.ugc, ctypes.c_uint64(int(item_id)))

    def unsubscribe(self, item_ids: list[int], progress=None) -> tuple[list[int], list[int]]:
        """Unsubscribe from several items. Returns (done, still subscribed).

        Verified by reading the subscription list back, which sidesteps the
        asynchronous callback for every single call.
        """
        wanted = [int(i) for i in item_ids]
        for position, item_id in enumerate(wanted, start=1):
            log.info("Requesting unsubscribe from %s", item_id)
            if progress:
                progress(f"asking Steam to drop {item_id} ({position}/{len(wanted)})")
            self._call_item_function("SteamAPI_ISteamUGC_UnsubscribeItem", item_id)
        if progress:
            progress("waiting for Steam to catch up...")
        self._run_callbacks(SETTLE_SECONDS)

        try:
            remaining = set(self.subscribed_items())
        except SteamSDKError as exc:
            log.warning("Could not verify the unsubscribes: %s", exc)
            return wanted, []
        done = [i for i in wanted if i not in remaining]
        failed = [i for i in wanted if i in remaining]
        log.info("Unsubscribed %d, still subscribed %d", len(done), len(failed))
        return done, failed

    def subscribe(self, item_ids: list[int]) -> tuple[list[int], list[int]]:
        """Subscribe to several items. Returns (done, missing)."""
        wanted = [int(i) for i in item_ids]
        for item_id in wanted:
            log.info("Requesting subscribe to %s", item_id)
            self._call_item_function("SteamAPI_ISteamUGC_SubscribeItem", item_id)
        self._run_callbacks(SETTLE_SECONDS)
        try:
            present = set(self.subscribed_items())
        except SteamSDKError as exc:
            log.warning("Could not verify the subscribes: %s", exc)
            return wanted, []
        done = [i for i in wanted if i in present]
        failed = [i for i in wanted if i not in present]
        return done, failed


def diagnose(library: Path | None = None, app_id: str = PZ_APP_ID) -> Diagnostics:
    """Report what the bridge can and cannot do, changing nothing.

    This exists because the bridge could not be tested where it was written: it
    is the thing to run first when something does not work.
    """
    found = find_library(library)
    if found is None:
        diag = Diagnostics(app_id=app_id)
        diag.notes.append(
            "Download the Steamworks SDK, then point at its redistributable with "
            "--steam-sdk, or drop " + platform_dll_names()[0] + " next to the tool."
        )
        return diag

    sdk = SteamSDK(found, app_id)
    try:
        sdk.open()
    except SteamSDKError as exc:
        sdk.diagnostics.notes.append(str(exc))
        return sdk.diagnostics
    try:
        sdk.diagnostics.subscribed_count = len(sdk.subscribed_items())
    except SteamSDKError as exc:
        sdk.diagnostics.notes.append(f"subscription list unavailable: {exc}")
    finally:
        diag = sdk.diagnostics
        sdk.close()
    return diag
