"""Launcher: decisions only, checked with a fake HTTP, fake windows and a fake clock (no real windows, processes or network)."""
import json
import socket
import urllib.error
from pathlib import Path

import pytest

from vissim_mcp import launcher as L

PORT = 8766
EXE = r"C:\PTV\Vissim260.exe"


# ---------------------------------------------------------------- fakes

class FakeUI:
    """Records events instead of showing windows. The answer to choose is given in advance as a button label (string) or None (window closed)."""

    def __init__(self, answers=()):
        self.events = []
        self.answers = list(answers)

    def show_progress(self, text):
        self.events.append(("progress", text))

    def hide_progress(self):
        self.events.append(("hide",))

    def notice(self, title, body):
        self.events.append(("notice", title, body))

    def choose(self, title, body, buttons, default):
        self.events.append(("choose", title, body, tuple(buttons), default))
        ans = self.answers.pop(0)
        return None if ans is None else list(buttons).index(ans)

    def close(self):
        self.events.append(("close",))

    def kinds(self, kind):
        return [e for e in self.events if e[0] == kind]


class FakeProc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class World:
    """Fake server, clock and process. status/open are lists of replies consumed in order (the last one repeats).

    port_free: whether the port is free (default False = something is listening). A list is consumed in order (the last repeats).
    """

    def __init__(self, status=None, opens=(), port_free=False, spawn=None, front=True):
        self.status = list(status if status is not None else [ok_status()])
        self.opens = list(opens)
        self.port_free_val = port_free
        self.spawn_result = spawn if spawn is not None else FakeProc()
        self.front_result = front
        self.t = 0.0
        self.gets = []
        self.posts = []
        self.spawns = []
        self.plain = []
        self.fronts = []
        self.logs = []
        self.mutex = {"held": False, "acquired": 0, "released": 0}
        self.log_raises = False

    # http
    def http_get(self, url, timeout):
        self.gets.append((url, timeout))
        ans = self.status.pop(0) if len(self.status) > 1 else self.status[0]
        if callable(ans):
            ans = ans(self)
        if isinstance(ans, BaseException):
            raise ans
        return ans

    def http_post(self, url, body, timeout):
        self.posts.append((url, json.loads(json.dumps(body)), timeout))
        ans = self.opens.pop(0)
        if isinstance(ans, BaseException):
            raise ans
        return ans

    def port_free(self, port):
        v = self.port_free_val
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v

    # process
    def spawn_server(self, cmd, cwd):
        self.spawns.append((list(cmd), cwd))
        self.gets_at_spawn = len(self.gets)
        if isinstance(self.spawn_result, BaseException):
            raise self.spawn_result
        return self.spawn_result

    def launch_plain(self, cmd):
        self.plain.append(list(cmd))

    def bring_to_front(self, pid):
        self.fronts.append(pid)
        if isinstance(self.front_result, BaseException):
            raise self.front_result
        return self.front_result

    # clock
    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    # mutex
    def acquire_mutex(self):
        if self.mutex["held"]:
            return None
        self.mutex["acquired"] += 1
        return "handle"

    def release_mutex(self, h):
        self.mutex["released"] += 1

    def write_log(self, path, line):
        if self.log_raises:
            raise PermissionError("locked")
        self.logs.append((Path(path), line))

    def deps(self, ui):
        return L.LauncherDeps(
            ui=ui, acquire_mutex=self.acquire_mutex, release_mutex=self.release_mutex,
            http_get=self.http_get, http_post=self.http_post, port_free=self.port_free,
            spawn_server=self.spawn_server, launch_plain=self.launch_plain, bring_to_front=self.bring_to_front,
            clock=self.clock, sleep=self.sleep, write_log=self.write_log, now=lambda: "2026-10-18T12:00:00",
            pid=4242, python_exe=r"C:\Py\python.exe",
        )

    def log_record(self):
        assert len(self.logs) == 1
        return json.loads(self.logs[0][1])


