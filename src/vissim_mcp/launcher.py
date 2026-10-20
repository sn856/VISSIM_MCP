"""Launcher: the side that `.inpx` double-clicks and the desktop shortcut call.

`run_open(cfg, path)` is the single entry point (wired to the command line as `open` in `__main__`). Order:
1. Only one at a time, using a named mutex (`Local\\vissim_mcp_launcher`). If it is already held, show a notice and finish.
2. Check the path (an existing file with extension `.inpx`). No path (None) means start an empty VISSIM.
3. An "Opening" window (always on top, no close button). It is closed when moving on to a confirmation or notice window and shown again if the flow continues.
4. Find the server (`GET /admin/status`). If there is none, start `python -m vissim_mcp --serve` without a console
   and wait up to `launcher_server_wait_s` seconds.
5. If a VISSIM that is not the server's is running (`other_vissim_processes` >= 1, or null when it cannot be determined), show a notice (OK only) and continue.
6. `POST /admin/open` (time limit `launcher_open_timeout_s`) and handle the reply as follows.
   - opened, front, launched: bring the server VISSIM window (pid) to the front. A failure here is not an error (a warning goes to the log).
   - unsaved_changes: a confirmation window [Discard and open][Cancel]. On discard, the values shown in the window are sent back as `expect_*`
     in a second request; if the server answers that it changed in the meantime (unsaved_changes again), the window is shown again with the new values.
   - sim_running: a notice.
   - cannot start at all (server wait exceeded, server start failed, `vissim_absent` + `launch_failed`): a failure window
     [Open with plain VISSIM] / [Start plain VISSIM] [Close].
   - anything else (open timeout, com_error, not_found, connection lost, server busy, ...): a failure window [Retry] [Cancel].
     Retry starts again from finding the server (if the server has exited in the meantime, it is started again).
7. Append one line (JSON) to `<log_root>/launcher.log`. Write failures are ignored. This is the only file the launcher writes.

Exit code: 0 opened / brought to front / the user cancelled in the confirmation window; 1 failure (including when a notice or failure window was shown).
The configuration error (2) is decided by the caller (`__main__`) when it reads the config.

All side effects (mutex, HTTP, process start, windows, bringing a window to the front, clock, log writing) can be replaced through `LauncherDeps`.
Tests never touch real windows, processes or the network. HTTP is fixed to 127.0.0.1 (port from the config), standard library only.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .config import HOST, TUNABLE_DEFAULTS, repo_root
from .server import ADMIN_OPEN_PATH, ADMIN_STATUS_PATH, port_free

__all__ = ["run_open", "LauncherDeps", "default_deps", "server_command", "classify_status_error",
           "append_log_line", "bring_window_to_front", "ADMIN_OPEN_PATH", "ADMIN_STATUS_PATH", "repo_root"]

MUTEX_NAME = "Local\\vissim_mcp_launcher"
LOG_NAME = "launcher.log"
STATUS_TIMEOUT_S = 10.0       # time limit for one GET /admin/status (the reply is slow while the server handles a long tool)
POLL_S = 0.5                  # interval for asking the status again after starting the server
FRONT_WAIT_S = 5.0            # maximum wait for the window of a just-started VISSIM to appear

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

EXIT_OK = 0
EXIT_FAIL = 1

# ---- on-screen text ----
TITLE = "VISSIM MCP"
MSG_ONE_AT_A_TIME = "Another file is being opened. Only one can be opened at a time."
MSG_OTHER_VISSIM = ("A VISSIM that is not the server's is running. Editing the same file on both sides can overwrite each other's changes, and "
                    "saving from the chat asks for one more confirmation.")
MSG_SIM_RUNNING = "A simulation is running, so the file cannot be opened. Try again after it finishes or is stopped."
CONFIRM_TITLE = "VISSIM MCP - Another file is open"
CONFIRM_DRAWN = "No file - there is content drawn on the VISSIM screen"
CONFIRM_SCREEN_WARNING = "Anything edited on the VISSIM screen and not saved will also be lost."
FAIL_TITLE_OPEN = "VISSIM MCP - Could not open"
FAIL_TITLE_LAUNCH = "VISSIM MCP - Could not start"
PROGRESS_LAUNCH = "Starting VISSIM..."

BTN_OK = "OK"
BTN_DISCARD = "Discard and open"
BTN_CANCEL = "Cancel"
BTN_PLAIN_OPEN = "Open with plain VISSIM"
BTN_PLAIN_LAUNCH = "Start plain VISSIM"
BTN_CLOSE = "Close"
BTN_RETRY = "Retry"


# ---------------------------------------------------------------- substitution points

class LauncherUI(Protocol):
    def show_progress(self, text: str) -> None: ...
    def hide_progress(self) -> None: ...
    def notice(self, title: str, body: str) -> None: ...
    def choose(self, title: str, body: str, buttons: Sequence[str], default: int) -> int | None: ...
    def close(self) -> None: ...


@dataclass
class LauncherDeps:
    """All side effects of the launcher. Tests inject fakes (`default_deps` returns the real ones)."""
    ui: LauncherUI
    acquire_mutex: Callable[[], Any]                    # a handle (truthy) if acquired, None if already held
    release_mutex: Callable[[Any], None]
    http_get: Callable[[str, float], Any]               # (url, timeout) -> JSON. Failure raises
    http_post: Callable[[str, dict, float], Any]        # (url, body, timeout) -> JSON. Failure raises
    port_free: Callable[[int], bool]
    spawn_server: Callable[[list, str], Any]            # (cmd, cwd) -> a process with poll()
    launch_plain: Callable[[list], None]                # plain VISSIM (no COM)
    bring_to_front: Callable[[int], bool]               # server VISSIM pid -> whether it was brought to the front
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    write_log: Callable[[Path, str], None] = None       # type: ignore[assignment]  # default append_log_line
    now: Callable[[], str] = field(default=lambda: datetime.now().isoformat(timespec="seconds"))
    pid: int = field(default_factory=os.getpid)
    python_exe: str = sys.executable

    def __post_init__(self) -> None:
        if self.write_log is None:
            self.write_log = append_log_line


# ---------------------------------------------------------------- small helpers (public)

def server_command(cfg: dict, python_exe: str = sys.executable) -> list[str]:
    """Same approach as `cmd_start`: pythonw.exe next to the Python (or that Python itself) running `-m vissim_mcp --serve [--config]`."""
    exe = Path(python_exe)
    pythonw = exe.with_name("pythonw.exe")
    cmd = [str(pythonw if pythonw.is_file() else exe), "-m", "vissim_mcp", "--serve"]
    if cfg.get("_path"):
        cmd += ["--config", str(cfg["_path"])]
    return cmd


def classify_status_error(exc: BaseException) -> str:
    """HTTP failure -> "absent" (nobody answered) | "busy" (answered but not within the time limit) | "foreign" (a reply from something other than our server)."""
    if isinstance(exc, urllib.error.HTTPError):
        return "foreign"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            return "busy"
        return "absent"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "busy"
    if isinstance(exc, ValueError):          # not JSON (includes JSONDecodeError and UnicodeDecodeError)
        return "foreign"
    return "absent"


def append_log_line(path: Path, line: str) -> None:
    """Append one line (create the folder if missing). The caller ignores failures."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _seconds(v: float) -> str:
    return f"{float(v):g}"


