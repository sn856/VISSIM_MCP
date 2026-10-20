"""Simulation and evaluation settings: checks the chunked run loop, stop, failure and state transitions on a fake COM (no VISSIM needed)."""
import threading

import pytest

from vissim_mcp import comthread as ct
from vissim_mcp import simulation as sm
from vissim_mcp import state as st
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.config import TUNABLE_DEFAULTS
from vissim_mcp.errors import ToolError


class FakeComError(Exception):
    """Mimics an error raised by VISSIM COM (the message must be passed through unchanged)."""


class FakeSimulation:
    def __init__(self, period=3600, fail_at_chunk=None, on_chunk=None, block=None, has_simsec=True):
        self.attrs = {"SimPeriod": period, "SimRes": 10, "RandSeed": 42, "NumRuns": 1, "RandSeedIncr": 1,
                      "StartTm": 0, "SimBreakAt": 0, "UseMaxSimSpeed": False, "SimSec": 0}
        self.has_simsec = has_simsec
        self.fail_at_chunk = fail_at_chunk
        self.on_chunk = on_chunk
        self.block = block
        self.breaks = []
        self.run_calls = 0
        self.stop_calls = 0

    def AttValue(self, name):
        if name == "SimSec" and not self.has_simsec:
            raise FakeComError("AttValue failed: no SimSec")
        if name not in self.attrs:
            raise FakeComError(f"AttValue failed: unknown attribute {name}")
        return self.attrs[name]

    def SetAttValue(self, name, value):
        if name not in self.attrs:
            raise FakeComError(f"SetAttValue failed: unknown attribute {name}")
        self.attrs[name] = value

    def RunContinuous(self):
        self.run_calls += 1
        if self.block is not None:
            self.block.wait()
        if self.fail_at_chunk == self.run_calls:
            raise FakeComError("RunContinuous failed: fake chunk error")
        self.breaks.append(self.attrs["SimBreakAt"])
        self.attrs["SimSec"] = self.attrs["SimBreakAt"]
        if self.on_chunk is not None:
            self.on_chunk(self.run_calls)

    def Stop(self):
        self.stop_calls += 1


class FakeWindow:
    def __init__(self):
        self.attrs = {"QuickMode": 0}
        self.history = []   # order of QuickMode writes (1 while running -> restored at the end)

    def SetAttValue(self, name, value):
        self.attrs[name] = value
        if name == "QuickMode":
            self.history.append(value)

    def AttValue(self, name):
        return self.attrs[name]


class FakeEvaluation:
    def __init__(self):
        self.attrs = {"DataCollCollectData": False, "DataCollWriteFile": False, "DataCollInterval": 900,
                      "DataCollFromTime": 0, "DataCollToTime": 99999,
                      "VehTravTmsCollectData": False, "VehTravTmsWriteFile": False, "VehTravTmsInterval": 900,
                      "VehTravTmsFromTime": 0, "VehTravTmsToTime": 99999,
                      "QueuesCollectData": False, "QueuesWriteFile": False, "QueuesInterval": 900,
                      "QueuesFromTime": 0, "QueuesToTime": 99999,
                      "VehNetPerfCollectData": False, "VehNetPerfWriteFile": False, "VehNetPerfInterval": 900,
                      "VehNetPerfFromTime": 0, "VehNetPerfToTime": 99999}
        for prefix in ("ParkLots", "ParkLotGrps", "ParkRoutDecs", "ParkSpcs"):
            self.attrs.update({f"{prefix}CollectData": False, f"{prefix}Interval": 99999,
                               f"{prefix}FromTime": 0, f"{prefix}ToTime": 99999})

    def AttValue(self, name):
        if name not in self.attrs:
            raise FakeComError(f"AttValue failed: unknown attribute {name}")
        return self.attrs[name]

    def SetAttValue(self, name, value):
        if name not in self.attrs:
            raise FakeComError(f"SetAttValue failed: unknown attribute {name}")
        self.attrs[name] = value


class FakeVissim:
    def __init__(self, **kw):
        self.Simulation = FakeSimulation(**kw)
        self.Evaluation = FakeEvaluation()
        self.Graphics = type("G", (), {})()
        self.Graphics.CurrentNetworkWindow = FakeWindow()
        self.Net = type("N", (), {})()
        self.Net.SimulationRuns = type("R", (), {"Count": 1})()


class RecordingState(st.ServerState):
    """State that records every set_simulation call (for the monotonic-progress check)."""

    def __init__(self, port=8766):
        super().__init__(port)
        self.calls = []

    def set_simulation(self, **fields):
        self.calls.append(dict(fields))
        super().set_simulation(**fields)


