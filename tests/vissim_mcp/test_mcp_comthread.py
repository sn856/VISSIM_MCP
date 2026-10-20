"""Dedicated COM thread — calls only on one thread, exceptions passed through unchanged, only busy rejections retried a limited number of times."""
import threading

import pytest

from vissim_mcp import comthread as ct


class FakeBusy(Exception):
    """Imitates the COM error VISSIM raises when it rejects a call as busy."""


class FakeComError(Exception):
    """An ordinary error that must not be retried."""


def is_fake_busy(exc):
    return isinstance(exc, FakeBusy)


@pytest.fixture
def thread():
    t = ct.ComThread(retry_count=5, retry_interval_s=0.0, is_retryable=is_fake_busy)
    t.start()
    yield t
    t.stop()


def test_all_calls_run_on_the_same_thread(thread):
    ids = {thread.submit(threading.get_ident) for _ in range(5)}
    assert len(ids) == 1
    assert ids.pop() != threading.get_ident()


def test_arguments_and_return_value_are_passed(thread):
    assert thread.submit(lambda a, b=0: a + b, 2, b=3) == 5


def test_exception_reaches_the_caller_with_type_and_message_preserved(thread):
    with pytest.raises(FakeComError, match="original message") as ei:
        thread.submit(_raise, FakeComError("original message"))
    assert type(ei.value) is FakeComError
    # not retryable, so it is called only once
    calls = []
    with pytest.raises(FakeComError):
        thread.submit(_count_then_raise, calls, FakeComError("x"))
    assert len(calls) == 1


def test_success_on_the_fifth_call_after_four_busy_rejections(thread):
    counter = {"n": 0}

    def fn():
        counter["n"] += 1
        if counter["n"] <= 4:
            raise FakeBusy("busy")
        return "ok"

    assert thread.submit(fn) == "ok"
    assert counter["n"] == 5


def test_six_busy_rejections_raise(thread):
    counter = {"n": 0}

    def fn():
        counter["n"] += 1
        raise FakeBusy(f"busy {counter['n']}")

    with pytest.raises(FakeBusy) as ei:
        thread.submit(fn)
    assert counter["n"] == 6          # first call + 5 retries
    assert "busy 6" in str(ei.value)   # the last exception as is


def test_retry_interval_is_respected():
    waits = []
    t = ct.ComThread(retry_count=2, retry_interval_s=0.25, is_retryable=is_fake_busy,
                     sleep=waits.append)
    t.start()
    try:
        with pytest.raises(FakeBusy):
            t.submit(_raise, FakeBusy("b"))
    finally:
        t.stop()
    assert waits == [0.25, 0.25]


def test_submit_nowait_returns_a_future(thread):
    fut = thread.submit_nowait(lambda: 7)
    assert fut.result(timeout=2) == 7
    fut = thread.submit_nowait(_raise, FakeComError("later"))
    with pytest.raises(FakeComError, match="later"):
        fut.result(timeout=2)


def test_timeout(thread):
    ev = threading.Event()
    try:
        with pytest.raises(TimeoutError):
            thread.submit(ev.wait, 5, timeout=0.05)
    finally:
        ev.set()


def test_rejected_before_start_and_after_stop():
    t = ct.ComThread(is_retryable=is_fake_busy)
    with pytest.raises(RuntimeError):
        t.submit(lambda: 1)
    t.start()
    assert t.submit(lambda: 1) == 1
    t.stop()
    with pytest.raises(RuntimeError):
        t.submit(lambda: 1)


def test_submit_from_inside_the_com_thread_runs_directly(thread):
    # nested calls do not deadlock
    assert thread.submit(lambda: thread.submit(lambda: 9)) == 9


def test_default_busy_check_uses_hresult():
    class ComErr(Exception):
        def __init__(self, hresult):
            super().__init__(hresult, "busy")
            self.hresult = hresult

    assert ct.is_busy_error(ComErr(ct.BUSY_HRESULT)) is True
    assert ct.is_busy_error(ComErr(-2147352567)) is False
    assert ct.is_busy_error(ValueError("x")) is False
    assert ct.is_busy_error(RuntimeError(f"error {ct.BUSY_HRESULT}")) is True


def test_defaults_equal_the_config_values():
    from vissim_mcp.config import TUNABLE_DEFAULTS
    t = ct.ComThread()
    assert t.retry_count == TUNABLE_DEFAULTS["com_retry_count"]
    assert t.retry_interval_s == TUNABLE_DEFAULTS["com_retry_interval_s"]


def _raise(exc):
    raise exc


def _count_then_raise(calls, exc):
    calls.append(1)
    raise exc