# ---------------------------------------------------------------- decisions

class _Outcome:
    """Result of finding the server: up(status) / never(reason): cannot start at all / other(reason): anything else."""

    def __init__(self, kind: str, status: dict | None = None, reason: str = ""):
        self.kind, self.status, self.reason = kind, status, reason


class _Run:
    def __init__(self, cfg: dict, path: str | None, deps: LauncherDeps):
        self.cfg, self.deps, self.ui = cfg, deps, deps.ui
        self.raw_path = path
        self.req_path = os.path.abspath(path) if path is not None else None
        self.port = int(cfg["port"])
        self.answers: list[str] = []
        self.choices: list[str] = []
        self.warnings: list[str] = []
        self.vissim_pid: int | None = None
        self.progress_text = PROGRESS_LAUNCH
        self.target: Path | None = None

    # ---- outer frame: mutex, cleanup, log ----
    def execute(self) -> int:
        handle = None
        code = EXIT_FAIL
        try:
            handle = self.deps.acquire_mutex()
            if handle is None:
                self.answers.append("mutex_held")
                self.ui.notice(TITLE, MSG_ONE_AT_A_TIME)
            else:
                code = self._after_mutex()
        except Exception as e:   # noqa: BLE001 — the launcher never dies silently, whatever happens; it leaves a record
            self.warnings.append(f"Unexpected error ({type(e).__name__}): {e}")
            code = EXIT_FAIL
        finally:
            try:
                self.ui.close()
            except Exception:   # noqa: BLE001
                pass
            if handle is not None:
                try:
                    self.deps.release_mutex(handle)
                except Exception:   # noqa: BLE001
                    pass
            self._write_log(code)
        return code

    def _after_mutex(self) -> int:
        if self.raw_path is not None:
            problem = self._check_path()
            if problem:
                self.answers.append("bad_path")
                self.ui.notice(TITLE, problem)
                return EXIT_FAIL
            self.progress_text = f"Opening {self.target.name}..."
        return self._flow()

    def _check_path(self) -> str | None:
        p = Path(self.req_path)
        if not p.is_file():
            return f"File not found.\n{p}"
        if p.suffix.lower() != ".inpx":
            return f"Not a VISSIM network file (.inpx).\n{p}"
        self.target = p
        return None

    # ---- main flow ----
    def _flow(self) -> int:
        self.ui.show_progress(self.progress_text)
        other_noticed = False
        first = {"path": str(self.target), "discard": False} if self.target is not None else {"path": None}
        while True:                                     # [Retry] starts again from finding the server
            srv = self._ensure_server()
            if srv.kind == "never":
                return self._never_came_up(srv.reason)
            if srv.kind == "other":
                if self._other_failure(srv.reason):
                    continue
                return EXIT_FAIL
            if not other_noticed:
                other_noticed = True
                self._other_vissim_notice(srv.status or {})
            reason = None
            req = dict(first)
            while reason is None:                        # confirmation window -> request again
                kind, res, why = self._post(req)
                if kind == "fail":
                    reason = why
                    break
                if res.get("ok") is True:
                    return self._success(res)
                err = res.get("error") if isinstance(res.get("error"), dict) else {}
                ekind = str(err.get("kind") or "unknown")
                message = str(err.get("message") or ekind)
                detail = err.get("detail") if isinstance(err.get("detail"), dict) else {}
                self.answers.append(ekind)
                if ekind == "unsaved_changes" and self.target is not None:
                    nxt = self._confirm_discard(detail)
                    if nxt is None:
                        return EXIT_OK                   # the user cancelled
                    req = nxt
                    self.ui.show_progress(self.progress_text)
                    continue
                if ekind == "sim_running":
                    self.ui.hide_progress()
                    self.ui.notice(TITLE, MSG_SIM_RUNNING)
                    return EXIT_FAIL
                if ekind == "vissim_absent" and detail.get("launch_failed"):
                    return self._never_came_up(message)
                reason = message
            if not self._other_failure(reason):
                return EXIT_FAIL

    # ---- finding and starting the server ----
    def _url(self, path: str) -> str:
        return f"http://{HOST}:{self.port}{path}"

    def _probe(self) -> tuple[str, Any]:
        try:
            st = self.deps.http_get(self._url(ADMIN_STATUS_PATH), STATUS_TIMEOUT_S)
        except Exception as e:   # noqa: BLE001
            return classify_status_error(e), f"{type(e).__name__}: {e}"
        if isinstance(st, dict) and isinstance(st.get("server"), dict):
            return "up", st
        return "foreign", "Not a reply from the VISSIM MCP server"

    def _ensure_server(self) -> _Outcome:
        # If the port is free there is no server: start it without asking (on Windows a refused connection to a closed port takes about 2 seconds).
        # After starting, the port is not checked again (the check's bind could collide with the bind of the server that is just coming up).
        if not self.deps.port_free(self.port):
            kind, info = self._probe()
            if kind == "up":
                return _Outcome("up", status=info)
            if kind == "busy":
                self.answers.append("server_busy")
                return _Outcome("other", reason=(f"The server did not answer within {_seconds(STATUS_TIMEOUT_S)} seconds. "
                                                 "It may be busy with other work (such as a simulation)."))
            if kind == "foreign":
                self.answers.append("foreign_server")
                return _Outcome("never",
                                reason=f"A program other than the VISSIM MCP server is answering on port {self.port} ({info}).")
            self.answers.append("port_in_use")
            return _Outcome("never", reason=f"Another program is using port {self.port}, so the server could not be started.")
        try:
            proc = self.deps.spawn_server(server_command(self.cfg, self.deps.python_exe), str(repo_root()))
        except Exception as e:   # noqa: BLE001
            self.answers.append("server_start_failed")
            return _Outcome("never", reason=f"Could not start the server: {e}")
        wait_s = float(self.cfg.get("launcher_server_wait_s", TUNABLE_DEFAULTS["launcher_server_wait_s"]))
        start = self.deps.clock()
        while True:
            self.deps.sleep(POLL_S)
            kind, info = self._probe()
            if kind == "up":
                return _Outcome("up", status=info)
            code = _poll(proc)
            if code is not None:
                self.answers.append("server_start_failed")
                log = Path(str(self.cfg.get("log_root", ""))) / "server.log"
                return _Outcome("never", reason=(f"The server exited right after starting (exit code {code}). Port {self.port} may be "
                                                 f"in use already, or there may be a problem with the config. Log: {log}"))
            if self.deps.clock() - start >= wait_s:
                self.answers.append("server_timeout")
                return _Outcome("never", reason=f"The server was started but did not answer within {_seconds(wait_s)} seconds.")

    def _other_vissim_notice(self, status: dict) -> None:
        other = status.get("other_vissim_processes")
        known = isinstance(other, int) and not isinstance(other, bool)
        if known and other < 1:
            return
        self.ui.hide_progress()
        self.ui.notice(TITLE, MSG_OTHER_VISSIM)
        self.ui.show_progress(self.progress_text)

    # ---- open request ----
    def _post(self, body: dict) -> tuple[str, dict, str]:
        timeout = float(self.cfg.get("launcher_open_timeout_s", TUNABLE_DEFAULTS["launcher_open_timeout_s"]))
        try:
            res = self.deps.http_post(self._url(ADMIN_OPEN_PATH), body, timeout)
        except Exception as e:   # noqa: BLE001
            if classify_status_error(e) == "busy":
                self.answers.append("open_timeout")
                return "fail", {}, (f"Opening did not finish within {_seconds(timeout)} seconds. "
                                    "The server may be busy with other work, or the file may be large.")
            self.answers.append("connection_error")
            return "fail", {}, f"The connection to the server was lost ({type(e).__name__}: {e})."
        if not isinstance(res, dict) or "ok" not in res:
            self.answers.append("bad_response")
            return "fail", {}, "The server's reply has an unexpected shape."
        return "answer", res, ""

    def _success(self, res: dict) -> int:
        action = str(res.get("action") or "ok")
        self.answers.append(action)
        for w in res.get("warnings") or []:
            self.warnings.append(str(w))
        pid = res.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool):
            self.vissim_pid = pid
            try:
                ok = bool(self.deps.bring_to_front(pid))
                why = ""
            except Exception as e:   # noqa: BLE001
                ok, why = False, f": {type(e).__name__}: {e}"
            if not ok:
                self.warnings.append(f"Could not bring the VISSIM window to the front (pid {pid}){why}")
        else:
            self.warnings.append("Could not bring the VISSIM window to the front (the server did not give a VISSIM pid)")
        self.ui.hide_progress()
        return EXIT_OK

    # ---- windows ----
    def _confirm_discard(self, detail: dict) -> dict | None:
        """Confirmation window. Returns the body to resend on discard-and-open, None on cancel."""
        open_path = detail.get("open_path")
        n = detail.get("unsaved_writes")
        n_ok = isinstance(n, int) and not isinstance(n, bool)
        body = "\n".join([
            f"File open now: {open_path if open_path else CONFIRM_DRAWN}",
            f"File to open: {self.target}",
            f"Unsaved changes made from the chat: {n if n_ok else 'unknown'}",
            "",
            CONFIRM_SCREEN_WARNING,
        ])
        self.ui.hide_progress()
        idx = self.ui.choose(CONFIRM_TITLE, body, [BTN_DISCARD, BTN_CANCEL], 1)
        if idx != 0:
            self.choices.append(BTN_CANCEL)
            return None
        self.choices.append(BTN_DISCARD)
        return {"path": str(self.target), "discard": True, "expect_open_path": open_path if open_path else None,
                "expect_unsaved_writes": n if n_ok else None}

    def _never_came_up(self, reason: str) -> int:
        """Cannot start at all -> [Open with|Start plain VISSIM] [Close]."""
        self.ui.hide_progress()
        if self.target is not None:
            title, plain = FAIL_TITLE_OPEN, BTN_PLAIN_OPEN
            head = "Could not open the file through the VISSIM MCP server."
            tail = (f"Pressing [{BTN_PLAIN_OPEN}] opens this file in VISSIM without the server. "
                    "That window cannot be controlled from the chat.")
            cmd = [self._exe(), str(self.target)]
        else:
            title, plain = FAIL_TITLE_LAUNCH, BTN_PLAIN_LAUNCH
            head = "Could not start VISSIM through the VISSIM MCP server."
            tail = f"Pressing [{BTN_PLAIN_LAUNCH}] starts VISSIM without the server. That window cannot be controlled from the chat."
            cmd = [self._exe()]
        idx = self.ui.choose(title, f"{head}\n\nReason: {reason}\n\n{tail}", [plain, BTN_CLOSE], 1)
        if idx != 0:
            self.choices.append(BTN_CLOSE)
            return EXIT_FAIL
        self.choices.append(plain)
        try:
            self.deps.launch_plain(cmd)
        except Exception as e:   # noqa: BLE001
            self.warnings.append(f"Could not start plain VISSIM: {e}")
            self.ui.notice(TITLE, f"Could not start plain VISSIM.\n{cmd[0]}\n{e}")
        return EXIT_FAIL

    def _other_failure(self, reason: str) -> bool:
        """Any other failure -> [Retry] [Cancel]. True on retry (the "Opening" window is shown again)."""
        self.ui.hide_progress()
        title = FAIL_TITLE_OPEN if self.target is not None else FAIL_TITLE_LAUNCH
        body = f"{reason}\n\nPressing [{BTN_RETRY}] checks the server again from the start."
        idx = self.ui.choose(title, body, [BTN_RETRY, BTN_CANCEL], 0)
        if idx == 0:
            self.choices.append(BTN_RETRY)
            self.ui.show_progress(self.progress_text)
            return True
        self.choices.append(BTN_CANCEL)
        return False

    def _exe(self) -> str:
        v = self.cfg.get("vissim")
        return str(v.get("exe")) if isinstance(v, dict) and v.get("exe") else "Vissim260.exe"

    # ---- log ----
    def _write_log(self, code: int) -> None:
        try:
            rec = {"time": self.deps.now(), "pid": self.deps.pid, "path": self.req_path, "answers": self.answers,
                   "choices": self.choices, "warnings": self.warnings, "vissim_pid": self.vissim_pid, "exit": code}
            line = json.dumps(rec, ensure_ascii=False)
            self.deps.write_log(Path(str(self.cfg["log_root"])) / LOG_NAME, line)
        except Exception:   # noqa: BLE001 — a log failure is ignored
            pass