@pytest.fixture
def com():
    t = ct.ComThread(retry_count=0, retry_interval_s=0.0)
    t.start()
    yield t
    t.stop()


@pytest.fixture
def cfg():
    c = dict(TUNABLE_DEFAULTS)
    c["sim_chunk_s"] = 60
    return c


def _make(cfg, com, tmp_path, vissim, state=None):
    state = state or RecordingState()
    state.set_network("C:/runs/sample/network.inpx")
    log = ChangeLog(tmp_path / "mcp")
    sim = sm.Simulation(cfg, state, com, log, lambda: vissim)
    return sim, state, log


def test_chunked_run_loop_3600s_in_60s_chunks(cfg, com, tmp_path):
    v = FakeVissim(period=3600)
    sim, state, log = _make(cfg, com, tmp_path, v)
    out = sim.run_simulation()
    assert set(out) >= {"run_id", "period_s", "chunk_s"}
    assert out["period_s"] == 3600 and out["chunk_s"] == 60
    assert sim.wait(timeout=10)
    assert v.Simulation.run_calls == 60
    assert v.Simulation.breaks == [60 * i for i in range(1, 61)]
    assert v.Simulation.breaks[-1] == 3600
    assert v.Simulation.stop_calls == 0
    progress = [c["progress_s"] for c in state.calls if "progress_s" in c and c["progress_s"] is not None]
    assert progress == sorted(progress) and progress[-1] == 3600
    full = state.full()["simulation"]
    assert full["state"] == st.SIM_FINISHED and full["run_id"] == out["run_id"]
    assert full["progress_s"] == 3600 and full["period_s"] == 3600 and full["error"] is None
    assert full["run_index"] == 1
    # runs with the quick-mode default (True), restores the pre-run value (0) at the end; max speed
    assert v.Graphics.CurrentNetworkWindow.history[:1] == [1]
    assert v.Graphics.CurrentNetworkWindow.attrs["QuickMode"] == 0
    assert v.Simulation.attrs["UseMaxSimSpeed"] is True
    # log
    rows = log.read()
    assert len(rows) == 1
    r = rows[0]
    assert r["tool"] == "run_simulation" and r["kind"] == "run_simulation" and r["ok"] is True
    assert r["network_path"] == "C:/runs/sample/network.inpx"
    assert r["result_state"] == st.SIM_FINISHED and r["run_id"] == out["run_id"]
    assert "elapsed_s" in r and r["params"]["quick_mode"] is True
    assert r["server_start_id"] == state.server_start_id


def test_stop_request_calls_stop_once_at_the_next_boundary_then_idle(cfg, com, tmp_path):
    holder = {}

    def on_chunk(n):
        if n == 1:
            # stop request after the first chunk (same as a request at ~30 s being handled at the boundary)
            holder["resp"] = holder["sim"].stop_simulation()

    v = FakeVissim(period=3600, on_chunk=on_chunk)
    sim, state, log = _make(cfg, com, tmp_path, v)
    holder["sim"] = sim
    sim.run_simulation()
    assert sim.wait(timeout=10)
    assert holder["resp"]["state"] == st.SIM_STOPPING
    assert v.Simulation.stop_calls == 1
    assert v.Simulation.run_calls == 1
    full = state.full()["simulation"]
    assert full["state"] == st.SIM_IDLE and full["progress_s"] == 60
    r = log.read()[-1]
    assert r["result_state"] == st.SIM_IDLE and r["ok"] is True and r["stopped"] is True


def test_run_and_configure_give_sim_running_while_running(cfg, com, tmp_path):
    gate = threading.Event()
    v = FakeVissim(period=120, block=gate)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation()
    try:
        assert state.full()["simulation"]["state"] == st.SIM_RUNNING
        with pytest.raises(ToolError) as ei:
            sim.run_simulation()
        assert ei.value.kind == "sim_running"
        with pytest.raises(ToolError) as ei:
            sim.configure_simulation(period_s=10)
        assert ei.value.kind == "sim_running"
        with pytest.raises(ToolError) as ei:
            sim.configure_evaluation(travel_times={"collect": True})
        assert ei.value.kind == "sim_running"
    finally:
        gate.set()
    assert sim.wait(timeout=10)
    assert state.full()["simulation"]["state"] == st.SIM_FINISHED


def test_run_simulation_gives_sim_running_while_stopping_too(cfg, com, tmp_path):
    gate = threading.Event()
    v = FakeVissim(period=120, block=gate)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation()
    try:
        sim.stop_simulation()
        assert state.full()["simulation"]["state"] == st.SIM_STOPPING
        with pytest.raises(ToolError) as ei:
            sim.run_simulation()
        assert ei.value.kind == "sim_running"
    finally:
        gate.set()
    assert sim.wait(timeout=10)
    assert state.full()["simulation"]["state"] == st.SIM_IDLE


