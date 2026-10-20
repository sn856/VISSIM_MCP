"""Behaviours added in the final review — open/save refused while running, PID not determinable, VISSIM loss detected from other layers' COM errors, and recording post-processing failures after creation."""
import pytest

from vissim_mcp import server as sv
from vissim_mcp import session as ss
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.comthread import ComThread
from vissim_mcp.errors import KIND_COM_ERROR, KIND_SIM_RUNNING, KIND_VISSIM_ABSENT, ToolError
from vissim_mcp.guard import Guard
from vissim_mcp.state import SIM_RUNNING, ServerState


class FakeV:
    """Imitates a live VISSIM — it only answers Version."""
    def __init__(self):
        self.Net = type("Net", (), {"Links": type("L", (), {"Count": 0})()})()

    def AttValue(self, k):
        return "PTV Vissim 2026 (SP 09)"


def _session(tmp_path, pids):
    cfg = {"vissim": {"progid": "X"}, "port": 1, "protected": [], "protected_roots": [], "log_root": str(tmp_path / "mcp")}
    state = ServerState(1)
    com = ComThread(retry_count=0, retry_interval_s=0)
    com.start()
    s = ss.Session(cfg, state, com, ChangeLog(cfg["log_root"]), process_lister=lambda: pids(), launcher=lambda p: (FakeV(), "fake"))
    return s, state, com


def test_undeterminable_pid_is_accepted_as_none_with_only_a_warning(tmp_path):
    s, state, com = _session(tmp_path, lambda: None)
    try:
        out = s.ensure_vissim()
        assert out["launched"] is True and out["pid"] is None
        assert any("tasklist" in w for w in out["warnings"])
        assert s.other_vissim_processes() is None
        assert s._detect_lost("t") is None            # without a PID, decide by the COM ping only -> alive
    finally:
        com.stop()


def test_open_and_save_refused_while_running(tmp_path):
    s, state, com = _session(tmp_path, lambda: [])
    try:
        s.ensure_vissim()
        state.set_network("x.inpx", {})
        state.set_simulation(state=SIM_RUNNING)
        with pytest.raises(ToolError) as ei:
            s.open_network(str(tmp_path / "other.inpx"))
        assert ei.value.kind == KIND_SIM_RUNNING
        with pytest.raises(ToolError) as ei:
            s.save_network()
        assert ei.value.kind == KIND_SIM_RUNNING
    finally:
        com.stop()


class LostSession:
    def __init__(self, lost):
        self._lost = lost
        self.asked = []

    def other_vissim_processes(self):
        return 0

    def _detect_lost(self, tool):
        self.asked.append(tool)
        return self._lost


class BoomObjects:
    def get_attributes(self, type, no=None, attrs=None):
        raise ToolError(KIND_COM_ERROR, "The RPC server is unavailable")


class NoCom:
    def start(self):
        pass

    def stop(self, timeout=None):
        pass


def _runtime(tmp_path, lost):
    cfg = {"vissim": {"progid": "X"}, "port": 1, "protected": [], "protected_roots": [], "log_root": str(tmp_path / "mcp")}
    return sv.Runtime(cfg, state=ServerState(1), com=NoCom(), changelog=ChangeLog(cfg["log_root"]), guard=Guard([]),
                      session=LostSession(lost), objects=BoomObjects(), simulation=object(), results=object())


def test_com_error_from_another_layer_caused_by_loss_becomes_vissim_absent(tmp_path):
    rt = _runtime(tmp_path, lost=3)
    out = rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert out["ok"] is False and out["error"]["kind"] == KIND_VISSIM_ABSENT
    assert out["error"]["detail"]["lost_writes"] == 3 and rt.session.asked == ["get_attributes"]


def test_com_error_from_another_layer_not_caused_by_loss_is_unchanged(tmp_path):
    rt = _runtime(tmp_path, lost=None)
    out = rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert out["ok"] is False and out["error"]["kind"] == KIND_COM_ERROR and "RPC" in out["error"]["message"]