def _poll(proc: Any) -> int | None:
    try:
        return proc.poll()
    except Exception:   # noqa: BLE001
        return None


def run_open(cfg: dict, path: str | None, *, deps: LauncherDeps | None = None) -> int:
    """Launcher entry point. path=None means an empty VISSIM (desktop shortcut). Exit code 0/1."""
    if deps is None:
        deps = default_deps(cfg)
    return _Run(cfg, path, deps).execute()


# ================================================================ real implementation (tests substitute it)

def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1: no proxy


def http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, method="GET")
    with _opener().open(req, timeout=timeout) as r:   # noqa: S310 — fixed to 127.0.0.1
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url: str, body: dict, timeout: float) -> Any:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with _opener().open(req, timeout=timeout) as r:   # noqa: S310 — fixed to 127.0.0.1
        return json.loads(r.read().decode("utf-8"))


def spawn_detached(cmd: list, cwd: str | None = None) -> subprocess.Popen:
    return subprocess.Popen([str(c) for c in cmd], cwd=cwd, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)


def launch_plain_vissim(cmd: list) -> None:
    exe = Path(str(cmd[0]))
    spawn_detached(cmd, str(exe.parent) if exe.parent.is_dir() else None)


class _NoHandle:
    """When the mutex could not be created (rare): do not block, proceed."""

    def __bool__(self) -> bool:
        return True