def ok_status(other=0):
    return {"ok": True, "server": {"port": PORT}, "vissim": {"running": False, "pid": None},
            "network": {"path": None, "unsaved_writes": 0}, "simulation": {"state": "idle"},
            "other_vissim_processes": other, "state": {}}


def opened(path, action="opened", pid=777, warnings=()):
    return {"ok": True, "action": action, "path": path, "pid": pid, "launched": action == "launched",
            "warnings": list(warnings), "state": {}}


def err(kind, message="failed", detail=None):
    e = {"kind": kind, "message": message}
    if detail is not None:
        e["detail"] = detail
    return {"ok": False, "error": e, "state": {}}


def unsaved(open_path, n, drawn=None):
    d = {"unsaved_writes": n, "open_path": open_path}
    if drawn is not None:
        d["drawn_links"] = drawn
    return err("unsaved_changes", "A file is open", d)


@pytest.fixture
def cfg(tmp_path):
    return {"port": PORT, "log_root": str(tmp_path / "logs"), "vissim": {"exe": EXE, "progid": "Vissim.Vissim.26"},
            "launcher_server_wait_s": 30, "launcher_open_timeout_s": 600, "_path": str(tmp_path / "vissim_mcp.json")}


@pytest.fixture
def net(tmp_path):
    p = tmp_path / "ünïcode net.inpx"   # non-ASCII file name (Unicode handling)
    p.write_text("<xml/>", encoding="utf-8")
    return str(p.resolve())


def run(cfg, path, world, ui):
    return L.run_open(cfg, path, deps=world.deps(ui))


def _assert_hidden_before_dialogs(ui):
    """While a confirmation or notice window is up, the "Opening" window is closed."""
    showing = False
    for e in ui.events:
        if e[0] == "progress":
            showing = True
        elif e[0] in ("hide", "close"):
            showing = False
        elif e[0] in ("notice", "choose"):
            assert not showing, ui.events


# ---------------------------------------------------------------- 1, 2. mutex and path

def test_mutex_held_notice_and_exit(cfg, net):
    w, ui = World(), FakeUI()
    w.mutex["held"] = True
    assert run(cfg, net, w, ui) == 1
    assert ui.kinds("notice") == [("notice", L.TITLE, L.MSG_ONE_AT_A_TIME)]
    assert "ünïcode net" not in L.MSG_ONE_AT_A_TIME
    assert w.gets == [] and w.posts == [] and ui.kinds("progress") == []
    assert w.log_record()["answers"] == ["mutex_held"]


def test_mutex_released_at_end(cfg, net):
    w, ui = World(opens=[opened(net)]), FakeUI()
    assert run(cfg, net, w, ui) == 0
    assert w.mutex == {"held": False, "acquired": 1, "released": 1}


@pytest.mark.parametrize("make", ["missing", "wrong_ext"])
def test_invalid_path_notice_exit1(cfg, tmp_path, make):
    if make == "missing":
        p = str(tmp_path / "missing.inpx")
    else:
        q = tmp_path / "net.txt"
        q.write_text("x", encoding="utf-8")
        p = str(q)
    w, ui = World(), FakeUI()
    assert run(cfg, p, w, ui) == 1
    notes = ui.kinds("notice")
    assert len(notes) == 1 and p in notes[0][2]
    assert w.gets == [] and w.posts == [] and w.spawns == []
    assert w.mutex["released"] == 1


def test_inpx_extension_case_insensitive(cfg, tmp_path):
    p = tmp_path / "BIG.INPX"
    p.write_text("x", encoding="utf-8")
    w, ui = World(opens=[opened(str(p))]), FakeUI()
    assert run(cfg, str(p), w, ui) == 0


# ---------------------------------------------------------------- table: opened / front

