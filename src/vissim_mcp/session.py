"""Session layer: VISSIM lifetime, opening networks, saving, backups, unsaved-change tracking, recovery copies.

Rules
  - Server : VISSIM = 1 : 1, at most one open network. VISSIM is launched on the first command (Dispatch only,
    no GetActiveObject).
  - Every COM call goes through ComThread.submit, on the COM thread only.
  - If VISSIM disappears (window closed, process died) we only detect it: no automatic reconnect, no retry loop,
    no periodic polling. The next open_network launches a fresh instance.
  - Files are written only by save_network (including save_on_quit from the quit-confirmation dialog) and
    save_recovery_copy. No auto-save. Writes other than to the target go only under <log_root>.
  - Failures are raised as ToolError(kind). COM error text goes into message unchanged.
  - note_write/changelog for successful object writes is the object layer's job. This module records only
    open, save, recovery, vissim_lost, file_switch and external_change.
  - Current-file check: on a tool or admin call, read WorkingFolder+InputFile of the top-level object once on the
    COM thread and reconcile it with the recorded file (no periodic probing). If it differs, follow it and raise
    the network-changed flag; if it is the same, compare the modification time against the baseline and record
    an out-of-server change.
  - Opening another file requires discard when a file is open or something was drawn on screen.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import vissim_com
from .changelog import LOGGER_NAME, ChangeLog
from .clock import now_iso
from .comthread import ComThread, is_busy_error
from .errors import (KIND_COM_ERROR, KIND_NOT_FOUND, KIND_NO_NETWORK, KIND_OTHER_VISSIM_RUNNING,
                     KIND_SIM_RUNNING, KIND_UNSAVED_CHANGES, KIND_VISSIM_ABSENT, ToolError)
from .state import SIM_RUNNING, SIM_STOPPING, ServerState

log = logging.getLogger(LOGGER_NAME)

BACKUP_DIR = "backup"
RECOVERY_DIR = "recovery"
RECOVERY_DRAWN_STEM = "drawn"        # name stem of the recovery copy when only drawn content exists (no open file)
BACKUP_SUFFIX = ".bak.inpx"
_TS_FORMAT = "%Y%m%d-%H%M%S"

CHANGED_BY_MENU = "vissim_menu"      # another file was opened or created via the VISSIM menu (noticed by the current-file check)
CHANGED_BY_LAUNCHER = "launcher"     # the launcher (/admin/open) opened another file
NOTE_VISSIM_ABSENT = "VISSIM not running"
NOTE_SIM_RUNNING = "simulation running"
EXTERNAL_CHANGE_NOTE = "changed outside the server (probably saved from the VISSIM UI)"
_UNSET: Any = object()               # admin_open expectation not supplied

# Containers counted on open: (Net attribute name, counts key). 0 if missing.
_COUNT_CONTAINERS = (
    ("VehicleInputs", "VehicleInput"),
    ("VehicleRoutingDecisionsStatic", "VehicleRoutingDecisionStatic"),
    ("DataCollectionPoints", "DataCollectionPoint"),
    ("ParkingLots", "ParkingLot"),
    ("Nodes", "Node"),
)


# ---------------------------------------------------------------- process list

CREATE_NO_WINDOW = 0x08000000   # keep tasklist from flashing a console window when called from pythonw


def list_vissim_pids() -> list[int] | None:
    """PIDs of running `Vissim*.exe` (tasklist CSV, no console window). Unknown -> None (treated as "running")."""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Vissim*", "/NH", "/FO", "CSV"],
                           capture_output=True, text=True, errors="replace", check=False,
                           creationflags=CREATE_NO_WINDOW)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    pids: list[int] = []
    for row in csv.reader(io.StringIO(r.stdout or "")):
        if not row or not row[0].strip().lower().startswith("vissim"):
            continue
        try:
            pids.append(int(row[1]))
        except (IndexError, ValueError):
            return None
    return pids


# ---------------------------------------------------------------- paths and modification times

def same_path(a: str | None, b: str | None) -> bool:
    """Path equality: compare absolute paths, ignoring case. Two Nones are equal."""
    if a is None or b is None:
        return a is None and b is None
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _show(path: str | None) -> str:
    return path if path is not None else "(no file)"


def _mtime_ns(path: str | None) -> int | None:
    if path is None:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _mtime_iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).astimezone().isoformat(timespec="milliseconds")


def _mtime_local(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- functions that run on the COM thread

def _read_open_file(v: Any) -> tuple[str, str]:
    """The file open in the server's VISSIM = WorkingFolder + InputFile of the top-level object (not v.Net;
    verified by experiment on 2026-10-18).

    An empty InputFile means no file is open. An unexpected format is raised as a read failure (so the recorded
    file is trusted instead).
    """
    folder = v.AttValue("WorkingFolder")
    name = v.AttValue("InputFile")
    if not isinstance(name, str) or (name and not isinstance(folder, str)):
        raise TypeError(f"Unexpected format of the open-file attributes (WorkingFolder={folder!r}, InputFile={name!r})")
    return (folder or ""), name


def _link_count(v: Any) -> int:
    """Number of links (+connectors), used to decide whether anything was drawn."""
    return int(v.Net.Links.Count)


def _ping(v: Any) -> bool:
    """Check liveness with a cheap call. A busy rejection is re-raised so ComThread retries it."""
    last: BaseException | None = None
    for attr in ("VersionStr", "Version"):
        try:
            v.AttValue(attr)
            return True
        except Exception as e:   # noqa: BLE001
            if is_busy_error(e):
                raise
            last = e
    raise last if last is not None else RuntimeError("VISSIM did not respond")


def _read_counts(v: Any) -> dict[str, int]:
    """Counts of links, connectors, vehicle inputs, etc. 0 if a container is missing or unreadable."""
    counts: dict[str, int] = {"Link": 0, "Connector": 0}
    try:
        links = v.Net.Links
        total = int(links.Count)
        conn = 0
        try:
            for item in links.GetMultiAttValues("IsConn"):
                val = item[-1] if isinstance(item, (tuple, list)) else item
                if val:
                    conn += 1
        except Exception:   # noqa: BLE001 — without GetMultiAttValues, go one by one
            for lk in links.GetAll():
                try:
                    if lk.AttValue("IsConn"):
                        conn += 1
                except Exception:   # noqa: BLE001
                    continue
        counts["Link"] = total - conn
        counts["Connector"] = conn
    except Exception:   # noqa: BLE001
        pass
    for container, key in _COUNT_CONTAINERS:
        try:
            counts[key] = int(getattr(v.Net, container).Count)
        except Exception:   # noqa: BLE001
            counts[key] = 0
    return counts


# ---------------------------------------------------------------- session

class Session:
    def __init__(self, cfg: dict[str, Any], state: ServerState, com: ComThread, changelog: ChangeLog,
                 process_lister: Callable[[], list[int]] | None = None,
                 launcher: Callable[[str], tuple[Any, str]] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self._cfg = cfg
        self._state = state
        self._com = com
        self._log = changelog
        self._list_pids = process_lister or list_vissim_pids
        self._launcher = launcher or vissim_com.launch
        self._clock = clock or datetime.now
        self._v: Any = None
        self._progid = str(cfg["vissim"]["progid"])
        self._log_root = Path(cfg["log_root"])
        self._protected_roots = [Path(r) for r in cfg.get("protected_roots", ())]
        self._mtime: int | None = None   # baseline modification time (ns) of the recorded file; refreshed on open, on follow and right after save

    # ---- access ----
    @property
    def vissim(self) -> Any:
        """The VISSIM COM object launched by the server (None if absent). Always call it through call()."""
        return self._v

    def require_vissim(self) -> Any:
        if self._v is None:
            raise ToolError(KIND_VISSIM_ABSENT, "VISSIM is not running. Open a network with open_network.")
        return self._v

    def require_network(self) -> str:
        path = self._state.snapshot()["network"]["path"]
        if path is None or self._v is None:
            raise ToolError(KIND_NO_NETWORK, "No network is open. Open one with open_network.")
        return path

    # ---- COM calls ----
    def call(self, fn: Callable[..., Any], *args: Any, tool: str = "session", **kwargs: Any) -> Any:
        """Run fn on the COM thread. On failure, decide whether VISSIM was lost: vissim_absent or com_error."""
        try:
            return self._com.submit(fn, *args, **kwargs)
        except ToolError:
            raise
        except Exception as e:   # noqa: BLE001
            if self._v is not None:
                lost = self._detect_lost(tool)
                if lost is not None:
                    raise ToolError(KIND_VISSIM_ABSENT,
                                    f"VISSIM disappeared ({e}). {lost} unsaved change(s) lost",
                                    {"lost_writes": lost}) from e
            raise ToolError(KIND_COM_ERROR, str(e)) from e

    # ---- lifetime ----
    def _pids(self) -> set[int] | None:
        """Set of Vissim*.exe PIDs. None if undeterminable (tasklist failed)."""
        pids = self._list_pids()
        return None if pids is None else set(pids)

    def _reject_if_running(self, what: str) -> None:
        s = self._state.full()["simulation"]["state"]
        if s in (SIM_RUNNING, SIM_STOPPING):
            raise ToolError(KIND_SIM_RUNNING, f"Cannot {what} while the simulation is {s} (run stop_simulation first)",
                            {"simulation": self._state.full()["simulation"]})

    def ensure_vissim(self) -> dict[str, Any]:
        """Launch VISSIM if absent (always a new instance). Remember the server VISSIM's PID from the before/after PID set difference."""
        if self._v is not None:
            v = self._state.full()["vissim"]
            return {"launched": False, "pid": v["pid"], "version": v["version"], "warnings": []}
        before = self._pids()
        try:
            v, how = self._com.submit(self._launcher, self._progid)
        except Exception as e:   # noqa: BLE001
            raise ToolError(KIND_COM_ERROR, str(e), {"launch_failed": True}) from e
        after = self._pids()
        warnings: list[str] = []
        pid: int | None = None
        if before is None or after is None:
            warnings.append("Could not identify the server VISSIM's PID (tasklist failed)")
        else:
            new = after - before
            if len(new) == 1:
                pid = next(iter(new))
            else:
                warnings.append(f"Could not identify the server VISSIM's PID ({len(new)} new Vissim*.exe processes after launch)")
        try:
            ver = self._com.submit(vissim_com.version, v)
        except Exception:   # noqa: BLE001
            ver = ""
        self._v = v
        self._mtime = None
        self._state.set_vissim(running=True, version=ver, launched_at=now_iso(), pid=pid)
        self._state.clear_network_changed()   # freshly launched VISSIM: clear the changed flag (the loss was already reported)
        return {"launched": True, "pid": pid, "version": ver, "how": how, "warnings": warnings}

    def is_alive(self, tool: str = "session") -> bool:
        """Is VISSIM alive? If the PID is gone or a cheap COM call fails, treat it as lost and return False."""
        if self._v is None:
            return False
        return self._detect_lost(tool) is None

    def _detect_lost(self, tool: str) -> int | None:
        """If lost, clear the state and return the number of lost writes. None if alive."""
        if self._v is None:
            return None
        pid = self._state.full()["vissim"]["pid"]
        dead = False
        pids = self._pids()
        if pid is not None and pids is not None and pid not in pids:
            dead = True
        else:
            try:
                self._com.submit(_ping, self._v)
            except Exception:   # noqa: BLE001
                dead = True
        if not dead:
            return None
        return self._mark_lost(tool)

    def _mark_lost(self, tool: str) -> int:
        snap = self._state.snapshot()["network"]
        lost = int(snap["unsaved_writes"])
        self._v = None   # a dead object cannot be released; just drop the reference
        self._mtime = None
        self._state.set_vissim(running=False)
        self._state.set_network(None)
        self._state.clear_network_changed()
        self._log.append({"tool": tool, "kind": "vissim_lost", "network_path": snap["path"], "ok": False,
                          "error": f"VISSIM disappeared: {lost} unsaved change(s) lost",
                          "lost_writes": lost})
        return lost

    def other_vissim_processes(self) -> int | None:
        """Number of current Vissim*.exe PIDs other than the server VISSIM's. None if undeterminable (treated as "others exist")."""
        pid = self._state.full()["vissim"]["pid"]
        pids = self._list_pids()
        if pids is None:
            return None
        return len([p for p in pids if p != pid])

    def shutdown(self) -> dict[str, Any]:
        """Drop the COM reference and set running=False. VISSIM closes together with the server (Dispatch lifetime)."""
        was_running = self._v is not None
        self._v = None
        self._mtime = None
        self._state.set_vissim(running=False)
        self._state.set_network(None)
        return {"was_running": was_running}

    # ---- current-file check ----
    def check_current(self, tool: str) -> dict[str, Any]:
        """Read the file open in the server's VISSIM once over COM and reconcile it with the recorded file.

        Returns {checked, note, warnings[], lost, lost_error, switched}:
          - VISSIM absent, or simulation running/stopping -> not checked (checked=False, note).
          - Read failure -> if VISSIM was lost, run the usual loss handling and set lost=number of lost writes;
            otherwise only a warning (the recorded file is trusted).
          - If the open file differs, follow it (record file_switch, write count 0, raise the changed flag,
            changed_by=vissim_menu).
          - If it is the same but the modification time differs from the baseline, record external_change,
            reset the write count and warn.
        """
        out: dict[str, Any] = {"checked": False, "note": None, "warnings": [], "lost": None, "lost_error": None,
                               "switched": False}
        if self._v is None:
            out["note"] = NOTE_VISSIM_ABSENT
            return out
        if self._state.full()["simulation"]["state"] in (SIM_RUNNING, SIM_STOPPING):
            out["note"] = NOTE_SIM_RUNNING   # the file cannot change while a simulation is running
            return out
        try:
            folder, name = self._com.submit(_read_open_file, self._v)
        except Exception as e:   # noqa: BLE001
            lost = self._detect_lost(tool)
            if lost is not None:
                out.update(lost=lost, lost_error=str(e), note=f"VISSIM disappeared ({e})")
                out["warnings"].append(f"VISSIM disappeared; {lost} unsaved change(s) were lost")
                return out
            log.warning("%s: current-file check failed (using the recorded file): %s", tool, e)
            out["note"] = str(e)
            out["warnings"].append(f"Could not check the file open in the server VISSIM; using the recorded file as is ({e})")
            return out
        out["checked"] = True
        current = os.path.abspath(os.path.join(folder, name)) if name else None
        recorded = self._state.snapshot()["network"]["path"]
        if not same_path(current, recorded):
            self._follow(tool, recorded, current, out)
        elif current is not None:
            self._check_mtime(tool, current, out)
        return out

    def _follow(self, tool: str, previous: str | None, current: str | None, out: dict[str, Any]) -> None:
        dropped = int(self._state.snapshot()["network"]["unsaved_writes"])
        counts: dict[str, int] = {}
        if current is not None:
            try:
                counts = self._com.submit(_read_counts, self._v)
            except Exception as e:   # noqa: BLE001 — follow even if the counts cannot be read
                log.warning("%s: failed to read object counts of the followed file: %s", tool, e)
        self._state.set_network(current, counts)   # write counter 0; path=None if no file is open
        self._mtime = _mtime_ns(current)
        self._state.set_network_changed(previous, current, CHANGED_BY_MENU)
        self._log.append({"tool": tool, "kind": "file_switch", "network_path": current, "ok": True,
                          "previous_path": previous, "current_path": current, "changed_by": CHANGED_BY_MENU,
                          "dropped_writes": dropped})
        log.info("%s: following the file open in the server VISSIM %s -> %s (%d write(s) dropped)", tool, previous, current, dropped)
        out["switched"] = True
        text = f"The file open in the server VISSIM changed; now following it: {_show(previous)} -> {_show(current)}"
        if dropped:
            text += f" ({dropped} unsaved change(s) made through this conversation belonged to the previous file and are treated as dropped)"
        out["warnings"].append(text)

    def _check_mtime(self, tool: str, current: str, out: dict[str, Any]) -> None:
        actual = _mtime_ns(current)
        if actual is None:
            return
        if self._mtime is None:
            self._mtime = actual
            return
        if actual == self._mtime:
            return
        before, self._mtime = self._mtime, actual
        reset = self._state.reset_writes()   # the in-memory network was saved, so the write count resets
        self._log.append({"tool": tool, "kind": "external_change", "network_path": current, "ok": True,
                          "before_mtime": _mtime_iso(before), "after_mtime": _mtime_iso(actual),
                          "note": EXTERNAL_CHANGE_NOTE, "writes_reset": reset})
        log.warning("%s: open file changed outside the server: %s", tool, current)
        out["warnings"].append(f"The open file was changed outside the server ({_mtime_local(actual)}). "
                               "Ask the user whether they saved from the VISSIM UI.")

    # ---- open state ----
    def open_info(self, tool: str = "session") -> dict[str, Any]:
        """{vissim, open_path, drawn_links, unsaved_writes}, based on the recorded state (the caller runs the
        current-file check first).

        drawn_links: number of links (+connectors) in the server VISSIM network when no file is open (one COM
        call). 0 if VISSIM is absent or a file is open; None if it could not be counted (treated as "something
        is drawn").
        """
        net = self._state.snapshot()["network"]
        info: dict[str, Any] = {"vissim": self._v is not None, "open_path": net["path"], "drawn_links": 0,
                                "unsaved_writes": int(net["unsaved_writes"])}
        if self._v is not None and net["path"] is None:
            try:
                info["drawn_links"] = int(self._com.submit(_link_count, self._v))
            except Exception as e:   # noqa: BLE001
                log.warning("%s: failed to check drawn content (link count): %s", tool, e)
                info["drawn_links"] = None
        return info

    @staticmethod
    def needs_discard(info: dict[str, Any]) -> bool:
        """Discard confirmation is needed if a file is open or something was drawn (an uncounted result counts as drawn)."""
        if not info["vissim"]:
            return False
        drawn = info["drawn_links"]
        return info["open_path"] is not None or drawn is None or drawn > 0

    @staticmethod
    def _unsaved_error(info: dict[str, Any]) -> ToolError:
        n = info["unsaved_writes"]
        open_path = info["open_path"]
        detail: dict[str, Any] = {"unsaved_writes": n, "open_path": open_path}
        tail = "Opening another file discards all of it. Ask the user, and call again with discard=true only after they answer."
        if open_path is not None:
            msg = (f"A file is open: {open_path} ({n} unsaved change(s) made through this conversation; "
                   f"edits made in the VISSIM UI are unknown). {tail}")
        else:
            drawn = info["drawn_links"]
            detail["drawn_links"] = drawn
            what = f"{drawn} link(s)" if drawn is not None else "link count unknown"
            msg = (f"No file is open, but the server VISSIM contains content drawn on screen ({what}) "
                   f"({n} unsaved change(s) made through this conversation). {tail}")
        return ToolError(KIND_UNSAVED_CHANGES, msg, detail)

    # ---- open ----
    def open_network(self, path: str, discard: bool = False) -> dict[str, Any]:
        """MCP open_network. If a file is open or something was drawn (judged after the current-file check),
        refuse unless discard=true.

        Refused even for the same file with 0 writes (LoadNet silently discards UI edits too). The conversation
        side opened it, so the changed flag is cleared.
        """
        self._reject_if_running("open another network")
        target = Path(os.path.abspath(str(path)))
        if not target.is_file():
            raise ToolError(KIND_NOT_FOUND, f"File not found: {target}", {"path": str(target)})
        warnings: list[str] = list(self.check_current("open_network")["warnings"])
        info = self.open_info("open_network")
        if self.needs_discard(info) and not discard:
            raise self._unsaved_error(info)
        ev = self.ensure_vissim()
        warnings.extend(ev["warnings"])
        counts = self._load(target, "open_network", info["unsaved_writes"], {}, warnings)
        self._state.clear_network_changed()
        return {"path": str(target), "counts": counts, "launched": ev["launched"],
                "discarded_writes": info["unsaved_writes"], "warnings": warnings}

    def admin_open(self, path: str | None = None, discard: bool = False, expect_open_path: Any = _UNSET,
                   expect_unsaved_writes: Any = _UNSET) -> dict[str, Any]:
        """Admin route POST /admin/open, called by the launcher.

        path=None: launch VISSIM if absent (launched), otherwise front. With a path: the same file -> front (not
        reloaded, discard ignored); another open file or drawn content with no discard, or with expectations
        (expect_*) that no longer match -> unsaved_changes (with fresh values); otherwise open it (opened, raise
        the changed flag with changed_by=launcher). A launch failure is vissim_absent + detail.launch_failed.
        """
        self._reject_if_running("open a file")
        warnings: list[str] = []
        if self._v is not None:
            warnings.extend(self.check_current("admin_open")["warnings"])
        if path is None:
            if self._v is None:
                ev = self._ensure_vissim_for_admin()
                warnings.extend(ev["warnings"])
                return self._admin_result("launched", True, warnings)
            return self._admin_result("front", False, warnings)
        target = Path(os.path.abspath(str(path)))
        if not target.is_file():
            raise ToolError(KIND_NOT_FOUND, f"File not found: {target}", {"path": str(target)})
        info = self.open_info("admin_open")
        if info["open_path"] is not None and same_path(info["open_path"], str(target)):
            return self._admin_result("front", False, warnings)
        if self.needs_discard(info):
            mismatch = ((expect_open_path is not _UNSET and not same_path(expect_open_path, info["open_path"]))
                        or (expect_unsaved_writes is not _UNSET and expect_unsaved_writes is not None
                            and int(expect_unsaved_writes) != info["unsaved_writes"]))
            if not discard or mismatch:
                raise self._unsaved_error(info)
        ev = self._ensure_vissim_for_admin()
        warnings.extend(ev["warnings"])
        previous = info["open_path"]
        counts = self._load(target, "admin_open", info["unsaved_writes"],
                            {"previous_path": previous, "changed_by": CHANGED_BY_LAUNCHER}, warnings)
        self._state.set_network_changed(previous, str(target), CHANGED_BY_LAUNCHER)
        return self._admin_result("opened", ev["launched"], warnings, counts)

    def _ensure_vissim_for_admin(self) -> dict[str, Any]:
        try:
            return self.ensure_vissim()
        except ToolError as e:
            if e.detail.get("launch_failed"):
                raise ToolError(KIND_VISSIM_ABSENT, f"Could not launch VISSIM: {e.message}",
                                {"launch_failed": True}) from e
            raise

    def _admin_result(self, action: str, launched: bool, warnings: list[str],
                      counts: dict[str, int] | None = None) -> dict[str, Any]:
        res: dict[str, Any] = {"action": action, "path": self._state.snapshot()["network"]["path"],
                               "pid": self._state.full()["vissim"]["pid"], "launched": bool(launched),
                               "warnings": warnings}
        if counts is not None:
            res["counts"] = counts
        return res

    def _load(self, target: Path, tool: str, discarded: int, extra: dict[str, Any],
              warnings: list[str]) -> dict[str, int]:
        """Assuming VISSIM is running: LoadNet -> object counts -> state, baseline, log. Appends other-VISSIM warnings to warnings."""
        v = self._v
        self.call(vissim_com.load, v, str(target), tool=tool)
        counts = self.call(_read_counts, v, tool=tool)
        self._state.set_network(str(target), counts)   # write counter 0
        self._mtime = _mtime_ns(str(target))
        entry: dict[str, Any] = {"tool": tool, "kind": "open", "network_path": str(target), "ok": True,
                                 "after": counts, "discarded_writes": discarded}
        entry.update(extra)
        self._log.append(entry)
        other = self.other_vissim_processes()
        if other is None:
            warnings.append("Could not check whether other VISSIM processes exist (saving will require force)")
        elif other:
            warnings.append(f"{other} VISSIM process(es) other than the server's are running (saving will require force)")
        return counts

    # ---- save ----
    def save_network(self, path: str | None = None, force: bool = False) -> dict[str, Any]:
        return self._save(path, force, "save_network")

    def save_on_quit(self, force: bool = False) -> dict[str, Any]:
        """Quit-confirmation dialog's "Save and quit": an in-place save under the same rules as save_network, logged with tool="quit"."""
        return self._save(None, force, "quit")

    def _save(self, path: str | None, force: bool, tool: str) -> dict[str, Any]:
        current = self.require_network()
        self._reject_if_running("save")
        other = self.other_vissim_processes()
        if other is None and not force:
            raise ToolError(KIND_OTHER_VISSIM_RUNNING,
                            "Could not check whether other VISSIM processes exist (tasklist failed). Verify, then save with force=true.",
                            {"other_vissim_processes": None})
        if other and not force:
            raise ToolError(KIND_OTHER_VISSIM_RUNNING,
                            f"{other} VISSIM process(es) other than the server's are running. Verify, then save with force=true.",
                            {"other_vissim_processes": other})
        snap = self._state.snapshot()["network"]
        unsaved = int(snap["unsaved_writes"])
        warnings: list[str] = []
        v = self._v
        result: dict[str, Any]
        if path is None:
            target = Path(os.path.abspath(current))
            backup = self._backup(target) if target.is_file() else None
            if backup is None:
                warnings.append(f"Backup skipped because the original file does not exist: {target}")
            self.call(vissim_com.save, v, tool=tool)
            self._mtime = _mtime_ns(str(target))   # baseline right after saving, so it is not mistaken for an out-of-server change
            result = {"saved_path": str(target), "backup_path": _opt(backup), "writes_flushed": unsaved,
                      "path_changed": False, "warnings": warnings}
        else:
            target = Path(os.path.abspath(str(path)))
            backup = self._backup(target) if target.is_file() else None
            self.call(vissim_com.save_as, v, str(target), tool=tool)
            self._state.set_network(str(target), snap["counts"])
            self._mtime = _mtime_ns(str(target))
            result = {"saved_path": str(target), "backup_path": _opt(backup), "writes_flushed": unsaved,
                      "path_changed": True, "note": f"Subsequent saves go to {target}", "warnings": warnings}
        self._state.reset_writes()
        self._log.append({"tool": tool, "kind": "save", "network_path": str(target), "ok": True,
                          "backup_path": _opt(backup), "writes_flushed": unsaved, "force": bool(force)})
        return result

    def save_recovery_copy(self, info: dict[str, Any] | None = None) -> dict[str, Any]:
        """If a file is open or something was drawn, save_as into <log_root>/recovery/ (original untouched).
        Otherwise recovery_path=None.

        Kept regardless of the conversation's write count (UI changes cannot be counted). info is the open_info
        result (computed here if omitted).
        Caution: after SaveNetAs the server VISSIM's open file is the recovery copy. Call this only right before
        shutdown, and do not run the current-file check afterwards (it would follow the recovery file).
        """
        snap = self._state.snapshot()["network"]
        unsaved = int(snap["unsaved_writes"])
        if self._v is None:
            return {"recovery_path": None, "unsaved_writes": unsaved}
        if info is None:
            info = self.open_info("save_recovery_copy")
        if not self.needs_discard(info):
            return {"recovery_path": None, "unsaved_writes": unsaved}
        path = snap["path"]
        stem = _stem(Path(os.path.abspath(path))) if path is not None else RECOVERY_DRAWN_STEM
        folder = (self._log_root / RECOVERY_DIR).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        target = _unique(folder, stem, self._stamp(), ".inpx")
        self.call(vissim_com.save_as, self._v, str(target), tool="save_recovery_copy")
        entry: dict[str, Any] = {"tool": "save_recovery_copy", "kind": "recovery", "network_path": path, "ok": True,
                                 "recovery_path": str(target), "unsaved_writes": unsaved}
        if path is None:
            entry["drawn_links"] = info.get("drawn_links")
        self._log.append(entry)
        return {"recovery_path": str(target), "unsaved_writes": unsaved}

    # ---- backup ----
    def _stamp(self) -> str:
        return self._clock().strftime(_TS_FORMAT)

    def _under_protected(self, path: Path) -> bool:
        p = Path(os.path.normcase(os.path.abspath(str(path))))
        for root in self._protected_roots:
            r = Path(os.path.normcase(os.path.abspath(str(root))))
            try:
                p.relative_to(r)
                return True
            except ValueError:
                continue
        return False

    def _backup(self, target: Path) -> Path:
        """File-copy backup <name>_<YYYYMMDD-HHMMSS>.bak.inpx. Next to the file; under <log_root>/backup/ if it is under protected_roots."""
        if self._under_protected(target):
            folder = (self._log_root / BACKUP_DIR).resolve()
            folder.mkdir(parents=True, exist_ok=True)
        else:
            folder = target.parent
        bak = _unique(folder, _stem(target), self._stamp(), BACKUP_SUFFIX)
        shutil.copy2(str(target), str(bak))
        return bak


def _stem(path: Path) -> str:
    name = path.name
    return name[:-5] if name.lower().endswith(".inpx") else path.stem


def _unique(folder: Path, stem: str, stamp: str, suffix: str) -> Path:
    """<stem>_<stamp><suffix>; if it already exists within the same second, _2, _3, ..."""
    cand = folder / f"{stem}_{stamp}{suffix}"
    n = 1
    while cand.exists():
        n += 1
        cand = folder / f"{stem}_{stamp}_{n}{suffix}"
    return cand


def _opt(p: Path | None) -> str | None:
    return str(p) if p is not None else None
