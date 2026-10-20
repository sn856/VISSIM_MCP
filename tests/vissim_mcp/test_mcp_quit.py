"""Shutdown procedure — shared by the tray's "quit server and VISSIM" and the stop command (without VISSIM, using a fake session and fake dialogs)."""
import pytest

from vissim_mcp import server as sv
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.errors import KIND_COM_ERROR, ToolError
from vissim_mcp.guard import Guard
from vissim_mcp.state import ServerState

from .test_mcp_server import FakeObjects, FakeResults, FakeView, NoCom, _cfg

PATH = "C:\\project\\net.inpx"


class QuitSession:
    """A fake with only the session functions the shutdown procedure calls. Records the call order in calls."""

    def __init__(self, rt_holder):
        self.rt = rt_holder
        self.calls = []
        self.check = {"checked": True, "note": None, "warnings": [], "lost": None, "lost_error": None,
                      "switched": False}
        self.check_raises = None
        self.info = {"vissim": True, "open_path": PATH, "drawn_links": 0, "unsaved_writes": 2}
        self.other = 0
        self.save_error = None
        self.recovery_error = None
        self.recovered = False

    def _locked(self):
        return self.rt[0].lock.locked()

    def check_current(self, tool):
        assert not self.recovered, "a current check after the recovery copy would make the server follow the recovery file"
        self.calls.append(("check", tool))
        if self.check_raises is not None:
            raise self.check_raises
        return self.check

    def open_info(self, tool="session"):
        assert not self.recovered
        self.calls.append(("info", tool))
        return dict(self.info)

    def other_vissim_processes(self):
        self.calls.append(("other",))
        return self.other

    def save_on_quit(self, force=False):
        self.calls.append(("save", force, self._locked()))
        if self.save_error is not None:
            raise self.save_error
        return {"saved_path": PATH, "backup_path": PATH + ".bak", "writes_flushed": 2}

    def save_recovery_copy(self, info=None):
        self.calls.append(("recovery", self._locked()))
        self.recovered = True
        if self.recovery_error is not None:
            raise self.recovery_error
        return {"recovery_path": "C:\\runs\\mcp\\recovery\\net_x.inpx", "unsaved_writes": 2}

    def shutdown(self):
        self.calls.append(("shutdown", self._locked()))
        return {"was_running": True}

    def names(self):
        return [c[0] for c in self.calls]


class QuitSimulation:
    def __init__(self, state, can_stop=True):
        self.state = state
        self.can_stop = can_stop
        self.calls = []

    def stop_simulation(self):
        self.calls.append("stop")
        self.state.set_simulation(state="stopping")
        return {"state": "stopping"}

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.can_stop:
            self.state.set_simulation(state="idle")
            return True
        return False


class Asker:
    """A fake dialog. Returns the answers in order and records (kind, body, timeout)."""

    def __init__(self, *answers, rt=None):
        self.answers = list(answers)
        self.asked = []
        self.rt = rt

    def __call__(self, kind, text, timeout_s):
        sim = self.rt.state.full()["simulation"]["state"] if self.rt is not None else None
        self.asked.append((kind, text, timeout_s, sim))
        if not self.answers:
            pytest.fail(f"unexpected dialog: {kind} {text}")
        return self.answers.pop(0)


def _rt(tmp_path, can_stop=True):
    cfg = _cfg(tmp_path)
    cfg["sim_chunk_s"] = 5
    state = ServerState(cfg["port"])
    holder = []
    sess = QuitSession(holder)
    sim = QuitSimulation(state, can_stop=can_stop)
    rt = sv.Runtime(cfg, state=state, com=NoCom(), changelog=ChangeLog(cfg["log_root"]), guard=Guard([]),
                    session=sess, objects=FakeObjects(), simulation=sim, results=FakeResults(), view=FakeView())
    holder.append(rt)
    return rt


def _quit(rt, asker):
    exits = []
    res = rt.request_quit(confirm=asker, exit_fn=exits.append)
    return res, exits


def _assert_recovery_last(rt):
    """The recovery copy is made once, right before session cleanup (inside the same lock), with no current check after it."""
    names = rt.session.names()
    assert names.count("recovery") == 1
    i = names.index("recovery")
    assert names[i + 1:] == ["shutdown"]
    assert rt.session.calls[i][1] is True and rt.session.calls[i + 1][1] is True   # inside the lock


# ---------------------------------------------------------------- the four target cases