@pytest.mark.parametrize("action", ["opened", "front"])
def test_opened_or_front_brings_window(cfg, net, action):
    w, ui = World(opens=[opened(net, action, pid=777)]), FakeUI()
    assert run(cfg, net, w, ui) == 0
    url, body, timeout = w.posts[0]
    assert url == f"http://127.0.0.1:{PORT}{L.ADMIN_OPEN_PATH}"
    assert body == {"path": net, "discard": False}
    assert timeout == 600
    assert w.gets[0][0] == f"http://127.0.0.1:{PORT}{L.ADMIN_STATUS_PATH}"
    assert w.fronts == [777]
    assert ui.events[0] == ("progress", "Opening ünïcode net.inpx...")
    assert ui.kinds("notice") == [] and ui.kinds("choose") == []
    assert w.spawns == []
    rec = w.log_record()
    assert rec["answers"] == [action] and rec["path"] == net and rec["pid"] == 4242 and rec["exit"] == 0


def test_relative_path_sent_absolute(cfg, net, monkeypatch):
    monkeypatch.chdir(Path(net).parent)
    w, ui = World(opens=[opened(net)]), FakeUI()
    assert run(cfg, Path(net).name, w, ui) == 0
    assert w.posts[0][1]["path"] == net


def test_open_timeout_is_config_value(cfg, net):
    cfg["launcher_open_timeout_s"] = 12
    w, ui = World(opens=[opened(net)]), FakeUI()
    run(cfg, net, w, ui)
    assert w.posts[0][2] == 12


def test_server_warnings_go_to_log(cfg, net):
    w, ui = World(opens=[opened(net, warnings=["1 VISSIM process other than the server's"])]), FakeUI()
    run(cfg, net, w, ui)
    assert "1 VISSIM process other than the server's" in w.log_record()["warnings"]


# ---------------------------------------------------------------- table: unsaved_changes

def test_unsaved_discard_reposts_with_expectations(cfg, net):
    other = r"C:\work\A.inpx"
    w, ui = World(opens=[unsaved(other, 3), opened(net)]), FakeUI([L.BTN_DISCARD])
    assert run(cfg, net, w, ui) == 0
    (_, title, body, buttons, default), = ui.kinds("choose")
    assert title == L.CONFIRM_TITLE == "VISSIM MCP - Another file is open"
    assert buttons == (L.BTN_DISCARD, L.BTN_CANCEL) == ("Discard and open", "Cancel")
    assert buttons[default] == "Cancel"
    assert f"File open now: {other}" in body
    assert f"File to open: {net}" in body
    assert "Unsaved changes made from the chat: 3" in body
    assert "Anything edited on the VISSIM screen and not saved will also be lost." in body
    assert [p[1] for p in w.posts] == [
        {"path": net, "discard": False},
        {"path": net, "discard": True, "expect_open_path": other, "expect_unsaved_writes": 3},
    ]
    assert w.fronts == [777]
    rec = w.log_record()
    assert rec["answers"] == ["unsaved_changes", "opened"]
    assert rec["choices"] == [L.BTN_DISCARD]
    _assert_hidden_before_dialogs(ui)


def test_unsaved_drawn_content_only(cfg, net):
    w, ui = World(opens=[unsaved(None, 0, drawn=12), opened(net)]), FakeUI([L.BTN_DISCARD])
    assert run(cfg, net, w, ui) == 0
    body = ui.kinds("choose")[0][2]
    assert "File open now: No file - there is content drawn on the VISSIM screen" in body
    assert "Unsaved changes made from the chat: 0" in body
    assert w.posts[1][1] == {"path": net, "discard": True, "expect_open_path": None, "expect_unsaved_writes": 0}


