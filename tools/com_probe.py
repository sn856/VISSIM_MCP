"""One-off COM check for the MCP server, run once on a PC with Vissim 2026 installed. It never saves a network.

  python tools/com_probe.py [--progid Vissim.Vissim.26] [--force] [--rot-automation] [--out PATH]

For each item it prints "OK" or "REJECTED + Vissim's message" for the creation signatures, remove methods and
result attributes listed in the registry (src/vissim_mcp/registry.py), to confirm that Vissim 2026 COM actually
accepts them. A rejection does not stop the run; the next item continues. No other forms are tried by guesswork -
only the registry (official example) forms.

Output sections:
 0   Connection (launch if present, otherwise Dispatch - never GetActiveObject), version, Vissim*.exe PIDs before/after start
 1   Empty new network, 2 links (2 lanes, 3D WKT) + 1 connector - the basis for the later sections
 2a  Each registry creation signature -> confirm creation via AttValue("No") -> write attributes after creation ->
     read default_attrs (for parking lots also the ParkSpcs container count and Index keys)
 2g  Remove the just-created reduced speed area, static route and static routing decision with the registry remove
     names (a leftover static route keeps the simulation from starting)
 3b  Lane attributes (blocking / time-related IDs), BlockedVehClasses writes
 4c  Simulation attribute reads/writes, QuickMode, SimBreakAt 60 -> RunContinuous -> 120, Stop, SimulationRuns.Count
 5d  Evaluation attribute IDs (collection / segment related, including the parking group), *CollectData writes
 5d' default_attrs reads of single objects (Simulation, Evaluation, network performance measurement) - before the
     simulation, and once more after 6e with --net
 6e  Result attribute IDs and TravTm(...) reads; parking lot, parking lot group, parking routing decision and parking space results
 6g  Remove parking objects (route -> routing decision -> parking lot -> parking lot group)
 7f  Attributes.GetAll() meta fields, Link/Connector geometry and coordinate attribute IDs and Editable
 8h  Whether the PID set difference is exactly one
 9i  ROT facts (whether the current instance is in the ROT; with --rot-automation, how many seconds GetActiveObject
     takes to attach to an instance started with -automation)
10   "Nothing saved" notice + the whole output is written to runs/mcp/probe_<YYYYMMDD-HHMMSS>.txt (change with --out)
Note: this creates an empty new network (aborts without --force if there is open work). It never calls SaveNet/SaveNetAs.

Scenario / signal / time interval checks. If any of the options below is given, only these run instead of 0-9i above:

  python tools/com_probe.py --scenario <copy folder> [--plain <copy .inpx>] [--label scenario]
  python tools/com_probe.py --signal <FIXEDTIME copy .inpx> [--label signal]
  python tools/com_probe.py --signal-simple <FIXEDTIMESIMPLE copy .inpx> [--label signal_simple]
  python tools/com_probe.py --intervals [--net <saved .inpx>] [--label intervals]

 S1  LoadProject(.vissimpdb) -> for each LoadBaseNet / LoadScenario(n) / LoadModification(n): WorkingFolder, InputFile,
     NetPara.UnderScenMngm, counts; folder changes after opening only; the .vissimpdb tables (SQLite, read-only)
 S2  Change the first vehicle input's Volume(1), then for each of SaveScenario / SaveScenarioAs(name) (return value) /
     SaveInMultipleScenarios (BSTR per the type library - a comma-separated string, or an integer list if rejected) /
     SaveModification: the changed files (size, modification time) and the tables
 S3  SaveNetAs(copy path) from a scenario: the file left open and the management mode; LoadNet of that copy;
     going back with LoadProject + LoadScenario
 S4  PlaceUnderScenarioManagement(project name, scenario 1 name) on a plain .inpx copy - the files and folders it creates
 S5  Two variants of a plain .inpx copy (A: link name, polyline point, new links, connector; B: reference point) ->
     GenerateModelTransferFile(..., Reverse False/True) / GenerateModelTransferFileBetweenFiles -> the shape of the
     .trax -> counts before and after ApplyModelTransferFile
 G   FIXEDTIME: summary of the base IntSupplyData (base64 VISSIG XML), SigState timeline (SimBreakAt in 1 s steps),
     SG attribute writes (b), SC CycTm/Offset/ProgNo writes (c), writing IntSupplyData with only one cmd changed (d),
     new SC, SG and signal head with a first program (f), RemoveSignalController (g), SimulationRuns numbers and
     result specifiers (Q9)
 G-h FIXEDTIMESIMPLE: SG EndRed, EndGreen and Amber writes and the timeline
 I   TimeIntervalSets and TimeInts (AddTimeInterval, Start, RemoveTimeInterval), Volume/VehComp/Cont/RelFlow of a new
     interval, VehCompRelFlows (key, add, remove)
Safety: the original official examples (Examples Training) are never taken as input - only copies placed under
runs/mcp/scratch/ are accepted, a run-only copy runs/mcp/scratch/<timestamp>/<name>/ is recreated from them, and
only that copy is opened and saved (SaveNetAs, Save* and PlaceUnderScenarioManagement also write only below it).
The log is written to the file line by line right away, and if one step exceeds --step-timeout (default 300 s) only
the Vissim this script started is terminated (a modal window is suspected). At the end: New() -> Exit().
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import probe_net as vc  # noqa: E402
from vissim_mcp.config import VISSIM_DEFAULTS  # noqa: E402
from vissim_mcp import registry  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

# ---------- Output (screen + log file) ----------

_LINES: list[str] = []
_FH: list[Any] = []      # log file: every line is written immediately (so the early part survives if Vissim hangs)


def out(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    _LINES.append(line)
    for fh in _FH:
        try:
            fh.write(line + "\n")
            fh.flush()
        except Exception:
            pass


def errtext(e: BaseException) -> str:
    """For a COM error, Vissim's description; otherwise str(e); on one line."""
    try:
        import pywintypes
        if isinstance(e, pywintypes.com_error) and len(e.args) >= 3 and e.args[2]:
            desc = e.args[2][2] if len(e.args[2]) > 2 else None
            if desc:
                return " ".join(str(desc).split())
    except Exception:
        pass
    return " ".join(str(e).split()) or type(e).__name__


def attempt(label: str, fn, *args, **kwargs):
    """Call fn and print 'OK label → value' or 'REJECTED label: message'. Returns (success, value|None)."""
    try:
        val = fn(*args, **kwargs)
        out("    OK  ", label, "→", repr(val) if val is not None else "")
        return True, val
    except Exception as e:
        out("    REJECTED", label + ":", errtext(e))
        return False, None


# ---------- COM helpers ----------

def vissim_pids() -> set[int]:
    """PIDs of processes in tasklist whose image name starts with Vissim."""
    try:
        txt = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             encoding="cp949", errors="replace").stdout
    except Exception:
        return set()
    pids: set[int] = set()
    for row in txt.splitlines():
        cells = [c.strip('"') for c in row.split('","')]
        if len(cells) >= 2 and cells[0].lower().startswith("vissim"):
            try:
                pids.add(int(cells[1]))
            except ValueError:
                pass
    return pids


def com_method_names(obj: Any) -> list[str]:
    """Count the COM object's method and property names from its type info (a dynamic Dispatch does not list them in dir()). Falls back to dir()."""
    names: list[str] = []
    try:
        ti = obj._oleobj_.GetTypeInfo()
        attr = ti.GetTypeAttr()
        for i in range(attr.cFuncs):
            fd = ti.GetFuncDesc(i)
            names.append(ti.GetNames(fd.memid)[0])
        return sorted(set(names))
    except Exception:
        return [n for n in dir(obj) if not n.startswith("_")]


META_FIELDS = ("ID", "Code", "Name", "ValueType", "Editable", "Category", "Description")   # fields confirmed in 2026


def meta(a: Any, field: str):
    try:
        return getattr(a, field)
    except Exception:
        try:
            return a.AttValue(field)
        except Exception:
            return None


def attr_rows(obj: Any) -> list[dict[str, Any]]:
    """container.Attributes.GetAll() → [{ID, Code, Name, ValueType, Editable, Category}].

    2026: the attribute list exists only on **containers** (Links, Links[n].Lanes ...), not on item objects or single objects (NetPara, Simulation, Evaluation).
    `Code` is the name used in AttValue (the ID is written in upper case).
    """
    rows: list[dict[str, Any]] = []
    for a in obj.Attributes.GetAll():
        rows.append({
            "ID": meta(a, "Code") if meta(a, "Code") is not None else meta(a, "ID"),
            "ShortName": meta(a, "ID"),
            "LongName": meta(a, "Name"),
            "ValueType": meta(a, "ValueType"),
            "Editable": meta(a, "Editable"),
        })
    return rows


def rows_matching(rows: list[dict[str, Any]], tokens: tuple[str, ...], in_names: bool = False) -> list[dict[str, Any]]:
    low = tuple(t.lower() for t in tokens)

    def hit(r):
        hay = [str(r["ID"] or "")]
        if in_names:
            hay += [str(r["ShortName"] or ""), str(r["LongName"] or "")]
        return any(t in h.lower() for h in hay for t in low)
    return [r for r in rows if hit(r)]


def print_rows(rows: list[dict[str, Any]], indent: str = "      ") -> None:
    if not rows:
        out(indent + "(none)")
    for r in rows:
        out(f"{indent}{r['ID']} | {r['ShortName']} | {r['LongName']} | {r['ValueType']} | Editable={r['Editable']}")


# ---------- 2a creation signatures ----------

def first_item(coll: Any) -> Any:
    items = coll.GetAll()
    if not items:
        raise RuntimeError("empty")
    return items[0]


# Parent object kind of a composite-key container - 'decision' differs per route kind
_PARENT_OF = {"VehicleRouteStatic": "VehicleRoutingDecisionStatic",
              "VehicleRouteParking": "VehicleRoutingDecisionParking"}


def container_of(v: Any, t: registry.ObjType, created: dict[str, Any]) -> Any:
    """Registry container path → COM collection. 'A[decision].B' is A.ItemByKey(No of the created decision).B."""
    cur = v
    for part in t.container.split("."):
        if "[" in part:
            name, key_name = part[:-1].split("[")
            cur = getattr(cur, name)
            src = _PARENT_OF[t.name] if key_name == "decision" else {"link": "Link", "lot": "ParkingLot"}[key_name]
            cur = cur.ItemByKey(int(created[src].AttValue("No")))
        else:
            cur = getattr(cur, part)
    return cur


