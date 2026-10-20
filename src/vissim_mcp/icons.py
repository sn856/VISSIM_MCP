"""Icons: the launcher and server icons are built on demand from icon 0 of the installed VISSIM exe.

Source (`Vissim260.exe` on a typical install): a rounded square, background `#D653D3`, white text "Vsi" on top and "26" below.
- Launcher icon (`.inpx` file icon and launcher window): only the background color of the source is inverted; the white text and the transparency stay as they are
  -> white "Vsi / 26" on a green `#29AC2C` background (`invert_bg_keep_text`; an early version inverted all of RGB, which made the text black).
- Server icon (tray only, never written to a file): the source shape (transparency) is filled with one inverted background color,
  and a single large white "V" (`_C_HEIGHT` of the shape height) is drawn fresh in the middle. The source text is not erased.
  The tray slot is about 16px, so the original two lines "CMP/26" were too small; it became a single letter
  (first "C", then "V"). Font: Segoe UI Bold (`segoeuib.ttf`), falling back to Semibold (`seguisb.ttf`) and Segoe UI (`segoeui.ttf`),
  and to Pillow's default font if none exist.
- The tool ships no icon files (PTV material); it always builds them from the installed VISSIM. The README pictures are rendered from it for identification only.

Reading the exe icon uses ctypes Win32 (`PrivateExtractIconsW` + `GetIconInfo`/`GetDIBits`). pywin32 is not used
(win32com/pythoncom live only inside comthread). If the icon cannot be read, None is returned instead of raising.
The fallback icon (green circle + V) is chosen by the caller.

Public API (used by other modules)
- `ICO_SIZES`: the `.ico` sizes (16, 24, 32, 48, 64, 128, 256).
- `FONT_CANDIDATES`: candidate fonts for the server icon (in order).
- `load_exe_icon(exe_path, size) -> Image | None`: icon 0 of the exe at one size (RGBA). Default of the test substitution point `loader`.
- `invert_rgb(img) -> Image`: invert RGB only, transparency unchanged.
- `invert_bg_keep_text(img) -> Image`: invert the background only, white text unchanged (launcher icon).
- `render_server_icon(src, *, fonts=FONT_CANDIDATES) -> Image`: one source image -> one server icon.
- `pick_font_path(candidates) -> str | None`: the first font file that exists.
- `launcher_images(exe_path, sizes=ICO_SIZES, *, loader=load_exe_icon) -> dict[int, Image] | None`
- `server_images(exe_path, sizes=ICO_SIZES, *, loader=load_exe_icon, fonts=FONT_CANDIDATES) -> dict[int, Image] | None`
- `launcher_icon(exe_path, size=256, *, loader=load_exe_icon) -> Image | None`: one size (window icon etc.).
- `server_icon(exe_path, size=64, *, loader=load_exe_icon, fonts=FONT_CANDIDATES) -> Image | None`: one size (tray).
- `write_ico(images, path) -> Path`: per-size images -> a multi-size `.ico`.
- `write_launcher_ico(exe_path, path, *, loader=load_exe_icon) -> Path | None`: the launcher `.ico` (used by install). Writes nothing and returns None if the icon cannot be read.

`*_images`, `*_icon` and `write_launcher_ico` return None if even one size cannot be read (no icon with only some sizes is built).
"""
from __future__ import annotations

import ctypes
import os
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from PIL import Image as _PILImage

ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_CANDIDATES: tuple[str, ...] = (str(_FONT_DIR / "segoeuib.ttf"), str(_FONT_DIR / "seguisb.ttf"),
                                     str(_FONT_DIR / "segoeui.ttf"))

