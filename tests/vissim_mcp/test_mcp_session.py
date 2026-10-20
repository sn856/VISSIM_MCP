"""Session layer: VISSIM lifetime, open, save, backup, unsaved-change tracking, recovery (fake COM, fake process list, temp folders)."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from vissim_mcp import vissim_com as vc
from vissim_mcp import comthread as ct
from vissim_mcp import errors
from vissim_mcp import session as ss
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.state import ServerState


class FakeComError(Exception):
    """Mimics pywintypes.com_error: (hresult, message)."""

    def __init__(self, hresult, msg):
        super().__init__(hresult, msg)
        self.hresult = hresult


class FakeContainer:
    def __init__(self, n=0):
        self.Count = n


class FakeLinks:
    def __init__(self, links=3, conns=2):
        self.Count = links + conns
        self._flags = [(i + 1, False) for i in range(links)] + [(links + i + 1, True) for i in range(conns)]

    def GetMultiAttValues(self, attr):
        assert attr == "IsConn"
        return tuple(self._flags)


class FakeNet:
    def __init__(self):
        self.Links = FakeLinks()
        self.VehicleInputs = FakeContainer(4)


class FakeVissim:
    """Fake VISSIM whose LoadNet/SaveNet/SaveNetAs write real files. With alive=False every call fails as disconnected."""

    def __init__(self):
        self.Net = FakeNet()
        self.loaded = None
        self.alive = True
        self.fail_queue = []      # exceptions LoadNet raises in turn
        self.calls = []

    def _check(self):
        if not self.alive:
            raise FakeComError(-2147417848, "The RPC server is unavailable")

    def AttValue(self, k):
        self._check()
        self.calls.append(("AttValue", k))
        if k == "WorkingFolder":   # open file of the top-level object = WorkingFolder + InputFile ('' for an empty VISSIM)
            return (str(Path(self.loaded).parent) + "\\") if self.loaded else "C:\\"
        if k == "InputFile":
            return Path(self.loaded).name if self.loaded else ""
        return "2026.00-09" if k == "VersionStr" else None

    def LoadNet(self, p, add):
        self._check()
        self.calls.append(("LoadNet", p))
        if self.fail_queue:
            raise self.fail_queue.pop(0)
        self.loaded = p

    def SaveNet(self):
        self._check()
        self.calls.append(("SaveNet",))
        Path(self.loaded).write_bytes(b"<saved by SaveNet/>")

    def SaveNetAs(self, p):
        self._check()
        self.calls.append(("SaveNetAs", p))
        Path(p).write_bytes(b"<saved by SaveNetAs/>")
        self.loaded = p


class FakeEnv:
    """Test helper bundling the process list, launching and the clock."""

    def __init__(self, tmp_path, others=()):
        self.pids = list(others)           # current Vissim*.exe PID list (can be changed from outside)
        self.next_pid = 200
        self.launches = 0
        self.vissims = []
        self.now = datetime(2026, 10, 12, 10, 0, 0)
        self.log_root = tmp_path / "runs" / "mcp"
        self.protected_root = tmp_path / "protected"
        self.cfg = {
            "vissim": {"progid": "Vissim.Vissim.26"},
            "log_root": str(self.log_root),
            "protected_roots": [str(self.protected_root)],
        }

    def list_pids(self):
        return list(self.pids)

    def launch(self, progid):
        self.launches += 1
        v = FakeVissim()
        self.vissims.append(v)
        self.pids.append(self.next_pid)
        self.next_pid += 1
        return v, f"Dispatch:{progid}"

    def clock(self):
        return self.now

    @property
    def vissim(self):
        return self.vissims[-1]


@pytest.fixture
def env(tmp_path):
    return FakeEnv(tmp_path)


@pytest.fixture
def com():
    t = ct.ComThread(retry_count=5, retry_interval_s=0.0)
    t.start()
    yield t
    t.stop()


def make_session(env, com, others=()):
    env.pids = list(others)
    state = ServerState(port=8766)
    log = ChangeLog(env.log_root)
    s = ss.Session(env.cfg, state, com, log, process_lister=env.list_pids, launcher=env.launch, clock=env.clock)
    return s, state, log


def write_inpx(path, body=b"<network original/>"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def entries(log, kind):
    return [r for r in log.read() if r.get("kind") == kind]


# ---------------------------------------------------------------- lifetime

def test_first_open_launches_vissim_and_remembers_pid_by_set_difference(env, com, tmp_path):
    s, state, log = make_session(env, com, others=(100,))
    inpx = write_inpx(tmp_path / "work" / "net.inpx")
    r = s.open_network(str(inpx))
    assert env.launches == 1 and r["launched"] is True
    v = state.full()["vissim"]
    assert v["running"] is True and v["pid"] == 200 and v["version"] == "2026.00-09" and v["launched_at"]
    assert s.other_vissim_processes() == 1          # the server's PID 200 is not counted
    assert r["path"] == str(inpx.resolve()) and env.vissim.loaded == str(inpx.resolve())
    assert r["counts"]["Link"] == 3 and r["counts"]["Connector"] == 2 and r["counts"]["VehicleInput"] == 4
    assert state.snapshot()["network"]["path"] == str(inpx.resolve())
    assert any("VISSIM" in w for w in r["warnings"])    # warning about 1 other VISSIM process (no refusal)
    rows = entries(log, "open")
    assert len(rows) == 1 and rows[0]["network_path"] == str(inpx.resolve()) and rows[0]["ok"] is True
    # with a file open, refused without discard even for the same file with 0 writes
    with pytest.raises(errors.ToolError) as ei:
        s.open_network(str(inpx))
    assert ei.value.kind == errors.KIND_UNSAVED_CHANGES
    assert ei.value.detail == {"unsaved_writes": 0, "open_path": str(inpx.resolve())}
    # the second open does not launch again
    s.open_network(str(inpx), discard=True)
    assert env.launches == 1


def test_pid_is_none_with_warning_when_set_difference_is_not_one(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")

    def launch_two(progid):
        v, how = env.launch(progid)
        env.pids.append(999)
        return v, how

    s._launcher = launch_two
    r = s.open_network(str(inpx))
    assert state.full()["vissim"]["pid"] is None
    assert any("PID" in w for w in r["warnings"])


def test_launch_does_not_call_get_active_object(monkeypatch):
    import sys
    import types
    called = []
    client = types.ModuleType("win32com.client")

    def GetActiveObject(progid):
        called.append(("GetActiveObject", progid))
        return object()

    def Dispatch(progid):
        called.append(("Dispatch", progid))
        return "obj"

    client.GetActiveObject = GetActiveObject
    client.Dispatch = Dispatch
    pkg = types.ModuleType("win32com")
    pkg.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", types.ModuleType("pythoncom"))
    monkeypatch.setitem(sys.modules, "win32com", pkg)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    v, how = vc.launch("Vissim.Vissim.26")
    assert v == "obj" and how == "Dispatch:Vissim.Vissim.26"
    assert called == [("Dispatch", "Vissim.Vissim.26")]


def test_missing_file_is_not_found_and_does_not_launch(env, com, tmp_path):
    s, state, log = make_session(env, com)
    with pytest.raises(errors.ToolError) as ei:
        s.open_network(str(tmp_path / "missing.inpx"))
    assert ei.value.kind == errors.KIND_NOT_FOUND
    assert env.launches == 0


# ---------------------------------------------------------------- unsaved tracking

def test_one_unsaved_write_refuses_open_discard_passes(env, com, tmp_path):
    s, state, log = make_session(env, com)
    a = write_inpx(tmp_path / "a.inpx")
    b = write_inpx(tmp_path / "b.inpx")
    s.open_network(str(a))
    state.note_write()
    with pytest.raises(errors.ToolError) as ei:
        s.open_network(str(b))
    assert ei.value.kind == errors.KIND_UNSAVED_CHANGES
    assert ei.value.detail["unsaved_writes"] == 1 and "1 unsaved change" in ei.value.message
    assert ei.value.detail["open_path"] == str(a.resolve()) and "Ask the user" in ei.value.message
    assert state.snapshot()["network"]["path"] == str(a.resolve())
    r = s.open_network(str(b), discard=True)
    assert r["discarded_writes"] == 1
    assert state.snapshot()["network"]["path"] == str(b.resolve())
    assert state.snapshot()["network"]["unsaved_writes"] == 0


# ---------------------------------------------------------------- save

def test_no_network_when_nothing_open(env, com):
    s, state, log = make_session(env, com)
    with pytest.raises(errors.ToolError) as ei:
        s.save_network()
    assert ei.value.kind == errors.KIND_NO_NETWORK
    assert env.launches == 0


def test_one_other_vissim_refuses_save_force_passes(env, com, tmp_path):
    s, state, log = make_session(env, com, others=(100,))
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    state.note_write()
    with pytest.raises(errors.ToolError) as ei:
        s.save_network()
    assert ei.value.kind == errors.KIND_OTHER_VISSIM_RUNNING
    assert ei.value.detail["other_vissim_processes"] == 1
    assert ("SaveNet",) not in env.vissim.calls
    r = s.save_network(force=True)
    assert r["saved_path"] == str(inpx.resolve()) and r["writes_flushed"] == 1
    assert ("SaveNet",) in env.vissim.calls
    assert state.snapshot()["network"]["unsaved_writes"] == 0
    row = entries(log, "save")[-1]
    assert row["force"] is True and row["writes_flushed"] == 1 and row["backup_path"] == r["backup_path"]


def test_in_place_save_makes_copy_backup_next_to_file_suffix_2_within_same_second(env, com, tmp_path):
    s, state, log = make_session(env, com)
    original = b"<network original bytes 0123456789/>"
    inpx = write_inpx(tmp_path / "work" / "net.inpx", original)
    s.open_network(str(inpx))
    r = s.save_network()
    bak = Path(r["backup_path"])
    assert bak.parent == inpx.resolve().parent
    assert bak.name == "net_20261012-100000.bak.inpx"
    assert bak.read_bytes() == original                     # a file copy, so the bytes equal the original
    assert inpx.read_bytes() == b"<saved by SaveNet/>"       # then VISSIM saved in place
    assert r["path_changed"] is False and "note" not in r
    r2 = s.save_network()
    assert Path(r2["backup_path"]).name == "net_20261012-100000_2.bak.inpx"
    r3 = s.save_network()
    assert Path(r3["backup_path"]).name == "net_20261012-100000_3.bak.inpx"


def test_backup_of_file_under_protected_root_goes_to_log_root_backup(env, com):
    s, state, log = make_session(env, com)
    original = b"<network in protected root/>"
    inpx = write_inpx(env.protected_root / "route" / "net.inpx", original)
    s.open_network(str(inpx))
    r = s.save_network()
    bak = Path(r["backup_path"])
    assert bak.parent == (env.log_root / "backup").resolve()
    assert bak.name == "net_20261012-100000.bak.inpx"
    assert bak.read_bytes() == original
    assert list(inpx.parent.iterdir()) == [inpx]              # no backup is created in the protected folder


def test_save_as_backs_up_only_if_target_exists_and_changes_path(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    state.note_write()
    new = tmp_path / "out" / "alt.inpx"
    new.parent.mkdir()
    r = s.save_network(str(new))
    assert r["backup_path"] is None
    assert r["saved_path"] == str(new.resolve()) and r["path_changed"] is True
    assert r["note"] == f"Subsequent saves go to {new.resolve()}"
    assert r["writes_flushed"] == 1
    net = state.snapshot()["network"]
    assert net["path"] == str(new.resolve()) and net["unsaved_writes"] == 0
    assert net["counts"]["Link"] == 3                          # counts kept
    assert new.read_bytes() == b"<saved by SaveNetAs/>"
    # if the target already exists, back it up under the same rules
    existing = write_inpx(tmp_path / "out" / "old.inpx", b"<old/>")
    r2 = s.save_network(str(existing))
    assert Path(r2["backup_path"]).name == "old_20261012-100000.bak.inpx"
    assert Path(r2["backup_path"]).read_bytes() == b"<old/>"


# ---------------------------------------------------------------- recovery copy

def test_recovery_copy_goes_to_log_root_recovery_when_file_open_regardless_of_write_count(env, com, tmp_path):
    s, state, log = make_session(env, com)
    assert s.save_recovery_copy()["recovery_path"] is None    # VISSIM absent
    inpx = write_inpx(tmp_path / "net.inpx", b"<orig/>")
    s.open_network(str(inpx))
    r = s.save_recovery_copy()                                 # even with 0 conversation writes (UI changes cannot be counted)
    rec = Path(r["recovery_path"])
    assert rec.parent == (env.log_root / "recovery").resolve()
    assert rec.name == "net_20261012-100000.inpx" and rec.is_file()
    assert inpx.read_bytes() == b"<orig/>"                    # original untouched
    assert state.snapshot()["network"]["path"] == str(inpx.resolve())
    rows = entries(log, "recovery")
    assert len(rows) == 1 and rows[0]["recovery_path"] == str(rec) and rows[0]["network_path"] == str(inpx.resolve())


def test_recovery_copy_is_kept_for_drawn_content_only_and_skipped_when_nothing_drawn(env, com, tmp_path):
    s, state, log = make_session(env, com)
    s.ensure_vissim()                                          # empty VISSIM (no open file)
    env.vissim.Net.Links = FakeLinks(links=0, conns=0)
    assert s.save_recovery_copy()["recovery_path"] is None    # nothing drawn
    env.vissim.Net.Links = FakeLinks(links=2, conns=1)
    r = s.save_recovery_copy()
    rec = Path(r["recovery_path"])
    assert rec.parent == (env.log_root / "recovery").resolve()
    assert rec.name == "drawn_20261012-100000.inpx" and rec.is_file()
    rows = entries(log, "recovery")
    assert len(rows) == 1 and rows[0]["network_path"] is None and rows[0]["drawn_links"] == 3


def test_recovery_copy_uses_the_passed_in_decision(env, com, tmp_path):
    s, state, log = make_session(env, com)
    s.ensure_vissim()
    env.vissim.Net.Links = FakeLinks(links=0, conns=0)
    info = {"vissim": True, "open_path": None, "drawn_links": None, "unsaved_writes": 0}   # could not count = treated as drawn
    assert s.save_recovery_copy(info)["recovery_path"] is not None


def test_quit_save_follows_same_rules_as_save_network_with_tool_quit(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx", b"<orig/>")
    s.open_network(str(inpx))
    state.note_write()
    r = s.save_on_quit()
    assert r["saved_path"] == str(inpx.resolve()) and r["writes_flushed"] == 1
    assert Path(r["backup_path"]).read_bytes() == b"<orig/>"
    assert inpx.read_bytes() == b"<saved by SaveNet/>"
    rows = entries(log, "save")
    assert rows[-1]["tool"] == "quit" and rows[-1]["ok"] is True and rows[-1]["force"] is False
    assert state.snapshot()["network"]["unsaved_writes"] == 0


def test_quit_save_also_refused_without_force_when_other_vissim_runs(env, com, tmp_path):
    s, state, log = make_session(env, com, others=(100,))
    inpx = write_inpx(tmp_path / "net.inpx", b"<orig/>")
    s.open_network(str(inpx))
    with pytest.raises(errors.ToolError) as ei:
        s.save_on_quit()
    assert ei.value.kind == errors.KIND_OTHER_VISSIM_RUNNING
    r = s.save_on_quit(force=True)
    assert r["saved_path"] == str(inpx.resolve())
    assert entries(log, "save")[-1]["force"] is True


# ---------------------------------------------------------------- quit procedure (real session + fake VISSIM)

def _quit_runtime(env, com, s, state, log):
    from vissim_mcp import server as sv
    from vissim_mcp.guard import Guard
    cfg = dict(env.cfg, port=8766, protected=[], quit_prompt_timeout_s=1)
    return sv.Runtime(cfg, state=state, com=com, changelog=log, guard=Guard([]), session=s, objects=object(),
                      simulation=object(), results=object(), view=object())


def test_quit_no_leaves_recovery_copy_last_and_original_intact(env, com, tmp_path):
    from vissim_mcp import server as sv
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx", b"<orig/>")
    s.open_network(str(inpx))
    rt = _quit_runtime(env, com, s, state, log)
    exits = []
    res = rt.request_quit(confirm=lambda kind, text, t: sv.QUIT_NO, exit_fn=exits.append)
    assert res["quit"] is True and exits == [0]
    rec = Path(res["recovery_path"])
    assert rec.is_file() and rec.parent == (env.log_root / "recovery").resolve()
    assert inpx.read_bytes() == b"<orig/>"
    calls = [c[0] for c in env.vissim.calls]
    assert calls[-1] == "SaveNetAs"                             # no COM call (current-file check) after the recovery copy
    assert entries(log, "file_switch") == []                   # did not follow the recovery file
    assert state.snapshot()["network"]["path"] is None and s.vissim is None


def test_quit_yes_saves_in_place_with_backup_and_log(env, com, tmp_path):
    from vissim_mcp import server as sv
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx", b"<orig/>")
    s.open_network(str(inpx))
    state.note_write()
    rt = _quit_runtime(env, com, s, state, log)
    texts = []
    res = rt.request_quit(confirm=lambda kind, text, t: texts.append(text) or sv.QUIT_YES, exit_fn=lambda c: None)
    assert res["quit"] is True and res["saved_path"] == str(inpx.resolve()) and res["recovery_path"] is None
    assert str(inpx.resolve()) in texts[0] and "1" in texts[0]   # dialog text comes from server.py
    assert inpx.read_bytes() == b"<saved by SaveNet/>"
    assert list(tmp_path.glob("net_*.bak.inpx"))
    row = entries(log, "save")[-1]
    assert row["tool"] == "quit" and row["ok"] is True
    assert entries(log, "recovery") == []


# ---------------------------------------------------------------- VISSIM lost

def test_vissim_disappearing_clears_state_records_lost_count_and_does_not_reconnect(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    state.note_write()
    state.note_write()
    env.pids.remove(200)            # the user closed the window
    env.vissim.alive = False
    assert s.is_alive() is False
    assert state.full()["vissim"] == {"running": False, "version": "2026.00-09", "launched_at": None, "pid": None}
    assert state.snapshot()["network"]["path"] is None and state.snapshot()["network"]["unsaved_writes"] == 0
    row = entries(log, "vissim_lost")[-1]
    assert row["lost_writes"] == 2 and "2 unsaved change" in row["error"]
    # zero reconnect attempts: save gives no_network, launch count unchanged
    with pytest.raises(errors.ToolError) as ei:
        s.save_network()
    assert ei.value.kind == errors.KIND_NO_NETWORK
    assert env.launches == 1
    assert s.is_alive() is False and env.launches == 1
    # the next open_network launches a fresh instance
    r = s.open_network(str(inpx))
    assert env.launches == 2 and r["launched"] is True and state.full()["vissim"]["pid"] == 201


def test_loss_during_com_call_gives_vissim_absent_with_lost_count(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    state.note_write()
    env.vissim.alive = False        # the PID is still listed but COM is disconnected
    with pytest.raises(errors.ToolError) as ei:
        s.save_network()
    assert ei.value.kind == errors.KIND_VISSIM_ABSENT
    assert ei.value.detail["lost_writes"] == 1 and "1 unsaved change" in ei.value.message
    assert state.full()["vissim"]["running"] is False
    assert entries(log, "vissim_lost")[-1]["lost_writes"] == 1
    assert env.launches == 1


# ---------------------------------------------------------------- COM retry and errors

def test_busy_4_times_succeeds_on_5th_busy_6_times_gives_com_error(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    busy = lambda: FakeComError(ct.BUSY_HRESULT, "Call was rejected by callee.")   # noqa: E731
    env.vissim.calls.clear()
    env.vissim.fail_queue = [busy() for _ in range(4)]
    r = s.open_network(str(inpx), discard=True)
    assert r["path"] == str(inpx.resolve())
    assert len([c for c in env.vissim.calls if c[0] == "LoadNet"]) == 5
    env.vissim.calls.clear()
    env.vissim.fail_queue = [busy() for _ in range(6)]
    with pytest.raises(errors.ToolError) as ei:
        s.open_network(str(inpx), discard=True)
    assert ei.value.kind == errors.KIND_COM_ERROR
    assert "-2147418111" in ei.value.message                  # COM text unchanged
    assert len([c for c in env.vissim.calls if c[0] == "LoadNet"]) == 6
    assert state.full()["vissim"]["running"] is True          # still alive, so not treated as lost
    assert env.launches == 1


def test_ordinary_com_error_becomes_com_error_with_text_unchanged(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    env.vissim.fail_queue = [FakeComError(-2147352567, "Network file format error")]
    with pytest.raises(errors.ToolError) as ei:
        s.open_network(str(inpx), discard=True)
    assert ei.value.kind == errors.KIND_COM_ERROR and "Network file format error" in ei.value.message


# ---------------------------------------------------------------- shutdown

def test_shutdown_drops_reference_and_sets_running_false(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    r = s.shutdown()
    assert r["was_running"] is True
    assert state.full()["vissim"]["running"] is False and s.vissim is None
    assert s.shutdown()["was_running"] is False


# ---------------------------------------------------------------- process list

def test_extracts_only_vissim_pids_from_tasklist_csv(monkeypatch):
    import subprocess

    class R:
        returncode = 0
        stdout = ('"Vissim260.exe","12345","Console","1","1,234,567 K"\n'
                  '"vissim.exe","777","Console","1","1 K"\n'
                  # Korean-locale Windows prints this notice for "no matching tasks"; kept as data on purpose.
                  '정보: 조건에 맞는 작업이 없습니다.\n')

    seen = {}

    def fake_run(*a, **k):
        seen.update(k)
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ss.list_vissim_pids() == [12345, 777]
    assert seen["creationflags"] & 0x08000000   # CREATE_NO_WINDOW: keeps a console window from popping up under the server (pythonw)

    def boom(*a, **k):
        raise OSError("tasklist not found")

    monkeypatch.setattr(subprocess, "run", boom)
    assert ss.list_vissim_pids() is None   # undeterminable -> None (conservative)


def test_changes_jsonl_entries_are_one_line_json_each(env, com, tmp_path):
    s, state, log = make_session(env, com)
    inpx = write_inpx(tmp_path / "net.inpx")
    s.open_network(str(inpx))
    s.save_network()
    for line in log.path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
