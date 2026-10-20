"""Results: reading result attributes of measurement objects (get_results), listing result files, and the .att parser.

The result specifier follows the official example (PTV Vissim 2026 "Examples Training/COM/Basic Commands",
comments after line 415): `metric(run, time interval, vehicle class)` where run = number, Current, Avg, StdDev, Min or
Max; time interval = number, Last, Avg, Total, Min or Max; vehicle class = number or All. As in the example, the queue
counter (QueueCounter) takes two items, `QLen(run, time interval)`.
.att files are only read, and nothing outside the `<name>.results\\` folder next to the network file is opened.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .comthread import ComThread
from .config import TUNABLE_DEFAULTS
from .errors import KIND_BAD_ARGS, KIND_COM_ERROR, KIND_NO_NETWORK, KIND_NOT_FOUND, ToolError
from .registry import RESULT_ATTRS, result_arity
from .state import ServerState

# kind -> (container name under Vissim.Net, single object or not, number of specifier items)
KINDS: dict[str, dict[str, Any]] = {
    "data_collection": {"container": "DataCollectionMeasurements", "single": False, "arity": 3},
    "travel_time": {"container": "VehicleTravelTimeMeasurements", "single": False, "arity": 3},
    "queue": {"container": "QueueCounters", "single": False, "arity": 2},
    "network_performance": {"container": "VehicleNetworkPerformanceMeasurement", "single": True, "arity": 3},
    "vehicle_input": {"container": "VehicleInputs", "single": False, "arity": 3},
    "parking_lot": {"container": "ParkingLots", "single": False, "arity": 3},
    "parking_lot_group": {"container": "ParkLotGrps", "single": False, "arity": 3},
    "parking_decision": {"container": "VehicleRoutingDecisionsParking", "single": False, "arity": 3},
}
# Candidate result metrics for kinds not in registry.RESULT_ATTRS (not yet confirmed with the probe)
EXTRA_RESULT_ATTRS: dict[str, tuple[str, ...]] = {
    "vehicle_input": ("Volume", "Vehs"),
}
RUN_TOKENS = ("Current", "Avg", "StdDev", "Min", "Max")
INTERVAL_TOKENS = ("Last", "Avg", "Total", "Min", "Max")
INTERVAL_ALL = "all"
VEH_CLASS_TOKENS = ("All",)

RESULTS_NOTE = ("Per-vehicle-class values (veh_class=number) only appear when that class is enabled in the evaluation "
                "settings (Evaluation > Configuration > Result Attributes). If there is no result, check collect in "
                "configure_evaluation.")

RESULTS_DIR_SUFFIX = ".results"
ATT_SUFFIX = ".att"
_NAME_RE = re.compile(r"^[^\\/:*?\"<>|]+$")


def result_attrs(kind: str) -> tuple[str, ...]:
    if kind in RESULT_ATTRS:
        return RESULT_ATTRS[kind]
    return EXTRA_RESULT_ATTRS[kind]


def build_spec(attr: str, run: Any, interval: Any, veh_class: Any, arity: int = 3) -> str:
    """Specifier string such as `TravTm(Avg,Avg,All)` (no spaces). arity 1 = (time interval) only, 2 = (run, time interval)."""
    if arity <= 1:
        return f"{attr}({interval})"
    parts = [str(run), str(interval)]
    if arity >= 3:
        parts.append(str(veh_class))
    return f"{attr}({','.join(parts)})"


def _check_token(value: Any, allowed: tuple[str, ...], what: str) -> Any:
    if isinstance(value, bool):
        raise ToolError(KIND_BAD_ARGS, f"{what}: must be a number or one of {', '.join(allowed)} ({value!r})")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value in allowed:
            return value
        if value.strip().isdigit():
            return int(value.strip())
    raise ToolError(KIND_BAD_ARGS, f"{what}: must be a number or one of {', '.join(allowed)} ({value!r})")


def _to_number(text: str):
    """Convert one semicolon-separated cell: int, then float, then string. An empty cell becomes None."""
    s = text.strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def parse_att(text: str) -> dict[str, Any]:
    """VISSIM .att body -> {table, columns, meta, rows (list of list of str)}. ValueError if there is no column header line."""
    meta: list[str] = []
    table = None
    columns: list[str] | None = None
    raw_rows: list[list[str]] = []
    for line in text.splitlines():
        if columns is None:
            if not line.strip():
                continue
            if line.startswith("$VISION") or line.startswith("*") or line.startswith("$TABLE:"):
                meta.append(line.rstrip())
                continue
            if line.startswith("$") and ":" in line:
                head, _, cols = line[1:].partition(":")
                table = head.strip()
                columns = [c.strip() for c in cols.split(";")]
                continue
            raise ValueError(f"a line that is not .att header text appeared before the column header line: {line[:80]!r}")
        if not line.strip():
            continue
        raw_rows.append(line.rstrip("\r\n").split(";"))
    if columns is None:
        raise ValueError("the .att file has no column header line ($table_name:column;column;...)")
    return {"table": table, "columns": columns, "meta": meta, "rows": raw_rows}


def read_att_text(path: Path) -> tuple[str, str]:
    """Read as UTF-8 (BOM allowed), falling back to cp949. Returns (text, encoding name)."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp949"), "cp949"


