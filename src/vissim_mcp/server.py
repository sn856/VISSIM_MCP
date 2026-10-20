"""MCP server core — tool definitions, serialization, response shape and the shutdown procedure.

Transport is the official mcp package (HTTP, fixed to 127.0.0.1). All tool calls are serialized behind a lock, and responses
always have the shape `{ok, ..., state}`. A ToolError raised by a layer becomes `{ok: false, error: {kind, message}}`.
"""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import socket
import threading
from typing import Any, Callable

import anyio

from . import __version__
from .changelog import ChangeLog, LOGGER_NAME
from .comthread import ComThread
from .config import HOST, TUNABLE_DEFAULTS
from .errors import KIND_BAD_ARGS, KIND_COM_ERROR, KIND_NETWORK_CHANGED, KIND_VISSIM_ABSENT, ToolError
from .guard import Guard
from .objects import Objects
from .results import Results
from .session import Session
from .simulation import Simulation
from .state import ServerState
from .view import View

SERVER_NAME = "vissim"
MCP_PATH = "/mcp"
ADMIN_STATUS_PATH = "/admin/status"
ADMIN_SHUTDOWN_PATH = "/admin/shutdown"
ADMIN_OPEN_PATH = "/admin/open"

# Write tools: while the changed flag is set, the first one to arrive is rejected before it reaches VISSIM
WRITE_TOOLS = ("set_attributes", "create_object", "delete_object", "configure_simulation", "configure_evaluation",
               "run_simulation", "save_network")
# Names that skip the common "current check": vissim_status (memory only), open_network and the admin paths (they check themselves)
_NO_PRECHECK = ("vissim_status", "open_network", "admin_open", "admin_status")

# Answers and kinds of the shutdown confirmation dialog. A dialog function is (kind, body, timeout in s) -> answer; tests swap it out.
QUIT_YES = "yes"           # Yes
QUIT_NO = "no"             # No
QUIT_CANCEL = "cancel"     # Cancel (keep running)
QUIT_OK = "ok"             # OK
QUIT_TIMEOUT = "timeout"   # no answer (including failure to show the dialog)
BOX_YES_NO_CANCEL = "yes_no_cancel"   # a file is open
BOX_OK_CANCEL = "ok_cancel"           # only drawn content, no open file (no path to save to, so no save button)
BOX_YES_NO = "yes_no"                 # re-confirm saving when another VISSIM is running
QUIT_TITLE = "Quit VISSIM MCP"
QUIT_TARGET_FILE = "file"
QUIT_TARGET_DRAWN = "drawn"
OTHER_VISSIM_SAVE_TEXT = "A VISSIM that is not the server's is running. Save anyway?"

INSTRUCTIONS = (
    "Resident server that drives PTV Vissim 2026. First open an .inpx with open_network, then query, change attributes, "
    "create objects, run simulations, read results and check the view (set_view, capture_view); call save_network only when "
    "the user asks to save. The state in every response holds the currently open file, the number of unsaved writes and the "
    "simulation state. Failures come back as ok=false with an error.kind (no silent fallbacks)."
)

log = logging.getLogger(LOGGER_NAME)


