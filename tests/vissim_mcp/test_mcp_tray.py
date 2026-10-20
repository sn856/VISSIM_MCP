"""Tray: menu labels, the server icon and the fallback icon (pystray is not started)."""
import logging

from PIL import Image

from vissim_mcp import tray as tr


def _tray(**kw):
    return tr.Tray(lambda: {}, "logs", lambda: {"quit": True}, **kw)


def test_quit_menu_label():
    assert tr.MENU_QUIT == "Quit server and VISSIM"
    labels = [label for label, _, _ in _tray().menu_items()]
    assert labels == ["Show status", "Open log folder", "Quit server and VISSIM"]
    assert "Quit" not in labels


def test_tray_icon_is_the_server_icon():
    seen = []
    img = Image.new("RGBA", (64, 64), (41, 172, 44, 255))

    def factory(exe, size=64):
        seen.append((exe, size))
        return img
    assert tr.tray_image("C:\\PTV\\Vissim260.exe", icon_factory=factory) is img
    assert seen == [("C:\\PTV\\Vissim260.exe", 64)]


def test_unreadable_exe_icon_falls_back_to_green_v_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="vissim_mcp"):
        img = tr.tray_image("C:\\missing\\Vissim260.exe", icon_factory=lambda exe, size=64: None)
    assert img.size == (64, 64) and img.tobytes() == tr.make_icon_image().tobytes()
    assert any("icon" in r.getMessage() for r in caplog.records)


def test_icon_factory_exception_also_falls_back_to_green_v_with_warning(caplog):
    def boom(exe, size=64):
        raise OSError("broken exe")
    with caplog.at_level(logging.WARNING, logger="vissim_mcp"):
        img = tr.tray_image("x.exe", icon_factory=boom)
    assert img.tobytes() == tr.make_icon_image().tobytes()
    assert any("broken exe" in r.getMessage() for r in caplog.records)


def test_no_exe_path_gives_green_v(caplog):
    with caplog.at_level(logging.WARNING, logger="vissim_mcp"):
        img = tr.tray_image(None, icon_factory=lambda exe, size=64: _fail())
    assert img.tobytes() == tr.make_icon_image().tobytes()


def _fail():
    raise AssertionError("no icon is built without an exe")


def test_tray_builds_icon_from_exe_path():
    seen = []
    t = _tray(exe_path="C:\\PTV\\Vissim260.exe", icon_factory=lambda exe, size=64: seen.append(exe) or None)
    img = t.icon_image()
    assert seen == ["C:\\PTV\\Vissim260.exe"] and img.size == (64, 64)


def test_native_icon_handle_draws_and_loads_at_slot_size():
    from PIL import Image
    seen = {}

    def factory(exe, size):
        seen["size"] = size
        return Image.new("RGBA", (size, size), (41, 172, 44, 255))

    def loader(path, px):
        from PIL import Image as I
        with I.open(path) as im:
            seen["ico_sizes"] = sorted(im.info.get("sizes") or [im.size])
        seen["px"] = px
        return 1234

    assert tr.native_icon_handle("x.exe", icon_factory=factory, px=20, loader=loader) == 1234
    assert seen["size"] == 20 and seen["px"] == 20 and seen["ico_sizes"] == [(20, 20)]


def test_native_icon_handle_failure_is_none():
    assert tr.native_icon_handle(None, px=16) is None
    assert tr.native_icon_handle("x.exe", icon_factory=lambda e, size: None, px=16) is None

    def boom(e, size):
        raise RuntimeError("x")
    assert tr.native_icon_handle("x.exe", icon_factory=boom, px=16) is None
    from PIL import Image
    ok = lambda e, size: Image.new("RGBA", (size, size), (0, 0, 0, 255))   # noqa: E731
    assert tr.native_icon_handle("x.exe", icon_factory=ok, px=16, loader=lambda p, px: 0) is None
