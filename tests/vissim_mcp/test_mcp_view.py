"""View: copy of the view layout (ADR 0002), set_view call order, capture_view paths and argument checks (no VISSIM needed)."""
import struct
import zlib
from datetime import datetime
from pathlib import Path

import pytest

from vissim_mcp import layout_xml as ix
from vissim_mcp import changelog as cl
from vissim_mcp import comthread as ct
from vissim_mcp import state as st
from vissim_mcp import view as vw
from vissim_mcp.errors import ToolError


# Same minimal .layx as in test_layout_view (copied rather than imported; test modules do not import each other)
def _layout(no, bg="true", s3d="true", show_map="true"):
    return (
        f'\t\t<networkEditorLayout name="" no="{no}" synchronization="NONE">\r\n'
        f'\t\t\t<camera coord2DX="0" coord2DY="0" zoom2D="0"/>\r\n'
        f'\t\t\t<gParSet>\r\n'
        f'\t\t\t\t<backgroundGPars borderColor="ff000000" borderLineStyle="NOLINE" objectVisibility="{bg}"/>\r\n'
        f'\t\t\t\t<baseGPars autoLvlTransp="true" mapType="DEFAULTMAPPROVIDER" showGrid="true" showMap="{show_map}" showScale="true">\r\n'
        f'\t\t\t\t\t<static/>\r\n'
        f'\t\t\t\t</baseGPars>\r\n'
        f'\t\t\t\t<linkGPars objectVisibility="true"/>\r\n'
        f'\t\t\t\t<gPars3D fogMode="OFF" showBuildings="false" showLand="true" showMap="{show_map}" showShad="false"/>\r\n'
        f'\t\t\t\t<static3DModelGPars borderColor="a0ffffff" fillStyle="NOFILL" objectVisibility="{s3d}" static3DModelLabelVisib="false">\r\n'
        f'\t\t\t\t\t<static3DModelLabelGraphicParameters attr="NO"/>\r\n'
        f'\t\t\t\t</static3DModelGPars>\r\n'
        f'\t\t\t</gParSet>\r\n'
        f'\t\t</networkEditorLayout>\r\n'
    )


def _layx(*layouts):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\r\n'
        '<layout version="1400" vissimVersion="2026.00-09 [307071]">\r\n'
        '\t<currentWindowLayouts>\r\n\t\t<windowLayoutCurrent no="1"/>\r\n\t</currentWindowLayouts>\r\n'
        '\t<networkEditorLayouts>\r\n' + "".join(layouts) + '\t</networkEditorLayouts>\r\n'
        '</layout>\r\n'
    )


def _png(path, w=4, h=3):
    """Minimal PNG with just the right width and height."""
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeWindow:
    def __init__(self, calls, make_png=True):
        self.calls = calls
        self.make_png = make_png
        self.mode3d = 0

    def SetAttValue(self, name, val):
        self.calls.append(("SetAttValue", name, val))
        if name == "3D":
            self.mode3d = val

    def AttValue(self, name):
        return self.mode3d if name == "3D" else None

    def ZoomTo(self, *a):
        self.calls.append(("ZoomTo",) + a)

    def SetCameraPositionAndAngle(self, *a):
        self.calls.append(("SetCameraPositionAndAngle",) + a)

    def Screenshot(self, path, scale):
        self.calls.append(("Screenshot", path, scale))
        if self.make_png:
            _png(path, 8, 5)


class FakeVissim:
    def __init__(self, make_png=True):
        self.calls = []
        self.Graphics = type("G", (), {})()
        self.Graphics.CurrentNetworkWindow = FakeWindow(self.calls, make_png)

    def LoadLayout(self, path):
        self.calls.append(("LoadLayout", path))


@pytest.fixture
def com():
    t = ct.ComThread(retry_count=0, retry_interval_s=0.0)
    t.start()
    yield t
    t.stop()


@pytest.fixture
def net(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "network.inpx").write_text("<network/>", encoding="utf-8")
    (folder / "network.layx").write_bytes(_layx(_layout(1, "false", "false", "true")).encode("utf-8"))
    return folder / "network.inpx"


