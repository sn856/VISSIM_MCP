"""Simulation and evaluation: changing settings, the chunked run loop (background), stop, status.

Every COM call goes through ComThread. One chunk is a single com.submit, so other COM commands can be
interleaved between chunks, and progress is updated through state.set_simulation only at chunk boundaries (no
periodic polling). Simulation state lives only in `state`; while running (running/stopping), changing settings
or starting again is rejected with sim_running.

The Simulation attribute IDs (SimPeriod, SimRes, RandSeed, NumRuns, RandSeedIncr, SimBreakAt, UseMaxSimSpeed,
StartTm) and the Evaluation attribute IDs (EVAL_ATTRS) were verified against VISSIM 2026 SP09 with a COM probe.
On a new, unsaved network RunContinuous is blocked by a save dialog, so simulations run only on networks
opened from a file.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .changelog import ChangeLog
from .comthread import ComThread
from .config import TUNABLE_DEFAULTS
from .errors import KIND_BAD_ARGS, KIND_COM_ERROR, KIND_SIM_RUNNING, ToolError
from .state import SIM_FAILED, SIM_FINISHED, SIM_IDLE, SIM_RUNNING, SIM_STOPPING, ServerState

# configure_simulation argument -> Simulation attribute ID
SIM_ATTRS: dict[str, str] = {
    "period_s": "SimPeriod",
    "resolution": "SimRes",
    "random_seed": "RandSeed",
    "num_runs": "NumRuns",
    "seed_increment": "RandSeedIncr",
    "start_time": "StartTm",
}

# configure_evaluation group/key -> Evaluation attribute ID. Verified by probe (2026 SP09): each group has
# CollectData, Interval, FromTime and ToTime, but no per-group "WriteFile" attribute (when collection is on, the
# result lists are also saved as .att files in the .results folder next to the network).
EVAL_KEYS = ("collect", "write_file", "interval_s", "from_s", "to_s")
_EVAL_SUFFIX = {"collect": "CollectData", "interval_s": "Interval", "from_s": "FromTime", "to_s": "ToTime"}
_EVAL_PREFIX = {"data_collection": "DataColl", "travel_times": "VehTravTms", "queues": "Queues",
                "network_performance": "VehNetPerf",
                "parking_lots": "ParkLots", "parking_lot_groups": "ParkLotGrps",
                "parking_decisions": "ParkRoutDecs", "parking_spaces": "ParkSpcs"}
EVAL_ATTRS: dict[str, dict[str, str]] = {
    group: {key: prefix + _EVAL_SUFFIX[key] for key in _EVAL_SUFFIX} for group, prefix in _EVAL_PREFIX.items()
}
EVAL_ATTRS_VERIFIED = True   # the four parking groups (ParkLots, ParkLotGrps, ParkRoutDecs, ParkSpcs) were probed on 2026-10-15
EVAL_WRITE_FILE_NOTE = ("write_file: VISSIM 2026 has no per-group file-writing attribute (ignored). Read aggregated results "
                        "with get_results; an .att file exists only if it was exported from VISSIM or raw-data "
                        "writing was turned on")

EVAL_NOTE = ("Both get_results and read_result_file require collect to be on for the group. "
             "The Evaluation attribute IDs are the values verified on VISSIM 2026 SP09.")


class Simulation:
    def __init__(self, cfg: dict[str, Any], state: ServerState, com: ComThread, changelog: ChangeLog,
                 vissim_getter: Callable[[], Any]):
        self.cfg = cfg
        self.state = state
        self.com = com
        self.changelog = changelog
        self._vissim = vissim_getter
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    # ---- shared ----
    def _tunable(self, key: str) -> Any:
        return self.cfg.get(key, TUNABLE_DEFAULTS[key])

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run on the COM thread. COM-side exceptions become com_error with the text unchanged."""
        try:
            return self.com.submit(fn, *args)
        except ToolError:
            raise
        except Exception as e:   # noqa: BLE001 — COM errors come in many kinds
            raise ToolError(KIND_COM_ERROR, str(e), {"type": type(e).__name__}) from e

    def _sim_state(self) -> str:
        return self.state.full()["simulation"]["state"]

    def _reject_if_running(self, what: str) -> None:
        s = self._sim_state()
        if s in (SIM_RUNNING, SIM_STOPPING):
            raise ToolError(KIND_SIM_RUNNING, f"Cannot {what} while the simulation is {s}",
                            {"simulation": self.state.full()["simulation"]})

    def _network_path(self) -> str | None:
        return self.state.full()["network"]["path"]

    def _set_attr(self, tool: str, type_name: str, get_obj: Callable[[], Any], attr: str, value: Any) -> dict[str, Any]:
        """Read the old value, write, read the new value. Records the change (set) and bumps the unsaved-write counter.

        get_obj is a function that fetches the object inside the COM thread (e.g. `lambda: vissim.Simulation`);
        touching a COM property from another thread raises an AttributeError such as `<unknown>.Simulation`
        (apartment restriction).
        """
        before = self._call(lambda: get_obj().AttValue(attr))
        try:
            self._call(lambda: get_obj().SetAttValue(attr, value))
        except ToolError as e:
            self.changelog.append({"tool": tool, "kind": "set", "server_start_id": self.state.server_start_id,
                                   "network_path": self._network_path(), "type": type_name, "attr": attr,
                                   "before": before, "after": value, "ok": False, "error": e.message})
            raise
        after = self._call(lambda: get_obj().AttValue(attr))
        self.state.note_write()
        self.changelog.append({"tool": tool, "kind": "set", "server_start_id": self.state.server_start_id,
                               "network_path": self._network_path(), "type": type_name, "attr": attr,
                               "before": before, "after": after, "ok": True})
        return {"attr": attr, "before": before, "after": after}

    # ---- settings ----
    def configure_simulation(self, period_s=None, resolution=None, random_seed=None, num_runs=None,
                             seed_increment=None, start_time=None) -> dict[str, Any]:
        """Write only the given values to the Simulation attributes and return before/after per item."""
        self._reject_if_running("change settings")
        given = {"period_s": period_s, "resolution": resolution, "random_seed": random_seed,
                 "num_runs": num_runs, "seed_increment": seed_increment, "start_time": start_time}
        vissim = self._vissim()
        changed: dict[str, Any] = {}
        for key, value in given.items():
            if value is None:
                continue
            changed[key] = self._set_attr("configure_simulation", "Simulation", lambda: vissim.Simulation,
                                          SIM_ATTRS[key], value)
        return {"changed": changed}

    def configure_evaluation(self, data_collection=None, travel_times=None, queues=None,
                             network_performance=None, parking_lots=None, parking_lot_groups=None,
                             parking_decisions=None, parking_spaces=None) -> dict[str, Any]:
        """For each group, write only the given keys of {collect, write_file, interval_s, from_s, to_s} to the Evaluation attributes."""
        self._reject_if_running("change evaluation settings")
        groups = {"data_collection": data_collection, "travel_times": travel_times, "queues": queues,
                  "network_performance": network_performance, "parking_lots": parking_lots,
                  "parking_lot_groups": parking_lot_groups, "parking_decisions": parking_decisions,
                  "parking_spaces": parking_spaces}
        for group, spec in groups.items():
            if spec is None:
                continue
            if not isinstance(spec, dict):
                raise ToolError(KIND_BAD_ARGS, f"{group}: must be an object {{collect, write_file, interval_s, from_s, to_s}}")
            bad = sorted(set(spec) - set(EVAL_KEYS))
            if bad:
                raise ToolError(KIND_BAD_ARGS, f"{group}: unknown key(s) {bad} (choose from {', '.join(EVAL_KEYS)})")
        vissim = self._vissim()
        changed: dict[str, Any] = {}
        warnings: list[str] = []
        for group, spec in groups.items():
            if not spec:
                continue
            out: dict[str, Any] = {}
            for key in EVAL_KEYS:
                if key not in spec or spec[key] is None:
                    continue
                if key == "write_file":
                    warnings.append(f"{group}.{EVAL_WRITE_FILE_NOTE}")
                    continue
                out[key] = self._set_attr("configure_evaluation", "Evaluation", lambda: vissim.Evaluation,
                                          EVAL_ATTRS[group][key], spec[key])
            if out:
                changed[group] = out
        res = {"changed": changed, "attr_ids_verified": EVAL_ATTRS_VERIFIED, "note": EVAL_NOTE}
        if warnings:
            res["warnings"] = warnings
        return res

    # ---- run ----
    def run_simulation(self, quick_mode: bool | None = None, num_runs: int | None = None) -> dict[str, Any]:
        """Start the chunked run loop in the background and return {run_id, period_s, chunk_s} immediately."""
        with self._start_lock:
            self._reject_if_running("start again")
            vissim = self._vissim()
            quick = bool(self._tunable("quick_mode_default")) if quick_mode is None else bool(quick_mode)
            chunk = float(self._tunable("sim_chunk_s"))
            if chunk <= 0:
                raise ToolError(KIND_BAD_ARGS, f"sim_chunk_s must be positive ({chunk})")
            if num_runs is not None:
                if isinstance(num_runs, bool) or not isinstance(num_runs, int) or num_runs < 1:
                    raise ToolError(KIND_BAD_ARGS, f"num_runs must be an integer >= 1 ({num_runs!r})")
                self._call(lambda: vissim.Simulation.SetAttValue("NumRuns", num_runs))
            period = self._call(lambda: vissim.Simulation.AttValue("SimPeriod"))
            run_id = uuid.uuid4().hex[:12]
            params = {"quick_mode": quick, "num_runs": num_runs}
            self._stop_event.clear()
            self.state.set_simulation(state=SIM_RUNNING, run_id=run_id, progress_s=0.0, period_s=period,
                                      run_index=None, num_runs=num_runs, error=None)
            self._thread = threading.Thread(target=self._loop, name=f"vissim-sim-{run_id}",
                                            args=(vissim, run_id, quick, period, chunk, params), daemon=True)
            self._thread.start()
        return {"run_id": run_id, "period_s": period, "chunk_s": chunk}

    def _loop(self, vissim: Any, run_id: str, quick: bool, period: float, chunk: float,
              params: dict[str, Any]) -> None:
        started = time.monotonic()
        result_state = SIM_FAILED
        error: str | None = None
        stopped = False
        t = 0.0
        quick_before = None
        try:
            quick_before = self._read_quick_mode(vissim)
            self._call(lambda: vissim.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", 1 if quick else 0))
            self._call(lambda: vissim.Simulation.SetAttValue("UseMaxSimSpeed", True))
            while True:
                break_at = min(t + chunk, period)
                reached = self._call(self._run_chunk, vissim, break_at)
                # if SimSec is missing or does not advance, use the computed value (chunk end) so the loop always terminates
                t = reached if (reached is not None and reached > t) else break_at
                run_index = self._run_index(vissim)
                self.state.set_simulation(progress_s=t, run_index=run_index)
                if self._stop_event.is_set():
                    self._call(lambda: vissim.Simulation.Stop())
                    stopped = True
                    result_state = SIM_IDLE
                    break
                if t >= period:
                    result_state = SIM_FINISHED
                    break
        except ToolError as e:
            error = e.message + self._err_file_tail()
        except Exception as e:   # noqa: BLE001 — background thread: record any exception in the state
            error = f"{type(e).__name__}: {e}" + self._err_file_tail()
        if quick_before is not None:   # restore the pre-run display mode (convention from the official examples; brings the animation back when the user looks at the window)
            try:
                self._call(lambda: vissim.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", quick_before))
            except Exception:   # noqa: BLE001 — a failed restore does not change the run result
                pass
        elapsed = round(time.monotonic() - started, 3)
        self.state.set_simulation(state=result_state, error=error)
        self.changelog.append({"tool": "run_simulation", "kind": "run_simulation",
                               "server_start_id": self.state.server_start_id,
                               "network_path": self._network_path(), "type": "Simulation",
                               "run_id": run_id, "params": params, "period_s": period, "progress_s": t,
                               "elapsed_s": elapsed, "result_state": result_state, "stopped": stopped,
                               "ok": error is None, "error": error})

    def _err_file_tail(self, max_lines: int = 5) -> str:
        """The Error lines of <name>.err that VISSIM writes next to the network. The real reason behind "could not be
        continued" is found here (e.g. a static route is incomplete). Empty string if there are none."""
        path = self._network_path()
        if not path:
            return ""
        err = Path(path).with_suffix(".err")
        try:
            lines = err.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return ""
        errors = [ln.strip() for ln in lines if "Error" in ln]
        if not errors:
            return ""
        return " | network.err: " + " / ".join(errors[-max_lines:])

    def _read_quick_mode(self, vissim: Any):
        """QuickMode value (0/1) before the run. None if unreadable (then it is not restored)."""
        try:
            val = self._call(lambda: vissim.Graphics.CurrentNetworkWindow.AttValue("QuickMode"))
        except Exception:   # noqa: BLE001
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _run_chunk(vissim: Any, break_at: float):
        """One chunk on the COM thread: SimBreakAt -> RunContinuous (returns at the chunk end) -> SimSec."""
        sim = vissim.Simulation
        sim.SetAttValue("SimBreakAt", break_at)
        sim.RunContinuous()
        try:
            return sim.AttValue("SimSec")
        except Exception:   # noqa: BLE001 — if SimSec is unreadable, the caller uses the computed value
            return None

    def _run_index(self, vissim: Any) -> int | None:
        """Use SimulationRuns.Count as the run index (None if unavailable)."""
        try:
            return self.com.submit(lambda: int(vissim.Net.SimulationRuns.Count))
        except Exception:   # noqa: BLE001 — the run index is auxiliary; keep running even if unreadable
            return None

    def stop_simulation(self) -> dict[str, Any]:
        """Mark as stopping. The loop calls the actual Stop() once at the next chunk boundary."""
        if self._sim_state() != SIM_RUNNING:
            raise ToolError(KIND_BAD_ARGS, "The simulation is not running",
                            {"simulation": self.state.full()["simulation"]})
        self.state.set_simulation(state=SIM_STOPPING)
        self._stop_event.set()
        sim = self.state.full()["simulation"]
        return {"state": SIM_STOPPING, "run_id": sim["run_id"], "progress_s": sim["progress_s"]}

    def status(self) -> dict[str, Any]:
        """The in-memory simulation state as is (no COM call)."""
        return self.state.full()["simulation"]

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until the loop thread ends (for tests and shutdown). True if it has ended."""
        th = self._thread
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()