def port_free(port: int, host: str = HOST) -> bool:
    """Whether the port is free (tries to bind). One server per PC."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _tunable(cfg: dict[str, Any], key: str) -> Any:
    return cfg.get(key, TUNABLE_DEFAULTS[key])


class Runtime:
    """Ties the layers together (state, COM thread, log, protection, session, objects, simulation, results, view). Tests inject fake layers."""

    def __init__(self, cfg: dict[str, Any], *, state: ServerState | None = None, com: ComThread | None = None,
                 changelog: ChangeLog | None = None, guard: Guard | None = None, session: Any = None,
                 objects: Any = None, simulation: Any = None, results: Any = None, view: Any = None):
        self.cfg = cfg
        self.state = state or ServerState(int(cfg["port"]))
        self.com = com or ComThread(retry_count=int(_tunable(cfg, "com_retry_count")),
                                    retry_interval_s=float(_tunable(cfg, "com_retry_interval_s")))
        self.changelog = changelog or ChangeLog(cfg["log_root"])
        self.guard = guard or Guard(cfg["protected"])
        self.session = session or Session(cfg, self.state, self.com, self.changelog)
        self.objects = objects or Objects(cfg, self.state, self.com, self.changelog, self.guard, self.vissim_for_tools)
        self.simulation = simulation or Simulation(cfg, self.state, self.com, self.changelog, self.vissim_for_tools)
        self.results = results or Results(cfg, self.state, self.com, self.vissim_for_tools)
        self.view = view or View(cfg, self.state, self.com, self.changelog, self.vissim_for_tools)
        self.lock = threading.Lock()          # serializes all tool calls
        self._quitting = False

    def start(self) -> None:
        self.com.start()

    def vissim_for_tools(self) -> Any:
        """The VISSIM object used by the objects, simulation and results layers. no_network if no network is open."""
        self.session.require_network()
        return self.session.require_vissim()

    # ---- response shape ----
    def run_tool(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Serialized handling: current check -> changed-flag check (write tools) -> layer call -> {ok, ..., state}."""
        with self.lock:
            pre: list[str] = []
            try:
                if name not in _NO_PRECHECK:
                    pre = self._precheck(name)
                if name in WRITE_TOOLS:
                    self._refuse_if_changed()
                res = fn(*args, **kwargs) or {}
                out: dict[str, Any] = {"ok": True}
                out.update(res)
            except ToolError as e:
                e = self._maybe_lost(name, e)
                out = {"ok": False, "error": e.to_dict()}
            except Exception as e:   # noqa: BLE001 — report even what the layers did not catch, do not hide it
                log.exception("%s: unexpected error", name)
                out = {"ok": False, "error": {"kind": KIND_COM_ERROR, "message": f"unexpected error ({type(e).__name__}): {e}"}}
            if pre:
                w = out.get("warnings")
                out["warnings"] = pre + (w if isinstance(w, list) else ([] if w is None else [w]))
            out["state"] = self.state.snapshot()
            return out

    def _precheck(self, name: str) -> list[str]:
        """The current check before a tool call. If VISSIM was lost, report it once as vissim_absent, like the existing loss handling. Returns the list of warnings."""
        check = getattr(self.session, "check_current", None)
        if check is None:
            return []
        r = check(name)
        if r.get("lost") is not None:
            raise ToolError(KIND_VISSIM_ABSENT,
                            f"VISSIM has disappeared ({r.get('lost_error')}). {r['lost']} unsaved change(s) lost",
                            {"lost_writes": r["lost"]})
        return list(r.get("warnings") or [])

    def _refuse_if_changed(self) -> None:
        """If the changed flag is set, clear it and reject with network_changed (once per server, before reaching VISSIM)."""
        flag = self.state.clear_network_changed()
        if flag is None:
            return
        prev = flag["previous_path"] if flag["previous_path"] is not None else "(no file)"
        cur = flag["current_path"] if flag["current_path"] is not None else "(no file)"
        raise ToolError(KIND_NETWORK_CHANGED,
                        f"The file open in the server's VISSIM has changed: {prev} -> {cur}. "
                        "Ask the user whether writing to this file is intended, then call again.", flag)

    # ---- admin paths ----
    def admin_open(self, **kwargs: Any) -> dict[str, Any]:
        """POST /admin/open. Called from inside run_tool("admin_open", ...)."""
        return self.session.admin_open(**kwargs)

    def refresh_status(self) -> dict[str, Any]:
        """GET /admin/status?refresh=1: status after a current check, plus refreshed and refresh_note."""
        check = getattr(self.session, "check_current", None)
        r = check("admin_status") if check is not None else {"checked": False, "note": "cannot run the current check"}
        out = self.status()
        out["refreshed"] = bool(r.get("checked"))
        out["refresh_note"] = r.get("note")
        if r.get("warnings"):
            out["warnings"] = list(r["warnings"])
        return out

    def _maybe_lost(self, name: str, e: ToolError) -> ToolError:
        """If a com_error from the objects, simulation or results layer is caused by VISSIM being lost, turn it into vissim_absent (reported once on the next command)."""
        if e.kind != KIND_COM_ERROR:
            return e
        detect = getattr(self.session, "_detect_lost", None)
        if detect is None:
            return e
        try:
            lost = detect(name)
        except Exception:   # noqa: BLE001 — if the loss check itself fails, keep the original error
            return e
        if lost is None:
            return e
        return ToolError(KIND_VISSIM_ABSENT, f"VISSIM has disappeared ({e.message}). {lost} unsaved change(s) lost",
                         {"lost_writes": lost})

    def status(self) -> dict[str, Any]:
        full = self.state.full()
        return {
            "server": {"version": __version__, "started_at": full["started_at"], "port": full["port"],
                       "server_start_id": full["server_start_id"], "config_path": self.cfg.get("_path")},
            "vissim": full["vissim"],
            "network": full["network"],
            "simulation": full["simulation"],
            "other_vissim_processes": self.session.other_vissim_processes(),
        }

    # ---- shutdown procedure (shared by the tray's "quit server and VISSIM" and the stop command) ----
    def request_quit(self, confirm: Callable[[str, str, float], str] | None = None,
                     exit_fn: Callable[[int], Any] = os._exit) -> dict[str, Any]:
        """Stop the simulation first, ask if there is an open file or drawn content, save or keep a recovery copy, then close together with VISSIM.

        confirm(kind, body, timeout in s) -> a QUIT_* answer (default quit_dialog). The decision, save, recovery copy and session
        cleanup all happen at once inside the same lock as tool calls — the recovery copy (SaveNetAs) replaces the server VISSIM's
        open file with the recovery copy, so it goes right before session cleanup and no current check may slip in after it
        (otherwise the check would follow the recovery file).
        """
        if self._quitting:
            return {"quit": False, "reason": "already quitting"}
        self._quitting = True
        ask = confirm or quit_dialog
        timeout_s = float(_tunable(self.cfg, "quit_prompt_timeout_s"))
        out: dict[str, Any] = {"quit": True, "target": None, "answer": None, "saved_path": None,
                               "recovery_path": None, "save_error": None,
                               "unsaved_writes": int(self.state.snapshot()["network"]["unsaved_writes"] or 0)}
        with self.lock:
            if not self._stop_simulation_for_quit():
                log.error("could not stop the simulation — trying only a recovery copy, without a dialog, and closing")
                out["simulation_stopped"] = False
                out["recovery_path"] = self._recovery_for_quit(None)
            else:
                target, info = self._quit_target()
                out["target"] = target
                out["unsaved_writes"] = int(info.get("unsaved_writes") or 0)
                if target is not None and not self._ask_and_keep(ask, target, info, timeout_s, out):
                    self._quitting = False
                    log.info("quit cancelled (%s)", info.get("open_path") or "drawn content")
                    return {"quit": False, "reason": "cancelled", "unsaved_writes": out["unsaved_writes"]}
            log.info("quit — target %s, answer %s, saved %s, recovery copy %s", out["target"], out["answer"], out["saved_path"],
                     out["recovery_path"])
            try:   # right after the recovery copy — no current check in between
                self.session.shutdown()
            except Exception as e:   # noqa: BLE001
                log.warning("session cleanup failed during quit: %s", e)
        self.com.stop()
        logging.shutdown()
        exit_fn(0)
        return out

    def _stop_simulation_for_quit(self) -> bool:
        """Stop a running simulation and wait for it to end (at most sim_chunk_s + 30 s). True if it stopped or was not running."""
        busy = ("running", "stopping")
        if self.state.full()["simulation"]["state"] not in busy:
            return True
        try:
            if self.state.full()["simulation"]["state"] == "running":
                self.simulation.stop_simulation()
        except Exception as e:   # noqa: BLE001
            log.warning("failed to stop the simulation before quitting: %s", e)
        waiter = getattr(self.simulation, "wait", None)
        if waiter is not None:
            try:
                waiter(timeout=float(_tunable(self.cfg, "sim_chunk_s")) + 30.0)
            except Exception as e:   # noqa: BLE001
                log.warning("failed waiting for the simulation to end before quitting: %s", e)
        return self.state.full()["simulation"]["state"] not in busy

    def _quit_target(self) -> tuple[str | None, dict[str, Any]]:
        """Target of the confirmation dialog: open file -> file, drawn content (one or more links, or could not count) -> drawn, neither -> None.

        Decided from the recorded state plus a current check. If the current check cannot be done (COM failure), assume there is content. None if there is no VISSIM.
        """
        check_failed = False
        try:
            r = self.session.check_current("quit")
            check_failed = not r.get("checked") and r.get("lost") is None
            for w in r.get("warnings") or ():
                log.warning("current check before quit: %s", w)
        except Exception as e:   # noqa: BLE001
            log.warning("current check before quit failed (assuming there is content): %s", e)
            check_failed = True
        try:
            info = dict(self.session.open_info("quit"))
        except Exception as e:   # noqa: BLE001
            log.warning("open-state check before quit failed (assuming there is content): %s", e)
            net = self.state.snapshot()["network"]
            info = {"vissim": True, "open_path": net["path"], "drawn_links": None,
                    "unsaved_writes": int(net["unsaved_writes"] or 0)}
        if not info.get("vissim"):
            return None, info
        if info.get("open_path") is not None:
            return QUIT_TARGET_FILE, info
        drawn = info.get("drawn_links")
        if drawn is None or drawn > 0:
            return QUIT_TARGET_DRAWN, info
        if check_failed:
            info["drawn_links"] = None   # could not check, so treat it as uncounted (same condition for the recovery copy)
            return QUIT_TARGET_DRAWN, info
        return None, info

    def _ask_and_keep(self, ask: Callable[[str, str, float], str], target: str, info: dict[str, Any],
                      timeout_s: float, out: dict[str, Any]) -> bool:
        """Show the confirmation dialog and save or keep a recovery copy according to the answer. False on cancel (do not quit)."""
        if target == QUIT_TARGET_FILE:
            answer = ask(BOX_YES_NO_CANCEL, quit_file_text(info["open_path"], out["unsaved_writes"], timeout_s),
                         timeout_s)
        else:
            answer = ask(BOX_OK_CANCEL, quit_drawn_text(info.get("drawn_links"), timeout_s), timeout_s)
        out["answer"] = answer
        if answer == QUIT_CANCEL:
            return False
        if target == QUIT_TARGET_FILE and answer == QUIT_YES and self._save_for_quit(ask, info, timeout_s, out):
            return True
        out["recovery_path"] = self._recovery_for_quit(info)   # No, OK, timeout, not saved or save failed
        return True

    def _save_for_quit(self, ask: Callable[[str, str, float], str], info: dict[str, Any], timeout_s: float,
                       out: dict[str, Any]) -> bool:
        """Save and quit: same rules as save_network (tool="quit"). If another VISSIM exists or cannot be determined, ask once more.

        True if saved, False if not saved or failed (the caller then keeps a recovery copy).
        """
        other = self.session.other_vissim_processes()
        force = False
        if other is None or other > 0:
            second = ask(BOX_YES_NO, other_vissim_text(other, timeout_s), timeout_s)
            out["other_vissim_answer"] = second
            if second != QUIT_YES:
                log.info("not saving on quit — answer to the other-VISSIM dialog: %s", second)
                return False
            force = True
        try:
            res = self.session.save_on_quit(force=force)
        except Exception as e:   # noqa: BLE001 — COM error etc.: keep a recovery copy and close
            msg = e.message if isinstance(e, ToolError) else f"{type(e).__name__}: {e}"
            log.error("save on quit failed — keeping a recovery copy: %s", msg)
            out["save_error"] = msg
            try:
                self.changelog.append({"tool": "quit", "kind": "save", "network_path": info.get("open_path"),
                                       "ok": False, "error": msg, "force": force})
            except Exception as le:   # noqa: BLE001
                log.warning("failed to log the save-on-quit failure: %s", le)
            return False
        out["saved_path"] = res.get("saved_path")
        return True

    def _recovery_for_quit(self, info: dict[str, Any] | None) -> str | None:
        """Recovery copy (<log_root>/recovery/). On failure just log it and return None — close anyway."""
        try:
            return self.session.save_recovery_copy(info).get("recovery_path")
        except ToolError as e:
            log.error("failed to save the recovery copy: %s", e.message)
        except Exception as e:   # noqa: BLE001
            log.error("failed to save the recovery copy: %s", e)
        return None