def test_exception_in_a_chunk_gives_failed_and_records_the_text(cfg, com, tmp_path):
    v = FakeVissim(period=300, fail_at_chunk=3)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation()
    assert sim.wait(timeout=10)
    full = state.full()["simulation"]
    assert full["state"] == st.SIM_FAILED
    assert "fake chunk error" in full["error"]
    assert full["progress_s"] == 120     # two chunks completed
    r = log.read()[-1]
    assert r["ok"] is False and "fake chunk error" in r["error"] and r["result_state"] == st.SIM_FAILED
    # can be run again after a failure
    v2 = FakeVissim(period=60)
    sim2, state2, _ = _make(cfg, com, tmp_path, v2, state=state)
    sim2.run_simulation()
    assert sim2.wait(timeout=10)
    assert state.full()["simulation"]["state"] == st.SIM_FINISHED


def test_quick_mode_off_per_call_and_num_runs(cfg, com, tmp_path):
    v = FakeVissim(period=60)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation(quick_mode=False, num_runs=3)
    assert sim.wait(timeout=10)
    assert v.Graphics.CurrentNetworkWindow.attrs["QuickMode"] == 0
    assert v.Simulation.attrs["NumRuns"] == 3
    assert state.full()["simulation"]["num_runs"] == 3
    assert log.read()[-1]["params"] == {"quick_mode": False, "num_runs": 3}


def test_quick_mode_config_default_false(cfg, com, tmp_path):
    cfg["quick_mode_default"] = False
    v = FakeVissim(period=60)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation()
    assert sim.wait(timeout=10)
    assert v.Graphics.CurrentNetworkWindow.attrs["QuickMode"] == 0


def test_without_simsec_progress_uses_the_computed_value(cfg, com, tmp_path):
    v = FakeVissim(period=180, has_simsec=False)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation()
    assert sim.wait(timeout=10)
    assert state.full()["simulation"]["state"] == st.SIM_FINISHED
    assert state.full()["simulation"]["progress_s"] == 180
    assert v.Simulation.run_calls == 3


def test_chunk_longer_than_period_finishes_in_one_go(cfg, com, tmp_path):
    v = FakeVissim(period=45)
    sim, state, log = _make(cfg, com, tmp_path, v)
    sim.run_simulation()
    assert sim.wait(timeout=10)
    assert v.Simulation.breaks == [45]


def test_no_network_when_nothing_open(cfg, com, tmp_path):
    def getter():
        raise ToolError("no_network", "No network is open")

    state = RecordingState()
    sim = sm.Simulation(cfg, state, com, ChangeLog(tmp_path / "mcp"), getter)
    with pytest.raises(ToolError) as ei:
        sim.run_simulation()
    assert ei.value.kind == "no_network"
    assert state.full()["simulation"]["state"] == st.SIM_IDLE


def test_stop_when_not_running_gives_bad_args(cfg, com, tmp_path):
    sim, state, log = _make(cfg, com, tmp_path, FakeVissim())
    with pytest.raises(ToolError) as ei:
        sim.stop_simulation()
    assert ei.value.kind == "bad_args" and "not running" in ei.value.message


def test_status_returns_state_simulation_as_is_without_com_calls(cfg, com, tmp_path):
    calls = {"n": 0}

    def getter():
        calls["n"] += 1
        return FakeVissim()

    state = RecordingState()
    sim = sm.Simulation(cfg, state, com, ChangeLog(tmp_path / "mcp"), getter)
    state.set_simulation(state=st.SIM_FINISHED, run_id="r9", progress_s=3600, period_s=3600)
    assert sim.status() == state.full()["simulation"]
    assert calls["n"] == 0


def test_configure_simulation_changes_only_given_values_with_before_and_after(cfg, com, tmp_path):
    v = FakeVissim()
    sim, state, log = _make(cfg, com, tmp_path, v)
    out = sim.configure_simulation(period_s=7200, random_seed=7, start_time=300)
    assert set(out["changed"]) == {"period_s", "random_seed", "start_time"}
    assert out["changed"]["period_s"] == {"attr": "SimPeriod", "before": 3600, "after": 7200}
    assert out["changed"]["random_seed"] == {"attr": "RandSeed", "before": 42, "after": 7}
    assert out["changed"]["start_time"] == {"attr": "StartTm", "before": 0, "after": 300}
    assert v.Simulation.attrs["SimRes"] == 10 and v.Simulation.attrs["NumRuns"] == 1
    # attribute ID mapping constant
    assert sm.SIM_ATTRS == {"period_s": "SimPeriod", "resolution": "SimRes", "random_seed": "RandSeed",
                            "num_runs": "NumRuns", "seed_increment": "RandSeedIncr", "start_time": "StartTm"}
    # it is a write, so it counts as an unsaved change + a set entry
    assert state.full()["network"]["unsaved_writes"] == 3
    rows = log.read()
    assert len(rows) == 3 and {r["attr"] for r in rows} == {"SimPeriod", "RandSeed", "StartTm"}
    assert all(r["kind"] == "set" and r["type"] == "Simulation" for r in rows)