def probe_creates(v: Any, lk1: Any, lk2: Any, created: dict[str, Any]) -> None:
    net = v.Net
    try:
        vt = first_item(net.VehicleTypes)
        dsd = first_item(net.SpeedDistributions)      # container name in 2026 (the official example's DesSpeedDistributions does not exist)
        cls_no = int(first_item(net.VehicleClasses).AttValue("No"))
        out("    Base data: VehicleType", vt.AttValue("No"), "| SpeedDistr", dsd.AttValue("No"), "| VehicleClass", cls_no)
    except Exception as e:
        out("    REJECTED reading base data (VehicleTypes/SpeedDistributions/VehicleClasses):", errtext(e))
        vt = dsd = None
        cls_no = 10
    try:
        tdist_no = int(first_item(net.TimeDistributions).AttValue("No"))
        out("    Base data: TimeDistribution", tdist_no)
    except Exception as e:
        out("    REJECTED reading base data (TimeDistributions):", errtext(e))
        tdist_no = None
    lane1 = lk1.Lanes.ItemByKey(1)
    len1 = float(lk1.AttValue("Length2D"))
    len2 = float(lk2.AttValue("Length2D"))
    # A parking lot needs a space length (LenPerSpc >= 1 m), so put it on the longest link (links from --net may be under 1 m)
    try:
        long_lk = max((lk for lk in net.Links.GetAll() if not bool(lk.AttValue("IsConn"))),
                      key=lambda lk: float(lk.AttValue("Length2D")))
    except Exception as e:
        out("    REJECTED finding the longest link:", errtext(e))
        long_lk = lk1
    len_long = float(long_lk.AttValue("Length2D"))
    out(f"    Parking test link: {long_lk.AttValue('No')} (Length2D={len_long:.1f})")
    lk3_wkt = vc.wkt_linestring([(300.5, 5.0, 56.0), (400.0, 5.0, 57.0)], True)
    # argument name -> value. Values that differ per kind go in overrides.
    base_ctx: dict[str, Any] = {
        "wkt": lk3_wkt, "lane_widths": [3.6, 3.6],
        "from_lane": lambda: lk2.Lanes.ItemByKey(1), "from_pos": len2,
        "to_lane": lambda: created["Link"].Lanes.ItemByKey(1), "to_pos": 0.0, "num_lanes": 2,
        "link": lk1, "lane": lane1, "pos": 10.0,
        "dest_link": lk2, "dest_pos": 50.0,
        "from_link": lk1, "to_link": lk2,
        "relative_flows": [vt, dsd],
        "parking_lot": lambda: created["ParkingLot"],
    }
    overrides: dict[str, dict[str, Any]] = {
        "Connector": {"wkt": "LINESTRING EMPTY"},
        # travel time measurement: from_pos and to_pos differ from the connector (end of lk2 -> start of the new link), so only override them here
        "VehicleTravelTimeMeasurement": {"from_pos": 10.0, "to_pos": 50.0},
        "VehicleRoutingDecisionStatic": {"pos": 20.0},
        "DesSpeedDecision": {"pos": 30.0},
        "ReducedSpeedArea": {"pos": 60.0},
        "DataCollectionPoint": {"pos": 100.0},
        "QueueCounter": {"pos": 150.0},
        # parking: on the longest link - routing decision at the front, parking lot in the middle
        "VehicleRoutingDecisionParking": {"link": long_lk, "pos": round(min(2.0, len_long * 0.02), 1)},
        "ParkingLot": {"lane": lambda: long_lk.Lanes.ItemByKey(1), "pos": round(len_long * 0.5, 1)},
    }
    def free_no(coll_name: str, start: int) -> int:
        try:
            used = {int(o.AttValue("No")) for o in getattr(net, coll_name).GetAll()}
        except Exception:
            return start
        n = start
        while n in used:
            n += 1
        return n
    numbers = {"Link": free_no("Links", 3), "Connector": free_no("Links", 10001)}

    for t in registry._TYPES:
        if t.create is None:
            continue
        sig = t.create
        no = numbers.get(t.name, 0)
        argv: list[Any] = []
        try:
            ctx = dict(base_ctx, **overrides.get(t.name, {}))
            for arg in sig.args:
                if arg.kind == "no":
                    argv.append(no)
                else:
                    val = ctx[arg.name]
                    # COM objects are callable (default member call), so call only Python functions
                    argv.append(val() if isinstance(val, types.FunctionType) else val)
            coll = container_of(v, t, created)
        except Exception as e:
            out(f"    REJECTED {t.name}.{sig.method}: failed to prepare arguments/container: {errtext(e)}")
            continue
        shown = ", ".join(_show(a) for a in argv)
        try:
            obj = getattr(coll, sig.method)(*argv)
        except Exception as e:
            out(f"    REJECTED {t.name}.{sig.method}({shown}): {errtext(e)}")
            continue
        try:
            got_no = obj.AttValue("No")
            out(f"    OK   {t.name}.{sig.method}({shown}) → No={got_no}")
        except Exception as e:
            out(f"    REJECTED {t.name}.{sig.method}({shown}): AttValue(No) failed after creation: {errtext(e)}")
            continue
        created[t.name] = obj
        for attr, val in post_writes(t.name, created, cls_no, dsd, tdist_no, len_long):
            attempt(f"{t.name}.SetAttValue({attr!r}, {val!r})", obj.SetAttValue, attr, val)
        print_default_attrs(t, obj, coll)
        if t.name == "ParkingLot":
            probe_parking_spaces(obj)
        if t.name == "VehicleComposition":
            try:
                flows = obj.VehCompRelFlows.GetAll()
                out("      VehCompRelFlows count:", len(flows))
                if flows:
                    attempt("VehCompRelFlows[0].SetAttValue('RelFlow', 1.0)", flows[0].SetAttValue, "RelFlow", 1.0)
            except Exception as e:
                out("    REJECTED VehCompRelFlows.GetAll():", errtext(e))


def _show(a: Any) -> str:
    if isinstance(a, (int, float, str)):
        s = repr(a)
        return s if len(s) <= 40 else s[:37] + "...'"
    if isinstance(a, list):
        return "[" + ", ".join(_show(x) for x in a) + "]"
    try:
        return f"<{a.AttValue('No')}>"
    except Exception:
        return "<obj>"


def print_default_attrs(t: registry.ObjType, obj: Any, coll: Any = None) -> None:
    """Read each registry default_attrs entry one by one and print one line for what reads and one for what is rejected. If coll is given, also
    one container GetMultipleAttributes call as list_objects does (a single rejected column fails the whole call)."""
    got, bad = {}, {}
    for attr in t.default_attrs:
        try:
            got[attr] = obj.AttValue(attr)
        except Exception as e:
            bad[attr] = errtext(e)
    out(f"      default_attrs read: {got}")
    if bad:
        out(f"      default_attrs rejected: {bad}")
    if coll is not None:
        attempt(f"{t.name} container GetMultipleAttributes(default_attrs) row count",
                lambda: len(coll.GetMultipleAttributes(list(t.default_attrs))))


def probe_parking_spaces(lot: Any) -> None:
    """Parking space (ParkSpcs) container - count, GetMultipleAttributes, ItemByKey(Index)."""
    try:
        spcs = lot.ParkSpcs
        attempt("ParkingLot.ParkSpcs.Count", lambda: int(spcs.Count))
        ok, rows = attempt("ParkSpcs.GetMultipleAttributes(['Index', 'ParkingLot'])",
                           lambda: spcs.GetMultipleAttributes(["Index", "ParkingLot"]))
        if ok and rows:
            idx = rows[0][0]
            attempt(f"ParkSpcs.ItemByKey({idx!r}).AttValue('Index')", lambda: spcs.ItemByKey(idx).AttValue("Index"))
    except Exception as e:
        out("    REJECTED ParkingLot.ParkSpcs:", errtext(e))


def post_writes(name: str, created: dict[str, Any], cls_no: int, dsd: Any, tdist_no: int | None = None,
                len_long: float = 0.0) -> list[tuple[str, Any]]:
    """Attributes written after creation (registry post + Name)."""
    dsd_no = None
    try:
        dsd_no = int(dsd.AttValue("No"))
    except Exception:
        pass
    writes: list[tuple[str, Any]] = []
    if name == "VehicleInput":
        writes.append(("Volume(1)", 600))
        try:
            writes.append(("VehComp(1)", int(created["VehicleComposition"].AttValue("No"))))
        except Exception:
            writes.append(("VehComp(1)", 1))
    elif name == "VehicleRouteStatic":
        writes.append(("RelFlow(1)", 1.0))
    elif name == "DesSpeedDecision":
        writes.append((f"DesSpeedDistr({cls_no})", dsd_no))
    elif name == "ReducedSpeedArea":
        writes.append(("Length", 20.0))
        writes.append((f"DesSpeedDistr({cls_no})", dsd_no))
    elif name == "ParkingLot":
        writes.append(("Length", round(min(20.0, len_long * 0.3), 1)))
        try:
            writes.append(("EvalGrps", str(int(created["ParkingLotGroup"].AttValue("No")))))
        except Exception:
            pass
    elif name == "VehicleRoutingDecisionParking":
        writes.append(("AllVehTypes", False))
        writes.append(("VehClasses", str(cls_no)))
        writes.append(("ParkRate(1)", 0.5))     # percentage attributes are 0-1 in COM (50.0 is rejected as "5000 %")
        if tdist_no is not None:
            writes.append(("ParkDur(1)", tdist_no))
    writes.append(("Name", f"probe {name}"))
    return writes


def probe_remove(v: Any, created: dict[str, Any], names: tuple[str, ...] = ("ReducedSpeedArea",)) -> None:
    """Remove the objects just created, in order, using the registry remove names (dependents first, e.g. route -> decision -> parking lot)."""
    for name in names:
        t = registry.get(name)
        obj = created.get(name)
        if obj is None or t is None or t.remove is None:
            out(f"    SKIPPED {name} is not present, so removal was not tested")
            continue
        try:
            coll = container_of(v, t, created)
            before = int(coll.Count)
            getattr(coll, t.remove)(obj)
            after = int(coll.Count)
            out(f"    OK   {t.container}.{t.remove}(obj) → Count {before} → {after}")
            created.pop(name, None)
        except Exception as e:
            out(f"    REJECTED {t.container}.{t.remove}(obj): {errtext(e)}")


# ---------- 4c Simulation ----------

SIM_WRITE_VALUES: dict[str, Any] = {
    "SimPeriod": 120, "SimRes": 10, "RandSeed": 42, "NumRuns": 1, "RandSeedIncr": 1,
    "SimBreakAt": 60, "UseMaxSimSpeed": True,
}
SIM_ATTRS = ("SimPeriod", "SimRes", "RandSeed", "NumRuns", "RandSeedIncr", "SimBreakAt",
             "UseMaxSimSpeed", "SimSec", "StartTm")


def probe_simulation(v: Any) -> None:
    sim = v.Simulation
    for attr in SIM_ATTRS:
        ok, cur = attempt(f"Simulation.AttValue({attr!r})", sim.AttValue, attr)
        val = SIM_WRITE_VALUES.get(attr, cur)    # SimSec and StartTm are written back with the value just read
        if ok or attr in SIM_WRITE_VALUES:
            attempt(f"Simulation.SetAttValue({attr!r}, {val!r})", sim.SetAttValue, attr, val)
    try:
        win = v.Graphics.CurrentNetworkWindow
        attempt("CurrentNetworkWindow.AttValue('QuickMode')", win.AttValue, "QuickMode")
        attempt("CurrentNetworkWindow.SetAttValue('QuickMode', 1)", win.SetAttValue, "QuickMode", 1)
        attempt("CurrentNetworkWindow.AttValue('QuickMode') again", win.AttValue, "QuickMode")
    except Exception as e:
        out("    REJECTED Graphics.CurrentNetworkWindow:", errtext(e))
    attempt("SetAttValue('SimPeriod', 120)", sim.SetAttValue, "SimPeriod", 120)
    attempt("SetAttValue('SimBreakAt', 60)", sim.SetAttValue, "SimBreakAt", 60)
    t0 = time.perf_counter()
    ok, _ = attempt("RunContinuous() (should stop at 60)", sim.RunContinuous)
    attempt(f"AttValue('SimSec') [{time.perf_counter() - t0:.1f}s elapsed]", sim.AttValue, "SimSec")
    attempt("SetAttValue('SimBreakAt', 120)", sim.SetAttValue, "SimBreakAt", 120)
    t0 = time.perf_counter()
    attempt("RunContinuous() (to the end)", sim.RunContinuous)
    attempt(f"AttValue('SimSec') [{time.perf_counter() - t0:.1f}s elapsed]", sim.AttValue, "SimSec")
    try:
        has_stop = hasattr(sim, "Stop")
        out("    Simulation.Stop exists:", has_stop)
        if has_stop:
            attempt("Simulation.Stop()", sim.Stop)
    except Exception as e:
        out("    REJECTED Simulation.Stop check:", errtext(e))
    attempt("Net.SimulationRuns.Count", lambda: int(v.Net.SimulationRuns.Count))


# ---------- 5d Evaluation ----------

def evaluation_obj(v: Any) -> tuple[Any, str] | tuple[None, str]:
    for path in ("Evaluation", "Net.Evaluation"):
        try:
            cur = v
            for part in path.split("."):
                cur = getattr(cur, part)
            cur.AttValue("DataCollCollectData")   # is it accessible? (Evaluation in 2026 has no Attributes)
            return cur, path
        except Exception as e:
            out(f"    REJECTED {path}: {errtext(e)}")
    return None, ""


def probe_evaluation(v: Any) -> None:
    ev, path = evaluation_obj(v)
    if ev is None:
        return
    out("    Evaluation object path:", path)
    out("    (2026: Evaluation has no Attributes list - attribute IDs are read one by one, taken from the attribute reference)")
    try:
        from vissim_mcp.simulation import EVAL_ATTRS
        ids = [aid for group in EVAL_ATTRS.values() for aid in group.values()]
    except Exception:
        ids = ["DataCollCollectData", "VehTravTmsCollectData", "QueuesCollectData", "VehNetPerfCollectData"]
    for attr in ids:
        attempt(f"{path}.AttValue({attr!r})", ev.AttValue, attr)
    for attr in [a for a in ids if a.endswith("CollectData")]:
        attempt(f"{path}.SetAttValue({attr!r}, True)", ev.SetAttValue, attr, True)
    for attr in ("DataCollWriteFile", "VehTravTmsWriteFile", "VehInputWriteFile", "KeepPrevResults", "EvalOutDir"):
        attempt(f"{path}.AttValue({attr!r}) (candidate related to file writing)", ev.AttValue, attr)


# ---------- 5d' default_attrs of single objects ----------

def probe_single_defaults(v: Any, when: str) -> None:
    """Read default_attrs of the registry single objects (Simulation, Evaluation, network performance measurement) by the same
    path as get_attributes/list_objects (AttValue per attribute). For the network performance measurement, also read one bare name without a specifier so its rejection message is recorded."""
    for t in (registry.get(n) for n in registry.names()):
        if t.key != registry.KEY_SINGLE or t.name == "NetPara":
            continue
        try:
            obj = v
            for part in t.container.split("."):
                obj = getattr(obj, part)
        except Exception as e:
            out(f"    REJECTED {t.container} ({when}):", errtext(e))
            continue
        out(f"    [{t.name}] {when}")
        print_default_attrs(t, obj)
        if t.name == "VehicleNetworkPerformanceMeasurement":
            attempt(f"{t.name}.AttValue('DelayAvg') (bare name; rejection expected)", obj.AttValue, "DelayAvg")