def acquire_launcher_mutex() -> Any:
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    h = k.CreateMutexW(None, False, MUTEX_NAME)
    last = ctypes.get_last_error()
    if not h:
        return _NoHandle()
    if last == 183:   # ERROR_ALREADY_EXISTS
        k.CloseHandle(wintypes.HANDLE(h))
        return None
    return h


def release_launcher_mutex(handle: Any) -> None:
    if isinstance(handle, _NoHandle) or not handle:
        return
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle(handle)


# ---- bring a window to the front (ctypes Win32) ----

def _find_main_window(u: Any, pid: int) -> int | None:
    from ctypes import wintypes
    found: list[tuple[int, int]] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4):   # GW_OWNER: exclude owned windows (dialogs etc.)
            return True
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or u.GetWindowTextLengthW(hwnd) <= 0:
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    u.EnumWindows(proc(cb), 0)
    if not found:
        return None
    return max(found)[1]


def bring_window_to_front(pid: int, *, wait_s: float = FRONT_WAIT_S, clock: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep) -> bool:
    """Bring the visible top-level window (the largest) of pid to the front. Restore it if minimized. Wait up to wait_s for a just-started window."""
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetForegroundWindow.restype = wintypes.HWND
    for name in ("IsWindowVisible", "GetWindowTextLengthW", "IsIconic", "SetForegroundWindow", "BringWindowToTop"):
        getattr(u, name).argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    deadline = clock() + wait_s
    while True:
        hwnd = _find_main_window(u, int(pid))
        if hwnd:
            break
        if clock() >= deadline:
            return False
        sleep(0.25)
    u.ShowWindow(hwnd, 9 if u.IsIconic(hwnd) else 5)   # SW_RESTORE / SW_SHOW
    if u.SetForegroundWindow(hwnd):
        return True
    # Attach briefly to the input thread of the foreground window to get permission (for when another process is in front)
    fg = u.GetForegroundWindow()
    fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    me = k.GetCurrentThreadId()
    attached = bool(fg_tid and fg_tid != me and u.AttachThreadInput(me, fg_tid, True))
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u.AttachThreadInput(me, fg_tid, False)
    return u.GetForegroundWindow() == hwnd


