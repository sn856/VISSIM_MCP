"""MCP server core — tool list, response shape, error conversion, shutdown procedure, port check and command line (without VISSIM, using fake layers)."""
import os
import socket
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from vissim_mcp import server as sv
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.errors import KIND_COM_ERROR, KIND_UNSAVED_CHANGES, ToolError
from vissim_mcp.guard import Guard
from vissim_mcp.state import ServerState

ROOT = Path(__file__).resolve().parents[2]


def _cfg(tmp_path):
    return {"vissim": {"progid": "Vissim.Vissim.26"}, "port": 8766, "protected": [], "protected_roots": [],
            "log_root": str(tmp_path / "mcp"), "_path": str(tmp_path / "vissim_mcp.json"), "quit_prompt_timeout_s": 1}


class FakeSession:
    def __init__(self):
        self.calls = []
        self.unsaved_raise = False
        self.recovery_path = None

    def other_vissim_processes(self):
        return 0

    def require_network(self):
        return "x.inpx"

    def require_vissim(self):
        return object()

    def open_network(self, path, discard=False):
        self.calls.append(("open", path, discard))
        if self.unsaved_raise and not discard:
            raise ToolError(KIND_UNSAVED_CHANGES, "2 unsaved changes", {"unsaved_writes": 2})
        return {"path": path, "counts": {"Link": 1}}

    def save_network(self, path=None, force=False):
        self.calls.append(("save", path, force))
        return {"saved_path": path or "x.inpx", "backup_path": None, "writes_flushed": 0}

    def save_recovery_copy(self):
        self.calls.append(("recovery",))
        return {"recovery_path": self.recovery_path, "unsaved_writes": 2}

    def shutdown(self):
        self.calls.append(("shutdown",))
        return {"was_running": True}


class FakeObjects:
    def list_object_types(self):
        return {"types": []}

    def describe_attributes(self, type, parent=None):
        return {"type": type, "attributes": []}

    def list_objects(self, type, attrs=None, name_contains=None, where=None, limit=None, offset=0, parent=None):
        return {"total": 0, "rows": []}

    def get_attributes(self, type, no=None, attrs=None):
        return {"type": type, "no": no, "values": {}}

    def set_attributes(self, changes, stop_on_error=True):
        return {"ok": False, "applied": [0], "failed": [1], "skipped": []}

    def create_object(self, type, params=None, no=None, attrs=None):
        raise RuntimeError("fake explosion")

    def delete_object(self, type, no=None):
        return {"type": type, "no": no, "snapshot": {}}


class FakeSimulation:
    def configure_simulation(self, **kw):
        return {"applied": []}

    def configure_evaluation(self, **kw):
        return {"applied": []}

    def run_simulation(self, quick_mode=None, num_runs=None):
        return {"run_id": "r1", "period_s": 10, "chunk_s": 5}

    def stop_simulation(self):
        return {"state": "stopping"}


class FakeResults:
    def get_results(self, kind, nos=None, run="Current", interval="all", veh_class="All"):
        return {"kind": kind, "rows": []}

    def list_result_files(self):
        return {"files": []}

    def read_result_file(self, name, max_rows=1000, where=None):
        return {"columns": [], "rows": []}


class FakeView:
    def __init__(self):
        self.calls = []

    def set_view(self, layout=None, mode=None, zoom=None, camera=None):
        self.calls.append(("set_view", layout, mode, zoom, camera))
        return {"mode": mode or "3d"}

    def capture_view(self, mode="network", label=None, scale=1.0, settle_s=None):
        self.calls.append(("capture_view", mode, label, scale, settle_s))
        return {"path": "c.png", "mode": mode}


class NoCom:
    """Stand-in for the COM thread — the fake layers do not use COM."""
    def start(self):
        pass

    def stop(self, timeout=None):
        pass


def _runtime(tmp_path):
    cfg = _cfg(tmp_path)
    state = ServerState(cfg["port"])
    rt = sv.Runtime(cfg, state=state, com=NoCom(), changelog=ChangeLog(cfg["log_root"]), guard=Guard([]),
                    session=FakeSession(), objects=FakeObjects(), simulation=FakeSimulation(), results=FakeResults(),
                    view=FakeView())
    return rt