# ---------- 6e Results ----------

RESULT_TOKENS = ("TravTm", "Vehs", "Speed", "QLen", "QStops", "Delay", "Dist", "Stops", "Occup", "Pers",
                 "Accel", "Length", "Demand", "VehA")


def probe_results(v: Any, created: dict[str, Any]) -> None:
    sim = v.Simulation
    attempt("SetAttValue('SimBreakAt', 0) (short run after enabling evaluation)", sim.SetAttValue, "SimBreakAt", 0)
    attempt("RunContinuous()", sim.RunContinuous)
    attempt("AttValue('SimSec')", sim.AttValue, "SimSec")
    targets: list[tuple[str, Any, Any, str, tuple[str, ...]]] = []
    net = v.Net
    try:
        tts = net.VehicleTravelTimeMeasurements.GetAll()
        out("    VehicleTravelTimeMeasurements count:", len(tts))
        if tts:
            targets.append(("VehicleTravelTimeMeasurement", tts[0], net.VehicleTravelTimeMeasurements, "travel_time",
                            ("TravTm(Current,Last,All)", "TravTm(Avg,Avg,All)", "Vehs(Current,Last,All)")))
    except Exception as e:
        out("    REJECTED VehicleTravelTimeMeasurements:", errtext(e))
    try:
        dcms = net.DataCollectionMeasurements.GetAll()
        out("    DataCollectionMeasurements count:", len(dcms))
        if dcms:
            targets.append(("DataCollectionMeasurement", dcms[0], net.DataCollectionMeasurements, "data_collection",
                            ("Vehs(Current,Last,All)", "SpeedAvgArith(Current,Last,All)")))
    except Exception as e:
        out("    REJECTED DataCollectionMeasurements:", errtext(e))
    try:
        qcs = net.QueueCounters.GetAll()
        out("    QueueCounters count:", len(qcs))
        if qcs:
            targets.append(("QueueCounter", qcs[0], net.QueueCounters, "queue",
                            ("QLen(Current,Last)", "QLenMax(Avg,Avg)")))
    except Exception as e:
        out("    REJECTED QueueCounters:", errtext(e))
    try:
        npm = net.VehicleNetworkPerformanceMeasurement
        targets.append(("VehicleNetworkPerformanceMeasurement", npm, None, "network_performance",
                        ("DelayAvg(Current,Last,All)", "SpeedAvg(Current,Last,All)", "VehAct(Current,Last,All)")))
    except Exception as e:
        out("    REJECTED VehicleNetworkPerformanceMeasurement:", errtext(e))
    # Parking - first a parking lot that was already in the network (one with parking spaces), otherwise the one the probe created
    for name, cname, kind, reads in (
            ("ParkingLot", "ParkingLots", "parking_lot",
             ("OccupRate(Current,Last,All)", "NumVehsPark(Current,Last,All)", "ParkDurAvg(Current,Last,All)",
              "DemLatent(Current,Last)")),
            ("ParkingLotGroup", "ParkLotGrps", "parking_lot_group",
             ("OccupRate(Current,Last,All)", "NumVehsEnter(Current,Last,All)")),
            ("VehicleRoutingDecisionParking", "VehicleRoutingDecisionsParking", "parking_decision",
             ("NumParkReq(Current,Last,All)", "ParkReqDeclShare(Current,Last,All)"))):
        try:
            coll = getattr(net, cname)
            items = coll.GetAll()
            out(f"    {cname} count:", len(items))
            if items:
                pick = items[0]
                if name == "ParkingLot":
                    pick = next((i for i in items if int(i.ParkSpcs.Count) > 0), pick)
                targets.append((name, pick, coll, kind, reads))
        except Exception as e:
            out(f"    REJECTED {cname}:", errtext(e))
    try:
        lots = [lot for lot in net.ParkingLots.GetAll() if int(lot.ParkSpcs.Count) > 0]
        out("    ParkingLot count with parking spaces:", len(lots))
        if lots:
            spc = lots[0].ParkSpcs.GetAll()[0]
            for r in ("OccupRate(Current,Last,All)", "NumVehsPark(Current,Last,All)"):
                attempt(f"ParkingSpace.AttValue({r!r})", spc.AttValue, r)
    except Exception as e:
        out("    REJECTED ParkingSpace results:", errtext(e))
    for name, obj, coll, kind, reads in targets:
        out(f"  [{name}] result measure IDs (registry result-measure prefix, or containing {RESULT_TOKENS}):")
        try:
            rows = attr_rows(coll) if coll is not None else []
            prefixes = registry.RESULT_ATTRS[kind]
            hits = [r for r in rows if any(str(r["ID"]).startswith(p) for p in prefixes)
                    or any(tok.lower() in str(r["ID"]).lower() for tok in RESULT_TOKENS)]
            print_rows(hits)
            missing = [p for p in prefixes if not any(str(r["ID"]).startswith(p) for r in rows)]
            out("      Registry result measures not found among the IDs:", missing or "none")
        except Exception as e:
            out("    REJECTED Attributes.GetAll():", errtext(e))
        for r in reads:
            attempt(f"{name}.AttValue({r!r})", obj.AttValue, r)


# ---------- 7f Meta and geometry attributes ----------

GEOM_TOKENS = ("Wkt", "Poly", "Point", "Coord", "Geo", "Length")


def probe_meta(v: Any, lk1: Any, conn: Any) -> None:
    # 2026: the attribute list exists only on containers (items and single objects have no Attributes)
    try:
        a = v.Net.Links.Attributes.GetAll()[0]
        ok_fields = {f: meta(a, f) for f in META_FIELDS if meta(a, f) is not None}
        out("    Fields readable from the meta item:", ok_fields)
        out("    Fields not readable:", [f for f in META_FIELDS if f not in ok_fields])
    except Exception as e:
        out("    REJECTED Link.Attributes.GetAll()[0]:", errtext(e))
    for label, obj in (("Link", lk1), ("Connector", conn)):
        if obj is None:
            out(f"  [{label}] no object")
            continue
        try:
            rows = attr_rows(v.Net.Links)   # links and connectors share one container
            out(f"  [{label}] {len(rows)} attributes; those whose ID or name contains {GEOM_TOKENS} (with Editable):")
            print_rows(rows_matching(rows, GEOM_TOKENS, in_names=True))
        except Exception as e:
            out(f"    REJECTED {label}.Attributes.GetAll(): {errtext(e)}")


# ---------- 9i ROT ----------

def rot_vissim_entries() -> list[str]:
    import pythoncom
    rot = pythoncom.GetRunningObjectTable(0)
    ctx = pythoncom.CreateBindCtx(0)
    enum = rot.EnumRunning()
    found: list[str] = []
    total = 0
    while True:
        items = enum.Next(1)
        if not items:
            break
        total += 1
        try:
            name = items[0].GetDisplayName(ctx, None)
        except Exception:
            name = "<no display name>"
        if "vissim" in str(name).lower():
            found.append(str(name))
    out(f"    ROT entries: {total}, containing Vissim: {found or 'none'}")
    return found


def probe_rot(progid: str, automation: bool) -> None:
    try:
        rot_vissim_entries()
    except Exception as e:
        out("    REJECTED ROT enumeration:", errtext(e))
    if not automation:
        out("    SKIPPED no --rot-automation (not launching with -automation to test GetActiveObject)")
        return
    exe = VISSIM_DEFAULTS["exe"]
    if not Path(exe).is_file():
        out("    REJECTED vissim.exe not found:", exe)
        return
    import win32com.client as wc
    before = vissim_pids()
    try:
        proc = subprocess.Popen([exe, "-automation"])
    except Exception as e:
        out("    REJECTED -automation launch:", errtext(e))
        return
    out(f"    -automation launched PID={proc.pid}: {exe} -automation (up to 90 s, GetActiveObject every 5 s)")
    t0 = time.perf_counter()
    attached = None
    while time.perf_counter() - t0 < 90:
        time.sleep(5)
        try:
            attached = wc.GetActiveObject(progid)   # only for the ROT test - not used as a connection path
            break
        except Exception as e:
            out(f"      {time.perf_counter() - t0:5.1f}s GetActiveObject failed: {errtext(e)}")
    if attached is None:
        out("    REJECTED GetActiveObject did not attach within 90 s")
    else:
        out(f"    OK   GetActiveObject succeeded after {time.perf_counter() - t0:.1f}s")
        try:
            n = int(attached.Net.Links.Count)
            out("      Attached instance Net.Links.Count =", n,
                "(0 means the -automation instance, >0 means the instance this script started via Dispatch)")
        except Exception as e:
            out("      Failed to read Links.Count of the attached instance:", errtext(e))
        try:
            rot_vissim_entries()
        except Exception as e:
            out("    REJECTED ROT enumeration:", errtext(e))
        attached = None
    out("    New Vissim PIDs:", sorted(vissim_pids() - before))
    try:
        proc.terminate()
        proc.wait(timeout=30)
        out("    -automation process terminated")
    except Exception as e:
        out("    REJECTED terminate:", errtext(e))


# ---------- Scenario, signal and time interval checks (--scenario / --signal / --signal-simple / --intervals) ----------
#
# The original official examples (Examples Training) are neither opened nor written. The probe takes a folder (or an
# .inpx) that the user copied under runs/mcp/scratch/, recreates a run-only copy runs/mcp/scratch/<timestamp>/<name>/ from it, and opens and saves only that copy.

SCRATCH = ROOT / "runs" / "mcp" / "scratch"
EXAMPLES_MARK = "examples training"

_STEP: dict[str, Any] = {"label": "", "t0": 0.0, "pids": [], "killed": False}


def step(label: str) -> None:
    """The current step, watched by the watchdog thread (if it hangs for long, only the Vissim this script started is ended)."""
    _STEP["label"], _STEP["t0"] = label, time.perf_counter()


def start_watchdog(timeout_s: float) -> None:
    """If a step does not return within timeout_s (modal window suspected), taskkill only the Vissim PID this script started."""
    import threading

    def loop():
        while True:
            time.sleep(5)
            if _STEP["killed"] or not _STEP["label"]:
                continue
            if time.perf_counter() - _STEP["t0"] > timeout_s and _STEP["pids"]:
                out(f"    [watchdog] '{_STEP['label']}' did not return for over {timeout_s:.0f} s - terminating the Vissim it started {_STEP['pids']}")
                for pid in _STEP["pids"]:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                _STEP["killed"] = True
                return
    threading.Thread(target=loop, daemon=True).start()


def require_scratch(src: Path) -> Path:
    """Accept only copies under runs/mcp/scratch/ (paths of the original official examples are rejected)."""
    src = src.resolve()
    if EXAMPLES_MARK in str(src).lower():
        raise RuntimeError(f"Paths of the original official examples are not accepted - provide a copy under runs/mcp/scratch/: {src}")
    try:
        src.relative_to(SCRATCH.resolve())
    except ValueError:
        raise RuntimeError(f"Only copies under runs/mcp/scratch/ are accepted: {src}")
    return src


def scratch_copy(src: Path, label: str, stamp: str) -> Path:
    """Copy src (a folder) whole to runs/mcp/scratch/<stamp>/<label>/. Rejected if it is an original example folder or outside scratch."""
    import shutil
    src = require_scratch(src)
    dst = SCRATCH / stamp / label
    if dst.exists():
        raise RuntimeError(f"Already exists: {dst}")
    shutil.copytree(src, dst)
    return dst


def snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    """All files under the folder → (size, modification time ns)."""
    snap: dict[str, tuple[int, int]] = {}
    for p in folder.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(folder))] = (st.st_size, st.st_mtime_ns)
    return snap


def print_diff(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]], indent: str = "      ") -> None:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    if not (added or removed or changed):
        out(indent + "no file changes")
    for k in added[:40]:
        out(f"{indent}+ {k} ({after[k][0]} B)")
    for k in removed[:40]:
        out(f"{indent}- {k}")
    for k in changed[:40]:
        out(f"{indent}~ {k} ({before[k][0]} → {after[k][0]} B)")
    outside = [k for k in added + removed + changed
               if not (k.lower().endswith(".vissimpdb") or k.lower().startswith(("modifications", "scenarios")))]
    out(f"{indent}Changes outside .vissimpdb, Modifications\\ and Scenarios\\: {outside or 'none'}")