class Results:
    def __init__(self, cfg: dict[str, Any], state: ServerState, com: ComThread, vissim_getter: Callable[[], Any]):
        self.cfg = cfg
        self.state = state
        self.com = com
        self._vissim = vissim_getter

    def _tunable(self, key: str) -> Any:
        return self.cfg.get(key, TUNABLE_DEFAULTS[key])

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return self.com.submit(fn, *args)
        except ToolError:
            raise
        except Exception as e:   # noqa: BLE001
            raise ToolError(KIND_COM_ERROR, str(e), {"type": type(e).__name__}) from e

    # ---- get_results ----
    def _objects(self, vissim: Any, kind: str, nos: list[int]) -> list[Any]:
        info = KINDS[kind]
        name = info["container"]
        # All container and item access happens inside the COM thread (apartment constraint)
        if info["single"]:
            return [self._call(lambda: getattr(vissim.Net, name))]
        if nos:
            objs = []
            for no in nos:
                try:
                    objs.append(self.com.submit(lambda n=no: getattr(vissim.Net, name).ItemByKey(n)))
                except Exception as e:   # noqa: BLE001 — a missing number comes back as a COM error
                    raise ToolError(KIND_NOT_FOUND, f"{kind} {no} not found: {e}",
                                    {"kind": kind, "no": no}) from e
            return objs
        return list(self._call(lambda: getattr(vissim.Net, name).GetAll()))

    def _read_row(self, obj: Any, attrs: tuple[str, ...], run: Any, interval: Any, veh_class: Any,
                  arity: int) -> dict[str, Any]:
        # The number of sub-specifiers differs per metric (QLen 2, Volume 1, ...): the registry's result_arity is authoritative, arity is an upper bound
        specs = [build_spec(a, run, interval, veh_class, min(arity, result_arity(a))) for a in attrs]

        def read_all():
            return [obj.AttValue(s) for s in specs]

        values = self._call(read_all)
        return dict(zip(attrs, values))

    def get_results(self, kind: str, nos: list[int] | None = None, run: Any = "Current",
                    interval: Any = INTERVAL_ALL, veh_class: Any = "All") -> dict[str, Any]:
        if kind not in KINDS:
            raise ToolError(KIND_BAD_ARGS, f"kind must be one of {', '.join(KINDS)} ({kind!r})")
        nos = list(nos or [])
        if not isinstance(nos, list) or any(isinstance(n, bool) or not isinstance(n, int) for n in nos):
            raise ToolError(KIND_BAD_ARGS, f"nos must be a list of integers ({nos!r})")
        run = _check_token(run, RUN_TOKENS, "run")
        if not (isinstance(interval, str) and interval == INTERVAL_ALL):
            interval = _check_token(interval, INTERVAL_TOKENS, "interval")
        veh_class = _check_token(veh_class, VEH_CLASS_TOKENS, "veh_class")

        info = KINDS[kind]
        attrs = result_attrs(kind)
        arity = info["arity"]
        max_intervals = int(self._tunable("result_max_intervals"))
        vissim = self._vissim()
        objs = self._objects(vissim, kind, nos)

        rows: list[dict[str, Any]] = []
        limit_reached = False
        for obj in objs:
            if info["single"]:
                no, name = None, None
            else:
                no, name = self._call(lambda o=obj: (o.AttValue("No"), o.AttValue("Name")))
            if interval == INTERVAL_ALL:
                i = 1
                while True:
                    try:
                        vals = self._read_row(obj, attrs, run, i, veh_class, arity)
                    except ToolError as e:
                        # VISSIM 2026 rejects an out-of-range time interval with a COM error ("Time Interval ... does not
                        # match") instead of returning None -> an error at the first interval is real, a later one means
                        # the intervals have ended
                        if i == 1 or "Time Interval" not in e.message:
                            raise
                        break
                    if all(v is None for v in vals.values()):
                        break
                    rows.append({"no": no, "name": name, "interval": i, **vals})
                    if i >= max_intervals:
                        limit_reached = True
                        break
                    i += 1
            else:
                vals = self._read_row(obj, attrs, run, interval, veh_class, arity)
                if all(v is None for v in vals.values()):
                    continue
                rows.append({"no": no, "name": name, "interval": interval, **vals})
        if not rows:
            raise ToolError(KIND_NOT_FOUND,
                            f"no {kind} results (all values are empty): evaluation was switched off (collect in "
                            f"configure_evaluation), the simulation has not been run yet, or there is no value for "
                            f"this run/interval/veh_class",
                            {"kind": kind, "nos": nos, "run": run, "interval": interval, "veh_class": veh_class,
                             "objects": len(objs)})
        out = {"kind": kind, "run": run, "interval": interval, "veh_class": veh_class, "attrs": list(attrs),
               "spec_example": build_spec(attrs[0], run, 1 if interval == INTERVAL_ALL else interval,
                                          veh_class, arity),
               "rows": rows, "count": len(rows), "note": RESULTS_NOTE}
        if interval == INTERVAL_ALL:
            out["interval_limit_reached"] = limit_reached
        if kind not in RESULT_ATTRS:
            out["attr_ids_verified"] = False
        return out

    # ---- result files ----
    def _results_dir(self) -> Path:
        path = self.state.full()["network"]["path"]
        if not path:
            raise ToolError(KIND_NO_NETWORK, "no network is open, so the results folder cannot be determined")
        p = Path(path)
        return p.parent / (p.stem + RESULTS_DIR_SUFFIX)

    def list_result_files(self) -> dict[str, Any]:
        d = self._results_dir()
        if not d.is_dir():
            return {"folder": str(d), "files": [],
                    "note": f"The results folder ({d.name}) does not exist; it is created once a simulation has run. Read aggregated results with get_results (.att files exist only if exported from VISSIM or if raw data writing is on)"}
        files = []
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() == ATT_SUFFIX:
                s = f.stat()
                files.append({"name": f.name, "size": s.st_size,
                              "modified_at": datetime.fromtimestamp(s.st_mtime).astimezone().isoformat(
                                  timespec="seconds"),
                              "_mtime": s.st_mtime})
        files.sort(key=lambda x: (-x["_mtime"], x["name"]))
        for f in files:
            del f["_mtime"]
        return {"folder": str(d), "files": files}

    def _resolve_file(self, name: Any) -> Path:
        if not isinstance(name, str) or not name.strip():
            raise ToolError(KIND_BAD_ARGS, "name must be a non-empty file name")
        if ".." in name or not _NAME_RE.match(name) or Path(name).is_absolute():
            raise ToolError(KIND_BAD_ARGS, f"name accepts only a file name inside the results folder (no paths or ..): {name!r}")
        if not name.lower().endswith(ATT_SUFFIX):
            raise ToolError(KIND_BAD_ARGS, f"name must be an {ATT_SUFFIX} file: {name!r}")
        d = self._results_dir()
        p = (d / name)
        try:
            p.resolve().relative_to(d.resolve())
        except ValueError as e:
            raise ToolError(KIND_BAD_ARGS, f"a file outside the results folder cannot be opened: {name!r}") from e
        if not p.is_file():
            raise ToolError(KIND_NOT_FOUND, f"result file not found: {p}")
        return p

    def read_result_file(self, name: str, max_rows: int = 1000, where: dict[str, Any] | None = None) -> dict[str, Any]:
        p = self._resolve_file(name)
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0:
            raise ToolError(KIND_BAD_ARGS, f"max_rows must be an integer >= 0 ({max_rows!r})")
        where = dict(where or {})
        text, encoding = read_att_text(p)
        try:
            parsed = parse_att(text)
        except ValueError as e:
            raise ToolError(KIND_BAD_ARGS, f"{p.name}: {e}") from e
        columns = parsed["columns"]
        missing = [c for c in where if c not in columns]
        if missing:
            raise ToolError(KIND_BAD_ARGS, f"where refers to columns that do not exist: {missing} (columns: {columns})")
        idx = {c: columns.index(c) for c in where}
        matched: list[list[str]] = []
        for raw in parsed["rows"]:
            ok = True
            for c, v in where.items():
                cell = raw[idx[c]].strip() if idx[c] < len(raw) else ""
                if cell != str(v):
                    ok = False
                    break
            if ok:
                matched.append(raw)
        rows = [[_to_number(cell) for cell in raw] for raw in matched[:max_rows]]
        return {"name": p.name, "path": str(p), "encoding": encoding, "table": parsed["table"],
                "columns": columns, "meta": parsed["meta"], "rows": rows, "row_count": len(matched),
                "truncated": len(matched) > len(rows), "max_rows": max_rows, "where": where}
