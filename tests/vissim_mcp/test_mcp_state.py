"""Shared state — the thread-safe server-wide state and the snapshot keys used in responses."""
import threading

import pytest

from vissim_mcp import state as st


@pytest.fixture
def s():
    return st.ServerState(port=8766)


def test_snapshot_keys_match_the_contract(s):
    snap = s.snapshot()
    assert set(snap) == {"vissim", "network", "simulation", "server_start_id"}
    assert set(snap["vissim"]) == {"running", "version"}
    assert set(snap["network"]) == {"path", "unsaved_writes", "last_write_at", "counts"}
    assert set(snap["simulation"]) == {"state", "run_id", "progress_s"}


def test_initial_values(s):
    snap = s.snapshot()
    assert snap["vissim"] == {"running": False, "version": None}
    assert snap["network"] == {"path": None, "unsaved_writes": 0, "last_write_at": None, "counts": {}}
    assert snap["simulation"] == {"state": st.SIM_IDLE, "run_id": None, "progress_s": None}
    assert isinstance(snap["server_start_id"], str) and snap["server_start_id"]


def test_full_keys(s):
    f = s.full()
    assert set(f) == {"server_start_id", "started_at", "port", "vissim", "network", "simulation"}
    assert f["port"] == 8766
    assert set(f["vissim"]) == {"running", "version", "launched_at", "pid"}
    assert set(f["network"]) == {"path", "unsaved_writes", "last_write_at", "counts"}
    assert set(f["simulation"]) == {"state", "run_id", "progress_s", "period_s", "run_index",
                                    "num_runs", "started_at", "error"}
    assert "T" in f["started_at"] and ("+" in f["started_at"] or "Z" in f["started_at"])


def test_note_write_and_reset(s):
    s.note_write()
    s.note_write()
    n = s.snapshot()["network"]
    assert n["unsaved_writes"] == 2 and n["last_write_at"] is not None
    s.reset_writes()
    assert s.snapshot()["network"]["unsaved_writes"] == 0


def test_set_network_resets_the_write_counter(s):
    s.note_write()
    s.set_network("C:/x/network.inpx", counts={"Link": 547})
    n = s.snapshot()["network"]
    assert n["path"] == "C:/x/network.inpx" and n["unsaved_writes"] == 0 and n["counts"] == {"Link": 547}
    s.set_network(None)
    assert s.snapshot()["network"]["path"] is None and s.snapshot()["network"]["counts"] == {}


def test_set_vissim_partial_update(s):
    s.set_vissim(running=True, version="2026", pid=123)
    assert s.full()["vissim"]["launched_at"] is not None
    s.set_vissim(version="2026.00-01")
    v = s.full()["vissim"]
    assert v["running"] is True and v["version"] == "2026.00-01" and v["pid"] == 123
    s.set_vissim(running=False)
    v = s.full()["vissim"]
    assert v["running"] is False and v["pid"] is None and v["launched_at"] is None


def test_set_simulation_and_state_set(s):
    assert set(st.SIM_STATES) == {"idle", "running", "stopping", "finished", "failed"}
    s.set_simulation(state=st.SIM_RUNNING, run_id="r1", progress_s=0.0, period_s=3600, num_runs=3)
    sim = s.full()["simulation"]
    assert sim["state"] == "running" and sim["run_id"] == "r1" and sim["started_at"] is not None
    s.set_simulation(progress_s=120.5, run_index=1)
    sim = s.full()["simulation"]
    assert sim["progress_s"] == 120.5 and sim["run_index"] == 1 and sim["period_s"] == 3600
    s.set_simulation(state=st.SIM_FAILED, error="interrupted")
    assert s.snapshot()["simulation"]["state"] == "failed"
    with pytest.raises(ValueError):
        s.set_simulation(state="paused")
    with pytest.raises(TypeError):
        s.set_simulation(bogus=1)


def test_snapshot_is_a_copy(s):
    snap = s.snapshot()
    snap["network"]["counts"]["Link"] = 1
    assert s.snapshot()["network"]["counts"] == {}


def test_thread_safe_counter(s):
    def work():
        for _ in range(500):
            s.note_write()

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert s.snapshot()["network"]["unsaved_writes"] == 4000


def test_server_start_id_differs_per_instance():
    assert st.ServerState(port=1).snapshot()["server_start_id"] != st.ServerState(port=1).snapshot()["server_start_id"]
