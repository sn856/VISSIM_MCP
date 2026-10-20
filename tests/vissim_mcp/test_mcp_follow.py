"""Following the server VISSIM's file, blocking the first write, out-of-server changes, open rules, admin routes.

The fake VISSIM reports the open file through the top-level object's WorkingFolder and InputFile (an empty VISSIM
has InputFile='').
Opening another file, creating a new one, or drawing on screen through the VISSIM UI is mimicked by menu_open,
menu_new and draw.
"""
import os
import time
from pathlib import Path

import pytest

from vissim_mcp import changelog as cl
from vissim_mcp import comthread as ct
from vissim_mcp import config as mc
from vissim_mcp import errors
from vissim_mcp import server as sv
from vissim_mcp import session as ss
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.guard import Guard
from vissim_mcp.state import SIM_RUNNING, SIM_STOPPING, ServerState


class FakeComError(Exception):
    def __init__(self, hresult, msg):
        super().__init__(hresult, msg)
        self.hresult = hresult


class FakeLinks:
    def __init__(self, links=0, conns=0):
        self.Count = links + conns
        self._flags = [(i + 1, False) for i in range(links)] + [(links + i + 1, True) for i in range(conns)]

    def GetMultiAttValues(self, attr):
        return tuple(self._flags)


class FakeNet:
    def __init__(self, links=0, conns=0):
        self.Links = FakeLinks(links, conns)


class FakeVissim:
    DEFAULT_FOLDER = "C:\\Users\\user\\Documents\\"

    def __init__(self):
        self.Net = FakeNet()
        self.loaded = None
        self.alive = True
        self.read_fail = None       # exception raised when reading WorkingFolder/InputFile
        self.calls = []

    def _check(self):
        if not self.alive:
            raise FakeComError(-2147417848, "The RPC server is unavailable")

    def AttValue(self, k):
        self._check()
        self.calls.append(("AttValue", k))
        if k in ("WorkingFolder", "InputFile") and self.read_fail is not None:
            raise self.read_fail
        if k == "WorkingFolder":
            return (os.path.dirname(self.loaded) + "\\") if self.loaded else self.DEFAULT_FOLDER
        if k == "InputFile":
            return os.path.basename(self.loaded) if self.loaded else ""
        return "2026.00-09" if k == "VersionStr" else None

    def LoadNet(self, p, add):
        self._check()
        self.calls.append(("LoadNet", p))
        self.loaded = p
        self.Net = FakeNet(3, 2)

    def SaveNet(self):
        self._check()
        self.calls.append(("SaveNet",))
        Path(self.loaded).write_bytes(b"<saved by SaveNet/>")
        _bump_mtime(Path(self.loaded))

    def SaveNetAs(self, p):
        self._check()
        self.calls.append(("SaveNetAs", p))
        Path(p).write_bytes(b"<saved by SaveNetAs/>")
        _bump_mtime(Path(p))
        self.loaded = p

    # ---- what the user did in the VISSIM UI ----
    def menu_open(self, p):
        self.loaded = str(p)
        self.Net = FakeNet(7, 1)

    def menu_new(self):
        self.loaded = None
        self.Net = FakeNet()

    def draw(self, n):
        self.Net = FakeNet(n, 0)

    def reads(self):
        return [c for c in self.calls if c == ("AttValue", "InputFile")]


def _bump_mtime(p, ahead=10.0):
    t = time.time() + ahead
    os.utime(p, (t, t))


class Env:
    def __init__(self, tmp_path):
        self.pids = []
        self.next_pid = 300
        self.launches = 0
        self.launch_fail = None
        self.vissims = []
        self.log_root = tmp_path / "runs" / "mcp"
        self.cfg = {"vissim": {"progid": "Vissim.Vissim.26"}, "port": 8766, "protected": [], "protected_roots": [],
                    "log_root": str(self.log_root), "_path": str(tmp_path / "vissim_mcp.json")}

    def list_pids(self):
        return list(self.pids)

    def launch(self, progid):
        if self.launch_fail is not None:
            raise self.launch_fail
        self.launches += 1
        v = FakeVissim()
        self.vissims.append(v)
        self.pids.append(self.next_pid)
        self.next_pid += 1
        return v, f"Dispatch:{progid}"

    @property
    def vissim(self):
        return self.vissims[-1]

    def kill(self):
        self.vissim.alive = False
        self.pids.remove(self.pids[-1])