def _view(tmp_path, com, net, v, **kw):
    cfg = {"log_root": str(tmp_path / "mcp"), "capture_settle_s": 0.0}
    state = st.ServerState(8766)
    state.set_network(str(net), {})
    state.set_vissim(running=True, version="2026", launched_at="x", pid=kw.pop("pid", 4242))
    log = cl.ChangeLog(cfg["log_root"])
    sleeps = []
    view = vw.View(cfg, state, com, log, lambda: v, clock=lambda: datetime(2026, 10, 15, 4, 0, 0),
                   sleep=sleeps.append, **kw)
    return view, log, sleeps


# ---------------------------------------------------------------- view layout copy

def test_write_view_layout_turns_on_and_off_and_leaves_source_unchanged(tmp_path):
    src = tmp_path / "network.layx"
    text = _layx(_layout(1, "false", "false", "true"), _layout(2, "true", "false", "false"))
    src.write_bytes(text.encode("utf-8"))
    dst = tmp_path / "out" / "network_view.layx"
    info = ix.write_view_layout(src, dst)
    assert info == {"layouts": 2, "changed": 5, "path": str(dst)}
    assert src.read_bytes().decode("utf-8") == text
    want = _layx(_layout(1, "true", "true", "false"), _layout(2, "true", "true", "false"))
    assert dst.read_bytes().decode("utf-8") == want


def test_write_view_layout_rejects_same_file(tmp_path):
    src = tmp_path / "network.layx"
    src.write_bytes(_layx(_layout(1)).encode("utf-8"))
    with pytest.raises(ix.LayoutFormatError, match="different file"):
        ix.write_view_layout(src, src)


def test_write_view_layout_does_not_write_when_format_differs(tmp_path):
    src = tmp_path / "network.layx"
    src.write_bytes(_layx(_layout(1).replace(' showMap="true" showShad', ' showShad')).encode("utf-8"))
    dst = tmp_path / "v.layx"
    with pytest.raises(ix.LayoutFormatError, match="showMap"):
        ix.write_view_layout(src, dst)
    assert not dst.exists()


# ---------------------------------------------------------------- set_view

def test_set_view_creates_and_loads_view_layout_and_logs(tmp_path, com, net):
    v = FakeVissim()
    view, log, _ = _view(tmp_path, com, net, v)
    out = view.set_view(layout="view")
    dst = (tmp_path / "mcp" / "layouts" / "network_view.layx").resolve()
    assert v.calls == [("LoadLayout", str(dst))]
    assert out["layout"]["path"] == str(dst) and out["layout"]["changed"] == 4
    assert 'objectVisibility="true"' in dst.read_text(encoding="utf-8")
    assert 'objectVisibility="false"' in (net.parent / "network.layx").read_text(encoding="utf-8")   # original unchanged
    rows = log.read()
    assert rows[-1]["kind"] == "view" and rows[-1]["layout_path"] == str(dst)


def test_set_view_network_uses_original_layx(tmp_path, com, net):
    v = FakeVissim()
    view, _, _ = _view(tmp_path, com, net, v)
    view.set_view(layout="network")
    assert v.calls == [("LoadLayout", str(net.parent / "network.layx"))]
    assert not (tmp_path / "mcp" / "layouts").exists()


def test_set_view_missing_layx_is_not_found(tmp_path, com, net):
    (net.parent / "network.layx").unlink()
    view, _, _ = _view(tmp_path, com, net, FakeVissim())
    with pytest.raises(ToolError) as ei:
        view.set_view(layout="view")
    assert ei.value.kind == "not_found"


def test_set_view_camera_turns_on_3d_and_passes_args_in_order(tmp_path, com, net):
    v = FakeVissim()
    view, _, _ = _view(tmp_path, com, net, v)
    out = view.set_view(camera={"x": 1, "y": 2, "z": 300, "yaw": 90, "pitch": 35})
    assert v.calls == [("SetAttValue", "3D", 1), ("SetCameraPositionAndAngle", 1.0, 2.0, 300.0, 90.0, 35.0)]
    assert out["mode"] == "3d"


def test_set_view_zoom_is_2d(tmp_path, com, net):
    v = FakeVissim()
    view, _, _ = _view(tmp_path, com, net, v)
    out = view.set_view(zoom=[0, 0, 100, 50])
    assert v.calls == [("SetAttValue", "3D", 0), ("ZoomTo", 0.0, 0.0, 100.0, 50.0)]
    assert out["mode"] == "2d"