def test_open_file_shows_yes_no_cancel_dialog(tmp_path):
    rt = _rt(tmp_path)
    asker = Asker(sv.QUIT_CANCEL)
    res, exits = _quit(rt, asker)
    kind, text, timeout, _ = asker.asked[0]
    assert kind == sv.BOX_YES_NO_CANCEL and timeout == 1.0
    assert "Shutting down the server also closes VISSIM." in text
    assert PATH in text and "Unsaved changes made through the conversation: 2" in text
    assert "The server cannot know about edits made in the VISSIM window." in text
    assert "Yes = save and quit / No = quit without saving (recovery copy) / Cancel = keep running" in text
    assert res["quit"] is False and exits == []
    assert rt.session.names()[:2] == ["check", "info"]


def test_drawn_content_without_a_file_shows_ok_cancel_dialog(tmp_path):
    rt = _rt(tmp_path)
    rt.session.info = {"vissim": True, "open_path": None, "drawn_links": 3, "unsaved_writes": 0}
    asker = Asker(sv.QUIT_CANCEL)
    res, exits = _quit(rt, asker)
    kind, text, _, _ = asker.asked[0]
    assert kind == sv.BOX_OK_CANCEL
    assert ("Shutting down the server also closes VISSIM. There is content drawn on screen that is not saved to a file (3 link(s))."
            in text)
    assert "OK = keep a recovery copy and quit / Cancel = keep running" in text
    assert "Yes =" not in text
    assert res["quit"] is False and exits == []


@pytest.mark.parametrize("info", [
    {"vissim": True, "open_path": None, "drawn_links": 0, "unsaved_writes": 0},
    {"vissim": False, "open_path": None, "drawn_links": 0, "unsaved_writes": 0},
])
def test_neither_file_nor_drawn_content_closes_without_asking(tmp_path, info):
    rt = _rt(tmp_path)
    rt.session.info = info
    res, exits = _quit(rt, Asker())
    assert res["quit"] is True and exits == [0]
    assert "recovery" not in rt.session.names() and "save" not in rt.session.names()
    assert rt.session.names()[-1] == "shutdown"


def test_uncountable_drawn_content_is_assumed_present(tmp_path):
    rt = _rt(tmp_path)
    rt.session.info = {"vissim": True, "open_path": None, "drawn_links": None, "unsaved_writes": 0}
    asker = Asker(sv.QUIT_OK)
    res, exits = _quit(rt, asker)
    assert asker.asked[0][0] == sv.BOX_OK_CANCEL and "could not count the links" in asker.asked[0][1]
    assert res["quit"] is True and exits == [0]
    _assert_recovery_last(rt)


@pytest.mark.parametrize("how", ["note", "raise"])
def test_failed_current_check_assumes_content_exists(tmp_path, how):
    rt = _rt(tmp_path)
    if how == "note":
        rt.session.check = {"checked": False, "note": "COM error", "warnings": ["could not check"], "lost": None,
                            "lost_error": None, "switched": False}
    else:
        rt.session.check_raises = RuntimeError("unexpected")
    rt.session.info = {"vissim": True, "open_path": None, "drawn_links": 0, "unsaved_writes": 0}
    asker = Asker(sv.QUIT_TIMEOUT)
    res, exits = _quit(rt, asker)
    assert asker.asked and asker.asked[0][0] == sv.BOX_OK_CANCEL
    assert res["quit"] is True and exits == [0]
    _assert_recovery_last(rt)


def test_failed_current_check_with_a_recorded_file_shows_the_three_button_dialog(tmp_path):
    rt = _rt(tmp_path)
    rt.session.check = {"checked": False, "note": "COM error", "warnings": [], "lost": None, "lost_error": None,
                        "switched": False}
    asker = Asker(sv.QUIT_NO)
    res, _ = _quit(rt, asker)
    assert asker.asked[0][0] == sv.BOX_YES_NO_CANCEL and res["quit"] is True
    _assert_recovery_last(rt)


def test_vissim_lost_in_current_check_closes_without_asking(tmp_path):
    rt = _rt(tmp_path)
    rt.session.check = {"checked": False, "note": "disappeared", "warnings": [], "lost": 2, "lost_error": "RPC",
                        "switched": False}
    rt.session.info = {"vissim": False, "open_path": None, "drawn_links": 0, "unsaved_writes": 0}
    res, exits = _quit(rt, Asker())
    assert res["quit"] is True and exits == [0] and "recovery" not in rt.session.names()


# ---------------------------------------------------------------- the three buttons and timeout