# ---- windows (tkinter, one UI thread) ----

class TkUI:
    """The "Opening" window and the confirmation, notice and failure windows. Tk is touched only from one dedicated thread (requests go through a queue).

    The window icon is `icons.launcher_icon` (in memory), or the default Tk icon if that cannot be made. If Tk is unavailable, MessageBoxW is used instead.
    """

    def __init__(self, exe_path: str | None):
        self.exe_path = exe_path
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failed = False
        self._root = None
        self._progress = None
        self._icons: list = []

    # public (called from the main thread)
    def show_progress(self, text: str) -> None:
        self._call(self._show_progress, text)

    def hide_progress(self) -> None:
        if self._thread is not None:
            self._call(self._hide_progress)

    def notice(self, title: str, body: str) -> None:
        self.choose(title, body, [BTN_OK], 0)

    def choose(self, title: str, body: str, buttons: Sequence[str], default: int) -> int | None:
        if not self._start():
            return _messagebox_choose(title, body, list(buttons), default)
        done = threading.Event()
        box: dict[str, Any] = {}
        self._q.put((self._dialog, (title, body, list(buttons), default, box, done)))
        done.wait()
        return box.get("index")

    def close(self) -> None:
        if self._thread is None or self._failed:
            return
        self._q.put(None)
        self._thread.join(timeout=5)

    # internal
    def _call(self, fn: Callable, *args: Any) -> None:
        if self._start():
            done = threading.Event()
            self._q.put((self._wrap, (fn, args, done)))
            done.wait(timeout=5)

    @staticmethod
    def _wrap(fn: Callable, args: tuple, done: threading.Event) -> None:
        try:
            fn(*args)
        finally:
            done.set()

    def _start(self) -> bool:
        if self._thread is None:
            self._thread = threading.Thread(target=self._main, name="vissim-mcp-launcher-ui", daemon=True)
            self._thread.start()
            self._ready.wait(timeout=10)
        return not self._failed

    def _main(self) -> None:
        try:
            import tkinter as tk
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:   # noqa: BLE001
                pass
            self._tk = tk
            self._root = tk.Tk()
            self._root.withdraw()
            self._set_icon()
        except Exception:   # noqa: BLE001
            self._failed = True
            self._ready.set()
            return
        self._ready.set()
        self._root.after(30, self._pump)
        self._root.mainloop()
        # Tk objects are deleted on this thread (if another thread deletes them, the process crashes through Tcl_AsyncDelete).
        self._icons = []
        self._label = None
        self._progress = None
        try:
            self._root.destroy()
        except Exception:   # noqa: BLE001
            pass
        self._root = None
        import gc
        gc.collect()

    def _set_icon(self) -> None:
        try:
            from PIL import ImageTk

            from .icons import launcher_icon
            imgs = [launcher_icon(self.exe_path, s) for s in (256, 32)] if self.exe_path else []
            if imgs and all(i is not None for i in imgs):
                self._icons = [ImageTk.PhotoImage(i, master=self._root) for i in imgs]
                self._root.iconphoto(True, *self._icons)
        except Exception:   # noqa: BLE001 — default icon
            self._icons = []

    def _pump(self) -> None:
        try:
            while True:
                item = self._q.get_nowait()
                if item is None:
                    self._hide_progress()
                    self._root.quit()
                    return
                fn, args = item
                try:
                    fn(*args)
                except Exception:   # noqa: BLE001
                    if fn is self._dialog:
                        args[-1].set()
        except queue.Empty:
            pass
        self._root.after(30, self._pump)

    def _place(self, win) -> None:
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    @staticmethod
    def _drop_close_button(win) -> None:
        try:
            from ctypes import wintypes
            u = ctypes.WinDLL("user32")
            u.GetParent.argtypes = [wintypes.HWND]
            u.GetParent.restype = wintypes.HWND
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
            style = u.GetWindowLongW(hwnd, -16)                 # GWL_STYLE
            u.SetWindowLongW(hwnd, -16, style & ~0x00080000)    # WS_SYSMENU off -> no close button
            u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)   # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
        except Exception:   # noqa: BLE001
            pass

    def _show_progress(self, text: str) -> None:
        tk = self._tk
        if self._progress is not None:
            self._label.configure(text=text)
            return
        win = tk.Toplevel(self._root)
        win.title(TITLE)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        self._label = tk.Label(win, text=text, padx=28, pady=18, font=("Malgun Gothic", 11))
        self._label.pack()
        self._place(win)
        win.update_idletasks()
        self._drop_close_button(win)
        win.lift()
        self._progress = win

    def _hide_progress(self) -> None:
        if self._progress is not None:
            try:
                self._progress.destroy()
            except Exception:   # noqa: BLE001
                pass
            self._progress = None

    def _dialog(self, title: str, body: str, buttons: list, default: int, box: dict, done: threading.Event) -> None:
        tk = self._tk
        win = tk.Toplevel(self._root)
        win.title(title)
        win.resizable(False, False)
        win.attributes("-topmost", True)

        def finish(index):
            box["index"] = index
            try:
                win.destroy()
            finally:
                done.set()

        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        win.bind("<Escape>", lambda _e: finish(None))
        tk.Label(win, text=body, justify="left", anchor="w", wraplength=520, padx=20, pady=16,
                 font=("Malgun Gothic", 10)).pack(fill="x")
        row = tk.Frame(win, padx=16, pady=12)
        row.pack(anchor="e")
        widgets = []
        for i, label in enumerate(buttons):
            b = tk.Button(row, text=label, width=max(10, len(label) * 2), command=lambda i=i: finish(i))
            b.pack(side="left", padx=4)
            b.bind("<Return>", lambda _e, i=i: finish(i))
            widgets.append(b)
        self._place(win)
        win.lift()
        win.focus_force()
        if 0 <= default < len(widgets):
            widgets[default].focus_set()