def test_unsaved_changed_meanwhile_shows_dialog_again(cfg, net):
    a, b = r"C:\work\A.inpx", r"C:\work\B.inpx"
    w = World(opens=[unsaved(a, 2), unsaved(b, 5), opened(net, "opened", pid=9)])
    ui = FakeUI([L.BTN_DISCARD, L.BTN_DISCARD])
    assert run(cfg, net, w, ui) == 0
    chooses = ui.kinds("choose")
    assert len(chooses) == 2
    assert f"File open now: {a}" in chooses[0][2] and "chat: 2" in chooses[0][2]
    assert f"File open now: {b}" in chooses[1][2] and "chat: 5" in chooses[1][2]
    assert w.posts[1][1]["expect_open_path"] == a and w.posts[1][1]["expect_unsaved_writes"] == 2
    assert w.posts[2][1]["expect_open_path"] == b and w.posts[2][1]["expect_unsaved_writes"] == 5
    assert w.fronts == [9]


def test_unsaved_changed_meanwhile_then_cancel(cfg, net):
    w = World(opens=[unsaved("C:/A.inpx", 1), unsaved("C:/B.inpx", 4)])
    ui = FakeUI([L.BTN_DISCARD, L.BTN_CANCEL])
    assert run(cfg, net, w, ui) == 0
    assert len(w.posts) == 2 and w.fronts == []


@pytest.mark.parametrize("answer", [L.BTN_CANCEL, None])
def test_unsaved_cancel_does_nothing(cfg, net, answer):
    w, ui = World(opens=[unsaved(r"C:\work\A.inpx", 1)]), FakeUI([answer])
    assert run(cfg, net, w, ui) == 0
    assert len(w.posts) == 1 and w.fronts == [] and w.plain == []
    rec = w.log_record()
    assert rec["answers"] == ["unsaved_changes"] and rec["choices"] == [L.BTN_CANCEL]
    assert ui.events[-1] == ("close",)


def test_unsaved_then_other_failure_after_discard(cfg, net):
    w = World(opens=[unsaved("C:/A.inpx", 1), err("com_error", "file read failed")])
    ui = FakeUI([L.BTN_DISCARD, L.BTN_CANCEL])
    assert run(cfg, net, w, ui) == 1
    assert ui.kinds("choose")[1][3] == (L.BTN_RETRY, L.BTN_CANCEL)


# ---------------------------------------------------------------- table: sim_running

def test_sim_running_notice(cfg, net):
    w, ui = World(opens=[err("sim_running", "simulation running")]), FakeUI()
    assert run(cfg, net, w, ui) == 1
    notes = ui.kinds("notice")
    assert len(notes) == 1 and notes[0][2] == L.MSG_SIM_RUNNING
    assert L.MSG_SIM_RUNNING == "A simulation is running, so the file cannot be opened. Try again after it finishes or is stopped."
    assert w.fronts == [] and ui.kinds("choose") == []
    assert w.log_record()["answers"] == ["sim_running"]
    _assert_hidden_before_dialogs(ui)


# ---------------------------------------------------------------- table: cannot start at all

def _fail_dialog(ui):
    chooses = ui.kinds("choose")
    assert len(chooses) == 1
    return chooses[0]


def test_launch_failed_offers_plain_vissim(cfg, net):
    w = World(opens=[err("vissim_absent", "Could not start VISSIM: license", {"launch_failed": True})])
    ui = FakeUI([L.BTN_PLAIN_OPEN])
    assert run(cfg, net, w, ui) == 1
    _, title, body, buttons, default = _fail_dialog(ui)
    assert buttons == (L.BTN_PLAIN_OPEN, L.BTN_CLOSE) == ("Open with plain VISSIM", "Close")
    assert "license" in body
    assert w.plain == [[EXE, net]]
    rec = w.log_record()
    assert rec["answers"] == ["vissim_absent"] and rec["choices"] == [L.BTN_PLAIN_OPEN]
    _assert_hidden_before_dialogs(ui)


def test_launch_failed_close_does_not_launch(cfg, net):
    w = World(opens=[err("vissim_absent", "x", {"launch_failed": True})])
    ui = FakeUI([L.BTN_CLOSE])
    assert run(cfg, net, w, ui) == 1
    assert w.plain == []


