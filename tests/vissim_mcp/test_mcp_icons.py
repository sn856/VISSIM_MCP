"""Icons: launcher (inverted colors), server (inverted background + white V), the seven .ico sizes, font fallback, unreadable exe (synthetic images only, no VISSIM needed)."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from vissim_mcp import icons

BG = (0xD6, 0x53, 0xD3)          # source background #D653D3
INV = (0x29, 0xAC, 0x2C)         # inverted #29AC2C
WHITE = (255, 255, 255)
# The two text lines of the synthetic source (vertical fractions): top 0.14-0.44, bottom 0.56-0.86
TOP = (0.14, 0.44)
BOTTOM = (0.56, 0.86)


def _src(size: int) -> Image.Image:
    """Purple rounded square + two white bars (instead of text; the exact positions make layout checks possible). One translucent dot."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=max(2, size // 5), fill=BG + (255,))
    for (a, b), (l, r) in ((TOP, (0.25, 0.75)), (BOTTOM, (0.33, 0.67))):
        d.rectangle((round(size * l), round(size * a), round(size * r) - 1, round(size * b) - 1), fill=WHITE + (255,))
    img.putpixel((size // 2, size - 1), BG + (128,))   # translucent at the bottom edge
    return img


def _loader(calls=None):
    def load(exe, size):
        if calls is not None:
            calls.append((exe, size))
        return _src(size)
    return load


def _none_loader(exe, size):
    return None


def _rows_with(img, color, tol=0):
    px = img.load()
    w, h = img.size
    rows = set()
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            if p[3] > 0 and all(abs(p[i] - color[i]) <= tol for i in range(3)):
                rows.add(y)
    return rows


def _cols_with(img, color, tol=0):
    px = img.load()
    w, h = img.size
    return {x for y in range(h) for x in range(w)
            if px[x, y][3] > 0 and all(abs(px[x, y][i] - color[i]) <= tol for i in range(3))}


# ---------- launcher: inverted background + white text ----------

def test_invert_rgb_colors_and_alpha():
    src = _src(64)
    out = icons.invert_rgb(src)
    assert out.mode == "RGBA" and out.size == (64, 64)
    assert out.getpixel((3, 32)) == INV + (255,)                    # background -> #29AC2C
    assert out.getpixel((32, round(64 * 0.3))) == (0, 0, 0, 255)   # white text -> black
    assert out.getpixel((0, 0))[3] == 0                             # transparent corner unchanged
    assert out.getpixel((32, 63)) == INV + (128,)                   # translucency unchanged
    assert out.getchannel("A").tobytes() == src.getchannel("A").tobytes()


def test_invert_bg_keep_text_white_text_on_inverted_bg():
    src = _src(64)
    out = icons.invert_bg_keep_text(src)
    assert out.mode == "RGBA" and out.size == (64, 64)
    assert out.getpixel((3, 32)) == INV + (255,)                    # background -> #29AC2C
    assert out.getpixel((32, round(64 * 0.3))) == (255, 255, 255, 255)   # white text stays white
    assert out.getpixel((0, 0))[3] == 0                             # transparent corner unchanged
    assert out.getpixel((32, 63)) == INV + (128,)                   # translucency unchanged
    assert out.getchannel("A").tobytes() == src.getchannel("A").tobytes()
    assert not _rows_with(out, (0, 0, 0), tol=20)                   # no black text


def test_launcher_images_all_sizes():
    calls = []
    imgs = icons.launcher_images("x.exe", loader=_loader(calls))
    assert sorted(imgs) == list(icons.ICO_SIZES) == [16, 24, 32, 48, 64, 128, 256]
    for s, im in imgs.items():
        assert im.size == (s, s) and im.mode == "RGBA"
        assert im.getpixel((2, s // 2))[:3] == INV
    assert [c[1] for c in calls] == list(icons.ICO_SIZES)
    assert icons.launcher_icon("x.exe", 32, loader=_loader()).getpixel((2, 16)) == INV + (255,)
    assert icons.launcher_icon("x.exe", 64, loader=_loader()).getpixel((32, round(64 * 0.3)))[:3] == WHITE


def test_write_launcher_ico_has_seven_sizes(tmp_path):
    p = icons.write_launcher_ico("x.exe", tmp_path / "sub" / "launcher.ico", loader=_loader())
    assert p == tmp_path / "sub" / "launcher.ico" and p.is_file()
    with Image.open(p) as ico:
        assert ico.format == "ICO"
        assert set(ico.info["sizes"]) == {(s, s) for s in icons.ICO_SIZES}
        for s in icons.ICO_SIZES:
            frame = ico.ico.getimage((s, s)).convert("RGBA")
            assert frame.size == (s, s)
            assert frame.getpixel((2, s // 2)) == INV + (255,)
            assert frame.getpixel((0, 0))[3] == 0


def test_write_ico_is_windows_standard(tmp_path):
    """The format Explorer draws: plane count 1, 32-bit, 256 as PNG, the rest as a double-height bitmap + AND mask (the blank-document icon problem)."""
    import struct
    imgs = icons.launcher_images("x.exe", loader=_loader())
    p = icons.write_ico(imgs, tmp_path / "a.ico")
    data = p.read_bytes()
    assert struct.unpack("<HHH", data[:6]) == (0, 1, len(icons.ICO_SIZES))
    for i, s in enumerate(icons.ICO_SIZES):
        w, h, _c, _r, planes, bpp, size, off = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        assert (w or 256, h or 256, planes, bpp) == (s, s, 1, 32)
        if s == 256:
            assert data[off + 1:off + 4] == b"PNG"
            continue
        hs, bw, bh, bpl, bbpp = struct.unpack("<IiiHH", data[off:off + 16])
        assert (hs, bw, bh, bpl, bbpp) == (40, s, 2 * s, 1, 32)
        assert size == 40 + s * s * 4 + ((s + 31) // 32) * 4 * s       # including the AND mask
    # Reading it back gives the same image (transparent corner kept)
    from PIL import Image as I
    with I.open(p) as im:
        im.size = (48, 48)
        back = im.convert("RGBA")
    assert back.getpixel((0, 0))[3] == imgs[48].getpixel((0, 0))[3]
    assert back.getpixel((24, 24)) == imgs[48].getpixel((24, 24))


def test_write_ico_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        icons.write_ico({}, tmp_path / "a.ico")


# ---------- server: inverted background + white V ----------

def test_server_icon_bg_and_white_text():
    src = _src(256)
    out = icons.render_server_icon(src)
    assert out.size == (256, 256) and out.mode == "RGBA"
    assert out.getchannel("A").tobytes() == src.getchannel("A").tobytes()   # shape (transparency) unchanged
    assert out.getpixel((4, 128)) == INV + (255,)                            # inverted background where there is no text
    assert out.getpixel((128, 4)) == INV + (255,)
    whites = _rows_with(out, WHITE)
    assert whites, "no white text pixels"
    # The source text is not erased but drawn fresh: every opaque pixel is between background and white (no black or other colors)
    px = out.load()
    for y in range(256):
        for x in range(256):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            t = (r - INV[0]) / (255 - INV[0])
            assert -0.02 <= t <= 1.02
            assert abs((g - INV[1]) / (255 - INV[1]) - t) < 0.03
            assert abs((b - INV[2]) / (255 - INV[2]) - t) < 0.03


def test_server_icon_is_one_big_centered_letter():
    out = icons.render_server_icon(_src(256))
    rows = sorted(_rows_with(out, WHITE, tol=20))
    cols = sorted(_cols_with(out, WHITE, tol=20))
    assert rows and cols
    h, w = max(rows) - min(rows) + 1, max(cols) - min(cols) + 1
    assert 256 * 0.62 <= h <= 256 * 0.72                       # about 70 % of the shape height
    assert w <= 256 * 0.80
    assert abs((min(rows) + max(rows)) / 2 - 128) <= 3 and abs((min(cols) + max(cols)) / 2 - 128) <= 6   # centered
    assert icons.SERVER_TEXT == "V"

def test_server_images_all_sizes():
    imgs = icons.server_images("x.exe", loader=_loader())
    assert sorted(imgs) == list(icons.ICO_SIZES)
    for s, im in imgs.items():
        assert im.size == (s, s)
        assert im.getpixel((1, s // 2))[:3] == INV
        # small sizes also have text (bright pixels)
        raw = im.tobytes()
        assert any(raw[i + 3] > 0 and raw[i] > 150 for i in range(0, len(raw), 4))
    assert icons.server_icon("x.exe", 64, loader=_loader()).size == (64, 64)


def test_server_icon_without_text_in_source_uses_default_layout():
    src = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(src).rounded_rectangle((0, 0, 63, 63), radius=12, fill=BG + (255,))
    out = icons.render_server_icon(src)
    rows = sorted(_rows_with(out, WHITE, tol=40))
    assert any(y < 32 for y in rows) and any(y >= 32 for y in rows)


# ---------- font fallback ----------

def test_font_candidates_default_order():
    names = [Path(p).name.lower() for p in icons.FONT_CANDIDATES]
    assert names == ["segoeuib.ttf", "seguisb.ttf", "segoeui.ttf"]


def test_pick_font_path_falls_back(tmp_path):
    second = tmp_path / "segoeui.ttf"
    second.write_bytes(b"x")
    missing = tmp_path / "seguisb.ttf"
    assert icons.pick_font_path([missing, second]) == str(second)
    assert icons.pick_font_path([missing]) is None


def test_server_icon_with_no_font_still_draws_white_text(tmp_path):
    out = icons.render_server_icon(_src(64), fonts=[tmp_path / "none.ttf"])
    assert _rows_with(out, WHITE, tol=40)


@pytest.mark.skipif(not Path(icons.FONT_CANDIDATES[1]).is_file(), reason="Segoe UI missing")
def test_server_icon_with_secondary_font(tmp_path):
    out = icons.render_server_icon(_src(128), fonts=[tmp_path / "none.ttf", icons.FONT_CANDIDATES[1]])
    assert _rows_with(out, WHITE)


# ---------- unreadable exe -> none ----------

def test_loader_none_gives_none(tmp_path):
    assert icons.launcher_images("x.exe", loader=_none_loader) is None
    assert icons.server_images("x.exe", loader=_none_loader) is None
    assert icons.launcher_icon("x.exe", loader=_none_loader) is None
    assert icons.server_icon("x.exe", loader=_none_loader) is None
    target = tmp_path / "launcher.ico"
    assert icons.write_launcher_ico("x.exe", target, loader=_none_loader) is None
    assert not target.exists()


def test_one_size_missing_gives_none():
    def partial(exe, size):
        return None if size == 48 else _src(size)
    assert icons.launcher_images("x.exe", loader=partial) is None


def test_load_exe_icon_missing_file(tmp_path):
    assert icons.load_exe_icon(tmp_path / "nope.exe", 32) is None
    assert icons.load_exe_icon(None, 32) is None
    assert icons.launcher_images(None) is None



# ---------- desktop shortcut: inverted background + white Vis/MCP ----------

def test_shortcut_icon_two_white_lines_on_inverted_bg():
    src = _src(256)
    out = icons.render_shortcut_icon(src)
    assert out.size == (256, 256)
    assert out.getchannel("A").tobytes() == src.getchannel("A").tobytes()
    assert out.getpixel((4, 128)) == INV + (255,)
    rows = sorted(_rows_with(out, WHITE, tol=20))
    top = [y for y in rows if y < 128]
    bottom = [y for y in rows if y >= 128]
    assert top and bottom                                    # two lines
    assert icons.SHORTCUT_TEXT == ("Vis", "MCP")
    assert not _rows_with(out, (0, 0, 0), tol=20)            # no black text (drawn fresh)


def test_shortcut_images_and_write(tmp_path):
    imgs = icons.shortcut_images("x.exe", loader=_loader())
    assert sorted(imgs) == list(icons.ICO_SIZES)
    p = icons.write_shortcut_ico("x.exe", tmp_path / "s.ico", loader=_loader())
    assert p == tmp_path / "s.ico" and p.is_file()
    assert icons.write_shortcut_ico("x.exe", tmp_path / "n.ico", loader=lambda e, s: None) is None
    assert not (tmp_path / "n.ico").exists()