def _timeout_note(timeout_s: float) -> str:
    return f"(If there is no answer within {int(timeout_s)} s, a recovery copy is kept and the server quits)"


def quit_file_text(open_path: str, unsaved: int, timeout_s: float) -> str:
    """Body of the confirmation dialog when a file is open."""
    return (f"Shutting down the server also closes VISSIM.\n\nOpen file: {open_path}\nUnsaved changes made through the conversation: {unsaved}\n"
            "The server cannot know about edits made in the VISSIM window.\n\n"
            "Yes = save and quit / No = quit without saving (recovery copy) / Cancel = keep running\n" + _timeout_note(timeout_s))


def quit_drawn_text(drawn_links: int | None, timeout_s: float) -> str:
    """Body of the confirmation dialog when only drawn content exists, with no open file."""
    what = f"{drawn_links} link(s)" if drawn_links is not None else "could not count the links"
    return (f"Shutting down the server also closes VISSIM. There is content drawn on screen that is not saved to a file ({what}).\n\n"
            "OK = keep a recovery copy and quit / Cancel = keep running\n" + _timeout_note(timeout_s))


def other_vissim_text(other: int | None, timeout_s: float) -> str:
    """Body of the second dialog after "save and quit" when another VISSIM exists or cannot be determined."""
    seen = f"{other} VISSIM(s) besides the server's" if other is not None else "could not check whether another VISSIM exists"
    return (f"{OTHER_VISSIM_SAVE_TEXT}\n({seen})\n\nYes = save and quit / No = quit without saving (recovery copy)\n"
            + _timeout_note(timeout_s))


