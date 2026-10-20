"""Command line.

python -m vissim_mcp [--config <path>]            start the tray + server without a console and return immediately
python -m vissim_mcp --serve [--config] [--no-tray] actually run (in this process; normally started by the command above)
python -m vissim_mcp status [--config]            status of the running server (JSON)
python -m vissim_mcp stop [--config]              shut down the server and VISSIM together (confirmation dialog if a file is open or something was drawn; recovery copy on timeout)
python -m vissim_mcp open [<path>] [--config]     launcher: open an .inpx through the server (empty VISSIM if no path). Double-click and the shortcut call it via pythonw
python -m vissim_mcp install [--config]           .inpx double-click association, file icon and desktop shortcut (current user only)
python -m vissim_mcp uninstall                    remove only what install created (as recorded in install.json)

Exit codes: 0 success / 1 no server, port in use, launch failure or install refused / 2 config error.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .changelog import setup_logging
from .config import ConfigError, HOST, load_config, repo_root
from .server import ADMIN_SHUTDOWN_PATH, ADMIN_STATUS_PATH, Runtime, build_server, port_free, serve_http

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
HTTP_TIMEOUT_S = 10.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m vissim_mcp", description="Resident VISSIM MCP server (tray icon + local HTTP)")
    p.add_argument("command", nargs="?", choices=["start", "status", "stop", "open", "install", "uninstall"],
                   default="start",
                   help="start (default): launch with the tray icon / status: show status / stop: shut down / open: open via the launcher / "
                        "install, uninstall: set up or remove the double-click association and shortcut")
    p.add_argument("path", nargs="?", default=None, help=".inpx to open with `open` (empty VISSIM if omitted)")
    p.add_argument("--config", default=None, help="config/vissim_mcp.json (default: that file at the repository root)")
    p.add_argument("--serve", action="store_true", help="actually run in this process (internal)")
    p.add_argument("--no-tray", action="store_true", help="HTTP only, no tray icon (for diagnostics; end it with the stop command)")
    return p


def _admin_url(cfg: dict, path: str) -> str:
    return f"http://{HOST}:{int(cfg['port'])}{path}"


def _http(url: str, method: str) -> dict:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:   # noqa: S310 — fixed to 127.0.0.1
        return json.loads(r.read().decode("utf-8"))


def cmd_status(cfg: dict) -> int:
    try:
        print(json.dumps(_http(_admin_url(cfg, ADMIN_STATUS_PATH), "GET"), ensure_ascii=False, indent=2))
        return 0
    except (urllib.error.URLError, OSError) as e:
        print(f"The server is not running (port {cfg['port']}): {e}", file=sys.stderr)
        return 1


def cmd_stop(cfg: dict) -> int:
    try:
        res = _http(_admin_url(cfg, ADMIN_SHUTDOWN_PATH), "POST")
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except (urllib.error.URLError, OSError) as e:
        print(f"The server is not running (port {cfg['port']}): {e}", file=sys.stderr)
        return 1


def cmd_start(cfg: dict, config_path: str | None) -> int:
    """Start --serve in a console-less child process (pythonw) and return."""
    if not port_free(int(cfg["port"])):
        print(f"Already running (port {cfg['port']} is in use). Check with 'python -m vissim_mcp status'.",
              file=sys.stderr)
        return 1
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    cmd = [str(pythonw if pythonw.is_file() else exe), "-m", "vissim_mcp", "--serve"]
    if config_path:
        cmd += ["--config", str(Path(config_path).resolve())]
    subprocess.Popen(cmd, cwd=str(repo_root()), close_fds=True,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    print(f"Server started (port {cfg['port']}). Look for the tray icon. Status: python -m vissim_mcp status")
    return 0


def _redirect_console_to_file(log_root: str) -> None:
    """Under pythonw, stdout/stderr are None and the HTTP library's console logging raises, so redirect them to a file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    Path(log_root).mkdir(parents=True, exist_ok=True)
    f = open(Path(log_root) / "server.console.log", "a", encoding="utf-8", errors="replace", buffering=1)
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


MUTEX_NAME = "Local\\vissim_mcp_server"


def _acquire_single_instance() -> bool:
    """Allow only one server process per PC via a named mutex (released when the process ends). Returns True if the mutex cannot be created (the port check still guards)."""
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if not handle:
            return True
        already = ctypes.windll.kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
        globals()["_MUTEX_HANDLE"] = handle   # hold it for the lifetime of the process
        return not already
    except Exception:   # noqa: BLE001
        return True


def _serve_http_or_die(mcp, port: int, ready: threading.Event, logger) -> None:
    """If HTTP dies, end the process so no ghost process with only a tray icon is left."""
    try:
        serve_http(mcp, port, ready)
        logger.error("HTTP server ended — shutting down the process")
    except Exception as e:   # noqa: BLE001
        logger.exception("HTTP server failed — shutting down the process: %s", e)
    logging.shutdown()
    os._exit(1)


def cmd_serve(cfg: dict, no_tray: bool) -> int:
    _redirect_console_to_file(cfg["log_root"])
    logger = setup_logging(cfg["log_root"])
    port = int(cfg["port"])
    if not _acquire_single_instance() or not port_free(port):
        logger.error("port %d in use or a server process already exists — already running", port)
        print(f"Already running (port {port} is in use)", file=sys.stderr)
        return 1
    rt = Runtime(cfg)
    rt.start()
    mcp = build_server(rt)
    ready = threading.Event()
    http_thread = threading.Thread(target=_serve_http_or_die, args=(mcp, port, ready, logger), name="vissim-mcp-http",
                                   daemon=True)
    http_thread.start()
    if not ready.wait(timeout=HTTP_TIMEOUT_S):
        logger.error("HTTP server did not come up within %.0f s — exiting", HTTP_TIMEOUT_S)
        return 1
    logger.info("VISSIM MCP server started: http://%s:%d (config %s)", HOST, port, cfg.get("_path"))
    if no_tray:
        threading.Event().wait()   # the stop command (/admin/shutdown) ends the process
        return 0
    try:
        from .tray import Tray
        Tray(rt.status, cfg["log_root"], rt.request_quit, exe_path=cfg["vissim"].get("exe")).run()
    except Exception as e:   # noqa: BLE001 — keep the server alive even if the tray dies (end it with the stop command)
        logger.exception("tray failed — keeping HTTP only: %s", e)
        threading.Event().wait()
    return 0


def _notify_config_error(message: str) -> None:
    """Report a launcher config error in a Windows message box. Failure is ignored silently (exit code 2 is unchanged)."""
    try:
        import ctypes
        mb_ok, mb_iconerror, mb_topmost = 0x0, 0x10, 0x40000
        ctypes.windll.user32.MessageBoxW(0, f"Config error: {message}", "VISSIM MCP", mb_ok | mb_iconerror | mb_topmost)
    except Exception:   # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:   # noqa: BLE001
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.path is not None and args.command != "open":
        parser.error(f"the path argument is only used with open: {args.path}")
    if args.command == "uninstall" and not args.serve:   # removes only what install.json records, so it works without a config
        from .install import uninstall
        return uninstall()
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        if args.command == "open" and sys.stderr is None:   # a double-click (pythonw) has no console, so use a message box
            _notify_config_error(str(e))
        return 2
    if args.serve:
        return cmd_serve(cfg, args.no_tray)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "stop":
        return cmd_stop(cfg)
    if args.command == "open":
        from .launcher import run_open
        return run_open(cfg, args.path)
    if args.command == "install":
        from .install import install
        return install(cfg)
    return cmd_start(cfg, args.config)


if __name__ == "__main__":
    sys.exit(main())
