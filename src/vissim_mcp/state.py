"""Shared state — the single state object shared by the whole server (protected by a lock).

snapshot() is the summary attached to every tool response; full() is the complete form for the vissim_status response.
"""
from __future__ import annotations

import copy
import threading
import uuid
from typing import Any

from .clock import now_iso

SIM_IDLE = "idle"
SIM_RUNNING = "running"
SIM_STOPPING = "stopping"
SIM_FINISHED = "finished"
SIM_FAILED = "failed"
SIM_STATES = (SIM_IDLE, SIM_RUNNING, SIM_STOPPING, SIM_FINISHED, SIM_FAILED)

_SIM_FIELDS = ("state", "run_id", "progress_s", "period_s", "run_index", "num_runs", "started_at", "error")
_VISSIM_FIELDS = ("running", "version", "launched_at", "pid")


def _empty_simulation() -> dict[str, Any]:
    return {"state": SIM_IDLE, "run_id": None, "progress_s": None, "period_s": None,
            "run_index": None, "num_runs": None, "started_at": None, "error": None}


class ServerState:
    def __init__(self, port: int):
        self._lock = threading.RLock()
        self.server_start_id = uuid.uuid4().hex
        self.started_at = now_iso()
        self.port = int(port)
        self._vissim: dict[str, Any] = {"running": False, "version": None, "launched_at": None, "pid": None}
        self._network: dict[str, Any] = {"path": None, "unsaved_writes": 0, "last_write_at": None, "counts": {}}
        self._simulation: dict[str, Any] = _empty_simulation()
        # Changed flag: the file open in the server's VISSIM changed without the conversation knowing. While set, the first write is rejected and the flag is cleared.
        # Not included in the response state (contract keys are fixed) — it only shows up in the rejection's error.detail.
        self._changed: dict[str, Any] | None = None

    # ---- changed flag ----
    def set_network_changed(self, previous_path: str | None, current_path: str | None, changed_by: str) -> None:
        with self._lock:
            self._changed = {"previous_path": previous_path, "current_path": current_path, "changed_by": changed_by}

    def network_changed(self) -> dict[str, Any] | None:
        """A copy of {previous_path, current_path, changed_by} if set, otherwise None."""
        with self._lock:
            return dict(self._changed) if self._changed is not None else None

    def clear_network_changed(self) -> dict[str, Any] | None:
        """Clear the flag and return its previous value (None if it was not set)."""
        with self._lock:
            prev, self._changed = self._changed, None
            return prev

    # ---- network ----
    def note_write(self) -> None:
        """Record one unsaved write (counter +1, timestamp)."""
        with self._lock:
            self._network["unsaved_writes"] += 1
            self._network["last_write_at"] = now_iso()

    def reset_writes(self) -> int:
        """Reset the write counter to 0 and return the value it had before."""
        with self._lock:
            before = int(self._network["unsaved_writes"])
            self._network["unsaved_writes"] = 0
            return before

    def set_network(self, path: str | None, counts: dict[str, int] | None = None) -> None:
        """Replace the open network. The write counter and timestamp are reset."""
        with self._lock:
            self._network = {"path": path, "unsaved_writes": 0, "last_write_at": None,
                             "counts": dict(counts or {})}

    # ---- VISSIM ----
    def set_vissim(self, **fields: Any) -> None:
        """Update only the given fields among running, version, launched_at and pid.

        When running becomes True, launched_at is set to now; when running is False, pid and launched_at are cleared.
        """
        bad = set(fields) - set(_VISSIM_FIELDS)
        if bad:
            raise TypeError(f"set_vissim: unknown fields {sorted(bad)}")
        with self._lock:
            if fields.get("running") is True and not self._vissim["running"] and "launched_at" not in fields:
                self._vissim["launched_at"] = now_iso()
            self._vissim.update(fields)
            if fields.get("running") is False:
                self._vissim["pid"] = None
                self._vissim["launched_at"] = None

    # ---- simulation ----
    def set_simulation(self, **fields: Any) -> None:
        """Update only the given fields among state, run_id, progress_s, period_s, run_index, num_runs, started_at and error.

        When state becomes running, started_at is set to now (unless given explicitly).
        """
        bad = set(fields) - set(_SIM_FIELDS)
        if bad:
            raise TypeError(f"set_simulation: unknown fields {sorted(bad)}")
        if "state" in fields and fields["state"] not in SIM_STATES:
            raise ValueError(f"simulation.state must be one of {', '.join(SIM_STATES)} ({fields['state']!r})")
        with self._lock:
            if fields.get("state") == SIM_RUNNING and "started_at" not in fields:
                self._simulation["started_at"] = now_iso()
            self._simulation.update(fields)

    # ---- reading ----
    def snapshot(self) -> dict[str, Any]:
        """Summary included in responses (key names are fixed)."""
        with self._lock:
            return {
                "vissim": {"running": self._vissim["running"], "version": self._vissim["version"]},
                "network": copy.deepcopy(self._network),
                "simulation": {"state": self._simulation["state"], "run_id": self._simulation["run_id"],
                               "progress_s": self._simulation["progress_s"]},
                "server_start_id": self.server_start_id,
            }

    def full(self) -> dict[str, Any]:
        """Complete form for the vissim_status response."""
        with self._lock:
            return {
                "server_start_id": self.server_start_id,
                "started_at": self.started_at,
                "port": self.port,
                "vissim": dict(self._vissim),
                "network": copy.deepcopy(self._network),
                "simulation": dict(self._simulation),
            }