# Dialog kind -> button and default-button flags (the default is the keep-running / do-not-save side), Win32 return value -> answer
_BOX_FLAGS = {BOX_YES_NO_CANCEL: 0x3 | 0x200, BOX_OK_CANCEL: 0x1 | 0x100, BOX_YES_NO: 0x4 | 0x100}
_BOX_ANSWERS = {BOX_YES_NO_CANCEL: {6: QUIT_YES, 7: QUIT_NO, 2: QUIT_CANCEL},
                BOX_OK_CANCEL: {1: QUIT_OK, 2: QUIT_CANCEL},
                BOX_YES_NO: {6: QUIT_YES, 7: QUIT_NO}}
_MB_ICONWARNING, _MB_TOPMOST, _MB_SETFOREGROUND = 0x30, 0x40000, 0x10000
_ID_TIMEOUT = 32000


def _message_box_timeout(text: str, title: str, flags: int, timeout_ms: int) -> int:
    return ctypes.windll.user32.MessageBoxTimeoutW(0, text, title, flags, 0, timeout_ms)


def quit_dialog(kind: str, text: str, timeout_s: float, *,
                box: Callable[[str, str, int, int], int] | None = None) -> str:
    """Windows message box with a timeout (MessageBoxTimeoutW). QUIT_TIMEOUT if there is no answer or the box cannot be shown."""
    flags = _BOX_FLAGS[kind] | _MB_ICONWARNING | _MB_TOPMOST | _MB_SETFOREGROUND
    try:
        res = (box or _message_box_timeout)(text, QUIT_TITLE, flags, int(timeout_s * 1000))
    except Exception as e:   # noqa: BLE001 — if the dialog cannot be shown, treat it as no answer
        log.warning("quit confirmation dialog failed: %s", e)
        return QUIT_TIMEOUT
    if res == _ID_TIMEOUT:
        return QUIT_TIMEOUT
    return _BOX_ANSWERS[kind].get(int(res), QUIT_TIMEOUT)


