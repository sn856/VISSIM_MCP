"""Tray icon (pystray). Menu: Show status / Open log folder / Quit server and VISSIM. Occupies the main thread.

When the server starts, the icon is built in memory from the icon of the configured vissim.exe (icons.server_icon, never written to a file).
If that cannot be read, a green circle with a V is used instead and a warning goes to server.log.
pystray loads the image at the large icon size (32px) and Windows scales it down to the tray size (about 16px), which looks blurry ->
the icon is drawn at the tray size (`SM_CXSMICON`), loaded from a temporary `.ico`, and its handle is passed on (`native_icon_handle`).
If that fails, pystray's default path is used.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
from typing import Any, Callable

from .changelog import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)
ICON_NAME = "vissim_mcp"
ICON_TITLE = "VISSIM MCP"
ICON_SIZE = 64
MENU_STATUS = "Show status"
MENU_LOGS = "Open log folder"
MENU_QUIT = "Quit server and VISSIM"


def make_icon_image(size: int = 64):
    """Simple icon drawn with Pillow (green circle + V)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 3, size - 3), fill=(38, 120, 70, 255), outline=(20, 60, 35, 255), width=2)
    d.line((size * 0.28, size * 0.3, size * 0.5, size * 0.75), fill=(255, 255, 255, 255), width=int(size * 0.1))
    d.line((size * 0.5, size * 0.75, size * 0.72, size * 0.3), fill=(255, 255, 255, 255), width=int(size * 0.1))
    return img


def _default_icon_factory(exe_path: Any, size: int = ICON_SIZE):
    from .icons import server_icon
    return server_icon(exe_path, size)


def tray_image(exe_path: Any, *, icon_factory: Callable[..., Any] | None = None, size: int = ICON_SIZE):
    """Server icon (from the configured vissim.exe). If it cannot be made (missing or error), the green V icon plus a server.log warning."""
    if not exe_path:
        log.warning("Tray icon: no vissim.exe path in the config, using the default icon")
        return make_icon_image(size)
    try:
        img = (icon_factory or _default_icon_factory)(exe_path, size=size)
    except Exception as e:   # noqa: BLE001
        log.warning("Tray icon: could not build it from the VISSIM icon, using the default icon (%s): %s", exe_path, e)
        return make_icon_image(size)
    if img is None:
        log.warning("Tray icon: could not read the VISSIM icon, using the default icon (%s)", exe_path)
        return make_icon_image(size)
    return img


def small_icon_px() -> int:
    """Tray slot size (small icon, `SM_CXSMICON`). 16 if it cannot be read."""
    try:
        n = int(ctypes.windll.user32.GetSystemMetrics(49))
        return n if n > 0 else 16
    except Exception:   # noqa: BLE001
        return 16


def _load_icon_file(path: str, px: int) -> int:
    """Load the px-sized frame of an `.ico` as an HICON (LoadImageW). 0 on failure."""
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    image_icon, lr_loadfromfile = 1, 0x10
    return int(user32.LoadImageW(None, path, image_icon, px, px, lr_loadfromfile) or 0)


def native_icon_handle(exe_path: Any, *, icon_factory: Callable[..., Any] | None = None, px: int | None = None,
                       loader: Callable[[str, int], int] = _load_icon_file) -> int | None:
    """HICON of the server icon drawn at the tray slot size. None if it cannot be made (the caller falls back to pystray's default)."""
    import tempfile
    from .icons import write_ico
    if not exe_path:
        return None
    px = px or small_icon_px()
    try:
        img = (icon_factory or _default_icon_factory)(exe_path, size=px)
        if img is None:
            return None
        fd, path = tempfile.mkstemp(suffix=".ico", prefix="vissim_mcp_tray_")
        os.close(fd)
        try:
            write_ico({px: img}, path)
            handle = loader(path, px)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return handle or None
    except Exception as e:   # noqa: BLE001
        log.warning("Tray icon: could not load it at the slot size (%spx), using the default method: %s", px, e)
        return None


def status_text(status: dict[str, Any]) -> str:
    """Human-readable status summary (status window and tooltip)."""
    v = status.get("vissim") or {}
    n = status.get("network") or {}
    s = status.get("simulation") or {}
    srv = status.get("server") or {}
    lines = [
        f"Server port {srv.get('port')} (started {srv.get('started_at')})",
        f"VISSIM: {'running ' + str(v.get('version') or '') if v.get('running') else 'not running (started on the first command)'}",
        f"Open file: {n.get('path') or 'none'}",
        f"Unsaved changes: {n.get('unsaved_writes', 0)} (last write {n.get('last_write_at') or '-'})",
        f"Simulation: {s.get('state')} progress {s.get('progress_s')}s / {s.get('period_s')}s",
        f"Other VISSIM processes: {status.get('other_vissim_processes')}",
    ]
    return "\n".join(lines)


def _message_box(text: str, title: str) -> None:
    mb_ok, mb_iconinfo, mb_topmost = 0x0, 0x40, 0x40000
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, mb_ok | mb_iconinfo | mb_topmost)
    except Exception as e:   # noqa: BLE001
        log.warning("Status window failed: %s", e)


class Tray:
    def __init__(self, status_fn: Callable[[], dict[str, Any]], log_root: str, on_quit: Callable[[], Any],
                 exe_path: Any = None, icon_factory: Callable[..., Any] | None = None):
        self._status_fn = status_fn
        self._log_root = log_root
        self._on_quit = on_quit
        self._exe_path = exe_path
        self._icon_factory = icon_factory
        self._icon = None

    def icon_image(self):
        return tray_image(self._exe_path, icon_factory=self._icon_factory)

    def menu_items(self) -> list[tuple[str, Callable[..., Any], bool]]:
        """List of (label, action, is_default)."""
        return [(MENU_STATUS, self._show_status, True), (MENU_LOGS, self._open_logs, False),
                (MENU_QUIT, self._quit, False)]

    def _show_status(self, *_: Any) -> None:
        try:
            st = self._status_fn()
            _message_box(status_text(st), ICON_TITLE + " status")
        except Exception as e:   # noqa: BLE001
            _message_box(f"Could not read the status: {e}", ICON_TITLE)

    def _open_logs(self, *_: Any) -> None:
        try:
            os.makedirs(self._log_root, exist_ok=True)
            os.startfile(self._log_root)   # type: ignore[attr-defined]
        except Exception as e:   # noqa: BLE001
            log.warning("Failed to open the log folder: %s", e)

    def _quit(self, *_: Any) -> None:
        res = self._on_quit()
        if isinstance(res, dict) and not res.get("quit", True):
            log.info("Tray quit cancelled: %s", json.dumps(res, ensure_ascii=False))

    def run(self) -> None:
        """Blocking. Call from the main thread."""
        import pystray
        menu = pystray.Menu(*(pystray.MenuItem(label, action, default=default)
                              for label, action, default in self.menu_items()))
        self._icon = pystray.Icon(ICON_NAME, self.icon_image(), ICON_TITLE, menu)
        handle = native_icon_handle(self._exe_path, icon_factory=self._icon_factory)
        if handle and hasattr(self._icon, "_icon_handle"):   # keep pystray (win32) from reloading it at the large size
            self._icon._icon_handle = handle
        self._icon.run()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