def test_yes_saves_in_place_then_quits(tmp_path):
    rt = _rt(tmp_path)
    res, exits = _quit(rt, Asker(sv.QUIT_YES))
    assert res["quit"] is True and exits == [0]
    assert res["saved_path"] == PATH and res["recovery_path"] is None
    assert ("save", False, True) in rt.session.calls
    assert "recovery" not in rt.session.names()
    assert rt.session.names()[-1] == "shutdown"


@pytest.mark.parametrize("answer", [sv.QUIT_NO, sv.QUIT_TIMEOUT])
def test_no_or_timeout_keeps_a_recovery_copy_then_quits(tmp_path, answer):
    rt = _rt(tmp_path)
    res, exits = _quit(rt, Asker(answer))
    assert res["quit"] is True and exits == [0]
    assert res["recovery_path"].endswith("net_x.inpx") and res["saved_path"] is None
    assert "save" not in rt.session.names()
    _assert_recovery_last(rt)


def test_cancel_does_not_quit_and_quit_can_be_requested_again(tmp_path):
    rt = _rt(tmp_path)
    res, exits = _quit(rt, Asker(sv.QUIT_CANCEL))
    assert res == {"quit": False, "reason": "cancelled", "unsaved_writes": 2}
    assert exits == [] and "shutdown" not in rt.session.names() and "recovery" not in rt.session.names()
    assert not rt.lock.locked()
    res2, exits2 = _quit(rt, Asker(sv.QUIT_NO))      # a quit request is accepted again after a cancel
    assert res2["quit"] is True and exits2 == [0]


def test_failure_to_show_the_dialog_counts_as_timeout():
    def boom(*a):
        raise OSError("no window")
    assert sv.quit_dialog(sv.BOX_YES_NO_CANCEL, "x", 1, box=boom) == sv.QUIT_TIMEOUT


@pytest.mark.parametrize("kind,ret,answer", [
    (sv.BOX_YES_NO_CANCEL, 6, sv.QUIT_YES), (sv.BOX_YES_NO_CANCEL, 7, sv.QUIT_NO),
    (sv.BOX_YES_NO_CANCEL, 2, sv.QUIT_CANCEL), (sv.BOX_YES_NO_CANCEL, 32000, sv.QUIT_TIMEOUT),
    (sv.BOX_OK_CANCEL, 1, sv.QUIT_OK), (sv.BOX_OK_CANCEL, 2, sv.QUIT_CANCEL), (sv.BOX_OK_CANCEL, 32000, sv.QUIT_TIMEOUT),
    (sv.BOX_YES_NO, 6, sv.QUIT_YES), (sv.BOX_YES_NO, 7, sv.QUIT_NO), (sv.BOX_YES_NO, 32000, sv.QUIT_TIMEOUT),
])
def test_message_box_buttons_and_answers(kind, ret, answer):
    seen = []

    def box(text, title, flags, timeout_ms):
        seen.append((text, title, flags, timeout_ms))
        return ret
    assert sv.quit_dialog(kind, "body", 2.5, box=box) == answer
    text, title, flags, timeout_ms = seen[0]
    assert text == "body" and timeout_ms == 2500
    assert flags & 0xF == {sv.BOX_YES_NO_CANCEL: 0x3, sv.BOX_OK_CANCEL: 0x1, sv.BOX_YES_NO: 0x4}[kind]


# ---------------------------------------------------------------- second dialog for another VISSIM

@pytest.mark.parametrize("other", [1, None])
def test_another_vissim_shows_a_second_dialog_and_yes_forces_the_save(tmp_path, other):
    rt = _rt(tmp_path)
    rt.session.other = other
    asker = Asker(sv.QUIT_YES, sv.QUIT_YES)
    res, exits = _quit(rt, asker)
    assert asker.asked[1][0] == sv.BOX_YES_NO
    assert asker.asked[1][1].startswith("A VISSIM that is not the server's is running. Save anyway?")
    assert asker.asked[1][2] == 1.0
    assert ("save", True, True) in rt.session.calls and "recovery" not in rt.session.names()
    assert res["saved_path"] == PATH and exits == [0]


@pytest.mark.parametrize("second", [sv.QUIT_NO, sv.QUIT_TIMEOUT])
def test_second_dialog_no_or_timeout_keeps_a_recovery_copy(tmp_path, second):
    rt = _rt(tmp_path)
    rt.session.other = 2
    res, exits = _quit(rt, Asker(sv.QUIT_YES, second))
    assert "save" not in rt.session.names()
    assert res["recovery_path"] is not None and exits == [0]
    _assert_recovery_last(rt)


