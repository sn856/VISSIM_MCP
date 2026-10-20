"""Screen capture: the VISSIM window started by the server, saved as a PNG exactly as it looks on the monitor (capture_view mode=window).

This uses Win32 GDI (ctypes), not COM: `PrintWindow(PW_RENDERFULLCONTENT)` captures the window content even when other windows cover it.
pywin32 is not used (win32com/pythoncom live only inside comthread). Pillow is imported inside the function.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

PW_RENDERFULLCONTENT = 2      # also captures windows drawn with DirectX (VISSIM 3D)
SW_SHOWNOACTIVATE = 4         # restore a minimized window without taking focus
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _user32():
    return ctypes.windll.user32


def find_main_window(pid: int) -> int | None:
    """The largest visible top-level window with a title in that process (the VISSIM main window), or None."""
    u = _user32()
    found: list[tuple[int, int]] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not u.IsWindowVisible(hwnd) or u.GetWindowTextLengthW(hwnd) == 0:
            return True
        if u.GetWindow(hwnd, 4):   # GW_OWNER: skip owned windows such as dialogs
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, max(0, r.right - r.left) * max(0, r.bottom - r.top)))
        return True

    u.EnumWindows(proc(cb), 0)
    if not found:
        return None
    return max(found, key=lambda t: t[1])[0]


def capture_window(hwnd: int, path: str | Path) -> dict[str, object]:
    """Save one window as a PNG. Returns {size: [w, h], restored: whether it was un-minimized, colors: color count of a 64x36 thumbnail (1 = blank image)}."""
    from PIL import Image

    u, g = _user32(), ctypes.windll.gdi32
    restored = False
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        restored = True
    r = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        raise OSError("Could not read the window position (GetWindowRect)")
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise OSError(f"The window size is zero ({w}x{h})")
    hdc = u.GetWindowDC(hwnd)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    old = g.SelectObject(mem, bmp)
    try:
        if not u.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
            raise OSError("PrintWindow failed")
        bih = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, _BI_RGB, 0, 0, 0, 0, 0)  # top-down
        buf = ctypes.create_string_buffer(w * h * 4)
        if g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), _DIB_RGB_COLORS) != h:
            raise OSError("GetDIBits failed")
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(hwnd, hdc)
    img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path))
    colors = len(set(img.resize((64, 36)).getdata()))
    return {"size": [w, h], "restored": restored, "colors": colors}


def capture_process_window(pid: int, path: str | Path) -> dict[str, object]:
    """Find the main window of a PID and call capture_window. Raises LookupError if there is no window."""
    hwnd = find_main_window(pid)
    if hwnd is None:
        raise LookupError(f"PID {pid} has no visible window")
    return capture_window(hwnd, path)