@pytest.mark.parametrize("kw", [
    {},
    {"layout": "other"},
    {"mode": "4d"},
    {"zoom": [0, 0, 1]},
    {"zoom": [5, 0, 1, 1]},
    {"camera": {"x": 1}},
    {"zoom": [0, 0, 1, 1], "camera": {"x": 0, "y": 0, "z": 1, "yaw": 0, "pitch": 0}},
    {"mode": "2d", "camera": {"x": 0, "y": 0, "z": 1, "yaw": 0, "pitch": 0}},
    {"mode": "3d", "zoom": [0, 0, 1, 1]},
])
def test_set_view_bad_args_fail_before_com(tmp_path, com, net, kw):
    v = FakeVissim()
    view, _, _ = _view(tmp_path, com, net, v)
    with pytest.raises(ToolError) as ei:
        view.set_view(**kw)
    assert ei.value.kind == "bad_args" and v.calls == []


# ---------------------------------------------------------------- capture_view

def test_capture_network_uses_com_screenshot(tmp_path, com, net):
    v = FakeVissim()
    view, _, sleeps = _view(tmp_path, com, net, v)
    out = view.capture_view(mode="network", label="corridor", scale=0.5, settle_s=1.5)
    want = (tmp_path / "mcp" / "captures" / "20261015-040000_corridor.png").resolve()
    assert out["path"] == str(want) and out["size"] == [8, 5] and out["warnings"] == []
    assert v.calls == [("Screenshot", str(want), 0.5)]
    assert sleeps == [1.5]
    out2 = view.capture_view(mode="network", label="corridor")
    assert out2["path"].endswith("20261015-040000_corridor_2.png")


def test_capture_network_missing_file_is_com_error(tmp_path, com, net):
    view, _, _ = _view(tmp_path, com, net, FakeVissim(make_png=False))
    with pytest.raises(ToolError) as ei:
        view.capture_view(mode="network")
    assert ei.value.kind == "com_error"


def test_capture_window_uses_server_pid_window(tmp_path, com, net):
    got = []

    def capturer(pid, path):
        got.append((pid, path))
        return {"size": [2576, 1408], "restored": True, "colors": 1}
    v = FakeVissim()
    view, _, _ = _view(tmp_path, com, net, v, window_capturer=capturer)
    out = view.capture_view(mode="window")
    assert got == [(4242, out["path"])] and v.calls == []
    assert out["size"] == [2576, 1408] and out["pid"] == 4242
    assert len(out["warnings"]) == 2   # restored from minimized + single color


def test_capture_window_unknown_pid_is_not_found(tmp_path, com, net):
    view, _, _ = _view(tmp_path, com, net, FakeVissim(), pid=None, window_capturer=lambda p, q: {})
    with pytest.raises(ToolError) as ei:
        view.capture_view(mode="window")
    assert ei.value.kind == "not_found"


def test_capture_window_no_window_is_not_found(tmp_path, com, net):
    def capturer(pid, path):
        raise LookupError("no window")
    view, _, _ = _view(tmp_path, com, net, FakeVissim(), window_capturer=capturer)
    with pytest.raises(ToolError) as ei:
        view.capture_view(mode="window")
    assert ei.value.kind == "not_found"


@pytest.mark.parametrize("kw", [{"mode": "screen"}, {"label": "../x"}, {"label": ""}, {"scale": 0}, {"settle_s": 99}])
def test_capture_bad_args_are_bad_args(tmp_path, com, net, kw):
    v = FakeVissim()
    view, _, _ = _view(tmp_path, com, net, v)
    with pytest.raises(ToolError) as ei:
        view.capture_view(**kw)
    assert ei.value.kind == "bad_args" and v.calls == []


def test_missing_network_passes_vissim_getter_error_through(tmp_path, com, net):
    def getter():
        raise ToolError("no_network", "No network is open")
    cfg = {"log_root": str(tmp_path / "mcp"), "capture_settle_s": 0.0}
    view = vw.View(cfg, st.ServerState(8766), com, cl.ChangeLog(cfg["log_root"]), getter, sleep=lambda s: None)
    with pytest.raises(ToolError) as ei:
        view.capture_view()
    assert ei.value.kind == "no_network"