def db_rows(pdb: Path) -> None:
    """.vissimpdb (SQLite) read-only - rows of PROJECT, SCENARIO and MODIFICATION, and the LOGENTRY count."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{pdb.as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
            out("      tables:", tables)
            for t in ("PROJECT", "SCENARIO", "MODIFICATION"):
                if t in tables:
                    cols = [r[1] for r in con.execute(f"pragma table_info({t})")]
                    out(f"      {t} {cols}")
                    for r in con.execute(f"select * from {t}"):
                        out(f"        {r}")
            if "LOGENTRY" in tables:
                rows = list(con.execute("select * from LOGENTRY order by NO"))
                out(f"      LOGENTRY {len(rows)} rows", ("last: " + repr(rows[-1])) if rows else "")
        finally:
            con.close()
    except Exception as e:
        out("      REJECTED reading .vissimpdb:", errtext(e))


def open_state(v: Any, label: str) -> tuple[str, str]:
    """One line: open file (WorkingFolder+InputFile), UnderScenMngm and counts."""
    try:
        wf, inp = str(v.AttValue("WorkingFolder")), str(v.AttValue("InputFile"))
    except Exception as e:
        out(f"    [{label}] REJECTED WorkingFolder/InputFile: {errtext(e)}")
        return "", ""
    parts = [f"WorkingFolder={wf!r}", f"InputFile={inp!r}"]
    try:
        parts.append(f"UnderScenMngm={v.Net.NetPara.AttValue('UnderScenMngm')!r}")
    except Exception as e:
        parts.append(f"UnderScenMngm rejected({errtext(e)})")
    for cname in ("Links", "SignalHeads", "SignalControllers", "VehicleInputs"):
        try:
            parts.append(f"{cname}={int(getattr(v.Net, cname).Count)}")
        except Exception as e:
            parts.append(f"{cname} rejected({errtext(e)})")
    try:
        vi = first_item(v.Net.VehicleInputs)
        parts.append(f"VI{vi.AttValue('No')}.Volume(1)={vi.AttValue('Volume(1)')!r}")
    except Exception:
        pass
    out(f"    [{label}] " + " | ".join(parts))
    return wf, inp


def bump_volume(v: Any, value: float) -> None:
    """Set Volume(1) of the first vehicle input to value (a small attribute change used to confirm saving)."""
    try:
        vi = first_item(v.Net.VehicleInputs)
        before = vi.AttValue("Volume(1)")
        vi.SetAttValue("Volume(1)", value)
        out(f"    OK   VI{vi.AttValue('No')}.Volume(1) {before!r} → {vi.AttValue('Volume(1)')!r}")
    except Exception as e:
        out("    REJECTED writing Volume(1):", errtext(e))


def scenario_numbers(pdb: Path, table: str) -> list[int]:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{pdb.as_posix()}?mode=ro", uri=True)
        try:
            return [int(r[0]) for r in con.execute(f"select NO from {table} order by NO")]
        finally:
            con.close()
    except Exception:
        return []


class _LazySM:
    """v.ScenarioManagement is only available in scenario management mode - fetch it anew on every call."""

    def __init__(self, v: Any):
        self._v = v

    def __getattr__(self, name: str) -> Any:
        return getattr(self._v.ScenarioManagement, name)


def probe_scenario(v: Any, folder: Path, plain_src: Path | None, stamp: str) -> None:
    attempt("v.ScenarioManagement (empty Vissim, before LoadProject)", lambda: v.ScenarioManagement)
    sm = _LazySM(v)
    pdbs = sorted(folder.glob("*.vissimpdb"))
    if not pdbs:
        out("    REJECTED: no .vissimpdb in the copy folder:", folder)
        return
    pdb = pdbs[0]
    out("  [S1] Project copy:", pdb)
    db_rows(pdb)
    scens = scenario_numbers(pdb, "SCENARIO")
    mods = scenario_numbers(pdb, "MODIFICATION")
    snap0 = snapshot(folder)
    step("LoadProject")
    attempt(f"LoadProject({str(pdb)!r})", v.LoadProject, str(pdb))
    open_state(v, "after LoadProject")
    step("LoadBaseNet")
    attempt("ScenarioManagement.LoadBaseNet()", sm.LoadBaseNet)
    open_state(v, "after LoadBaseNet")
    for n in scens:
        step(f"LoadScenario({n})")
        ok, _ = attempt(f"ScenarioManagement.LoadScenario({n}) (integer)", sm.LoadScenario, n)
        open_state(v, f"after LoadScenario({n})")
    for m in mods:
        step(f"LoadModification({m})")
        attempt(f"ScenarioManagement.LoadModification({m}) (integer)", sm.LoadModification, m)
        open_state(v, f"after LoadModification({m})")
    out("    Project folder changes after opening only:")
    snap1 = snapshot(folder)
    print_diff(snap0, snap1)

    out("  [S2] Files changed per save method (after changing Volume(1) of the first vehicle input)")
    if not scens:
        out("    SKIPPED: no SCENARIO rows")
        return
    s1 = scens[0]
    step("LoadScenario s1")
    attempt(f"LoadScenario({s1})", sm.LoadScenario, s1)
    # SaveScenario
    bump_volume(v, 811)
    before = snapshot(folder)
    step("SaveScenario")
    attempt("ScenarioManagement.SaveScenario()", sm.SaveScenario)
    print_diff(before, snapshot(folder))
    open_state(v, "after SaveScenario")
    db_rows(pdb)
    # SaveScenarioAs
    bump_volume(v, 822)
    before = snapshot(folder)
    step("SaveScenarioAs")
    ok, new_id = attempt("ScenarioManagement.SaveScenarioAs('probe scenario') (return value)", sm.SaveScenarioAs, "probe scenario")
    out(f"      return value type: {type(new_id).__name__} {new_id!r}")
    print_diff(before, snapshot(folder))
    open_state(v, "after SaveScenarioAs")
    db_rows(pdb)
    # SaveInMultipleScenarios - type library: Scenarios is a BSTR. Comma-separated string first, integer list if rejected
    targets = scens[:2] if len(scens) >= 2 else scens
    bump_volume(v, 833)
    before = snapshot(folder)
    step("SaveInMultipleScenarios")
    arg = ",".join(str(n) for n in targets)
    ok, _ = attempt(f"ScenarioManagement.SaveInMultipleScenarios({arg!r}) (comma-separated string)", sm.SaveInMultipleScenarios, arg)
    if not ok:
        attempt(f"ScenarioManagement.SaveInMultipleScenarios({targets!r}) (integer list)", sm.SaveInMultipleScenarios, targets)
    print_diff(before, snapshot(folder))
    open_state(v, "after SaveInMultipleScenarios")
    db_rows(pdb)
    # SaveModification
    mods = scenario_numbers(pdb, "MODIFICATION")
    if mods:
        step("LoadModification for save")
        attempt(f"LoadModification({mods[0]})", sm.LoadModification, mods[0])
        bump_volume(v, 844)
        before = snapshot(folder)
        step("SaveModification")
        attempt("ScenarioManagement.SaveModification()", sm.SaveModification)
        print_diff(before, snapshot(folder))
        open_state(v, "after SaveModification")
        db_rows(pdb)
    out("    Volume(1) per scenario after saving (which save left its change where):")
    attempt("LoadBaseNet()", sm.LoadBaseNet)
    open_state(v, "base network")
    for n in scenario_numbers(pdb, "SCENARIO"):
        step(f"reload scenario {n}")
        attempt(f"LoadScenario({n})", sm.LoadScenario, n)
        open_state(v, f"scenario {n}")
    out("    Whole-S2 project folder changes (before opening → now):")
    print_diff(snap0, snapshot(folder))

    out("  [S3] SaveNetAs(copy path) from a scenario project")
    attempt(f"LoadScenario({s1})", sm.LoadScenario, s1)
    open_state(v, "before SaveNetAs")
    target = SCRATCH / stamp / "scen_saveas" / "scen_saveas.inpx"
    target.parent.mkdir(parents=True, exist_ok=True)
    before = snapshot(folder)
    step("SaveNetAs scenario")
    attempt(f"SaveNetAs({str(target)!r}, False)", v.SaveNetAs, str(target), False)
    open_state(v, "after SaveNetAs")
    out("    Project folder changes:")
    print_diff(before, snapshot(folder))
    out("    Saved copy folder:", sorted(snapshot(target.parent)))
    step("LoadNet saved copy")
    attempt(f"LoadNet({target.name}) (the saved copy as a plain network)", v.LoadNet, str(target), False)
    open_state(v, "after LoadNet of the copy")
    attempt("v.ScenarioManagement (after LoadNet of the copy)", lambda: v.ScenarioManagement)
    step("reopen project")
    attempt(f"LoadProject({pdb.name!r}) again", v.LoadProject, str(pdb))
    attempt(f"LoadScenario({s1}) again", sm.LoadScenario, s1)
    open_state(v, "after reopening")

    if plain_src is None:
        out("  [S4]·[S5] SKIPPED: no plain .inpx")
        return
    out(f"  [S4] PlaceUnderScenarioManagement - plain .inpx copy: {plain_src.name}")
    place_dir = SCRATCH / stamp / "place"
    place_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    for p in plain_src.parent.glob(plain_src.stem + ".*"):
        if p.suffix.lower() in (".inpx", ".layx"):
            shutil.copy2(p, place_dir / p.name)
    plain = place_dir / plain_src.name
    step("LoadNet plain")
    attempt(f"LoadNet({plain.name!r})", v.LoadNet, str(plain), False)
    open_state(v, "plain .inpx")
    before = snapshot(place_dir)
    step("PlaceUnderScenarioManagement")
    attempt("PlaceUnderScenarioManagement('probe project', 'probe scenario1')", v.PlaceUnderScenarioManagement,
            "probe project", "probe scenario1")
    open_state(v, "after PlaceUnderScenarioManagement")
    print_diff(before, snapshot(place_dir))
    for p in sorted(place_dir.rglob("*")):
        out("      ", ("[folder] " if p.is_dir() else "") + str(p.relative_to(place_dir)))
    for pdb2 in place_dir.glob("*.vissimpdb"):
        db_rows(pdb2)

    probe_transfer(v, plain_src, stamp)


def trax_outline(path: Path, head_chars: int = 1500) -> None:
    """.trax (XML) element outline: top-level object group -> added/deleted/modified -> tag and attributes of the first item (short) + the raw beginning of the file."""
    import xml.etree.ElementTree as ET
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        out("      REJECTED reading .trax:", errtext(e))
        return
    out(f"      {path.name}: {len(raw)} chars")
    try:
        root = ET.fromstring(raw.encode("utf-8"))
        out(f"      <{root.tag} {dict(root.attrib)}>")
        for grp in root:
            for kind in grp:
                items = list(kind)
                first = items[0] if items else None
                desc = ""
                if first is not None:
                    attrs = dict(first.attrib)
                    s = " ".join(f'{k}="{val}"' for k, val in list(attrs.items())[:8])
                    kids = [c.tag for c in first][:6]
                    desc = f" first item <{first.tag} {s}> children {kids}"
                out(f"        {grp.tag}/{kind.tag}: {len(items)} items{desc}")
    except Exception as e:
        out("      XML parse failed:", errtext(e))
    for line in raw[:head_chars].splitlines():
        out("      | " + line)


def link_counts(v: Any) -> str:
    try:
        allk = v.Net.Links.GetAll()
        n_conn = sum(1 for lk in allk if bool(lk.AttValue("IsConn")))
        return f"links {len(allk) - n_conn} · connectors {n_conn}"
    except Exception as e:
        return f"count rejected({errtext(e)})"


def probe_transfer(v: Any, plain_src: Path, stamp: str) -> None:
    import shutil
    out("  [S5] Model transfer file (.trax) - two variants of a plain .inpx copy")
    tdir = SCRATCH / stamp / "transfer"
    tdir.mkdir(parents=True, exist_ok=True)
    src = tdir / ("src" + plain_src.suffix)
    shutil.copy2(plain_src, src)
    base, var_a, var_b = tdir / "base.inpx", tdir / "varA.inpx", tdir / "varB.inpx"
    step("transfer base")
    attempt("LoadNet(src)", v.LoadNet, str(src), False)
    try:
        if all(len(lk.LinkPolyPts.GetAll()) < 3 for lk in v.Net.Links.GetAll() if not bool(lk.AttValue("IsConn"))):
            used0 = {int(lk.AttValue("No")) for lk in v.Net.Links.GetAll()}
            n3 = max(used0) + 100
            vc.add_link(v, n3, [(6000.0, 6000.0, 0.0), (6050.0, 6020.0, 0.0), (6100.0, 6000.0, 0.0)], [3.5],
                        "probe 3pt")
            out(f"    no link has a middle polyline point, so added 3-point link {n3} to base (variant A moves its middle point)")
    except Exception as e:
        out("    REJECTED adding 3-point link:", errtext(e))
    attempt("SaveNetAs(base.inpx)", v.SaveNetAs, str(base), False)
    lk1 = next(lk for lk in v.Net.Links.GetAll() if not bool(lk.AttValue("IsConn")))
    lk1_no = int(lk1.AttValue("No"))
    out("    Base:", link_counts(v), "| test link", lk1_no, "Name", repr(lk1.AttValue("Name")))
    # Variant A: one link attribute + polyline point move + two new links and one connector
    step("variant A")
    attempt(f"Link{lk1_no}.SetAttValue('Name', 'probe A')", lk1.SetAttValue, "Name", "probe A")
    try:
        # move one middle point (excluding both ends) of the link with the most polyline points
        cand = max((lk for lk in v.Net.Links.GetAll() if not bool(lk.AttValue("IsConn"))),
                   key=lambda lk: len(lk.LinkPolyPts.GetAll()))
        pts = cand.LinkPolyPts.GetAll()
        out(f"    link with the most polyline points {cand.AttValue('No')}: {len(pts)} points")
        if len(pts) >= 3:
            x = float(pts[1].AttValue("X"))
            attempt(f"Link{cand.AttValue('No')}.LinkPolyPts[1].SetAttValue('X', {x:.3f}+0.5)", pts[1].SetAttValue,
                    "X", x + 0.5)
        else:
            out("    SKIPPED: no link with a middle polyline point")
    except Exception as e:
        out("    REJECTED LinkPolyPts:", errtext(e))
    used = {int(lk.AttValue("No")) for lk in v.Net.Links.GetAll()}
    na = max(used) + 1
    try:
        _, la = vc.add_link(v, na, [(5000.0, 5000.0, 0.0), (5100.0, 5000.0, 0.0)], [3.5], "probe new A")
        _, lb = vc.add_link(v, na + 1, [(5101.0, 5000.0, 0.0), (5200.0, 5000.0, 0.0)], [3.5], "probe new B")
        vc.add_connector(v, na + 2, na, [1], float(la or 100.0), na + 1, [1], 0.0, "probe conn")
        out(f"    OK   new links {na}·{na + 1} + connector {na + 2}")
    except Exception as e:
        out("    REJECTED new links/connector:", errtext(e))
    attempt("SaveNetAs(varA.inpx)", v.SaveNetAs, str(var_a), False)
    out("    Variant A:", link_counts(v))
    # Variant B: reference point (writable because the network was opened from a file)
    step("variant B")
    attempt("LoadNet(base.inpx)", v.LoadNet, str(base), False)
    np_ = v.Net.NetPara
    for attr in ("RefPointMapX", "RefPointMapY", "RefPointNetX", "RefPointNetY", "UnitSpeed", "UnitLenLong"):
        attempt(f"NetPara.AttValue({attr!r})", np_.AttValue, attr)
    try:
        mx = float(np_.AttValue("RefPointMapX"))
        attempt("NetPara.SetAttValue('RefPointMapX', +100)", np_.SetAttValue, "RefPointMapX", mx + 100.0)
    except Exception as e:
        out("    REJECTED RefPointMapX:", errtext(e))
    attempt("SaveNetAs(varB.inpx)", v.SaveNetAs, str(var_b), False)
    # generate
    step("GenerateModelTransferFile")
    attempt("LoadNet(base.inpx)", v.LoadNet, str(base), False)
    open_state(v, "base opened")
    t_a = tdir / "A_fromOpen.trax"
    attempt(f"GenerateModelTransferFile(varA, {t_a.name}, False, False)", v.GenerateModelTransferFile,
            str(var_a), str(t_a), False, False)
    open_state(v, "after GenerateModelTransferFile")
    t_ar = tdir / "A_fromOpen_reverse.trax"
    attempt(f"GenerateModelTransferFile(varA, {t_ar.name}, True, False)", v.GenerateModelTransferFile,
            str(var_a), str(t_ar), True, False)
    step("GenerateModelTransferFileBetweenFiles")
    t_b = tdir / "B_between.trax"
    attempt(f"GenerateModelTransferFileBetweenFiles(base, varB, {t_b.name}, False)",
            v.GenerateModelTransferFileBetweenFiles, str(base), str(var_b), str(t_b), False)
    t_a2 = tdir / "A_between.trax"
    attempt(f"GenerateModelTransferFileBetweenFiles(base, varA, {t_a2.name}, False)",
            v.GenerateModelTransferFileBetweenFiles, str(base), str(var_a), str(t_a2), False)
    open_state(v, "after BetweenFiles")
    for t in (t_a, t_ar, t_a2, t_b):
        if t.is_file():
            trax_outline(t)
        else:
            out(f"      {t.name} missing")
    # apply
    step("ApplyModelTransferFile")
    for t, what in ((t_a, "A_fromOpen(Reverse=False)"), (t_ar, "A_fromOpen_reverse(Reverse=True)"),
                    (t_a2, "A_between"), (t_b, "B_between")):
        if not t.is_file():
            continue
        attempt("LoadNet(base.inpx)", v.LoadNet, str(base), False)
        out(f"    before apply ({what}):", link_counts(v), "| RefPointMapX", repr(v.Net.NetPara.AttValue("RefPointMapX")))
        attempt(f"ApplyModelTransferFile({t.name})", v.ApplyModelTransferFile, str(t))
        try:
            nm = v.Net.Links.ItemByKey(lk1_no).AttValue("Name")
        except Exception as e:
            nm = f"rejected({errtext(e)})"
        out(f"    after apply ({what}):", link_counts(v), f"| Link{lk1_no}.Name {nm!r}",
            "| RefPointMapX", repr(v.Net.NetPara.AttValue("RefPointMapX")))
        open_state(v, f"after Apply {what}")
    out("    transfer folder:", sorted(snapshot(tdir)))


# ---- Signals ----

def sim_setup(v: Any, period: int) -> None:
    sim = v.Simulation
    for attr, val in (("SimPeriod", period), ("RandSeed", 42), ("UseMaxSimSpeed", True), ("SimBreakAt", 0)):
        try:
            sim.SetAttValue(attr, val)
        except Exception as e:
            out(f"    REJECTED Simulation.SetAttValue({attr!r}, {val!r}): {errtext(e)}")
    try:
        v.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", 1)
    except Exception as e:
        out("    REJECTED QuickMode:", errtext(e))


def sig_timeline(v: Any, groups: list[tuple[str, Any]], t_end: int, label: str) -> dict[str, list[tuple[int, str]]]:
    """Pause at every SimBreakAt = 1..t_end, read SigState, and reduce it per group to a list of 'seconds at which the state changed' for printing. Stop() at the end."""
    sim = v.Simulation
    sim_setup(v, t_end + 10)
    seq: dict[str, list[tuple[int, str]]] = {g: [] for g, _ in groups}
    step(f"timeline {label}")
    t0 = time.perf_counter()
    try:
        for t in range(1, t_end + 1):
            sim.SetAttValue("SimBreakAt", t)
            sim.RunContinuous()
            for g, sg in groups:
                st = str(sg.AttValue("SigState"))
                if not seq[g] or seq[g][-1][1] != st:
                    seq[g].append((t, st))
    except Exception as e:
        out(f"    REJECTED timeline {label} (t={t}): {errtext(e)}")
    try:
        sim.Stop()
    except Exception as e:
        out("    REJECTED Simulation.Stop():", errtext(e))
    out(f"    timeline [{label}] SigState read at SimBreakAt=t, changed seconds only ({time.perf_counter() - t0:.1f}s):")
    for g, s in seq.items():
        out(f"      SG {g}: " + " ".join(f"{st}@{t}" for t, st in s))
    return seq


def decode_supply(val: Any) -> bytes | None:
    import base64
    if val is None:
        return None
    s = str(val)
    if not s:
        return b""
    if s.lstrip().startswith("<"):
        return s.encode("utf-8")
    try:
        return base64.b64decode(s)
    except Exception:
        return None


def prog_summary(data: bytes | None) -> list[str]:
    """checkSum, signalsequence and <prog> of the IntSupplyData XML, and per-group cmd/fixedstate, one line each."""
    import re
    if data is None:
        return ["(cannot parse)"]
    if not data:
        return ["(empty value)"]
    txt = data.decode("utf-8", errors="replace")
    lines = []
    m = re.search(r'checkSum="([^"]*)"', txt)
    lines.append(f"checkSum={m.group(1) if m else None} | length {len(data)} B | starts with {txt[:60]!r}")
    lines.append("signalsequences: " + ", ".join(re.findall(r'<signalsequence id="(\d+)"', txt)))
    lines.append("sgs (definitions): " + ", ".join(re.findall(r'<sg id="(\d+)"', txt)))
    for pm in re.finditer(r"<prog ([^>]*)>(.*?)</prog>", txt, re.S):
        lines.append(f"<prog {pm.group(1)}>")
        for sm in re.finditer(r'<sg sg_id="(\d+)"[^>]*>(.*?)</sg>', pm.group(2), re.S):
            cmds = re.findall(r'<cmd display="(\d+)" begin="(\d+)"', sm.group(2))
            fixed = re.findall(r'<fixedstate display="(\d+)" duration="(\d+)"', sm.group(2))
            lines.append(f"  sg {sm.group(1)}: cmds {cmds} fixed {fixed}")
    if "<progs />" in txt or "<progs/>" in txt:
        lines.append("<progs /> (no programs)")
    return lines


def show_supply(sc: Any, label: str) -> bytes | None:
    try:
        raw = sc.AttValue("IntSupplyData")
    except Exception as e:
        out(f"    REJECTED reading {label} IntSupplyData: {errtext(e)}")
        return None
    data = decode_supply(raw)
    out(f"    {label} IntSupplyData (AttValue type {type(raw).__name__}, first 12 chars {str(raw)[:12]!r}):")
    for line in prog_summary(data):
        out("      " + line)
    return data


def sc_attrs(sc: Any, label: str) -> None:
    got = {}
    for attr in ("No", "Type", "CycTm", "CycTmIsVar", "Offset", "ProgNo", "SupplyFile1", "Active"):
        try:
            got[attr] = sc.AttValue(attr)
        except Exception as e:
            got[attr] = f"rejected({errtext(e)})"
    out(f"    {label} SC attributes: {got}")
    for sg in sc.SGs.GetAll():
        g = {}
        for attr in ("No", "EndRed", "EndGreen", "Amber", "RedAmber", "MinGreen", "ContrByCOM", "Type"):
            try:
                g[attr] = sg.AttValue(attr)
            except Exception as e:
                g[attr] = f"rejected({errtext(e)})"
        out(f"      SG {g}")


def sg_groups(sc: Any) -> list[tuple[str, Any]]:
    scno = int(sc.AttValue("No"))
    return [(f"{scno}-{int(sg.AttValue('No'))}", sg) for sg in sc.SGs.GetAll()]


def changed_lines(a: bytes | None, b: bytes | None) -> None:
    import difflib
    if a is None or b is None:
        out("      (cannot compare)")
        return
    la = a.decode("utf-8", errors="replace").splitlines()
    lb = b.decode("utf-8", errors="replace").splitlines()
    diff = [d for d in difflib.unified_diff(la, lb, lineterm="", n=0) if not d.startswith(("---", "+++", "@@"))]
    out(f"      IntSupplyData: {len(diff)} changed lines" + (":" if diff else " (bytes identical)" if a == b else ""))
    for d in diff[:20]:
        out("        " + d.strip()[:200])


def probe_signal(v: Any, inpx: Path, stamp: str) -> None:
    folder = scratch_copy(inpx.parent, "signal", stamp)
    net = folder / inpx.name
    out("    Copy:", net)

    def reload(why: str) -> None:
        step(f"LoadNet {why}")
        vc.load(v, str(net))
        out(f"    LoadNet(copy) - {why}")

    reload("base")
    sc = first_item(v.Net.SignalControllers)
    sc_attrs(sc, "base")
    base = show_supply(sc, "base")
    try:
        heads = v.Net.SignalHeads.GetAll()
        for h in heads[:2]:
            out(f"    SignalHead {h.AttValue('No')}: SG={h.AttValue('SG')!r} Lane={h.AttValue('Lane')!r} Pos={h.AttValue('Pos')!r}")
        rows = attr_rows(v.Net.SignalHeads)
        out("    SignalHeads attributes related to SG and Lane:")
        print_rows(rows_matching(rows, ("SG", "Lane")))
    except Exception as e:
        out("    REJECTED SignalHeads:", errtext(e))
    out("  [G-e] Base timeline (cmd vs SigState)")
    groups = sg_groups(sc)
    sig_timeline(v, groups, 65, "base")

    out("  [G-b] FIXEDTIME: write SG attributes EndRed, EndGreen, Amber, RedAmber")
    reload("G-b")
    sc = first_item(v.Net.SignalControllers)
    sg = sc.SGs.GetAll()[0]
    sgno = int(sg.AttValue("No"))
    for attr, val in (("EndRed", 5), ("EndGreen", 19), ("Amber", 4), ("RedAmber", 2)):
        attempt(f"SG{sgno}.SetAttValue({attr!r}, {val})", sg.SetAttValue, attr, val)
        attempt(f"SG{sgno}.AttValue({attr!r}) again", sg.AttValue, attr)
    after = show_supply(sc, "after G-b")
    changed_lines(base, after)
    sig_timeline(v, sg_groups(sc)[:1], 65, "after G-b")

    out("  [G-c] FIXEDTIME: write SC attributes CycTm, Offset, ProgNo")
    reload("G-c")
    sc = first_item(v.Net.SignalControllers)
    for attr, val in (("CycTm", 70), ("Offset", 10)):
        attempt(f"SC.SetAttValue({attr!r}, {val})", sc.SetAttValue, attr, val)
        attempt(f"SC.AttValue({attr!r}) again", sc.AttValue, attr)
    after = show_supply(sc, "after G-c")
    changed_lines(base, after)
    sig_timeline(v, sg_groups(sc)[:1], 80, "after G-c (CycTm 70, Offset 10)")
    reload("G-c ProgNo")
    sc = first_item(v.Net.SignalControllers)
    attempt("SC.SetAttValue('ProgNo', 2) (the example has no program 2)", sc.SetAttValue, "ProgNo", 2)
    attempt("SC.AttValue('ProgNo') again", sc.AttValue, "ProgNo")
    sig_timeline(v, sg_groups(sc)[:1], 20, "after G-c ProgNo=2")

    out("  [G-d] Write IntSupplyData with only one cmd begin changed (checkSum untouched)")
    reload("G-d")
    sc = first_item(v.Net.SignalControllers)
    raw0 = sc.AttValue("IntSupplyData")
    data0 = decode_supply(raw0)
    if data0:
        import base64
        p = data0.find(b"<progs>")
        g = data0.find(f'<sg sg_id="{sgno}"'.encode(), p)
        c = data0.find(b'<cmd display="1" begin="', g)
        if p < 0 or g < 0 or c < 0:
            out("    SKIPPED: could not find the display=1 cmd of the first group in the program")
        else:
            i = c + len(b'<cmd display="1" begin="')
            j = data0.find(b'"', i)
            old = int(data0[i:j])
            new_bytes = data0[:i] + str(old + 5000).encode() + data0[j:]
            out(f"    SG{sgno} display=1 cmd begin {old} → {old + 5000} (other bytes identical, length {len(data0)} → {len(new_bytes)})")
            enc = base64.b64encode(new_bytes).decode("ascii")
            ok, _ = attempt("SC.SetAttValue('IntSupplyData', base64)", sc.SetAttValue, "IntSupplyData", enc)
            back = show_supply(sc, "after G-d")
            changed_lines(data0, back)
            if back is not None:
                out("      Value read back == value written (bytes):", back == new_bytes)
            sig_timeline(v, sg_groups(sc)[:1], 65, "after G-d")
    else:
        out("    SKIPPED: IntSupplyData is empty or cannot be parsed")

    out("  [G-a·f·g] New signal controller, groups and signal head; insert a first program; remove them")
    reload("G-a")
    scs = v.Net.SignalControllers
    heads = v.Net.SignalHeads
    ok, nsc = attempt("SignalControllers.AddSignalController(0)", scs.AddSignalController, 0)
    if ok and nsc is not None:
        nsc_no = int(nsc.AttValue("No"))
        out("    New SC No:", nsc_no)
        sc_attrs(nsc, "new SC (before groups)")
        empty = show_supply(nsc, "new SC (before groups)")
        new_sgs = []
        for _ in range(2):
            ok, nsg = attempt(f"SC{nsc_no}.SGs.AddSignalGroup(0)", nsc.SGs.AddSignalGroup, 0)
            if ok and nsg is not None:
                new_sgs.append(int(nsg.AttValue("No")))
        out("    New SG numbers:", new_sgs)
        sc_attrs(nsc, "new SC (after groups)")
        with_sgs = show_supply(nsc, "new SC (after groups)")
        changed_lines(empty, with_sgs)
        if with_sgs:
            out("    New SC IntSupplyData raw text (first 3000 chars):")
            for line in with_sgs.decode("utf-8", errors="replace")[:3000].splitlines():
                out("      | " + line)
        # signal head
        try:
            lk = next(lk for lk in v.Net.Links.GetAll() if not bool(lk.AttValue("IsConn")))
            lane = lk.Lanes.ItemByKey(1)
            pos = round(min(10.0, float(lk.AttValue("Length2D")) / 2), 2)
            ok, nh = attempt(f"SignalHeads.AddSignalHead(0, Link{lk.AttValue('No')}.Lanes[1], {pos})",
                             heads.AddSignalHead, 0, lane, pos)
        except Exception as e:
            out("    REJECTED preparing signal head:", errtext(e))
            ok, nh = False, None
        nh_no = None
        if ok and nh is not None:
            nh_no = int(nh.AttValue("No"))
            attempt(f"new SignalHead {nh_no}.AttValue('SG') (right after creation)", nh.AttValue, "SG")
            import re
            try:
                ref = str(first_item(heads).AttValue("SG"))
                m = re.fullmatch(r"(\d+)(\D+)(\d+)", ref)
            except Exception:
                ref, m = None, None
            out(f"    SG value format of an existing signal head: {ref!r}")
            if m and new_sgs:
                val = f"{nsc_no}{m.group(2)}{new_sgs[0]}"
                attempt(f"new SignalHead.SetAttValue('SG', {val!r}) (in the format that was read)", nh.SetAttValue, "SG", val)
                attempt("new SignalHead.AttValue('SG') again", nh.AttValue, "SG")
            else:
                out("    SKIPPED: SG value format could not be read, so nothing written")
        # Insert a first program - if the new SC's IntSupplyData is empty, read it again after save and reopen; if still empty,
        # try the example SC's document (only the sc id, group definitions, <progs> and <stages> changed to the new numbers)
        if not with_sgs and new_sgs:
            saved = SCRATCH / stamp / "signal_saved" / "new_sc.inpx"
            saved.parent.mkdir(parents=True, exist_ok=True)
            step("SaveNetAs new SC")
            attempt(f"SaveNetAs({saved.name}) (read again after saving the new SC)", v.SaveNetAs, str(saved), False)
            attempt(f"LoadNet({saved.name})", v.LoadNet, str(saved), False)
            scs, heads = v.Net.SignalControllers, v.Net.SignalHeads
            nsc = scs.ItemByKey(nsc_no)
            with_sgs = show_supply(nsc, "new SC (after save and reopen)")
        if new_sgs:
            import re
            import base64
            nl = "\r\n"
            sg_xml = ""
            for k, gno in enumerate(new_sgs):
                g_begin, r_begin = 2000 + k * 20000, 15000 + k * 20000
                sg_xml += (f'{nl}        <sg sg_id="{gno}" signal_sequence="3">{nl}          <cmds>{nl}'
                           f'            <cmd display="3" begin="{g_begin}" />{nl}            <cmd display="1" begin="{r_begin}" />{nl}'
                           f'          </cmds>{nl}          <fixedstates>{nl}            <fixedstate display="2" duration="1000" />{nl}'
                           f'            <fixedstate display="4" duration="3000" />{nl}          </fixedstates>{nl}        </sg>')
            prog = (f'<progs>{nl}    <prog id="1" cycletime="60000" switchpoint="0" offset="0" intergreens="0" '
                    f'fitness="0.000000" vehicleCount="0" name="Signal program">{nl}      <sgs>{sg_xml}{nl}      </sgs>{nl}'
                    f'    </prog>{nl}  </progs>')
            new_txt = None
            if with_sgs and ("<progs />" in with_sgs.decode("utf-8") or "<progs/>" in with_sgs.decode("utf-8")):
                txt = with_sgs.decode("utf-8")
                new_txt = txt.replace("<progs />", prog, 1).replace("<progs/>", prog, 1)
                how = "one <prog> in the empty <progs /> of the new SC document"
            elif not with_sgs and base:
                txt = base.decode("utf-8")
                t1 = re.sub(r'(<sc [^>]*?\bid=")\d+(")', lambda m: f"{m.group(1)}{nsc_no}{m.group(2)}", txt, count=1)
                dm = re.search(r'(\r?\n    <sg id="\d+"[^>]*>.*?</sg>)', t1, re.S)
                defs = ""
                if dm:
                    for gno in new_sgs:
                        one = re.sub(r'<sg id="\d+" name="[^"]*"', f'<sg id="{gno}" name="probe {gno}"', dm.group(1), count=1)
                        defs += one
                    first_sgs = t1.index("<sgs>")
                    close = t1.index("</sgs>", first_sgs)
                    t1 = t1[:first_sgs] + "<sgs>" + defs + "\r\n  " + t1[close:]
                t1 = re.sub(r"<progs>.*?</progs>", lambda m: prog, t1, count=1, flags=re.S)
                t1 = re.sub(r"<stages>.*?</stages>", "<stages />", t1, count=1, flags=re.S)
                new_txt = t1
                how = "the whole example SC document with only the sc id, group definitions, <progs> and <stages> changed to the new numbers"
            if new_txt is None:
                out("    SKIPPED: could not build a document to insert")
            else:
                out(f"    Inserting: {how} - groups {new_sgs} green 2-15 s / 22-35 s, signal_sequence=3")
                for line in prog_summary(new_txt.encode("utf-8")):
                    out("      (value to write) " + line)
                ok, _ = attempt("new SC.SetAttValue('IntSupplyData', base64(...))", nsc.SetAttValue,
                                "IntSupplyData", base64.b64encode(new_txt.encode("utf-8")).decode("ascii"))
                back = show_supply(nsc, "after inserting program")
                if back is not None:
                    out("      Value read back == value written (bytes):", back == new_txt.encode("utf-8"))
                attempt("new SC.AttValue('ProgNo')", nsc.AttValue, "ProgNo")
                sc_attrs(nsc, "after inserting program")
                if ok:
                    sig_timeline(v, sg_groups(nsc), 40, "new SC")
        # remove
        try:
            n_sc, n_h = int(scs.Count), int(heads.Count)
            attempt(f"SignalControllers.RemoveSignalController(SC{nsc_no})", scs.RemoveSignalController, nsc)
            out(f"    SC count {n_sc} → {int(scs.Count)} | SignalHead count {n_h} → {int(heads.Count)}")
            if nh_no is not None:
                exists = None
                try:
                    exists = bool(heads.ItemKeyExists(nh_no))
                except Exception as e:
                    out("    REJECTED ItemKeyExists:", errtext(e))
                out(f"    New signal head {nh_no} still exists? {exists}")
                if exists:
                    attempt(f"signal head {nh_no}.AttValue('SG') (after removing the SC)", heads.ItemByKey(nh_no).AttValue, "SG")
        except Exception as e:
            out("    REJECTED RemoveSignalController:", errtext(e))

    out("  [Q9] Net.SimulationRuns - read the numbers after two full runs; numbers in result specifiers")
    reload("Q9")
    ev = v.Evaluation
    attempt("Evaluation.AttValue('KeepPrevResults')", ev.AttValue, "KeepPrevResults")
    attempt("Evaluation.SetAttValue('KeepPrevResults', 'KEEPALL')", ev.SetAttValue, "KeepPrevResults", "KEEPALL")
    attempt("Evaluation.SetAttValue('VehNetPerfCollectData', True)", ev.SetAttValue, "VehNetPerfCollectData", True)
    attempt("Evaluation.SetAttValue('VehTravTmsCollectData', True)", ev.SetAttValue, "VehTravTmsCollectData", True)
    if not v.Net.VehicleTravelTimeMeasurements.GetAll():
        try:
            lk = max((lk for lk in v.Net.Links.GetAll() if not bool(lk.AttValue("IsConn"))),
                     key=lambda lk: float(lk.AttValue("Length2D")))
            ln = float(lk.AttValue("Length2D"))
            attempt(f"AddVehicleTravelTimeMeasurement(0, Link{lk.AttValue('No')}, 5, same link, {ln - 5:.1f}) (to check TravTm)",
                    v.Net.VehicleTravelTimeMeasurements.AddVehicleTravelTimeMeasurement, 0, lk, 5.0, lk, round(ln - 5, 1))
        except Exception as e:
            out("    REJECTED creating travel time measurement:", errtext(e))
    runs = v.Net.SimulationRuns
    attempt("SimulationRuns.Count (before run)", lambda: int(runs.Count))
    sim_setup(v, 60)
    for seed in (42, 43):
        step(f"Q9 run seed {seed}")
        v.Simulation.SetAttValue("RandSeed", seed)
        attempt(f"RunContinuous() RandSeed={seed} (to the end)", v.Simulation.RunContinuous)
    attempt("SimulationRuns.Count", lambda: int(runs.Count))
    ok, nos = attempt("SimulationRuns.GetMultiAttValues('No')", runs.GetMultiAttValues, "No")
    attempt("SimulationRuns.GetMultipleAttributes(['No','Timestamp','RandSeed','SimEnd'])",
            runs.GetMultipleAttributes, ["No", "Timestamp", "RandSeed", "SimEnd"])
    run_nos: list[int] = []
    try:
        run_nos = [int(r.AttValue("No")) for r in runs.GetAll()]
        out("    GetAll() → AttValue('No'):", run_nos)
    except Exception as e:
        out("    REJECTED SimulationRuns.GetAll():", errtext(e))
    npm = v.Net.VehicleNetworkPerformanceMeasurement
    for spec in [f"DelayAvg({n},Total,All)" for n in run_nos] + ["DelayAvg(Current,Total,All)", "DelayAvg(Avg,Total,All)"]:
        attempt(f"VehicleNetworkPerformanceMeasurement.AttValue({spec!r})", npm.AttValue, spec)
    try:
        tts = v.Net.VehicleTravelTimeMeasurements.GetAll()
        if tts:
            for spec in [f"TravTm({n},Total,All)" for n in run_nos] + ["TravTm(Current,Total,All)"]:
                attempt(f"VehicleTravelTimeMeasurement{tts[0].AttValue('No')}.AttValue({spec!r})", tts[0].AttValue, spec)
        else:
            out("    no travel time measurement - TravTm is checked via network performance DelayAvg instead")
    except Exception as e:
        out("    REJECTED VehicleTravelTimeMeasurements:", errtext(e))


def probe_signal_simple(v: Any, inpx: Path, stamp: str) -> None:
    folder = scratch_copy(inpx.parent, "signal_simple", stamp)
    net = folder / inpx.name
    out("    Copy:", net)
    step("LoadNet simple")
    vc.load(v, str(net))
    sc = first_item(v.Net.SignalControllers)
    sc_attrs(sc, "FIXEDTIMESIMPLE base")
    show_supply(sc, "FIXEDTIMESIMPLE base")
    cyc = int(float(sc.AttValue("CycTm") or 72))
    sig_timeline(v, sg_groups(sc), cyc + 5, "FIXEDTIMESIMPLE base")
    step("LoadNet simple 2")
    vc.load(v, str(net))
    sc = first_item(v.Net.SignalControllers)
    sg = sc.SGs.GetAll()[0]
    sgno = int(sg.AttValue("No"))
    er, eg = float(sg.AttValue("EndRed")), float(sg.AttValue("EndGreen"))
    for attr, val in (("EndRed", er + 2), ("EndGreen", eg + 5), ("Amber", 4)):
        attempt(f"SG{sgno}.SetAttValue({attr!r}, {val})", sg.SetAttValue, attr, val)
        attempt(f"SG{sgno}.AttValue({attr!r}) again", sg.AttValue, attr)
    sig_timeline(v, sg_groups(sc)[:1], cyc + 5, f"after FIXEDTIMESIMPLE (EndRed {er}→{er + 2}, EndGreen {eg}→{eg + 5}, Amber 4)")


# ---- Time intervals ----

def probe_intervals(v: Any, net_path: str | None) -> None:
    net = v.Net
    if net_path:
        step("LoadNet intervals")
        vc.load(v, str(Path(net_path).resolve()))
        lks = [lk for lk in net.Links.GetAll() if not bool(lk.AttValue("IsConn"))]
        lk1, lk2 = lks[0], (lks[1] if len(lks) > 1 else lks[0])
        out("    LoadNet:", net_path)
    else:
        vc.new_network(v)
        net = v.Net
        _, len1 = vc.add_link(v, 1, [(0.0, 0.0, 0.0), (200.0, 0.0, 0.0)], [3.6], "probe 1")
        vc.add_link(v, 2, [(200.5, 0.0, 0.0), (400.0, 0.0, 0.0)], [3.6], "probe 2")
        vc.add_connector(v, 10000, 1, [1], float(len1 or 200.0), 2, [1], 0.0, "1>2")
        lk1, lk2 = net.Links.ItemByKey(1), net.Links.ItemByKey(2)
        out("    New() + links 1·2 + connector 10000")
    out("  [I1] TimeIntervalSets")
    sets = net.TimeIntervalSets
    try:
        print_rows(attr_rows(sets))
    except Exception as e:
        out("    REJECTED TimeIntervalSets.Attributes:", errtext(e))
    try:
        for s in sets.GetAll():
            ints = s.TimeInts.GetAll()
            desc = []
            for ti in ints:
                vals = {}
                for attr in ("Index", "Start", "End", "TmIntSet"):
                    try:
                        vals[attr] = ti.AttValue(attr)
                    except Exception as e:
                        vals[attr] = f"rejected({errtext(e)})"
                desc.append(vals)
            out(f"    set No={s.AttValue('No')!r}: TimeInts {len(ints)}: {desc}")
    except Exception as e:
        out("    REJECTED TimeIntervalSets.GetAll():", errtext(e))
    try:
        print_rows(attr_rows(sets.ItemByKey(1).TimeInts), "      TimeInts attributes: ")
    except Exception as e:
        out("    REJECTED TimeInts.Attributes:", errtext(e))
    attempt("TimeIntervalSets.ItemByKey(1).AttValue('No')", lambda: sets.ItemByKey(1).AttValue("No"))
    attempt("TimeIntervalSets.ItemByKey(2).AttValue('No')", lambda: sets.ItemByKey(2).AttValue("No"))
    out("    Methods:", [n for n in com_method_names(sets.ItemByKey(1).TimeInts) if n.startswith(("Add", "Remove"))])

    out("  [I2] Vehicle composition, vehicle input, static route (test subjects)")
    vt = first_item(net.VehicleTypes)
    vts = net.VehicleTypes.GetAll()
    vt2 = next((t for t in vts if int(t.AttValue("No")) == 200), vts[1] if len(vts) > 1 else vt)
    out("    Vehicle types:", [(int(t.AttValue("No")), t.AttValue("Name")) for t in vts][:8])
    dsd = first_item(net.SpeedDistributions)
    comp = net.VehicleCompositions.AddVehicleComposition(0, [vt, dsd])
    comp_no = int(comp.AttValue("No"))
    vi = net.VehicleInputs.AddVehicleInput(0, lk1)
    vi.SetAttValue("Volume(1)", 600)
    vi.SetAttValue("VehComp(1)", comp_no)
    dec = net.VehicleRoutingDecisionsStatic.AddVehicleRoutingDecisionStatic(0, lk1, 20.0)
    route = dec.VehRoutSta.AddVehicleRouteStatic(0, lk2, 50.0)
    route.SetAttValue("RelFlow(1)", 0.7)
    out(f"    VehComp {comp_no}, VehicleInput {vi.AttValue('No')} Volume(1)=600 VehComp(1)={comp_no}, "
        f"routing decision {dec.AttValue('No')} route {route.AttValue('No')} RelFlow(1)=0.7")

    def read_vi(n: int) -> None:
        vals = {}
        for k in range(1, n + 1):
            for attr in ("Volume", "VehComp", "Cont", "VolType"):
                try:
                    vals[f"{attr}({k})"] = vi.AttValue(f"{attr}({k})")
                except Exception as e:
                    vals[f"{attr}({k})"] = f"rejected({errtext(e)})"
        out(f"      VehicleInput: {vals}")
        try:
            out(f"      TimeIntVehVols count: {len(vi.TimeIntVehVols.GetAll())}")
        except Exception as e:
            out("      REJECTED TimeIntVehVols:", errtext(e))

    def read_route(n: int) -> None:
        vals = {}
        for k in range(1, n + 1):
            try:
                vals[f"RelFlow({k})"] = route.AttValue(f"RelFlow({k})")
            except Exception as e:
                vals[f"RelFlow({k})"] = f"rejected({errtext(e)})"
        out(f"      Route: {vals}")

    out("  [I3] Add an interval to the vehicle input time interval set (key 1), write Start, remove")
    ti_vi = sets.ItemByKey(1).TimeInts
    n0 = int(ti_vi.Count)
    ok, ti_new = attempt("TimeIntervalSets.ItemByKey(1).TimeInts.AddTimeInterval(0)", ti_vi.AddTimeInterval, 0)
    if ok and ti_new is not None:
        for attr in ("Index", "Start", "End"):
            attempt(f"new interval.AttValue({attr!r}) (before writing Start)", ti_new.AttValue, attr)
        attempt("new interval.SetAttValue('Start', 900)", ti_new.SetAttValue, "Start", 900)
        for attr in ("Index", "Start", "End"):
            attempt(f"new interval.AttValue({attr!r})", ti_new.AttValue, attr)
        out(f"    interval count {n0} → {int(ti_vi.Count)}")
        read_vi(int(ti_vi.Count))
        n_new = int(ti_vi.Count)
        attempt(f"VehicleInput.SetAttValue('Volume({n_new})', 1200) (Cont={n_new} unchanged)", vi.SetAttValue,
                f"Volume({n_new})", 1200)
        attempt(f"VehicleInput.SetAttValue('Cont({n_new})', False)", vi.SetAttValue, f"Cont({n_new})", False)
        attempt(f"VehicleInput.SetAttValue('Volume({n_new})', 1200) (after turning Cont off)", vi.SetAttValue,
                f"Volume({n_new})", 1200)
        read_vi(n_new)
        ok2, ti3 = attempt("AddTimeInterval(0) once more (Start not written)", ti_vi.AddTimeInterval, 0)
        if ok2 and ti3 is not None:
            for attr in ("Index", "Start"):
                attempt(f"third interval.AttValue({attr!r})", ti3.AttValue, attr)
            read_vi(int(ti_vi.Count))
            attempt("third interval.SetAttValue('Start', 1800)", ti3.SetAttValue, "Start", 1800)
            read_vi(int(ti_vi.Count))
        n_before = int(ti_vi.Count)
        attempt("TimeInts.RemoveTimeInterval(second interval object)", ti_vi.RemoveTimeInterval, ti_new)
        out(f"    interval count {n_before} → {int(ti_vi.Count)}")
        try:
            out("    Remaining interval Start:", [ti.AttValue("Start") for ti in ti_vi.GetAll()])
        except Exception as e:
            out("    REJECTED remaining intervals:", errtext(e))
        read_vi(int(ti_vi.Count))

    out("  [I4] Add an interval to the static route time interval set (key 2)")
    ti_rt = sets.ItemByKey(2).TimeInts
    ok, tr = attempt("TimeIntervalSets.ItemByKey(2).TimeInts.AddTimeInterval(0)", ti_rt.AddTimeInterval, 0)
    if ok and tr is not None:
        attempt("new interval.SetAttValue('Start', 900)", tr.SetAttValue, "Start", 900)
        read_route(int(ti_rt.Count))
        read_vi(int(ti_vi.Count))

    out("  [Q8] VehCompRelFlows - key, add, remove")
    flows = comp.VehCompRelFlows
    try:
        print_rows(attr_rows(flows))
    except Exception as e:
        out("    REJECTED VehCompRelFlows.Attributes:", errtext(e))
    out("    Methods:", [n for n in com_method_names(flows) if n.startswith(("Add", "Remove", "Item"))])
    def show_flows() -> None:
        try:
            for f in comp.VehCompRelFlows.GetAll():   # fetch the container anew every time
                out(f"      item: VehType={f.AttValue('VehType')!r} DesSpeedDistr={f.AttValue('DesSpeedDistr')!r} "
                    f"RelFlow={f.AttValue('RelFlow')!r}")
        except Exception as e:
            out("      REJECTED reading items:", errtext(e))
    show_flows()
    vt_no = int(vt.AttValue("No"))
    attempt(f"VehCompRelFlows.ItemByKey({vt_no}) (vehicle type number).AttValue('VehType')",
            lambda: flows.ItemByKey(vt_no).AttValue("VehType"))
    attempt("VehCompRelFlows.ItemByKey(1) (sequence number).AttValue('VehType')", lambda: flows.ItemByKey(1).AttValue("VehType"))
    ok, nf = attempt(f"AddVehicleCompositionRelativeFlow(vehicle type {vt2.AttValue('No')} object, distribution {dsd.AttValue('No')} object)",
                     flows.AddVehicleCompositionRelativeFlow, vt2, dsd)
    attempt("VehCompRelFlows.Count", lambda: int(flows.Count))
    attempt("comp.VehCompRelFlows.Count (fetched again)", lambda: int(comp.VehCompRelFlows.Count))
    show_flows()
    if ok and nf is not None:
        for attr in ("VehType", "DesSpeedDistr", "RelFlow", "VehComp"):
            attempt(f"new item.AttValue({attr!r})", nf.AttValue, attr)
        attempt("new item.SetAttValue('RelFlow', 0.25)", nf.SetAttValue, "RelFlow", 0.25)
        attempt("RemoveVehicleCompositionRelativeFlow(new item)", flows.RemoveVehicleCompositionRelativeFlow, nf)
        attempt("VehCompRelFlows.Count (after removal)", lambda: int(flows.Count))
        show_flows()


def run9(v: Any, a: argparse.Namespace, stamp: str) -> int:
    """Run only the scenario, signal and time interval checks (the 0-9i items run only without these options)."""
    if a.scenario:
        out("[S] Scenario management (--scenario)")
        try:
            folder = scratch_copy(Path(a.scenario), "scenario", stamp)
            out("    Copy:", folder)
            plain = None
            if a.plain or a.signal:
                plain = require_scratch(Path(a.plain or a.signal))
            else:
                cands = sorted(folder.glob("*.inpx"))
                plain = cands[0] if cands else None
            probe_scenario(v, folder, plain, stamp)
        except Exception as e:
            out("    [exception] scenario:", type(e).__name__, errtext(e))
    if a.signal:
        out("[G] Signals - FIXEDTIME (--signal)")
        try:
            probe_signal(v, Path(a.signal), stamp)
        except Exception as e:
            out("    [exception] signal:", type(e).__name__, errtext(e))
    if a.signal_simple:
        out("[G-h] Signals - FIXEDTIMESIMPLE (--signal-simple)")
        try:
            probe_signal_simple(v, Path(a.signal_simple), stamp)
        except Exception as e:
            out("    [exception] signal (simple):", type(e).__name__, errtext(e))
    if a.intervals:
        out("[I] Time intervals and vehicle composition (--intervals" + (f", --net {a.net}" if a.net else ", empty new network") + ")")
        try:
            probe_intervals(v, a.net)
        except Exception as e:
            out("    [exception] time intervals:", type(e).__name__, errtext(e))
    step("")
    return 0


# ---------- Main ----------

def connect(progid: str) -> tuple[Any, str]:
    """vissim_com.launch if present, otherwise Dispatch. GetActiveObject is not used."""
    launch = getattr(vc, "launch", None)
    if callable(launch):
        v, how = launch(progid)          # launch returns an (object, "Dispatch:<progid>") tuple
        return v, f"vissim_com.launch({how})"
    import pythoncom  # noqa: F401
    import win32com.client as wc
    return wc.Dispatch(progid), f"Dispatch:{progid}"


def run(a: argparse.Namespace) -> int:
    out("[0] Connection")
    pids_before = vissim_pids()
    out("    Vissim*.exe PIDs before launch:", sorted(pids_before) or "none")
    try:
        v, how = connect(a.progid)
    except Exception as e:
        out("    REJECTED connection:", errtext(e))
        return 2
    pids_after = vissim_pids()
    new_pids = sorted(pids_after - pids_before)
    out("    OK  ", how, "| version:", vc.version(v), "| PIDs after launch:", sorted(pids_after) or "none", "| new PIDs:", new_pids)
    out("    Network open?", vc.has_open_network(v))
    try:
        vc.ensure_can_open_new(v, a.force)
    except vc.UnsavedNetworkError as e:
        out("    Aborted:", str(e))
        return 3

    if a.nine:
        if len(new_pids) != 1:
            out(f"    Aborted: {len(new_pids)} new Vissim PIDs ({new_pids}) - cannot identify the Vissim this script started")
            return 3
        _STEP["pids"] = new_pids
        start_watchdog(a.step_timeout)
        try:
            return run9(v, a, a.stamp)
        finally:
            step("close")
            out("[close] New() → Exit() without saving (no files outside the copies)")
            try:
                v.New()
                v.Exit()
                out("    OK   Exit()")
            except Exception as e:
                out("    Exit:", errtext(e))
            step("")

    created: dict[str, Any] = {}
    lk1 = lk2 = conn = None
    if a.net:
        out(f"[1] Open a saved network (not saved): {a.net}")
        try:
            vc.load(v, str(Path(a.net).resolve()))
            links = [lk for lk in v.Net.Links.GetAll() if not bool(lk.AttValue("IsConn"))]
            conns = [lk for lk in v.Net.Links.GetAll() if bool(lk.AttValue("IsConn"))]
            wide = [lk for lk in links if int(lk.AttValue("NumLanes")) >= 2] or links
            lk1, lk2 = wide[0], (wide[1] if len(wide) > 1 else wide[0])
            conn = conns[0] if conns else None
            out(f"    OK   LoadNet | links {len(links)} · connectors {len(conns)} | test links {lk1.AttValue('No')}, {lk2.AttValue('No')}")
        except Exception as e:
            out("    REJECTED LoadNet:", errtext(e))
            return 4
    else:
        out("[1] Empty new network + 2 links (2 lanes, 3D) + 1 connector  (simulation and result items run only with --net - "
            "calling RunContinuous on an unsaved network makes Vissim open a save dialog and COM hangs)")
        try:
            vc.new_network(v)
            out("    OK   New()")
            mode1, len1 = vc.add_link(v, 1, [(0.0, 0.0, 50.0), (100.0, 0.0, 52.0), (200.0, 5.0, 55.0)], [3.6, 3.6], "probe 1")
            mode2, len2 = vc.add_link(v, 2, [(200.5, 5.0, 55.0), (300.0, 5.0, 56.0)], [3.6, 3.6], "probe 2")
            lk1, lk2 = v.Net.Links.ItemByKey(1), v.Net.Links.ItemByKey(2)
            out(f"    OK   add_link 1({mode1}, Length2D={len1}) · 2({mode2}, Length2D={len2})")
            vc.add_connector(v, 10000, 1, [1, 2], float(len1 or 200.0), 2, [1, 2], 0.0, "1>2")
            conn = v.Net.Links.ItemByKey(10000)
            out("    OK   add_connector 10000 (1>2, 2 lanes)")
        except Exception as e:
            out("    REJECTED base network:", errtext(e))
            if lk1 is None:
                out("    No links, so the later items cannot proceed")
                return 4

    out("[2a] Registry creation signatures (the registry._TYPES kinds that have create)")
    probe_creates(v, lk1, lk2, created)
    out("[2a'] DataCollectionMeasurement (no creation signature - one candidate + list the method names)")
    try:
        dcm_coll = v.Net.DataCollectionMeasurements
        attempt("DataCollectionMeasurements.GetAll() count", lambda: len(dcm_coll.GetAll()))
        adds = [n for n in com_method_names(dcm_coll) if n.startswith("Add")]
        out("    Add* among the method names:", adds or "none (no type info)")
        ok, obj = attempt("AddDataCollectionMeasurement(0)", dcm_coll.AddDataCollectionMeasurement, 0)
        if ok and obj is not None:
            attempt("→ AttValue('No')", obj.AttValue, "No")
    except Exception as e:
        out("    REJECTED DataCollectionMeasurements:", errtext(e))
    out("[2g] Remove methods (reduced speed area, static route → static routing decision)")
    # A static route is not completed when the two test links from --net are not connected - if left in place, simulation initialisation is rejected
    # ("Static Vehicle Route ... is not complete"). So remove it before [4c] and [6e].
    probe_remove(v, created, ("ReducedSpeedArea", "VehicleRouteStatic", "VehicleRoutingDecisionStatic"))

    out(f"[3b] Lane attributes (Links[{lk1.AttValue('No')}].Lanes)")
    try:
        lane = lk1.Lanes.ItemByKey(1)
        rows = attr_rows(lk1.Lanes)      # the attribute list exists only on the container (Lanes)
        out(f"    {len(rows)} attributes; those whose ID contains Block, Clos or Time:")
        print_rows(rows_matching(rows, ("Block", "Clos", "Time")))
        attempt("Lane.AttValue('BlockedVehClasses')", lane.AttValue, "BlockedVehClasses")
        try:
            cls_no = int(first_item(v.Net.VehicleClasses).AttValue("No"))
        except Exception:
            cls_no = 10
        attempt(f"Lane.SetAttValue('BlockedVehClasses', '{cls_no}')", lane.SetAttValue, "BlockedVehClasses", str(cls_no))
        attempt("Lane.AttValue('BlockedVehClasses') again", lane.AttValue, "BlockedVehClasses")
        attempt("Lane.SetAttValue('BlockedVehClasses', '') (revert)", lane.SetAttValue, "BlockedVehClasses", "")
    except Exception as e:
        out("    REJECTED Lane:", errtext(e))

    out("[4c] Simulation")
    if a.net:
        probe_simulation(v)
    else:
        out("    SKIPPED: not run on an unsaved network (give a saved .inpx with --net). Attribute reads only:")
        for attr in ("SimPeriod", "SimRes", "RandSeed", "NumRuns", "RandSeedIncr", "SimBreakAt", "UseMaxSimSpeed", "SimSec", "StartTm"):
            attempt(f"Simulation.AttValue('{attr}')", v.Simulation.AttValue, attr)

    out("[5d] Evaluation")
    probe_evaluation(v)
    out("[5d'] default_attrs of single objects (before simulation)")
    probe_single_defaults(v, "before sim")

    out("[6e] Result attributes")
    if a.net:
        probe_results(v, created)
        out("[6e'] default_attrs of single objects (after simulation)")
        probe_single_defaults(v, "after sim")
    else:
        out("    SKIPPED: no --net, so the simulation was not run")
    out("[6g] Remove parking objects (route → routing decision → parking lot → parking lot group)")
    probe_remove(v, created, ("VehicleRouteParking", "VehicleRoutingDecisionParking", "ParkingLot",
                              "ParkingLotGroup"))

    out("[7f] Attributes meta fields · Link/Connector geometry and coordinate attributes")
    probe_meta(v, lk1, conn)

    out("[8h] PID set difference")
    out("    " + ("OK   one new PID: %s" % new_pids if len(new_pids) == 1
                  else f"REJECTED {len(new_pids)} new PIDs: {new_pids} (0 means it attached to an existing process or the name is not Vissim*)"))

    out("[9i] ROT")
    probe_rot(a.progid, a.rot_automation)

    try:
        v.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", 0)
    except Exception:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Vissim 2026 COM check (for the MCP registry). Does not save the network.")
    ap.add_argument("--progid", default="Vissim.Vissim.26")
    ap.add_argument("--net", default=None, help="path of a saved .inpx - if given, this network is opened instead of New() and the simulation and result items run too (not saved)")
    ap.add_argument("--force", action="store_true", help="create a new network even if one is open")
    ap.add_argument("--rot-automation", action="store_true",
                    help="start Vissim260.exe -automation and test how many seconds GetActiveObject takes to attach (terminated afterwards)")
    ap.add_argument("--out", default=None,
                    help="result log file (default runs/mcp/probe_<YYYYMMDD-HHMMSS>[_label].txt)")
    ap.add_argument("--label", default=None, help="short name to append to the log file name")
    # scenario / signal / time interval checks - if any is given, only these run instead of the 0-9i items
    ap.add_argument("--scenario", default=None,
                    help="scenario management example folder (incl. .vissimpdb) copied under runs/mcp/scratch/ - opened and saved only in a run-only copy")
    ap.add_argument("--signal", default=None,
                    help="FIXEDTIME example .inpx copied under runs/mcp/scratch/ - its folder is copied again as a run-only copy and opened")
    ap.add_argument("--signal-simple", default=None,
                    help="FIXEDTIMESIMPLE example .inpx copied under runs/mcp/scratch/ (same way)")
    ap.add_argument("--plain", default=None,
                    help="plain .inpx copy for S4 (move under scenario management) and S5 (model transfer file) of --scenario"
                         " (if absent, --signal; if that is absent too, the .inpx in the scenario folder)")
    ap.add_argument("--intervals", action="store_true",
                    help="time interval sets, interval add/remove, VehCompRelFlows (empty new network, or that file with --net)")
    ap.add_argument("--step-timeout", type=float, default=300.0,
                    help="if one step of the scenario/signal/time interval checks exceeds this many seconds (modal window suspected), only the Vissim this script started is terminated")
    a = ap.parse_args()
    a.nine = bool(a.scenario or a.signal or a.signal_simple or a.intervals)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    a.stamp = stamp
    suffix = f"_{a.label}" if a.label else ""
    out_path = Path(a.out) if a.out else ROOT / "runs" / "mcp" / f"probe_{stamp}{suffix}.txt"
    if a.nine:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _FH.append(open(out_path, "w", encoding="utf-8"))
    out(f"# com_probe {stamp} | progid={a.progid} | force={a.force} | rot-automation={a.rot_automation}"
        + (f" | scenario={a.scenario} | signal={a.signal} | signal-simple={a.signal_simple} | intervals={a.intervals}"
           f" | net={a.net}" if a.nine else ""))
    rc = 1
    try:
        rc = run(a)
    except Exception as e:
        out("[exception]", type(e).__name__, errtext(e))
    finally:
        out("[end] Nothing saved. Close the new network in Vissim." if not a.nine else
            "[end] Scenario/signal/time interval checks: only the copies under runs/mcp/scratch/<timestamp>/ were opened and saved.")
        for fh in _FH:
            fh.close()
        _FH.clear()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
            print("[log]", out_path)
        except Exception as e:
            print("[log failed]", out_path, errtext(e))
    return rc


if __name__ == "__main__":
    sys.exit(main())
