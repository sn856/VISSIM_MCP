"""Dedicated COM thread — every VISSIM COM call runs on this one thread (apartment constraint).

win32com/pythoncom are imported only inside this module, and lazily inside functions (skipped when absent, e.g. in tests).
Exceptions raised during a call are re-raised to the caller unchanged (type and message preserved).
Only COM errors where VISSIM rejects the call as busy are retried, retry_count times at retry_interval_s intervals;
if it still fails, the last exception is raised as is. The retry predicate can be injected (for tests).
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from .config import TUNABLE_DEFAULTS

# hresult used when VISSIM rejects a call because it is busy with another one (RPC_E_CALL_REJECTED)
BUSY_HRESULT = -2147418111


def is_busy_error(exc: BaseException) -> bool:
    """True if the hresult of a pywintypes.com_error is BUSY_HRESULT or the message contains that value."""
    if getattr(exc, "hresult", None) == BUSY_HRESULT:
        return True
    args = getattr(exc, "args", ())
    if args and args[0] == BUSY_HRESULT:
        return True
    return str(BUSY_HRESULT) in str(exc)


class ComThread:
    def __init__(self, retry_count: int = TUNABLE_DEFAULTS["com_retry_count"],
                 retry_interval_s: float = TUNABLE_DEFAULTS["com_retry_interval_s"],
                 is_retryable: Callable[[BaseException], bool] = is_busy_error,
                 sleep: Callable[[float], None] = time.sleep,
                 name: str = "vissim-com"):
        self.retry_count = int(retry_count)
        self.retry_interval_s = float(retry_interval_s)
        self._is_retryable = is_retryable
        self._sleep = sleep
        self._name = name
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop = object()   # stop sentinel

    # ---- lifecycle ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(self._stop)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._thread = None

    @property
    def is_com_thread(self) -> bool:
        return self._thread is not None and threading.current_thread() is self._thread

    # ---- calls ----
    def submit_nowait(self, fn: Callable[..., Any], *args, **kwargs) -> Future:
        """Queue the call and return a Future (for background work)."""
        if not self._running or self._thread is None:
            raise RuntimeError("the COM thread is not running (before start or after stop)")
        fut: Future = Future()
        if self.is_com_thread:
            # submitting again from inside the COM thread would deadlock, so run it directly
            self._execute(fut, fn, args, kwargs)
            return fut
        self._queue.put((fut, fn, args, kwargs))
        return fut

    def submit(self, fn: Callable[..., Any], *args, timeout: float | None = None, **kwargs) -> Any:
        """Run fn(*args, **kwargs) on the COM thread and wait for the result. Exceptions are re-raised as is."""
        return self.submit_nowait(fn, *args, **kwargs).result(timeout=timeout)

    # ---- internals ----
    def _call_with_retry(self, fn, args, kwargs):
        attempts = 1 + self.retry_count
        for i in range(attempts):
            try:
                return fn(*args, **kwargs)
            except BaseException as e:   # noqa: BLE001 — passed through to the caller unchanged
                if i + 1 < attempts and self._is_retryable(e):
                    self._sleep(self.retry_interval_s)
                    continue
                raise

    def _execute(self, fut: Future, fn, args, kwargs) -> None:
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(self._call_with_retry(fn, args, kwargs))
        except BaseException as e:   # noqa: BLE001
            fut.set_exception(e)

    def _run(self) -> None:
        pythoncom = None
        try:
            import pythoncom as _pc   # lazy import — skipped where pywin32 is absent (tests)
            pythoncom = _pc
            pythoncom.CoInitialize()
        except ImportError:
            pythoncom = None
        try:
            while True:
                item = self._queue.get()
                if item is self._stop:
                    break
                fut, fn, args, kwargs = item
                self._execute(fut, fn, args, kwargs)
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()