def test_vissim_absent_without_launch_failed_is_other_failure(cfg, net):
    w = World(opens=[err("vissim_absent", "VISSIM disappeared")])
    ui = FakeUI([L.BTN_CANCEL])
    assert run(cfg, net, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_RETRY, L.BTN_CANCEL)


def test_server_absent_starts_server_detached(cfg, net):
    calls = {"n": 0}

    def status(world):
        calls["n"] += 1
        return ConnectionRefusedError("refused") if calls["n"] <= 3 else ok_status()

    w = World(status=[status], opens=[opened(net)], port_free=True)
    ui = FakeUI()
    assert run(cfg, net, w, ui) == 0
    assert len(w.spawns) == 1
    assert w.gets_at_spawn == 0                    # if the port is free, nothing is asked before starting
    cmd, cwd = w.spawns[0]
    assert cmd == L.server_command(cfg, r"C:\Py\python.exe")
    assert cmd[1:] == ["-m", "vissim_mcp", "--serve", "--config", cfg["_path"]]
    assert Path(cwd) == L.repo_root()
    assert w.fronts == [777]


def test_server_command_falls_back_when_no_pythonw(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("")
    assert L.server_command({"_path": "c.json"}, str(exe))[0] == str(exe)
    (tmp_path / "pythonw.exe").write_text("")
    assert L.server_command({"_path": "c.json"}, str(exe)) == [
        str(tmp_path / "pythonw.exe"), "-m", "vissim_mcp", "--serve", "--config", "c.json"]
    assert L.server_command({}, str(exe)) == [str(tmp_path / "pythonw.exe"), "-m", "vissim_mcp", "--serve"]


def test_server_wait_timeout_offers_plain_vissim(cfg, net):
    cfg["launcher_server_wait_s"] = 7
    w = World(status=[ConnectionRefusedError("refused")], opens=[], port_free=True)
    ui = FakeUI([L.BTN_PLAIN_OPEN])
    assert run(cfg, net, w, ui) == 1
    assert len(w.spawns) == 1
    assert 7 <= w.t <= 8.5           # waited for the configured wait time
    _, _, body, buttons, _ = _fail_dialog(ui)
    assert buttons == (L.BTN_PLAIN_OPEN, L.BTN_CLOSE)
    assert "7 seconds" in body
    assert w.posts == [] and w.plain == [[EXE, net]]
    assert w.log_record()["answers"] == ["server_timeout"]


def test_server_spawn_error_offers_plain_vissim(cfg, net):
    w = World(status=[ConnectionRefusedError("refused")], spawn=OSError("executable missing"), port_free=True)
    ui = FakeUI([L.BTN_CLOSE])
    assert run(cfg, net, w, ui) == 1
    _, _, body, buttons, _ = _fail_dialog(ui)
    assert buttons == (L.BTN_PLAIN_OPEN, L.BTN_CLOSE) and "executable missing" in body
    assert w.t < 1                  # does not wait
    assert w.log_record()["answers"] == ["server_start_failed"]


def test_server_child_exits_early_offers_plain_vissim(cfg, net):
    w = World(status=[ConnectionRefusedError("refused")], spawn=FakeProc(code=1), port_free=True)
    ui = FakeUI([L.BTN_CLOSE])
    assert run(cfg, net, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_PLAIN_OPEN, L.BTN_CLOSE)
    assert w.t < 2


def test_port_occupied_by_other_program_offers_plain_vissim(cfg, net):
    w = World(status=[ConnectionRefusedError("refused")], port_free=False)
    ui = FakeUI([L.BTN_CLOSE])
    assert run(cfg, net, w, ui) == 1
    _, _, body, buttons, _ = _fail_dialog(ui)
    assert buttons == (L.BTN_PLAIN_OPEN, L.BTN_CLOSE) and str(PORT) in body
    assert w.spawns == []


@pytest.mark.parametrize("answer", [
    urllib.error.HTTPError("http://127.0.0.1", 404, "Not Found", {}, None),
    ValueError("not JSON"),
    {"hello": "world"},
])
def test_foreign_program_on_port_offers_plain_vissim(cfg, net, answer):
    w = World(status=[answer])
    ui = FakeUI([L.BTN_CLOSE])
    assert run(cfg, net, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_PLAIN_OPEN, L.BTN_CLOSE)
    assert w.spawns == [] and w.posts == []


# ---------------------------------------------------------------- table: any other failure

@pytest.mark.parametrize("answer", [
    err("com_error", "file read failed"),
    err("not_found", "file not found"),
    err("bad_args", "argument"),
    TimeoutError("timed out"),
    socket.timeout("timed out"),
    ConnectionResetError("connection lost"),
    urllib.error.URLError(ConnectionResetError("reset")),
    {"unexpected": True},
])
def test_other_failures_retry_cancel_without_plain_button(cfg, net, answer):
    w, ui = World(opens=[answer]), FakeUI([L.BTN_CANCEL])
    assert run(cfg, net, w, ui) == 1
    _, _, body, buttons, default = _fail_dialog(ui)
    assert buttons == (L.BTN_RETRY, L.BTN_CANCEL) == ("Retry", "Cancel")
    assert L.BTN_PLAIN_OPEN not in buttons
    assert w.plain == [] and w.fronts == []
    if isinstance(answer, dict) and "error" in answer:
        assert answer["error"]["message"] in body
    _assert_hidden_before_dialogs(ui)


def test_open_timeout_message_mentions_seconds(cfg, net):
    w, ui = World(opens=[TimeoutError("timed out")]), FakeUI([L.BTN_CANCEL])
    run(cfg, net, w, ui)
    assert "600 seconds" in _fail_dialog(ui)[2]
    assert w.log_record()["answers"] == ["open_timeout"]


def test_server_busy_on_status_is_other_failure(cfg, net):
    w, ui = World(status=[TimeoutError("timed out")]), FakeUI([L.BTN_CANCEL])
    assert run(cfg, net, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_RETRY, L.BTN_CANCEL)
    assert w.spawns == [] and w.posts == []


def test_retry_after_failure_then_success(cfg, net):
    w = World(status=[ok_status(other=1)], opens=[err("com_error", "x"), opened(net, "opened", pid=5)])
    ui = FakeUI([L.BTN_RETRY])
    assert run(cfg, net, w, ui) == 0
    assert len(w.posts) == 2 and w.posts[1][1] == {"path": net, "discard": False}
    assert len(w.gets) == 2                       # starts again from finding the server
    assert len(ui.kinds("notice")) == 1           # the other-VISSIM notice only once
    assert w.fronts == [5]
    assert ui.kinds("progress")[-1] == ("progress", "Opening ünïcode net.inpx...")
    rec = w.log_record()
    assert rec["answers"] == ["com_error", "opened"] and rec["choices"] == [L.BTN_RETRY]


def test_retry_after_server_died_restarts_server(cfg, net):
    seq = {"n": 0}

    def status(world):
        seq["n"] += 1
        if seq["n"] == 1:
            return ok_status()
        if seq["n"] == 2:
            return ConnectionRefusedError("no server")
        return ok_status()

    w = World(status=[status], opens=[ConnectionResetError("reset"), opened(net)], port_free=[False, True])
    ui = FakeUI([L.BTN_RETRY])
    assert run(cfg, net, w, ui) == 0
    assert len(w.spawns) == 1 and len(w.posts) == 2


def test_retry_twice_then_cancel(cfg, net):
    w = World(opens=[err("com_error", "a"), err("com_error", "b"), err("com_error", "c")])
    ui = FakeUI([L.BTN_RETRY, L.BTN_RETRY, L.BTN_CANCEL])
    assert run(cfg, net, w, ui) == 1
    assert len(w.posts) == 3
    assert w.log_record()["choices"] == [L.BTN_RETRY, L.BTN_RETRY, L.BTN_CANCEL]


def test_failure_dialog_closed_counts_as_cancel(cfg, net):
    w, ui = World(opens=[err("com_error", "a")]), FakeUI([None])
    assert run(cfg, net, w, ui) == 1
    assert len(w.posts) == 1


# ---------------------------------------------------------------- other-VISSIM notice

@pytest.mark.parametrize("other", [1, 3, None])
def test_other_vissim_notice_then_continue(cfg, net, other):
    w, ui = World(status=[ok_status(other=other)], opens=[opened(net)]), FakeUI()
    assert run(cfg, net, w, ui) == 0
    notes = ui.kinds("notice")
    assert notes == [("notice", L.TITLE, L.MSG_OTHER_VISSIM)]
    assert L.MSG_OTHER_VISSIM == ("A VISSIM that is not the server's is running. Editing the same file on both sides can overwrite each other's changes, and "
                                  "saving from the chat asks for one more confirmation.")
    i_notice = ui.events.index(notes[0])
    assert ui.events[i_notice - 1] == ("hide",)
    assert ("progress", "Opening ünïcode net.inpx...") in ui.events[i_notice + 1:]   # shown again when it continues to open
    assert len(w.posts) == 1


def test_other_vissim_missing_key_treated_as_unknown(cfg, net):
    st = ok_status()
    del st["other_vissim_processes"]
    w, ui = World(status=[st], opens=[opened(net)]), FakeUI()
    run(cfg, net, w, ui)
    assert len(ui.kinds("notice")) == 1


def test_no_other_vissim_no_notice(cfg, net):
    w, ui = World(status=[ok_status(other=0)], opens=[opened(net)]), FakeUI()
    run(cfg, net, w, ui)
    assert ui.kinds("notice") == []


# ---------------------------------------------------------------- no path (shortcut)

@pytest.mark.parametrize("action", ["launched", "front"])
def test_no_path_posts_null_and_fronts(cfg, action):
    w, ui = World(opens=[opened(None, action, pid=31)]), FakeUI()
    assert run(cfg, None, w, ui) == 0
    assert w.posts[0][1] == {"path": None}
    assert w.fronts == [31]
    assert ui.events[0] == ("progress", "Starting VISSIM...")
    rec = w.log_record()
    assert rec["path"] is None and rec["answers"] == [action]


def test_no_path_never_came_up_offers_plain_launch(cfg):
    w = World(opens=[err("vissim_absent", "x", {"launch_failed": True})])
    ui = FakeUI([L.BTN_PLAIN_LAUNCH])
    assert run(cfg, None, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_PLAIN_LAUNCH, L.BTN_CLOSE) == ("Start plain VISSIM", "Close")
    assert w.plain == [[EXE]]


def test_no_path_server_timeout_offers_plain_launch(cfg):
    w = World(status=[ConnectionRefusedError("x")], port_free=True)
    ui = FakeUI([L.BTN_CLOSE])
    assert run(cfg, None, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_PLAIN_LAUNCH, L.BTN_CLOSE)
    assert w.plain == []


def test_no_path_sim_running_and_other_failure(cfg):
    w, ui = World(opens=[err("sim_running")]), FakeUI()
    assert run(cfg, None, w, ui) == 1
    assert ui.kinds("notice")[0][2] == L.MSG_SIM_RUNNING
    w, ui = World(opens=[err("com_error", "y")]), FakeUI([L.BTN_CANCEL])
    assert run(cfg, None, w, ui) == 1
    assert _fail_dialog(ui)[3] == (L.BTN_RETRY, L.BTN_CANCEL)


def test_no_path_mutex_held(cfg):
    w, ui = World(), FakeUI()
    w.mutex["held"] = True
    assert run(cfg, None, w, ui) == 1
    assert ui.kinds("notice")[0][2] == L.MSG_ONE_AT_A_TIME


# ---------------------------------------------------------------- failure to bring the window to the front, and the log

@pytest.mark.parametrize("front", [False, OSError("no window")])
def test_front_failure_ignored_and_logged(cfg, net, front):
    w, ui = World(opens=[opened(net, pid=88)], front=front), FakeUI()
    assert run(cfg, net, w, ui) == 0
    assert any("88" in x and "front" in x for x in w.log_record()["warnings"])
    assert ui.kinds("notice") == [] and ui.kinds("choose") == []


def test_front_skipped_when_pid_missing(cfg, net):
    w, ui = World(opens=[opened(net, pid=None)]), FakeUI()
    assert run(cfg, net, w, ui) == 0
    assert w.fronts == []
    assert any("front" in x for x in w.log_record()["warnings"])


def test_log_line_written_to_log_root(cfg, net):
    w, ui = World(opens=[opened(net)]), FakeUI()
    run(cfg, net, w, ui)
    path, line = w.logs[0]
    assert path == Path(cfg["log_root"]) / "launcher.log"
    assert "\n" not in line
    rec = json.loads(line)
    assert set(rec) >= {"time", "pid", "path", "answers", "choices", "warnings", "exit"}
    assert rec["time"] == "2026-10-18T12:00:00"


def test_log_write_failure_ignored(cfg, net):
    w, ui = World(opens=[opened(net)]), FakeUI()
    w.log_raises = True
    assert run(cfg, net, w, ui) == 0
    w2, ui2 = World(opens=[err("com_error", "x")]), FakeUI([L.BTN_CANCEL])
    w2.log_raises = True
    assert run(cfg, net, w2, ui2) == 1


def test_append_log_line_creates_folder_and_appends(tmp_path):
    p = tmp_path / "a" / "b" / "launcher.log"
    L.append_log_line(p, '{"x": 1}')
    L.append_log_line(p, '{"x": 2}')
    assert p.read_text(encoding="utf-8").splitlines() == ['{"x": 1}', '{"x": 2}']


def test_ui_closed_and_mutex_released_on_unexpected_error(cfg, net):
    w = World()

    def boom(url, body, timeout):
        raise RuntimeError("unexpected")

    deps = w.deps(FakeUI())
    deps.http_post = boom
    ui = deps.ui
    ui.answers = [L.BTN_CANCEL]
    assert L.run_open(cfg, net, deps=deps) == 1
    assert ui.events[-1] == ("close",)
    assert w.mutex["released"] == 1


# ---------------------------------------------------------------- classifying the status reply

@pytest.mark.parametrize("exc,kind", [
    (ConnectionRefusedError("x"), "absent"),
    (urllib.error.URLError(ConnectionRefusedError("x")), "absent"),
    (TimeoutError("x"), "busy"),
    (socket.timeout("x"), "busy"),
    (urllib.error.URLError(socket.timeout("x")), "busy"),
    (urllib.error.HTTPError("u", 500, "e", {}, None), "foreign"),
    (ValueError("json"), "foreign"),
])
def test_classify_status_error(exc, kind):
    assert L.classify_status_error(exc) == kind


def test_plain_launch_failure_notice(cfg, net):
    w = World(opens=[err("vissim_absent", "x", {"launch_failed": True})])

    def bad(cmd):
        raise FileNotFoundError("exe missing")

    w.launch_plain = bad
    ui = FakeUI([L.BTN_PLAIN_OPEN])
    assert run(cfg, net, w, ui) == 1
    assert "exe missing" in ui.kinds("notice")[0][2]
    assert any("plain VISSIM" in x for x in w.log_record()["warnings"])


def test_default_log_writer_is_append_log_line():
    d = L.LauncherDeps(ui=FakeUI(), acquire_mutex=lambda: 1, release_mutex=lambda h: None,
                       http_get=None, http_post=None, port_free=None, spawn_server=None, launch_plain=None,
                       bring_to_front=None)
    assert d.write_log is L.append_log_line
    assert isinstance(d.now(), str) and isinstance(d.pid, int)