SERVER_TEXT = "V"                    # single letter (originally two lines "CMP"/"26", then "C", then "V")
_C_HEIGHT = 0.70                     # letter ink height (fraction of the shape height)
_C_MAX_WIDTH = 0.80                  # upper limit of the letter ink width (fraction of the shape width)
_SUPERSAMPLE_PX = 512                # draw the letter at least this large, then scale down
SHORTCUT_TEXT = ("Vis", "MCP")       # desktop shortcut icon (top / bottom line)
SHORTCUT_FONTS: tuple[str, ...] = (str(_FONT_DIR / "seguisb.ttf"), str(_FONT_DIR / "segoeui.ttf"))   # same font as the source text
_LINE_BANDS = ((0.14, 0.44), (0.56, 0.86))   # vertical ranges of the two source lines (fraction of the shape height)
_LINE_SIDE_MARGIN = 0.12             # left/right margin of a line (fraction of the shape width, per side)

Loader = Callable[[object, int], "_PILImage.Image | None"]


# ---------- reading the exe icon (ctypes Win32) ----------

def _win32():
    """A separate DLL instance with argtypes set (leaves the global windll prototypes alone). Prevents truncating 64-bit handles."""
    from ctypes import wintypes

    class IconInfo(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BitmapInfo(ctypes.Structure):   # header + color table space (roomy enough to accept a monochrome mask too)
        _fields_ = [("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 256)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    u.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                                       wintypes.UINT, wintypes.UINT]
    u.PrivateExtractIconsW.restype = wintypes.UINT
    u.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(IconInfo)]
    u.GetIconInfo.restype = wintypes.BOOL
    u.DestroyIcon.argtypes = [wintypes.HICON]
    u.DestroyIcon.restype = wintypes.BOOL
    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.ReleaseDC.restype = ctypes.c_int
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(BitmapInfo), wintypes.UINT]
    g.GetDIBits.restype = ctypes.c_int
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteObject.restype = wintypes.BOOL
    return u, g, IconInfo, BitmapInfo


def _dib_bgra(g, hdc, hbm, size: int, bitmap_info) -> bytes | None:
    """Bitmap -> top-down 32-bit BGRA bytes. None on failure."""
    bi = bitmap_info()
    h = bi.bmiHeader
    h.biSize = ctypes.sizeof(type(h))
    h.biWidth, h.biHeight, h.biPlanes, h.biBitCount, h.biCompression = size, -size, 1, 32, 0   # BI_RGB
    buf = ctypes.create_string_buffer(size * size * 4)
    if g.GetDIBits(hdc, hbm, 0, size, buf, ctypes.byref(bi), 0) != size:   # DIB_RGB_COLORS
        return None
    return buf.raw


def load_exe_icon(exe_path, size: int):
    """Icon 0 of the exe as size x size RGBA. Transparency is preserved (via the mask if there is no alpha). None if it cannot be read; never raises."""
    if not exe_path:
        return None
    try:
        from ctypes import wintypes

        from PIL import Image

        path = str(exe_path)
        if not os.path.isfile(path):
            return None
        u, g, IconInfo, BitmapInfo = _win32()
        hicon = wintypes.HICON()
        icon_id = wintypes.UINT()
        n = u.PrivateExtractIconsW(path, 0, size, size, ctypes.byref(hicon), ctypes.byref(icon_id), 1, 0)
        if n in (0, 0xFFFFFFFF) or not hicon.value:
            return None
        info = IconInfo()
        hdc = None
        try:
            if not u.GetIconInfo(hicon, ctypes.byref(info)):
                return None
            if not info.hbmColor:    # monochrome icon: not applicable to VISSIM
                return None
            hdc = u.GetDC(None)
            color = _dib_bgra(g, hdc, info.hbmColor, size, BitmapInfo)
            if color is None:
                return None
            img = Image.frombuffer("RGBA", (size, size), color, "raw", "BGRA", 0, 1).copy()
            if img.getchannel("A").getextrema()[1] == 0:   # old format without alpha -> AND mask (white = transparent)
                mask = _dib_bgra(g, hdc, info.hbmMask, size, BitmapInfo) if info.hbmMask else None
                if mask is None:
                    return None
                m = Image.frombuffer("RGBA", (size, size), mask, "raw", "BGRA", 0, 1).convert("L")
                img.putalpha(m.point(lambda v: 0 if v >= 128 else 255))
            return img
        finally:
            if hdc:
                u.ReleaseDC(None, hdc)
            if info.hbmColor:
                g.DeleteObject(info.hbmColor)
            if info.hbmMask:
                g.DeleteObject(info.hbmMask)
            u.DestroyIcon(hicon)
    except Exception:   # noqa: BLE001 — any failure means "none" (the caller provides the fallback)
        return None


# ---------- building the images (Pillow, no Win32 needed) ----------

def invert_rgb(img):
    """Invert RGB only, keep transparency (alpha). Source background #D653D3 -> #29AC2C, white text -> black."""
    from PIL import Image, ImageOps

    src = img.convert("RGBA")
    rgb = ImageOps.invert(src.convert("RGB"))
    out = Image.merge("RGBA", (*rgb.split(), src.getchannel("A")))
    return out


def _bg_color(src) -> tuple[int, int, int]:
    """The most common color among opaque pixels (the source background). Includes translucent ones if none are opaque, and falls back to #D653D3."""
    px = src.tobytes()
    for limit in (255, 1):
        c = Counter(px[i:i + 3] for i in range(0, len(px), 4) if px[i + 3] >= limit)
        if c:
            r, gg, b = c.most_common(1)[0][0]
            return (r, gg, b)
    return (0xD6, 0x53, 0xD3)


def pick_font_path(candidates: Iterable) -> str | None:
    """The first candidate font file that actually exists. None if none."""
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    return None


def _font(path: str | None, px: int):
    from PIL import ImageFont

    px = max(1, int(px))
    if path:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            pass
    return ImageFont.load_default(size=px)


def _ink(text: str, font):
    """Draw the text in white (L), then crop to the ink and return it."""
    from PIL import Image, ImageDraw

    l, t, r, b = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font)
    pad = 4
    canvas = Image.new("L", (max(1, r - l) + 2 * pad, max(1, b - t) + 2 * pad), 0)
    ImageDraw.Draw(canvas).text((pad - l, pad - t), text, fill=255, font=font)
    box = canvas.getbbox()
    return canvas.crop(box) if box else canvas.crop((0, 0, 1, 1))