class Rec:
    """Stand-in for the write/read tools; only records that they were called."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def fn(*a, **k):
            self.calls.append(name)
            return {"done": name}
        return fn


class NoCom:
    def start(self):
        pass

    def stop(self, timeout=None):
        pass


@pytest.fixture
def com():
    t = ct.ComThread(retry_count=2, retry_interval_s=0.0)
    t.start()
    yield t
    t.stop()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def make(env, com):
    state = ServerState(port=8766)
    log = ChangeLog(env.log_root)
    s = ss.Session(env.cfg, state, com, log, process_lister=env.list_pids, launcher=env.launch)
    rt = sv.Runtime(env.cfg, state=state, com=NoCom(), changelog=log, guard=Guard([]), session=s,
                    objects=Rec(), simulation=Rec(), results=Rec(), view=Rec())
    return rt, s, state, log


def inpx(tmp_path, name, body=b"<network/>"):
    p = tmp_path / "work" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


def rows(log, kind):
    return [r for r in log.read() if r.get("kind") == kind]


def R(p):
    return str(Path(p).resolve())


# ---------------------------------------------------------------- constants

def test_ten_error_kinds_including_network_changed():
    assert errors.KIND_NETWORK_CHANGED == "network_changed"
    assert len(errors.ERROR_KINDS) == 10 and "network_changed" in errors.ERROR_KINDS


def test_entry_kinds_include_file_switch_and_external_change():
    assert "file_switch" in cl.ENTRY_KINDS and "external_change" in cl.ENTRY_KINDS


def test_three_new_tunables():
    assert mc.TUNABLE_DEFAULTS["launcher_server_wait_s"] == 30
    assert mc.TUNABLE_DEFAULTS["launcher_open_timeout_s"] == 600
    assert mc.TUNABLE_DEFAULTS["shortcut_name"] == "VISSIM MCP"


# ---------------------------------------------------------------- current-file check: five branches (+ VISSIM absent)

def test_current_check_skipped_when_vissim_absent(env, com):
    rt, s, state, log = make(env, com)
    r = s.check_current("get_attributes")
    assert r["checked"] is False and r["note"] == "VISSIM not running" and r["lost"] is None
    assert env.launches == 0


def test_current_check_same_path_changes_nothing(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    r = s.check_current("get_attributes")
    assert r["checked"] is True and r["switched"] is False and r["warnings"] == []
    assert state.snapshot()["network"]["path"] == R(a) and state.snapshot()["network"]["unsaved_writes"] == 1
    assert rows(log, "file_switch") == [] and state.network_changed() is None


def test_current_check_paths_differing_only_in_case_are_same(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    env.vissim.loaded = env.vissim.loaded.upper()
    r = s.check_current("get_attributes")
    assert r["switched"] is False and state.snapshot()["network"]["path"] == R(a)


def test_current_check_different_path_is_followed_logged_and_flagged(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    state.note_write()
    state.note_write()
    env.vissim.menu_open(R(b))
    r = s.check_current("list_objects")
    assert r["checked"] is True and r["switched"] is True
    net = state.snapshot()["network"]
    assert net["path"] == R(b) and net["unsaved_writes"] == 0
    assert net["counts"]["Link"] == 7 and net["counts"]["Connector"] == 1      # re-read object counts
    row = rows(log, "file_switch")[-1]
    assert row["tool"] == "list_objects" and row["previous_path"] == R(a) and row["current_path"] == R(b)
    assert row["changed_by"] == "vissim_menu" and row["dropped_writes"] == 2
    assert state.network_changed() == {"previous_path": R(a), "current_path": R(b), "changed_by": "vissim_menu"}


def test_current_check_empty_value_means_no_open_file(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    env.vissim.menu_new()
    r = s.check_current("get_attributes")
    assert r["switched"] is True and state.snapshot()["network"]["path"] is None
    row = rows(log, "file_switch")[-1]
    assert row["previous_path"] == R(a) and row["current_path"] is None
    assert state.network_changed()["current_path"] is None


def test_current_check_com_failure_only_warns_and_trusts_recorded_file(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    env.vissim.read_fail = RuntimeError("attribute read failed XYZ")
    r = s.check_current("get_attributes")
    assert r["checked"] is False and "XYZ" in r["note"] and r["lost"] is None
    assert any("XYZ" in w for w in r["warnings"])
    assert state.snapshot()["network"]["path"] == R(a) and state.snapshot()["network"]["unsaved_writes"] == 1
    assert rows(log, "file_switch") == [] and rows(log, "vissim_lost") == []
    # does not block writes or saves
    out = rt.run_tool("set_attributes", rt.objects.set_attributes, changes=[])
    assert out["ok"] is True and rt.objects.calls == ["set_attributes"]


@pytest.mark.parametrize("sim", [SIM_RUNNING, SIM_STOPPING])
def test_current_check_skipped_while_simulation_runs(env, com, tmp_path, sim):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    env.vissim.calls.clear()
    env.vissim.menu_open(R(inpx(tmp_path, "b.inpx")))
    state.set_simulation(state=sim)
    r = s.check_current("get_results")
    assert r["checked"] is False and r["note"] == "simulation running"
    assert env.vissim.reads() == [] and state.snapshot()["network"]["path"] == R(a)


def test_current_check_loss_runs_existing_loss_handling(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    env.kill()
    r = s.check_current("get_attributes")
    assert r["checked"] is False and r["lost"] == 1
    assert state.full()["vissim"]["running"] is False and state.snapshot()["network"]["path"] is None
    assert rows(log, "vissim_lost")[-1]["lost_writes"] == 1
    assert any("disappeared" in w for w in r["warnings"])
    assert s.check_current("get_attributes")["note"] == "VISSIM not running" and env.launches == 1   # no reconnect


def test_loss_found_by_tool_call_current_check_gives_vissim_absent(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    env.kill()
    out = rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_VISSIM_ABSENT
    assert out["error"]["detail"]["lost_writes"] == 1
    assert rt.objects.calls == []


def test_vissim_status_does_not_run_current_check(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    env.vissim.calls.clear()
    rt.run_tool("vissim_status", rt.status)
    assert env.vissim.reads() == []
    rt.run_tool("get_change_log", lambda: {"entries": []})
    assert len(env.vissim.reads()) == 1                  # other tools check once


# ---------------------------------------------------------------- changed flag and first-write block

def test_after_menu_change_reads_pass_and_only_the_first_write_is_refused(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    env.vissim.menu_open(R(b))
    out = rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert out["ok"] is True and out["state"]["network"]["path"] == R(b)
    out = rt.run_tool("set_attributes", rt.objects.set_attributes, changes=[])
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_NETWORK_CHANGED
    assert out["error"]["detail"] == {"previous_path": R(a), "current_path": R(b), "changed_by": "vissim_menu"}
    msg = out["error"]["message"]
    assert R(a) in msg and R(b) in msg and "user" in msg.lower()   # message text comes from server.py
    assert rt.objects.calls == ["get_attributes"]            # the write never reached the layer (COM)
    assert state.network_changed() is None
    out = rt.run_tool("set_attributes", rt.objects.set_attributes, changes=[])
    assert out["ok"] is True and rt.objects.calls[-1] == "set_attributes"


@pytest.mark.parametrize("tool", ["set_attributes", "create_object", "delete_object", "configure_simulation",
                                  "configure_evaluation", "run_simulation", "save_network"])
def test_all_seven_write_tools_refuse_the_first_call(env, com, tmp_path, tool):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    env.vissim.menu_open(R(inpx(tmp_path, "b.inpx")))
    called = []
    out = rt.run_tool(tool, lambda **k: called.append(1) or {})
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_NETWORK_CHANGED and called == []
    out = rt.run_tool(tool, lambda **k: called.append(1) or {})
    assert out["ok"] is True and called == [1]


@pytest.mark.parametrize("tool", ["stop_simulation", "list_objects", "get_results", "set_view", "capture_view",
                                  "list_result_files", "read_result_file", "get_change_log", "describe_attributes",
                                  "list_object_types"])
def test_non_write_tools_are_not_refused_and_flag_stays(env, com, tmp_path, tool):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    env.vissim.menu_open(R(inpx(tmp_path, "b.inpx")))
    out = rt.run_tool(tool, lambda: {})
    assert out["ok"] is True and state.network_changed() is not None


def test_open_network_clears_the_flag_instead_of_setting_it(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    assert state.network_changed() is None
    out = rt.run_tool("open_network", s.open_network, path=str(b), discard=True)
    assert out["ok"] is True and state.network_changed() is None
    env.vissim.menu_open(R(a))
    rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert state.network_changed() is not None
    out = rt.run_tool("open_network", s.open_network, path=str(b), discard=True)
    assert out["ok"] is True and state.network_changed() is None
    out = rt.run_tool("set_attributes", rt.objects.set_attributes, changes=[])
    assert out["ok"] is True


def test_flag_is_cleared_when_vissim_disappears_and_relaunches(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    env.vissim.menu_open(R(inpx(tmp_path, "b.inpx")))
    s.check_current("get_attributes")
    assert state.network_changed() is not None
    env.kill()
    out = rt.run_tool("admin_open", rt.admin_open, path=None)
    assert out["ok"] is True and out["action"] == "launched" and env.launches == 2
    assert state.network_changed() is None


# ---------------------------------------------------------------- out-of-server change (modification time)

def test_changed_mtime_gives_external_change_warning_and_zero_writes(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    state.note_write()
    _bump_mtime(a, 30)
    out = rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert out["ok"] is True
    assert any("changed outside the server" in w and "Ask the user" in w for w in out["warnings"])
    assert out["state"]["network"]["unsaved_writes"] == 0
    ext = rows(log, "external_change")
    assert len(ext) == 1
    assert ext[0]["network_path"] == R(a) and ext[0]["before_mtime"] and ext[0]["after_mtime"]
    assert ext[0]["before_mtime"] != ext[0]["after_mtime"] and "outside the server" in ext[0]["note"]
    out = rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)   # the baseline moved, so it is not logged again
    assert len(rows(log, "external_change")) == 1 and not out.get("warnings")


def test_right_after_save_network_is_not_an_out_of_server_change(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    out = rt.run_tool("save_network", s.save_network, path=None, force=False)
    assert out["ok"] is True
    rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    other = tmp_path / "work" / "other.inpx"
    out = rt.run_tool("save_network", s.save_network, path=str(other), force=False)
    assert out["ok"] is True
    rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert rows(log, "external_change") == [] and rows(log, "file_switch") == []


def test_followed_files_mtime_also_becomes_the_baseline(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    env.vissim.menu_open(R(b))
    rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert rows(log, "external_change") == []
    _bump_mtime(b, 40)
    rt.run_tool("get_attributes", rt.objects.get_attributes, "Link", 1)
    assert rows(log, "external_change")[-1]["network_path"] == R(b)


# ---------------------------------------------------------------- open_network rules

def test_open_network_refused_when_a_file_is_open_even_with_zero_writes_or_same_file(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    for target in (a, inpx(tmp_path, "b.inpx")):
        with pytest.raises(errors.ToolError) as ei:
            s.open_network(str(target))
        e = ei.value
        assert e.kind == errors.KIND_UNSAVED_CHANGES
        assert e.detail == {"unsaved_writes": 0, "open_path": R(a)}
        assert "Ask the user" in e.message and R(a) in e.message and "0 unsaved change" in e.message
    assert len([c for c in env.vissim.calls if c[0] == "LoadNet"]) == 1
    r = s.open_network(str(a), discard=True)
    assert r["path"] == R(a)


def test_open_network_on_empty_vissim_opens_without_asking(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.ensure_vissim()
    r = s.open_network(str(inpx(tmp_path, "a.inpx")))
    assert r["path"].endswith("a.inpx") and r["launched"] is False


def test_open_network_refused_when_something_is_drawn(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.ensure_vissim()
    env.vissim.draw(4)
    with pytest.raises(errors.ToolError) as ei:
        s.open_network(str(inpx(tmp_path, "a.inpx")))
    e = ei.value
    assert e.kind == errors.KIND_UNSAVED_CHANGES
    assert e.detail == {"unsaved_writes": 0, "open_path": None, "drawn_links": 4}
    assert "Ask the user" in e.message and "4" in e.message
    r = s.open_network(str(inpx(tmp_path, "a.inpx")), discard=True)
    assert r["path"].endswith("a.inpx")


def test_open_network_judges_a_menu_changed_file_after_the_current_check(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    env.vissim.menu_open(R(b))
    out = rt.run_tool("open_network", s.open_network, path=str(a), discard=False)
    assert out["ok"] is False and out["error"]["detail"]["open_path"] == R(b)


def test_open_network_tool_description_tells_to_ask_the_user(env, com, tmp_path):
    import anyio
    from mcp.client.client import Client
    rt, s, state, log = make(env, com)
    mcp = sv.build_server(rt)

    async def go():
        async with Client(mcp) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = anyio.run(go)
    assert "user" in tools["open_network"].description.lower()   # description text comes from server.py/registry.py
    assert set(tools["open_network"].input_schema["properties"]) == {"path", "discard"}


# ---------------------------------------------------------------- /admin/open (session)

def test_admin_open_during_simulation_gives_sim_running(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    state.set_simulation(state=SIM_RUNNING)
    out = rt.run_tool("admin_open", rt.admin_open, path=str(inpx(tmp_path, "a.inpx")))
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_SIM_RUNNING and env.launches == 0


def test_admin_open_without_path_launches_or_fronts(env, com):
    rt, s, state, log = make(env, com)
    out = rt.run_tool("admin_open", rt.admin_open, path=None)
    assert out["ok"] is True and out["action"] == "launched" and out["launched"] is True
    assert out["pid"] == 300 and out["path"] is None and "state" in out
    out = rt.run_tool("admin_open", rt.admin_open, path=None)
    assert out["action"] == "front" and out["launched"] is False and out["pid"] == 300 and env.launches == 1


def test_admin_open_missing_file_is_not_found(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    out = rt.run_tool("admin_open", rt.admin_open, path=str(tmp_path / "missing.inpx"))
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_NOT_FOUND and env.launches == 0


def test_admin_open_same_file_fronts_without_reloading_ignores_discard_keeps_flag(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    env.vissim.menu_open(R(b))
    s.check_current("get_attributes")                       # flag is on
    state.note_write()
    out = rt.run_tool("admin_open", rt.admin_open, path=str(b).upper(), discard=True)
    assert out["ok"] is True and out["action"] == "front" and out["path"] == R(b) and out["pid"] == 300
    assert [c for c in env.vissim.calls if c[0] == "LoadNet"] == [("LoadNet", R(a))]
    assert state.snapshot()["network"]["unsaved_writes"] == 1
    assert state.network_changed() is not None


def test_admin_open_with_another_file_open_gives_unsaved_changes_and_opens_with_discard(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    state.note_write()
    out = rt.run_tool("admin_open", rt.admin_open, path=str(b))
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_UNSAVED_CHANGES
    assert out["error"]["detail"] == {"unsaved_writes": 1, "open_path": R(a)}
    out = rt.run_tool("admin_open", rt.admin_open, path=str(b), discard=True)
    assert out["ok"] is True and out["action"] == "opened" and out["path"] == R(b) and out["launched"] is False
    assert out["counts"]["Link"] == 3 and out["pid"] == 300
    assert state.network_changed() == {"previous_path": R(a), "current_path": R(b), "changed_by": "launcher"}
    row = rows(log, "open")[-1]
    assert row["tool"] == "admin_open" and row["previous_path"] == R(a) and row["changed_by"] == "launcher"
    assert row["discarded_writes"] == 1
    assert rows(log, "file_switch") == []
    # the next first write is refused (launcher)
    out = rt.run_tool("save_network", s.save_network, path=None, force=False)
    assert out["error"]["kind"] == errors.KIND_NETWORK_CHANGED and out["error"]["detail"]["changed_by"] == "launcher"


def test_admin_open_with_drawn_content_gives_unsaved_changes(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    rt.run_tool("admin_open", rt.admin_open, path=None)
    env.vissim.draw(5)
    out = rt.run_tool("admin_open", rt.admin_open, path=str(inpx(tmp_path, "a.inpx")))
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_UNSAVED_CHANGES
    assert out["error"]["detail"] == {"unsaved_writes": 0, "open_path": None, "drawn_links": 5}


def test_admin_open_on_empty_vissim_opens_without_asking_and_sets_flag(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    rt.run_tool("admin_open", rt.admin_open, path=None)
    a = inpx(tmp_path, "a.inpx")
    out = rt.run_tool("admin_open", rt.admin_open, path=str(a))
    assert out["ok"] is True and out["action"] == "opened" and out["launched"] is False
    assert state.network_changed() == {"previous_path": None, "current_path": R(a), "changed_by": "launcher"}


def test_admin_open_without_vissim_launches_and_opens(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    out = rt.run_tool("admin_open", rt.admin_open, path=str(inpx(tmp_path, "a.inpx")))
    assert out["ok"] is True and out["action"] == "opened" and out["launched"] is True and out["pid"] == 300


@pytest.mark.parametrize("expect", [
    {"expect_open_path": "C:\\elsewhere\\x.inpx"},
    {"expect_unsaved_writes": 0},
    {"expect_open_path": None},
])
def test_admin_open_with_mismatching_expectation_does_not_discard_and_asks_again_with_fresh_values(env, com, tmp_path, expect):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    state.note_write()
    out = rt.run_tool("admin_open", rt.admin_open, path=str(inpx(tmp_path, "b.inpx")), discard=True, **expect)
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_UNSAVED_CHANGES
    assert out["error"]["detail"] == {"unsaved_writes": 2, "open_path": R(a)}
    assert state.snapshot()["network"]["path"] == R(a)


def test_admin_open_with_matching_expectation_opens(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a))
    state.note_write()
    out = rt.run_tool("admin_open", rt.admin_open, path=str(inpx(tmp_path, "b.inpx")), discard=True,
                      expect_open_path=str(a).upper(), expect_unsaved_writes=1)
    assert out["ok"] is True and out["action"] == "opened"


def test_admin_open_launch_failure_gives_vissim_absent_launch_failed(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    env.launch_fail = RuntimeError("Dispatch failed: class not registered")
    for path in (None, str(inpx(tmp_path, "a.inpx"))):
        out = rt.run_tool("admin_open", rt.admin_open, path=path)
        assert out["ok"] is False and out["error"]["kind"] == errors.KIND_VISSIM_ABSENT
        assert out["error"]["detail"]["launch_failed"] is True and "class not registered" in out["error"]["message"]


def test_admin_open_after_window_closed_warns_of_loss_then_relaunches_and_opens(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    state.note_write()
    env.kill()
    out = rt.run_tool("admin_open", rt.admin_open, path=str(inpx(tmp_path, "b.inpx")))
    assert out["ok"] is True and out["action"] == "opened" and out["launched"] is True
    assert any("disappeared" in w for w in out["warnings"]) and env.launches == 2
    assert rows(log, "vissim_lost")[-1]["lost_writes"] == 1


# ---------------------------------------------------------------- admin routes (HTTP)

def _client(rt):
    from starlette.testclient import TestClient
    return TestClient(sv.build_server(rt).streamable_http_app())


def test_http_admin_open_response_shape(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    with _client(rt) as c:
        r = c.post(sv.ADMIN_OPEN_PATH, json={"path": str(a), "discard": False})
        body = r.json()
        assert r.status_code == 200 and body["ok"] is True
        assert {"ok", "action", "path", "pid", "launched", "counts", "warnings", "state"} <= set(body)
        assert body["action"] == "opened" and body["path"] == R(a)
        r = c.post(sv.ADMIN_OPEN_PATH, json={"path": str(inpx(tmp_path, "b.inpx"))})
        assert r.json()["error"]["kind"] == errors.KIND_UNSAVED_CHANGES
        r = c.post(sv.ADMIN_OPEN_PATH, json={"path": str(tmp_path / "b" / ".." / "work" / "b.inpx"), "discard": True,
                                            "expect_open_path": str(a), "expect_unsaved_writes": 0})
        assert r.json()["ok"] is True and r.json()["action"] == "opened"
        r = c.post(sv.ADMIN_OPEN_PATH, json={"path": None})
        assert r.json()["action"] == "front"


@pytest.mark.parametrize("body", [[1, 2], {"path": 3}, {"path": None, "discard": "yes"},
                                  {"path": None, "expect_unsaved_writes": "2"}, {"path": None, "expect_open_path": 5}])
def test_http_admin_open_argument_errors_are_bad_args(env, com, body):
    rt, s, state, log = make(env, com)
    with _client(rt) as c:
        out = c.post(sv.ADMIN_OPEN_PATH, json=body).json()
    assert out["ok"] is False and out["error"]["kind"] == errors.KIND_BAD_ARGS and "state" in out
    assert env.launches == 0


def test_http_admin_status_without_refresh_makes_no_com_call(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    env.vissim.calls.clear()
    with _client(rt) as c:
        body = c.get(sv.ADMIN_STATUS_PATH).json()
    assert body["ok"] is True and "refreshed" not in body and env.vissim.reads() == []


def test_http_admin_status_refresh_with_vissim_absent(env, com):
    rt, s, state, log = make(env, com)
    with _client(rt) as c:
        body = c.get(sv.ADMIN_STATUS_PATH, params={"refresh": "1"}).json()
    assert body["ok"] is True and body["refreshed"] is False and body["refresh_note"] == "VISSIM not running"


def test_http_admin_status_refresh_while_simulation_runs(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    state.set_simulation(state=SIM_RUNNING)
    with _client(rt) as c:
        body = c.get(sv.ADMIN_STATUS_PATH, params={"refresh": "1"}).json()
    assert body["refreshed"] is False and body["refresh_note"] == "simulation running"


def test_http_admin_status_refresh_com_failure(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    s.open_network(str(inpx(tmp_path, "a.inpx")))
    env.vissim.read_fail = RuntimeError("read failed ABC")
    with _client(rt) as c:
        body = c.get(sv.ADMIN_STATUS_PATH, params={"refresh": "1"}).json()
    assert body["refreshed"] is False and "ABC" in body["refresh_note"]


def test_http_admin_status_refresh_true_also_logs_a_change(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    a = inpx(tmp_path, "a.inpx")
    b = inpx(tmp_path, "b.inpx")
    s.open_network(str(a))
    with _client(rt) as c:
        body = c.get(sv.ADMIN_STATUS_PATH, params={"refresh": "1"}).json()
        assert body["refreshed"] is True and body["refresh_note"] is None
        assert body["vissim"]["pid"] == 300 and body["network"]["path"] == R(a)
        env.vissim.menu_open(R(b))
        body = c.get(sv.ADMIN_STATUS_PATH, params={"refresh": "1"}).json()
    assert body["refreshed"] is True and body["network"]["path"] == R(b)
    row = rows(log, "file_switch")[-1]
    assert row["tool"] == "admin_status" and row["changed_by"] == "vissim_menu"
    assert state.network_changed()["changed_by"] == "vissim_menu"


# ---------------------------------------------------------------- querying the open state

def test_open_info_for_file_drawn_content_and_neither(env, com, tmp_path):
    rt, s, state, log = make(env, com)
    assert s.open_info() == {"vissim": False, "open_path": None, "drawn_links": 0, "unsaved_writes": 0}
    s.ensure_vissim()
    assert s.open_info()["drawn_links"] == 0
    env.vissim.draw(2)
    assert s.open_info() == {"vissim": True, "open_path": None, "drawn_links": 2, "unsaved_writes": 0}
    a = inpx(tmp_path, "a.inpx")
    s.open_network(str(a), discard=True)
    state.note_write()
    assert s.open_info() == {"vissim": True, "open_path": R(a), "drawn_links": 0, "unsaved_writes": 1}


# ---------------------------------------------------------------- launcher <-> real server routes (contract)

class _UI:
    """Records events instead of showing a launcher window. choose picks the button name supplied in advance."""

    def __init__(self, answers=()):
        self.events, self.answers = [], list(answers)

    def show_progress(self, text):
        self.events.append(("progress", text))

    def hide_progress(self):
        self.events.append(("hide",))

    def notice(self, title, body):
        self.events.append(("notice", title, body))

    def choose(self, title, body, buttons, default):
        self.events.append(("choose", title, body))
        return list(buttons).index(self.answers.pop(0))

    def close(self):
        self.events.append(("close",))


def _launcher_deps(client, ui, fronts):
    from urllib.parse import urlsplit
    from vissim_mcp import launcher as L

    def path_of(url):
        u = urlsplit(url)
        return u.path + (f"?{u.query}" if u.query else "")

    return L.LauncherDeps(
        ui=ui, acquire_mutex=lambda: "h", release_mutex=lambda h: None,
        http_get=lambda url, t: client.get(path_of(url)).json(),
        http_post=lambda url, body, t: client.post(path_of(url), json=body).json(),
        port_free=lambda port: False, spawn_server=lambda cmd, cwd: None, launch_plain=lambda cmd: None,
        bring_to_front=lambda pid: fronts.append(pid) or True, clock=lambda: 0.0, sleep=lambda s: None,
        write_log=lambda path, line: None, now=lambda: "2026-10-18T12:00:00", pid=1, python_exe="python.exe")


def test_launcher_open_switch_and_front_through_real_server_routes(env, com, tmp_path):
    """Runs the launcher flow end to end against the real /admin/status and /admin/open of build_server, not fake responses."""
    from vissim_mcp import launcher as L
    rt, s, state, log = make(env, com)
    a, b = inpx(tmp_path, "a.inpx"), inpx(tmp_path, "b.inpx")
    cfg = dict(env.cfg, launcher_server_wait_s=30, launcher_open_timeout_s=600, vissim={"exe": "Vissim260.exe"})
    fronts: list = []
    with _client(rt) as c:
        assert L.run_open(cfg, str(a), deps=_launcher_deps(c, _UI(), fronts)) == 0
        assert state.snapshot()["network"]["path"] == R(a) and env.launches == 1
        ui = _UI(answers=[L.BTN_DISCARD])                       # another file -> confirmation window -> discard and open
        assert L.run_open(cfg, str(b), deps=_launcher_deps(c, ui, fronts)) == 0
        assert len([e for e in ui.events if e[0] == "choose"]) == 1
        assert state.snapshot()["network"]["path"] == R(b)
        assert [r["changed_by"] for r in rows(log, "open") if r.get("tool") == "admin_open"][-1] == "launcher"
        ui = _UI()                                              # same file -> bring to front without asking
        assert L.run_open(cfg, str(b), deps=_launcher_deps(c, ui, fronts)) == 0
        assert not [e for e in ui.events if e[0] == "choose"]
        assert env.launches == 1 and len(fronts) == 3 and set(fronts) == {env.pids[-1]}