def parse_admin_open_body(body: Any) -> dict[str, Any]:
    """POST /admin/open body -> session.admin_open arguments. bad_args if malformed (before reaching VISSIM).

    {"path": string|null, "discard": boolean (default false), "expect_open_path": string|null (optional),
     "expect_unsaved_writes": integer (optional)} — expected values are passed only when the key is present (otherwise not compared).
    """
    if not isinstance(body, dict):
        raise ToolError(KIND_BAD_ARGS, "the body must be a JSON object: {\"path\": string|null, \"discard\": boolean}")
    path = body.get("path")
    if path is not None and (not isinstance(path, str) or not path.strip()):
        raise ToolError(KIND_BAD_ARGS, "path must be a non-empty string or null")
    discard = body.get("discard", False)
    if not isinstance(discard, bool):
        raise ToolError(KIND_BAD_ARGS, "discard must be true or false")
    kwargs: dict[str, Any] = {"path": path, "discard": discard}
    if "expect_open_path" in body:
        e = body["expect_open_path"]
        if e is not None and not isinstance(e, str):
            raise ToolError(KIND_BAD_ARGS, "expect_open_path must be a string or null")
        kwargs["expect_open_path"] = e
    if body.get("expect_unsaved_writes") is not None:
        n = body["expect_unsaved_writes"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ToolError(KIND_BAD_ARGS, "expect_unsaved_writes must be an integer >= 0")
        kwargs["expect_unsaved_writes"] = n
    return kwargs


# ---------------------------------------------------------------- tool definitions

def build_server(rt: Runtime):
    """An MCPServer with the 20 tools and three admin paths (/admin/status[?refresh=1], /admin/open, /admin/shutdown)."""
    from mcp.server.mcpserver import MCPServer
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    async def _run(name: str, fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(functools.partial(rt.run_tool, name, fn, **kwargs))

    @mcp.tool(description="Status of the server, VISSIM, the open network and the simulation, plus the number of VISSIM processes other than the server's")
    async def vissim_status() -> dict[str, Any]:
        return await _run("vissim_status", rt.status)

    @mcp.tool(description="Open a network (.inpx). Starts VISSIM if it is not running. If the server's VISSIM has an open file or "
                          "content drawn on screen (even the same file with 0 writes), it is refused with unsaved_changes "
                          "unless discard=true. Use discard=true only after asking the user and getting an answer")
    async def open_network(path: str, discard: bool = False) -> dict[str, Any]:
        return await _run("open_network", rt.session.open_network, path=path, discard=discard)

    @mcp.tool(description="Call only when the user asks to save. Without path, saves in place (.bak first); with path, saves under another name. "
                          "Refused without force=true if a VISSIM other than the server's is running")
    async def save_network(path: str | None = None, force: bool = False) -> dict[str, Any]:
        return await _run("save_network", rt.session.save_network, path=path, force=force)

    @mcp.tool(description="Registered object types, their key formats, whether they can be created, and whether they are protected")
    async def list_object_types() -> dict[str, Any]:
        return await _run("list_object_types", rt.objects.list_object_types)

    @mcp.tool(description="Attribute list of a type (ID, name, value format, editable) with protection flags. Composite-key types need parent (the parent number)")
    async def describe_attributes(type: str, parent: str | int | None = None) -> dict[str, Any]:
        return await _run("describe_attributes", rt.objects.describe_attributes, type=type, parent=parent)

    @mcp.tool(description="List of objects (no + attributes). Empty attrs means the default attributes; name_contains matches part of the name; where matches attribute values")
    async def list_objects(type: str, attrs: list[str] | None = None, name_contains: str | None = None,
                           where: dict[str, Any] | None = None, limit: int | None = None, offset: int = 0,
                           parent: str | int | None = None) -> dict[str, Any]:
        return await _run("list_objects", rt.objects.list_objects, type=type, attrs=attrs, name_contains=name_contains,
                          where=where, limit=limit, offset=offset, parent=parent)

    @mcp.tool(description="Attribute dictionary of one object. no is a number or a composite key ('link-lane'); null for a single-instance object")
    async def get_attributes(type: str, no: str | int | None = None, attrs: list[str] | None = None) -> dict[str, Any]:
        return await _run("get_attributes", rt.objects.get_attributes, type=type, no=no, attrs=attrs)

    @mcp.tool(description="Apply a list of attribute changes in order (before/after per item). attr is the VISSIM attribute ID as is (e.g. Volume(1)). "
                          "A protected attribute rejects only that item; there is no rollback")
    async def set_attributes(changes: list[dict[str, Any]], stop_on_error: bool = True) -> dict[str, Any]:
        return await _run("set_attributes", rt.objects.set_attributes, changes=changes, stop_on_error=stop_on_error)

    @mcp.tool(description="Create an object (following the registry signature). params depend on the type (e.g. VehicleInput: link, volume, veh_comp, name; "
                          "ParkingLot: link, lane, pos, length; VehicleRouteParking: decision, parking_lot). "
                          "If no is omitted, max + 1 is used. attrs are attributes to set after creation")
    async def create_object(type: str, params: dict[str, Any] | None = None, no: str | int | None = None,
                            attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _run("create_object", rt.objects.create_object, type=type, params=params, no=no, attrs=attrs)

    @mcp.tool(description="Delete an object. Returns an attribute snapshot taken before deletion (so it can be recreated)")
    async def delete_object(type: str, no: str | int | None = None) -> dict[str, Any]:
        return await _run("delete_object", rt.objects.delete_object, type=type, no=no)

    @mcp.tool(description="Simulation settings (period, resolution, seed, number of runs, seed increment, start time). Changes only what is given and returns the before/after values")
    async def configure_simulation(period_s: float | None = None, resolution: int | None = None,
                                   random_seed: int | None = None, num_runs: int | None = None,
                                   seed_increment: int | None = None, start_time: float | None = None) -> dict[str, Any]:
        return await _run("configure_simulation", rt.simulation.configure_simulation, period_s=period_s,
                          resolution=resolution, random_seed=random_seed, num_runs=num_runs,
                          seed_increment=seed_increment, start_time=start_time)

    @mcp.tool(description="Evaluation settings. Each group takes {collect, write_file, interval_s, from_s, to_s}. "
                          "get_results needs collect to be on, and read_result_file needs write_file to be on. "
                          "Parking groups: parking_lots, parking_lot_groups, parking_decisions, parking_spaces")
    async def configure_evaluation(data_collection: dict[str, Any] | None = None,
                                   travel_times: dict[str, Any] | None = None,
                                   queues: dict[str, Any] | None = None,
                                   network_performance: dict[str, Any] | None = None,
                                   parking_lots: dict[str, Any] | None = None,
                                   parking_lot_groups: dict[str, Any] | None = None,
                                   parking_decisions: dict[str, Any] | None = None,
                                   parking_spaces: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _run("configure_evaluation", rt.simulation.configure_evaluation, data_collection=data_collection,
                          travel_times=travel_times, queues=queues, network_performance=network_performance,
                          parking_lots=parking_lots, parking_lot_groups=parking_lot_groups,
                          parking_decisions=parking_decisions, parking_spaces=parking_spaces)

    @mcp.tool(description="Start the simulation in the background (run in chunks, quick mode by default). Progress is in the simulation field of vissim_status")
    async def run_simulation(quick_mode: bool | None = None, num_runs: int | None = None) -> dict[str, Any]:
        return await _run("run_simulation", rt.simulation.run_simulation, quick_mode=quick_mode, num_runs=num_runs)

    @mcp.tool(description="Stop the running simulation at the next chunk boundary")
    async def stop_simulation() -> dict[str, Any]:
        return await _run("stop_simulation", rt.simulation.stop_simulation)

    @mcp.tool(description="Result table. kind is one of data_collection, travel_time, queue, network_performance, vehicle_input, "
                          "parking_lot, parking_lot_group, parking_decision; "
                          "run = Current|Avg|StdDev|Min|Max|number, interval = all|Last|Avg|Total|Min|Max|number, veh_class = All|number")
    async def get_results(kind: str, nos: list[int] | None = None, run: str | int = "Current",
                          interval: str | int = "all", veh_class: str | int = "All") -> dict[str, Any]:
        return await _run("get_results", rt.results.get_results, kind=kind, nos=nos, run=run, interval=interval,
                          veh_class=veh_class)

    @mcp.tool(description="List of .att result files in the <name>.results folder next to the network (newest first)")
    async def list_result_files() -> dict[str, Any]:
        return await _run("list_result_files", rt.results.list_result_files)

    @mcp.tool(description="Read an .att result file as a table (columns, rows). where is a column=value filter")
    async def read_result_file(name: str, max_rows: int = 1000, where: dict[str, Any] | None = None) -> dict[str, Any]:
        return await _run("read_result_file", rt.results.read_result_file, name=name, max_rows=max_rows, where=where)

    @mcp.tool(description="View settings (network and file unchanged). layout='view' loads a copy of the layout (runs/mcp/layouts) with background "
                          "images and 3D models on and the online map off; 'network' loads the original .layx next to the network. mode=2d|3d, "
                          "zoom=[x0,y0,x1,y1] (2D), camera={x,y,z,yaw,pitch} (3D, network coordinates in m, angles in degrees). Display settings "
                          "loaded this way are also saved to the .layx if save_network is called afterwards")
    async def set_view(layout: str | None = None, mode: str | None = None, zoom: list[float] | None = None,
                       camera: dict[str, float] | None = None) -> dict[str, Any]:
        return await _run("set_view", rt.view.set_view, layout=layout, mode=mode, zoom=zoom, camera=camera)

    @mcp.tool(description="Capture the view as a PNG (runs/mcp/captures). mode=network captures only the network editor picture (COM Screenshot, for reports); "
                          "window captures the whole server VISSIM window as it appears on screen (for development checks; works even if covered by other windows). "
                          "Waits settle_s for drawing to finish before capturing. Returns the path")
    async def capture_view(mode: str = "network", label: str | None = None, scale: float = 1.0,
                           settle_s: float | None = None) -> dict[str, Any]:
        return await _run("capture_view", rt.view.capture_view, mode=mode, label=label, scale=scale, settle_s=settle_s)

    @mcp.tool(description="This server's change log (open, save, attribute change, create, delete, run, recovery). since is ISO8601")
    async def get_change_log(since: str | None = None, limit: int = 200) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            return {"entries": rt.changelog.read(since=since, limit=limit), "path": str(rt.changelog.path)}
        return await _run("get_change_log", _read)

    @mcp.custom_route(ADMIN_STATUS_PATH, methods=["GET"], include_in_schema=False)
    async def admin_status(request: Request) -> JSONResponse:
        if str(request.query_params.get("refresh", "")).lower() in ("1", "true", "yes"):
            return JSONResponse(await _run("admin_status", rt.refresh_status))
        return JSONResponse(await _run("vissim_status", rt.status))

    @mcp.custom_route(ADMIN_OPEN_PATH, methods=["POST"], include_in_schema=False)
    async def admin_open(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:   # noqa: BLE001 — a body that is not JSON becomes an argument error
            body = None
        try:
            kwargs = parse_admin_open_body(body)
        except ToolError as e:
            def _refuse(err: ToolError = e) -> dict[str, Any]:
                raise err
            return JSONResponse(await _run("admin_open", _refuse))
        return JSONResponse(await _run("admin_open", rt.admin_open, **kwargs))

    @mcp.custom_route(ADMIN_SHUTDOWN_PATH, methods=["POST"], include_in_schema=False)
    async def admin_shutdown(request: Request) -> JSONResponse:
        threading.Thread(target=rt.request_quit, name="vissim-mcp-quit", daemon=True).start()
        return JSONResponse({"accepted": True, "note": "A confirmation dialog appears if a file is open or content has been drawn on screen"})

    return mcp


TOOL_NAMES = (
    "vissim_status", "open_network", "save_network", "list_object_types", "describe_attributes", "list_objects",
    "get_attributes", "set_attributes", "create_object", "delete_object", "configure_simulation",
    "configure_evaluation", "run_simulation", "stop_simulation", "get_results", "list_result_files",
    "read_result_file", "get_change_log", "set_view", "capture_view",
)


def serve_http(mcp, port: int, ready: threading.Event | None = None) -> None:
    """Run the HTTP transport to the end on the current thread (blocking). Fixed to 127.0.0.1."""
    import asyncio

    async def _main() -> None:
        if ready is not None:
            ready.set()
        await mcp.run_streamable_http_async(host=HOST, port=int(port), streamable_http_path=MCP_PATH)

    asyncio.run(_main())