def _fit_ink(text: str, font_path: str | None, height: float, max_width: float):
    """Text ink with the ink height equal to height, shrunk to fit if the width exceeds max_width."""
    px = max(1.0, height)
    ink = _ink(text, _font(font_path, round(px)))
    for _ in range(3):   # fit the font size to the ink height (the ink/font-size ratio differs per font)
        if ink.height <= 0:
            break
        nxt = px * height / ink.height
        if abs(nxt - px) < 0.5:
            break
        px = max(1.0, nxt)
        ink = _ink(text, _font(font_path, round(px)))
    if ink.width > max_width > 0:
        px = max(1.0, px * max_width / ink.width)
        ink = _ink(text, _font(font_path, round(px)))
        while ink.width > max_width and px > 1:   # step down one at a time if rounding overflows
            px -= 1
            ink = _ink(text, _font(font_path, round(px)))
    return ink


def render_server_icon(src, *, fonts: Sequence = FONT_CANDIDATES):
    """One source image -> server icon: source transparency + one inverted background color + a large white "V" in the middle."""
    from PIL import Image

    src = src.convert("RGBA")
    w, h = src.size
    alpha = src.getchannel("A")
    inv = tuple(255 - c for c in _bg_color(src))
    sx0, sy0, sx1, sy1 = alpha.getbbox() or (0, 0, w, h)

    k = max(2, -(-_SUPERSAMPLE_PX // max(w, h)))   # draw large and scale down (keeps small sizes crisp)
    mask = Image.new("L", (w * k, h * k), 0)
    ink = _fit_ink(SERVER_TEXT, pick_font_path(fonts), (sy1 - sy0) * _C_HEIGHT * k, (sx1 - sx0) * _C_MAX_WIDTH * k)
    cx, cy = (sx0 + sx1) / 2 * k, (sy0 + sy1) / 2 * k
    mask.paste(255, (round(cx - ink.width / 2), round(cy - ink.height / 2)), ink)
    mask = mask.reduce(k)

    base = Image.new("RGBA", (w, h), inv + (255,))
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    out = Image.composite(white, base, mask)
    out.putalpha(alpha)
    return out


def render_shortcut_icon(src, *, fonts: Sequence = SHORTCUT_FONTS):
    """One source image -> desktop shortcut icon: inverted (green) background shape with white "Vis"/"MCP" on two lines.

    The background is the source shape (transparency) in one inverted background color; the text is drawn fresh in white (originally black).
    The vertical positions of the lines use the same fractions as the two source lines ("Vsi"/"26"); each line is fitted to the same height within the side margins.
    """
    from PIL import Image

    src = src.convert("RGBA")
    w, h = src.size
    alpha = src.getchannel("A")
    inv = tuple(255 - c for c in _bg_color(src))
    sx0, sy0, sx1, sy1 = alpha.getbbox() or (0, 0, w, h)
    sh = sy1 - sy0

    k = max(2, -(-_SUPERSAMPLE_PX // max(w, h)))
    mask = Image.new("L", (w * k, h * k), 0)
    font_path = pick_font_path(fonts)
    cx = (sx0 + sx1) / 2 * k
    max_w = (sx1 - sx0) * (1 - 2 * _LINE_SIDE_MARGIN) * k
    inks = [_fit_ink(text, font_path, sh * (b - a) * k, max_w) for text, (a, b) in zip(SHORTCUT_TEXT, _LINE_BANDS)]
    for ink, (a, b) in zip(inks, _LINE_BANDS):
        cy = (sy0 + sh * (a + b) / 2) * k
        mask.paste(255, (round(cx - ink.width / 2), round(cy - ink.height / 2)), ink)
    mask = mask.reduce(k)

    base = Image.new("RGBA", (w, h), inv + (255,))
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    out = Image.composite(white, base, mask)
    out.putalpha(alpha)
    return out


# ---------- per-size sets and .ico ----------

def _sources(exe_path, sizes: Sequence[int], loader: Loader):
    if not exe_path:
        return None
    out = {}
    for s in sizes:
        img = loader(exe_path, s)
        if img is None:
            return None
        out[s] = img.convert("RGBA")
    return out


def invert_bg_keep_text(img):
    """Invert only the background color and keep the white text white (transparency unchanged). Source #D653D3 and white "Vsi 26" -> #29AC2C and white "Vsi 26".

    For each pixel, find the blend ratio t from background to white in the source (from the channel with the largest background/white difference), then blend the inverted background and white at the same ratio.
    The soft edges of the text are preserved. (An early version inverted all of RGB, which made the text black.)
    """
    from PIL import Image

    src = img.convert("RGBA")
    bg = _bg_color(src)
    inv = tuple(255 - c for c in bg)
    ch = max(range(3), key=lambda i: 255 - bg[i])
    span = max(1, 255 - bg[ch])
    raw = src.tobytes()
    out = bytearray(len(raw))
    for i in range(0, len(raw), 4):
        t = (raw[i + ch] - bg[ch]) / span
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        for k in range(3):
            out[i + k] = round(inv[k] + (255 - inv[k]) * t)
        out[i + 3] = raw[i + 3]
    return Image.frombytes("RGBA", src.size, bytes(out))


def launcher_images(exe_path, sizes: Sequence[int] = ICO_SIZES, *, loader: Loader = load_exe_icon):
    """Per-size launcher icons {size: RGBA}: white text on the inverted (green) background. None if even one size cannot be read."""
    src = _sources(exe_path, sizes, loader)
    return None if src is None else {s: invert_bg_keep_text(im) for s, im in src.items()}


def server_images(exe_path, sizes: Sequence[int] = ICO_SIZES, *, loader: Loader = load_exe_icon,
                  fonts: Sequence = FONT_CANDIDATES):
    """Per-size server icons {size: RGBA}. None if even one size cannot be read."""
    src = _sources(exe_path, sizes, loader)
    return None if src is None else {s: render_server_icon(im, fonts=fonts) for s, im in src.items()}


def launcher_icon(exe_path, size: int = 256, *, loader: Loader = load_exe_icon):
    """Launcher icon at one size. None if it cannot be read."""
    imgs = launcher_images(exe_path, (size,), loader=loader)
    return None if imgs is None else imgs[size]


def server_icon(exe_path, size: int = 64, *, loader: Loader = load_exe_icon, fonts: Sequence = FONT_CANDIDATES):
    """Server icon at one size (tray). None if it cannot be read."""
    imgs = server_images(exe_path, (size,), loader=loader, fonts=fonts)
    return None if imgs is None else imgs[size]


def write_ico(images: Mapping[int, object], path) -> Path:
    """Write per-size images as they are (no rescaling) into one `.ico`. Creates the parent folder and replaces the file after writing a temporary one."""
    if not images:
        raise ValueError("No icon images")
    path = Path(path)
    sizes = sorted(images)
    frames = [images[s].convert("RGBA") for s in sizes]
    for s, im in zip(sizes, frames):
        if im.size != (s, s):
            raise ValueError(f"Image {im.size} for the {s} size slot")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(_ico_bytes(sizes, frames))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def _ico_bytes(sizes: Sequence[int], frames: Sequence) -> bytes:
    """Build standard `.ico` bytes directly: 256 as PNG, the rest as 32-bit bitmaps (XOR flipped vertically + AND mask, height doubled).

    Pillow's ICO save (PNG frames or bitmap_format="bmp") leaves the plane count of the directory entry at 0 and has no AND mask,
    so while LoadImage could read the result, Explorer and the desktop drew the shortcut icon as a blank document (seen on Windows 11;
    the py.ico of python showed fine on the same shortcut). So the format Windows itself uses is written.
    """
    import io
    import struct

    blobs: list[bytes] = []
    for s, im in zip(sizes, frames):
        if s >= 256:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            blobs.append(buf.getvalue())
            continue
        rgba = im.tobytes()                       # top-down, RGBA
        row = s * 4
        xor = bytearray()
        for y in range(s - 1, -1, -1):            # bitmaps go bottom-up, BGRA
            line = rgba[y * row:(y + 1) * row]
            for x in range(0, row, 4):
                r, g, b, a = line[x:x + 4]
                xor += bytes((b, g, r, a))
        mask_row = ((s + 31) // 32) * 4
        and_mask = bytearray()
        for y in range(s - 1, -1, -1):            # 1 if fully transparent (background shows through)
            bits = bytearray(mask_row)
            for x in range(s):
                if rgba[(y * s + x) * 4 + 3] == 0:
                    bits[x // 8] |= 0x80 >> (x % 8)
            and_mask += bits
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0)
        blobs.append(header + bytes(xor) + bytes(and_mask))

    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for s, blob in zip(sizes, blobs):
        dim = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    return bytes(out)


def write_launcher_ico(exe_path, path, *, loader: Loader = load_exe_icon) -> Path | None:
    """Launcher `.ico` (seven sizes, 16 to 256) written to path. Writes nothing and returns None if the exe icon cannot be read."""
    imgs = launcher_images(exe_path, ICO_SIZES, loader=loader)
    if imgs is None:
        return None
    return write_ico(imgs, path)


def shortcut_images(exe_path, sizes: Sequence[int] = ICO_SIZES, *, loader: Loader = load_exe_icon,
                    fonts: Sequence = SHORTCUT_FONTS):
    """Per-size desktop shortcut icons {size: RGBA}. None if even one size cannot be read."""
    src = _sources(exe_path, sizes, loader)
    return None if src is None else {s: render_shortcut_icon(im, fonts=fonts) for s, im in src.items()}


def write_shortcut_ico(exe_path, path, *, loader: Loader = load_exe_icon) -> Path | None:
    """Desktop shortcut `.ico` ("Vis"/"MCP", seven sizes) written to path. Writes nothing and returns None if it cannot be read."""
    imgs = shortcut_images(exe_path, ICO_SIZES, loader=loader)
    if imgs is None:
        return None
    return write_ico(imgs, path)