def _call(mcp, name, args=None):
    from mcp.client.client import Client

    async def go():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args or {})
            return res
    return anyio.run(go)


def _tool_names(mcp):
    from mcp.client.client import Client

    async def go():
        async with Client(mcp) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)
    return anyio.run(go)


# ---------------------------------------------------------------- tool list and response shape

def test_tool_list_matches_the_contract(tmp_path):
    mcp = sv.build_server(_runtime(tmp_path))
    assert _tool_names(mcp) == sorted(sv.TOOL_NAMES)
    assert len(sv.TOOL_NAMES) == 20


def test_vissim_status_response_has_ok_and_state(tmp_path):
    rt = _runtime(tmp_path)
    res = _call(sv.build_server(rt), "vissim_status")
    body = res.structured_content
    assert body["ok"] is True
    assert body["server"]["version"] == sv.__version__
    assert body["server"]["port"] == 8766
    assert body["other_vissim_processes"] == 0
    assert set(body["state"]) == {"vissim", "network", "simulation", "server_start_id"}
    assert set(body["state"]["network"]) == {"path", "unsaved_writes", "last_write_at", "counts"}


def test_layer_tool_error_becomes_ok_false_with_error_kind(tmp_path):
    rt = _runtime(tmp_path)
    rt.session.unsaved_raise = True
    mcp = sv.build_server(rt)
    body = _call(mcp, "open_network", {"path": "a.inpx"}).structured_content
    assert body["ok"] is False
    assert body["error"]["kind"] == KIND_UNSAVED_CHANGES
    assert body["error"]["detail"]["unsaved_writes"] == 2
    assert "state" in body
    body2 = _call(mcp, "open_network", {"path": "a.inpx", "discard": True}).structured_content
    assert body2["ok"] is True and body2["counts"] == {"Link": 1}
    assert rt.session.calls[-1] == ("open", "a.inpx", True)


def test_partial_failure_keeps_ok_false(tmp_path):
    body = _call(sv.build_server(_runtime(tmp_path)), "set_attributes",
                 {"changes": [{"type": "Link", "no": 1, "attr": "Name", "value": "a"}]}).structured_content
    assert body["ok"] is False and body["applied"] == [0] and body["failed"] == [1]


def test_view_tool_arguments_are_passed_to_the_layer_unchanged(tmp_path):
    # If a tool argument name collides with the name in _run(name, fn, **kw), MCP only returns "Error executing tool" (this happened in practice)
    rt = _runtime(tmp_path)
    mcp = sv.build_server(rt)
    body = _call(mcp, "capture_view", {"mode": "window", "label": "x1", "scale": 0.5, "settle_s": 2}).structured_content
    assert body["ok"] is True and body["path"] == "c.png"
    body = _call(mcp, "set_view", {"layout": "view", "camera": {"x": 1, "y": 2, "z": 3, "yaw": 90, "pitch": 30}})
    assert body.structured_content["ok"] is True
    assert rt.view.calls == [("capture_view", "window", "x1", 0.5, 2.0),
                             ("set_view", "view", None, None, {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 90.0, "pitch": 30.0})]


def test_unexpected_exception_is_reported_as_com_error_not_hidden(tmp_path):
    body = _call(sv.build_server(_runtime(tmp_path)), "create_object",
                 {"type": "VehicleInput", "params": {"link": 1}}).structured_content
    assert body["ok"] is False
    assert body["error"]["kind"] == KIND_COM_ERROR
    assert "unexpected error" in body["error"]["message"] and "fake explosion" in body["error"]["message"]


def test_arguments_are_passed_to_the_layer_unchanged(tmp_path):
    rt = _runtime(tmp_path)
    mcp = sv.build_server(rt)
    body = _call(mcp, "save_network", {"path": "b.inpx", "force": True}).structured_content
    assert body["ok"] is True and body["saved_path"] == "b.inpx"
    assert rt.session.calls[-1] == ("save", "b.inpx", True)
    body = _call(mcp, "get_results", {"kind": "travel_time", "run": "Avg", "interval": 3}).structured_content
    assert body["kind"] == "travel_time"
    body = _call(mcp, "get_change_log", {"limit": 5}).structured_content
    assert body["ok"] is True and body["entries"] == [] and body["path"].endswith("changes.jsonl")