def test_failed_save_keeps_a_recovery_copy_and_logs_the_error(tmp_path):
    rt = _rt(tmp_path)
    rt.session.save_error = ToolError(KIND_COM_ERROR, "SaveNet rejected (fake)")
    res, exits = _quit(rt, Asker(sv.QUIT_YES))
    assert res["quit"] is True and exits == [0]
    assert res["saved_path"] is None and res["recovery_path"] is not None
    assert "SaveNet rejected (fake)" in res["save_error"]
    _assert_recovery_last(rt)
    rows = [r for r in rt.changelog.read() if r.get("tool") == "quit"]
    assert rows and rows[-1]["kind"] == "save" and rows[-1]["ok"] is False and "SaveNet rejected (fake)" in rows[-1]["error"]


def test_failed_recovery_copy_still_closes(tmp_path):
    rt = _rt(tmp_path)
    rt.session.recovery_error = ToolError(KIND_COM_ERROR, "SaveNetAs rejected (fake)")
    res, exits = _quit(rt, Asker(sv.QUIT_NO))
    assert res["quit"] is True and res["recovery_path"] is None and exits == [0]
    assert rt.session.names()[-1] == "shutdown"


# ---------------------------------------------------------------- drawn content only

@pytest.mark.parametrize("answer", [sv.QUIT_OK, sv.QUIT_TIMEOUT])
def test_drawn_content_ok_or_timeout_keeps_a_recovery_copy(tmp_path, answer):
    rt = _rt(tmp_path)
    rt.session.info = {"vissim": True, "open_path": None, "drawn_links": 4, "unsaved_writes": 0}
    res, exits = _quit(rt, Asker(answer))
    assert res["quit"] is True and exits == [0] and res["recovery_path"] is not None
    assert "save" not in rt.session.names()
    _assert_recovery_last(rt)


def test_drawn_content_cancel_keeps_running(tmp_path):
    rt = _rt(tmp_path)
    rt.session.info = {"vissim": True, "open_path": None, "drawn_links": 4, "unsaved_writes": 0}
    res, exits = _quit(rt, Asker(sv.QUIT_CANCEL))
    assert res["quit"] is False and exits == [] and "shutdown" not in rt.session.names()


# ---------------------------------------------------------------- while a simulation is running

def test_running_simulation_is_stopped_and_awaited_before_the_dialog(tmp_path):
    rt = _rt(tmp_path)
    rt.state.set_simulation(state="running")
    asker = Asker(sv.QUIT_NO, rt=rt)
    res, exits = _quit(rt, asker)
    assert rt.simulation.calls == ["stop", ("wait", 35.0)]
    assert asker.asked[0][3] == "idle"                     # already stopped by the time the dialog appears
    assert res["quit"] is True and exits == [0]
    _assert_recovery_last(rt)


def test_stopping_simulation_is_awaited_without_calling_stop_again(tmp_path):
    rt = _rt(tmp_path)
    rt.state.set_simulation(state="stopping")
    res, _ = _quit(rt, Asker(sv.QUIT_NO, rt=rt))
    assert rt.simulation.calls == [("wait", 35.0)] and res["quit"] is True


def test_unstoppable_simulation_tries_only_a_recovery_copy_without_a_dialog_and_closes(tmp_path):
    rt = _rt(tmp_path, can_stop=False)
    rt.state.set_simulation(state="running")
    res, exits = _quit(rt, Asker())
    assert res["quit"] is True and exits == [0]
    assert rt.session.names() == ["recovery", "shutdown"]   # no current check, no dialog
    assert res["recovery_path"] is not None


def test_unstoppable_simulation_and_failed_recovery_copy_still_closes(tmp_path):
    rt = _rt(tmp_path, can_stop=False)
    rt.state.set_simulation(state="running")
    rt.session.recovery_error = ToolError(KIND_COM_ERROR, "busy")
    res, exits = _quit(rt, Asker())
    assert res["quit"] is True and exits == [0] and res["recovery_path"] is None


# ---------------------------------------------------------------- other

def test_second_quit_request_is_ignored(tmp_path):
    rt = _rt(tmp_path)
    rt.session.info = {"vissim": False, "open_path": None, "drawn_links": 0, "unsaved_writes": 0}
    exits = []
    rt.request_quit(confirm=Asker(), exit_fn=exits.append)
    res = rt.request_quit(confirm=Asker(), exit_fn=exits.append)
    assert res["quit"] is False and exits == [0]


def test_answer_constants():
    assert (sv.QUIT_YES, sv.QUIT_NO, sv.QUIT_CANCEL, sv.QUIT_OK, sv.QUIT_TIMEOUT) == \
        ("yes", "no", "cancel", "ok", "timeout")