def _messagebox_choose(title: str, body: str, buttons: list, default: int) -> int | None:
    """Fallback when Tk is unavailable: with two buttons, MessageBoxW is used with [Yes] = first and [No] = second."""
    try:
        u = ctypes.windll.user32
        flags = 0x00040000 | 0x00001000                   # MB_TOPMOST | MB_SYSTEMMODAL
        if len(buttons) >= 2:
            text = f"{body}\n\n[Yes] = {buttons[0]}    [No] = {buttons[1]}"
            flags |= 0x00000004 | (0x00000100 if default == 1 else 0)   # MB_YESNO (+ MB_DEFBUTTON2)
            res = u.MessageBoxW(None, text, title, flags)
            return 0 if res == 6 else 1                    # IDYES
        u.MessageBoxW(None, body, title, flags)
        return 0
    except Exception:   # noqa: BLE001
        return None


def default_deps(cfg: dict) -> LauncherDeps:
    v = cfg.get("vissim")
    exe = str(v.get("exe")) if isinstance(v, dict) and v.get("exe") else None
    return LauncherDeps(
        ui=TkUI(exe), acquire_mutex=acquire_launcher_mutex, release_mutex=release_launcher_mutex,
        http_get=http_get_json, http_post=http_post_json, port_free=port_free,
        spawn_server=spawn_detached, launch_plain=launch_plain_vissim, bring_to_front=bring_window_to_front,
    )