# The shutdown procedure is tested in tests/vissim_mcp/test_mcp_quit.py


# ---------------------------------------------------------------- port, admin paths and command line

def test_port_in_use_is_detected():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        assert sv.port_free(port) is False
    assert sv.port_free(port) is True


def test_admin_paths_status_and_shutdown(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    rt = _runtime(tmp_path)
    quits = []
    monkeypatch.setattr(rt, "request_quit", lambda *a, **k: quits.append(1) or {"quit": True})
    mcp = sv.build_server(rt)
    with TestClient(mcp.streamable_http_app()) as c:
        r = c.get(sv.ADMIN_STATUS_PATH)
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["server"]["port"] == 8766
        r = c.post(sv.ADMIN_SHUTDOWN_PATH)
        assert r.status_code == 200 and r.json()["accepted"] is True
    import time
    for _ in range(50):
        if quits:
            break
        time.sleep(0.02)
    assert quits == [1]


def test_cli_help_exits_0():
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-m", "vissim_mcp", "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                       cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert "status" in r.stdout and "stop" in r.stdout and "--serve" in r.stdout
    assert "open" in r.stdout and "install" in r.stdout and "uninstall" in r.stdout


def test_cli_open_and_install_config_error_exits_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    for cmd in (["open", str(tmp_path / "x.inpx")], ["install"]):
        r = subprocess.run([sys.executable, "-m", "vissim_mcp", *cmd, "--config", str(bad)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
        assert r.returncode == 2 and "missing required keys" in r.stderr, (cmd, r.stderr)


def test_cli_path_argument_only_with_open(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-m", "vissim_mcp", "status", str(tmp_path / "x.inpx")], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
    assert r.returncode == 2 and "only used with open" in r.stderr


def test_cli_open_install_uninstall_wiring(monkeypatch, tmp_path):
    """open, install and uninstall call each module's entry function and return its exit code (no real install or open)."""
    import vissim_mcp.__main__ as cli
    import vissim_mcp.install as inst
    import vissim_mcp.launcher as lau
    calls: list = []
    monkeypatch.setattr(lau, "run_open", lambda cfg, path: calls.append(("open", path)) or 0)
    monkeypatch.setattr(inst, "install", lambda cfg: calls.append(("install",)) or 1)
    monkeypatch.setattr(inst, "uninstall", lambda cfg=None: calls.append(("uninstall",)) or 0)
    target = str(tmp_path / "a.inpx")
    assert cli.main(["open", target]) == 0
    assert cli.main(["open"]) == 0
    assert cli.main(["install"]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert cli.main(["uninstall", "--config", str(bad)]) == 0   # uninstall works even when the config is broken
    assert calls == [("open", target), ("open", None), ("install",), ("uninstall",)]


def test_cli_config_error_exits_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-m", "vissim_mcp", "status", "--config", str(bad)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
    assert r.returncode == 2 and "missing required keys" in r.stderr


def test_cli_status_and_stop_exit_1_without_a_server(tmp_path):
    cfg = tmp_path / "c.json"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:   # a port that is certainly free
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg.write_text('{"vissim": {"progid": "Vissim.Vissim.26"}, "port": %d, "protected": [], "protected_roots": []}'
                   % port, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    for cmd in ("status", "stop"):
        r = subprocess.run([sys.executable, "-m", "vissim_mcp", cmd, "--config", str(cfg)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
        assert r.returncode == 1 and "not running" in r.stderr


def test_cli_open_config_error_shows_a_message_box_only_without_a_console(monkeypatch, tmp_path):
    import vissim_mcp.__main__ as cli
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    shown: list = []
    monkeypatch.setattr(cli, "_notify_config_error", lambda msg: shown.append(msg))
    assert cli.main(["open", "--config", str(bad)]) == 2 and shown == []      # console present (test) -> no message box
    monkeypatch.setattr(cli.sys, "stderr", None)
    monkeypatch.setattr(cli.sys, "stdout", None)
    assert cli.main(["open", "--config", str(bad)]) == 2 and len(shown) == 1 and "missing required keys" in shown[0]
    assert cli.main(["install", "--config", str(bad)]) == 2 and len(shown) == 1   # open only
