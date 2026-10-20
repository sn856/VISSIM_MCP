"""View: view settings (set_view) and capture (capture_view). Never touches network objects or the network file (ADR 0002).

  - set_view: load a layout (`Vissim.LoadLayout`) -> 2D/3D (`NetworkWindow.SetAttValue("3D")`) -> 2D extent (`ZoomTo`) ->
    3D camera (`SetCameraPositionAndAngle`). layout="view" reads the `.layx` next to the network, turns on background images and 3D models, turns off the online
    map, writes that copy to `<log_root>/layouts/` (`layout_xml.write_view_layout`) and loads it. The original `.layx` is
    left as is. The loaded display settings stay inside VISSIM, though, so a later save_network also saves them to the network's `.layx`.
  - capture_view: PNG in `<log_root>/captures/`. mode=network uses COM `NetworkWindow.Screenshot` (editor view only),
    mode=window captures the whole server VISSIM window as it appears on screen (`screen.capture_process_window`, for development checks).
  - COM calls run only on the ComThread. Not refused while a simulation runs (only the view changes; it runs between steps in the COM thread queue).
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import layout_xml
from .changelog import ChangeLog
from .comthread import ComThread
from .config import TUNABLE_DEFAULTS
from .errors import KIND_BAD_ARGS, KIND_COM_ERROR, KIND_NOT_FOUND, ToolError
from .state import ServerState

LAYOUT_DIR = "layouts"
CAPTURE_DIR = "captures"
LAYOUTS = ("view", "network")          # view = copy of the layout for viewing, network = the original .layx next to the network, unchanged
MODES = ("2d", "3d")
CAPTURE_MODES = ("network", "window")
CAMERA_KEYS = ("x", "y", "z", "yaw", "pitch")
_TS_FORMAT = "%Y%m%d-%H%M%S"
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


# ---------------------------------------------------------------- functions that run on the COM thread

def _load_layout(v: Any, path: str) -> None:
    v.LoadLayout(path)


def _apply_view(v: Any, mode: str | None, zoom: list[float] | None, camera: dict[str, float] | None) -> dict[str, Any]:
    nw = v.Graphics.CurrentNetworkWindow
    if mode is not None:
        nw.SetAttValue("3D", 1 if mode == "3d" else 0)
    if zoom is not None:
        nw.SetAttValue("3D", 0)
        nw.ZoomTo(*zoom)
    if camera is not None:
        nw.SetAttValue("3D", 1)
        nw.SetCameraPositionAndAngle(camera["x"], camera["y"], camera["z"], camera["yaw"], camera["pitch"])
    return {"mode": "3d" if int(nw.AttValue("3D") or 0) else "2d"}


def _screenshot(v: Any, path: str, scale: float) -> None:
    v.Graphics.CurrentNetworkWindow.Screenshot(path, scale)


# ---------------------------------------------------------------- service layer

class View:
    def __init__(self, cfg: dict[str, Any], state: ServerState, com: ComThread, changelog: ChangeLog,
                 vissim_getter: Callable[[], Any],
                 window_capturer: Callable[[int, str], dict[str, Any]] | None = None,
                 view_layout_writer: Callable[[Path, Path], dict[str, Any]] | None = None,
                 clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.state = state
        self.com = com
        self.log = changelog
        self._vissim = vissim_getter
        self._capture_window = window_capturer or _default_window_capturer
        self._write_view_layout = view_layout_writer or layout_xml.write_view_layout
        self._clock = clock or datetime.now
        self._sleep = sleep
        self._root = Path(cfg["log_root"])

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return self.com.submit(fn, *args)
        except ToolError:
            raise
        except Exception as e:   # noqa: BLE001
            raise ToolError(KIND_COM_ERROR, str(e), {"type": type(e).__name__}) from e

    # ---- set_view ----
    def set_view(self, layout: str | None = None, mode: str | None = None, zoom: list[float] | None = None,
                 camera: dict[str, Any] | None = None) -> dict[str, Any]:
        if layout is not None and layout not in LAYOUTS:
            raise ToolError(KIND_BAD_ARGS, f"layout must be one of {', '.join(LAYOUTS)} ({layout!r})")
        if mode is not None and mode not in MODES:
            raise ToolError(KIND_BAD_ARGS, f"mode must be one of {', '.join(MODES)} ({mode!r})")
        zoom_v = _check_zoom(zoom)
        cam = _check_camera(camera)
        if zoom_v is not None and cam is not None:
            raise ToolError(KIND_BAD_ARGS, "zoom (2D extent) and camera (3D camera) cannot be given together")
        if zoom_v is not None and mode == "3d" or cam is not None and mode == "2d":
            raise ToolError(KIND_BAD_ARGS, "zoom is for 2D and camera is for 3D; they do not match mode")
        if layout is None and mode is None and zoom_v is None and cam is None:
            raise ToolError(KIND_BAD_ARGS, "at least one of layout, mode, zoom, camera is required")
        v = self._vissim()
        net = Path(self.state.snapshot()["network"]["path"])
        out: dict[str, Any] = {"warnings": []}
        if layout is not None:
            out["layout"] = self._load(v, net, layout)
        out.update(self._call(_apply_view, v, mode, zoom_v, cam))
        if zoom_v is not None:
            out["zoom"] = zoom_v
        if cam is not None:
            out["camera"] = cam
        return out

    def _load(self, v: Any, net: Path, layout: str) -> dict[str, Any]:
        src = net.with_suffix(".layx")
        if not src.is_file():
            raise ToolError(KIND_NOT_FOUND, f"No layout file next to the network: {src}", {"path": str(src)})
        if layout == "network":
            path, info = src, {"layouts": None, "changed": 0}
        else:
            dst = (self._root / LAYOUT_DIR / f"{_stem(net)}_view.layx").resolve()
            try:
                info = self._write_view_layout(src, dst)
            except layout_xml.LayoutFormatError as e:
                raise ToolError(KIND_BAD_ARGS, f"Cannot create the view layout: {e}", {"path": str(src)}) from e
            path = dst
        self._call(_load_layout, v, str(path))
        entry = {"tool": "set_view", "kind": "view", "network_path": str(net), "ok": True,
                 "layout": layout, "layout_path": str(path), "source_layx": str(src), "changed": info.get("changed")}
        self.log.append(entry)
        return {"name": layout, "path": str(path), "source": str(src), "changed": info.get("changed"),
                "note": "The display settings stay inside VISSIM; a later save_network also saves them to the network's .layx"}

    # ---- capture_view ----
    def capture_view(self, mode: str = "network", label: str | None = None, scale: float = 1.0,
                     settle_s: float | None = None) -> dict[str, Any]:
        if mode not in CAPTURE_MODES:
            raise ToolError(KIND_BAD_ARGS, f"mode must be one of {', '.join(CAPTURE_MODES)} ({mode!r})")
        if label is not None and (not label or any(c not in _SAFE for c in label) or len(label) > 60):
            raise ToolError(KIND_BAD_ARGS, "label must be at most 60 characters of letters, digits, - or _")
        if not 0.1 <= float(scale) <= 4.0:
            raise ToolError(KIND_BAD_ARGS, "scale must be between 0.1 and 4")
        wait = float(settle_s if settle_s is not None else self.cfg.get("capture_settle_s",
                                                                          TUNABLE_DEFAULTS["capture_settle_s"]))
        if not 0.0 <= wait <= 60.0:
            raise ToolError(KIND_BAD_ARGS, "settle_s must be between 0 and 60 seconds")
        v = self._vissim()
        folder = (self._root / CAPTURE_DIR).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        path = _unique(folder, self._clock().strftime(_TS_FORMAT) + (f"_{label}" if label else ""), ".png")
        warnings: list[str] = []
        if wait > 0:
            self._sleep(wait)
        if mode == "network":
            self._call(_screenshot, v, str(path), float(scale))
            if not path.is_file():
                raise ToolError(KIND_COM_ERROR, f"Screenshot did not create a file: {path}")
            size = _png_size(path)
            out = {"path": str(path), "mode": mode, "size": size}
        else:
            pid = self.state.full()["vissim"]["pid"]
            if pid is None:
                raise ToolError(KIND_NOT_FOUND, "The PID of the server VISSIM is unknown, so the window cannot be found (use mode=network)")
            try:
                info = self._capture_window(int(pid), str(path))
            except LookupError as e:
                raise ToolError(KIND_NOT_FOUND, str(e), {"pid": pid}) from e
            except OSError as e:
                raise ToolError(KIND_COM_ERROR, f"Window capture failed: {e}", {"pid": pid}) from e
            if info.get("restored"):
                warnings.append("The VISSIM window was minimized; it was restored before the capture")
            if info.get("colors") == 1:
                warnings.append("The captured image has only one color (the window may not have been drawn yet; try a larger settle_s)")
            out = {"path": str(path), "mode": mode, "size": info.get("size"), "pid": pid}
        out["warnings"] = warnings
        return out


# ---------------------------------------------------------------- helpers

def _default_window_capturer(pid: int, path: str) -> dict[str, Any]:
    from . import screen
    return screen.capture_process_window(pid, path)


def _check_zoom(zoom: Any) -> list[float] | None:
    if zoom is None:
        return None
    try:
        vals = [float(x) for x in zoom]
    except (TypeError, ValueError) as e:
        raise ToolError(KIND_BAD_ARGS, "zoom must be four numbers [x0, y0, x1, y1]") from e
    if len(vals) != 4 or vals[0] >= vals[2] or vals[1] >= vals[3]:
        raise ToolError(KIND_BAD_ARGS, "zoom must be [x0, y0, x1, y1] with x0<x1 and y0<y1")
    return vals


def _check_camera(camera: Any) -> dict[str, float] | None:
    if camera is None:
        return None
    if not isinstance(camera, dict) or any(k not in camera for k in CAMERA_KEYS):
        raise ToolError(KIND_BAD_ARGS, f"camera must be {{{', '.join(CAMERA_KEYS)}}} (network coordinates in m, angles in degrees)")
    try:
        return {k: float(camera[k]) for k in CAMERA_KEYS}
    except (TypeError, ValueError) as e:
        raise ToolError(KIND_BAD_ARGS, "camera values must be numbers") from e


def _stem(path: Path) -> str:
    name = path.name
    return name[:-5] if name.lower().endswith(".inpx") else path.stem


def _unique(folder: Path, stem: str, suffix: str) -> Path:
    cand = folder / f"{stem}{suffix}"
    n = 1
    while cand.exists():
        n += 1
        cand = folder / f"{stem}_{n}{suffix}"
    return cand


def _png_size(path: Path) -> list[int] | None:
    """Read width and height from the PNG header (IHDR). None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return [int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")]
    except OSError:
        return None