def test_configure_simulation_with_nothing_given_changes_nothing(cfg, com, tmp_path):
    sim, state, log = _make(cfg, com, tmp_path, FakeVissim())
    assert sim.configure_simulation()["changed"] == {}
    assert state.full()["network"]["unsaved_writes"] == 0


def test_configure_simulation_com_rejection_is_com_error_with_text_unchanged(cfg, com, tmp_path):
    v = FakeVissim()
    del v.Simulation.attrs["RandSeedIncr"]
    sim, state, log = _make(cfg, com, tmp_path, v)
    with pytest.raises(ToolError) as ei:
        sim.configure_simulation(seed_increment=2)
    assert ei.value.kind == "com_error" and "RandSeedIncr" in ei.value.message


def test_configure_evaluation_gives_per_group_before_and_after_and_write_file_warning(cfg, com, tmp_path):
    v = FakeVissim()
    sim, state, log = _make(cfg, com, tmp_path, v)
    out = sim.configure_evaluation(travel_times={"collect": True, "write_file": True, "interval_s": 600},
                                   queues={"collect": True})
    assert out["attr_ids_verified"] is True
    assert "collect" in out["note"]
    tt = out["changed"]["travel_times"]
    assert tt["collect"] == {"attr": "VehTravTmsCollectData", "before": False, "after": True}
    assert "write_file" not in tt                      # 2026 has no per-group file-writing attribute -> warning only
    assert any("write_file" in w for w in out["warnings"])
    assert tt["interval_s"] == {"attr": "VehTravTmsInterval", "before": 900, "after": 600}
    assert out["changed"]["queues"] == {"collect": {"attr": "QueuesCollectData", "before": False, "after": True}}
    assert "data_collection" not in out["changed"]
    assert v.Evaluation.attrs["DataCollCollectData"] is False
    assert state.full()["network"]["unsaved_writes"] == 3   # write_file has no attribute, so nothing is written


def test_configure_evaluation_parking_groups(cfg, com, tmp_path):
    v = FakeVissim()
    sim, state, log = _make(cfg, com, tmp_path, v)
    out = sim.configure_evaluation(parking_lots={"collect": True, "interval_s": 900},
                                   parking_decisions={"collect": True}, parking_spaces={"collect": True})
    assert out["changed"]["parking_lots"] == {
        "collect": {"attr": "ParkLotsCollectData", "before": False, "after": True},
        "interval_s": {"attr": "ParkLotsInterval", "before": 99999, "after": 900}}
    assert out["changed"]["parking_decisions"]["collect"]["attr"] == "ParkRoutDecsCollectData"
    assert out["changed"]["parking_spaces"]["collect"]["attr"] == "ParkSpcsCollectData"
    assert "parking_lot_groups" not in out["changed"] and v.Evaluation.attrs["ParkLotGrpsCollectData"] is False
    assert out["attr_ids_verified"] is True


def test_eval_attrs_mapping_constant_in_one_place():
    assert set(sm.EVAL_ATTRS) == {"data_collection", "travel_times", "queues", "network_performance",
                                  "parking_lots", "parking_lot_groups", "parking_decisions", "parking_spaces"}
    for group, m in sm.EVAL_ATTRS.items():
        assert set(m) == {"collect", "interval_s", "from_s", "to_s"}, group
    assert sm.EVAL_ATTRS["data_collection"]["collect"] == "DataCollCollectData"
    assert sm.EVAL_ATTRS["network_performance"]["interval_s"] == "VehNetPerfInterval"
    assert sm.EVAL_ATTRS_VERIFIED is True


def test_configure_evaluation_unknown_key_gives_bad_args(cfg, com, tmp_path):
    sim, state, log = _make(cfg, com, tmp_path, FakeVissim())
    with pytest.raises(ToolError) as ei:
        sim.configure_evaluation(travel_times={"colect": True})
    assert ei.value.kind == "bad_args"
    with pytest.raises(ToolError) as ei:
        sim.configure_evaluation(travel_times=True)
    assert ei.value.kind == "bad_args"
