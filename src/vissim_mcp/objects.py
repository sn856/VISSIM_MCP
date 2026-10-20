"""Object layer: finds VISSIM objects by the type names of the registry and reads, modifies, creates and deletes them.

Rules
  - Every COM call happens inside ComThread.submit only. The protection list (guard) and argument checks finish
    before COM is called.
  - Writes (set/create/delete) are rejected with sim_running while the simulation is running/stopping; reads are allowed.
  - A write always puts the before and after values in the response and, on every success, calls state.note_write()
    and changelog.append().
  - When COM rejects a call, its message is returned as-is as com_error. No substitute calls or workarounds.
  - An unregistered type name is read-only (Net.<name> container). Writing it gives bad_args.
  - A composite key ("link-lane") is kept as a string in the response's no. A single object has no=None.
"""
from __future__ import annotations

from typing import Any, Callable

from . import registry as rg
from .changelog import ChangeLog
from .comthread import ComThread
from .errors import (KIND_BAD_ARGS, KIND_COM_ERROR, KIND_NOT_FOUND, KIND_PROTECTED, KIND_SIM_RUNNING,
                     ToolError)
from .guard import Guard, ProtectedError
from .state import SIM_RUNNING, SIM_STOPPING, ServerState

# Attribute holding the last part of a composite key (default "No"; Index for lanes, polyline points and parking spaces)
_KEY_ATTR = {"Lane": "Index", "LinkPolyPoint": "Index", "ParkingSpace": "Index"}
# Routing decisions that take veh_classes after creation (AllVehTypes=false + VehClasses)
_VEH_CLASS_DECISIONS = ("VehicleRoutingDecisionStatic", "VehicleRoutingDecisionParking")
# Default attributes when reading an unregistered type
_UNREGISTERED_ATTRS = ("No", "Name")
# list_of item kind -> referenced container (otherwise the number is used as-is)
_LIST_ITEM_CONTAINER = {"veh_type": "VehicleTypes", "des_speed_distr": "SpeedDistributions"}   # 2026 container name
_SIM_BUSY = (SIM_RUNNING, SIM_STOPPING)


def _base_id(attr: str) -> str:
    """'Volume(1)' -> 'Volume' (the protection check also matches the ID before the parenthesis)."""
    return attr.split("(", 1)[0]


def _plain(value: Any) -> Any:
    """Make a value returned by COM JSON-serialisable: tuples become lists."""
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def _key_text(key: Any) -> Any:
    """Key for responses and the change log: 'a-b' string for a composite key, int for a number, None for a single object."""
    if isinstance(key, tuple):
        return "-".join(str(p) for p in key)
    return key


class _Resolved:
    """Result of resolving one type name (registered or not, container path, key format)."""

    def __init__(self, name: str, t: rg.ObjType | None):
        self.name = name
        self.t = t
        self.registered = t is not None
        self.container = t.container if t else f"Net.{name}"
        self.key = t.key if t else rg.KEY_NO
        self.key_parts = t.key_parts if t else ()
        self.default_attrs = tuple(t.default_attrs) if t else _UNREGISTERED_ATTRS
        self.filter_attr = t.filter_attr if t else None
        self.key_attr = _KEY_ATTR.get(name, "No")

    @property
    def parent_parts(self) -> tuple[str, ...]:
        """Leading part of a composite key that selects the container (the last part is the item key)."""
        return self.key_parts[:-1] if self.key == rg.KEY_COMPOSITE else ()


class Objects:
    def __init__(self, cfg: dict, state: ServerState, com: ComThread, changelog: ChangeLog,
                 guard: Guard, vissim_getter: Callable[[], Any]):
        self._cfg = cfg
        self._state = state
        self._com = com
        self._log = changelog
        self._guard = guard
        self._vissim = vissim_getter
        self._pending_attrs: dict[str, Any] = {}   # attrs that create_object hands to post-processing

    # ------------------------------------------------------------ common (outside COM)
    def _resolve(self, type_name: str) -> _Resolved:
        if not isinstance(type_name, str) or not type_name:
            raise ToolError(KIND_BAD_ARGS, f"type must be a non-empty string ({type_name!r})")
        return _Resolved(type_name, rg.get(type_name))

    def _resolve_writable(self, type_name: str) -> _Resolved:
        r = self._resolve(type_name)
        if not r.registered:
            raise ToolError(KIND_BAD_ARGS, f"an unregistered type can only be read: {type_name!r}")
        if r.t.read_only:
            raise ToolError(KIND_BAD_ARGS, f"{type_name} is read-only")
        return r

    def _parse_key(self, r: _Resolved, no: Any):
        if not r.registered:
            try:
                return rg._to_int(r.name, no, "no")
            except rg.KeyFormatError as e:
                raise ToolError(KIND_BAD_ARGS, str(e)) from e
        try:
            return rg.parse_key(r.name, no)
        except rg.KeyFormatError as e:
            raise ToolError(KIND_BAD_ARGS, str(e)) from e

    def _parse_parent(self, r: _Resolved, parent: Any) -> tuple[int, ...]:
        """Parse the parent key of a composite-key type (the leading part that selects the container)."""
        parts = r.parent_parts
        if not parts:
            return ()
        fmt = "-".join(parts)
        if parent is None:
            raise ToolError(KIND_BAD_ARGS, f"{r.name} has a composite key, so parent (\"{fmt}\") is required")
        if isinstance(parent, (tuple, list)):
            items = list(parent)
        else:
            items = str(parent).split("-")
        if len(items) != len(parts):
            raise ToolError(KIND_BAD_ARGS, f"{r.name} parent must have the form \"{fmt}\" ({parent!r})")
        try:
            return tuple(rg._to_int(r.name, p, name) for p, name in zip(items, parts))
        except rg.KeyFormatError as e:
            raise ToolError(KIND_BAD_ARGS, str(e)) from e

    def _require_idle(self) -> None:
        sim = self._state.full()["simulation"]["state"]
        if sim in _SIM_BUSY:
            raise ToolError(KIND_SIM_RUNNING, f"the network cannot be changed while the simulation is {sim}")

    def _check_guard(self, type_name: str, op: str, attr: str | None = None) -> None:
        """Protection list check; raises ToolError(protected) when blocked. An attribute is matched by its full ID and by the ID before the parenthesis."""
        try:
            self._guard_require(type_name, op, attr)
        except ProtectedError as e:
            raise ToolError(KIND_PROTECTED, str(e)) from e

    def _guard_require(self, type_name: str, op: str, attr: str | None) -> None:
        self._guard.require(type_name, op, attr)
        if attr is not None and _base_id(attr) != attr:
            self._guard.require(type_name, op, _base_id(attr))

    def _record(self, kind: str, tool: str, type_name: str, no: Any, attr: str | None,
                before: Any, after: Any, ok: bool, error: str | None = None) -> None:
        self._log.append({"kind": kind, "tool": tool, "network_path": self._state.full()["network"]["path"],
                          "type": type_name, "no": _key_text(no), "attr": attr,
                          "before": _plain(before), "after": _plain(after), "ok": ok, "error": error})

    # ------------------------------------------------------------ common (inside COM)
    @staticmethod
    def _com_error(e: BaseException, what: str) -> ToolError:
        return ToolError(KIND_COM_ERROR, f"{what}: {e}", {"exception": type(e).__name__})

    def _walk(self, v: Any, r: _Resolved, parent: tuple[int, ...]) -> Any:
        """Follow the container path ("Net.Links[link].Lanes") and return the container (or the single object)."""
        obj = v
        prefix = list(parent)
        for seg in r.container.split("."):
            name, _, rest = seg.partition("[")
            try:
                obj = getattr(obj, name)
            except Exception as e:   # noqa: BLE001 — a failed COM property access also lands here
                raise ToolError(KIND_NOT_FOUND, f"{r.name}: container {r.container} not found ({e})") from e
            if obj is None:
                raise ToolError(KIND_NOT_FOUND, f"{r.name}: container {r.container} not found")
            if rest:
                k = prefix.pop(0)
                obj = self._item_by_key(obj, k, f"{r.name} parent {rest[:-1]}")
        return obj

    @staticmethod
    def _item_by_key(container: Any, key: Any, what: str) -> Any:
        try:
            item = container.ItemByKey(key)
        except Exception as e:   # noqa: BLE001
            raise ToolError(KIND_NOT_FOUND, f"{what} {key} not found: {e}") from e
        if item is None:
            raise ToolError(KIND_NOT_FOUND, f"{what} {key} not found")
        return item

    def _find(self, v: Any, r: _Resolved, key: Any) -> Any:
        """Find one object by its parsed key (for a single object the end of the container path is the object)."""
        if r.key == rg.KEY_SINGLE:
            return self._walk(v, r, ())
        if isinstance(key, tuple):
            container = self._walk(v, r, key[:-1])
            return self._item_by_key(container, key[-1], r.name)
        container = self._walk(v, r, ())
        item = self._item_by_key(container, key, r.name)
        if r.filter_attr is not None:
            attr, want = r.filter_attr
            try:
                got = item.AttValue(attr)
            except Exception as e:   # noqa: BLE001
                raise self._com_error(e, f"{r.name} {key} {attr} read") from e
            if bool(got) != bool(want):
                raise ToolError(KIND_NOT_FOUND, f"{r.name} {key} not found ({attr}={got!r})")
        return item

    @staticmethod
    def _read(obj: Any, attr: str) -> Any:
        return _plain(obj.AttValue(attr))

    def _snapshot(self, obj: Any, attrs: tuple[str, ...]) -> tuple[dict[str, Any], list[str]]:
        """Attribute dict; an attribute that fails becomes None plus a warning."""
        out: dict[str, Any] = {}
        warnings: list[str] = []
        for a in attrs:
            try:
                out[a] = self._read(obj, a)
            except Exception as e:   # noqa: BLE001
                out[a] = None
                warnings.append(f"{a}: {e}")
        return out, warnings

    def _set_one(self, obj: Any, attr: str, value: Any, what: str) -> tuple[Any, Any]:
        """Read before -> write -> read again. A failure raises ToolError(com_error)."""
        try:
            before = self._read(obj, attr)
        except Exception as e:   # noqa: BLE001
            raise self._com_error(e, f"{what} {attr} read before applying") from e
        try:
            obj.SetAttValue(attr, value)
        except Exception as e:   # noqa: BLE001
            err = self._com_error(e, f"{what} {attr} write")
            err.detail["before"] = _plain(before)
            raise err from e
        try:
            after = self._read(obj, attr)
        except Exception as e:   # noqa: BLE001
            err = self._com_error(e, f"{what} {attr} read after applying")
            err.detail["before"] = _plain(before)
            raise err from e
        return before, after

    def _fetch_rows(self, container: Any, cols: list[str]) -> list[list[Any]]:
        """One GetMultipleAttributes call; if unavailable, GetMultiAttValues per attribute. Never loops AttValue per object."""
        fn = getattr(container, "GetMultipleAttributes", None)
        if callable(fn):
            try:
                data = fn(cols)
            except Exception as e:   # noqa: BLE001
                raise self._com_error(e, f"GetMultipleAttributes({', '.join(cols)})") from e
            return [[_plain(c) for c in row] for row in (data or ())]
        columns: list[list[Any]] = []
        for a in cols:
            try:
                data = container.GetMultiAttValues(a)
            except Exception as e:   # noqa: BLE001
                raise self._com_error(e, f"GetMultiAttValues({a})") from e
            columns.append([_plain(pair[1]) for pair in (data or ())])
        n = len(columns[0]) if columns else 0
        return [[col[i] for col in columns] for i in range(n)]

    # ------------------------------------------------------------ type and attribute descriptions
    def list_object_types(self) -> dict[str, Any]:
        """Registry types, containers, key formats, create signatures and a protection summary. Does not call COM."""
        types = []
        for name in rg.names():
            t = rg.get(name)
            create = None
            if t.create is not None:
                create = {"method": t.create.method,
                          "args": [{"name": a.name, "kind": a.kind, "items": list(a.items)} for a in t.create.args],
                          "post": list(t.create.post), "source": t.create.source,
                          "verified": t.create.verified}
            types.append({
                "type": name, "container": t.container, "key": t.key, "key_parts": list(t.key_parts),
                "filter_attr": list(t.filter_attr) if t.filter_attr else None,
                "create": create, "create_note": t.create_note, "remove": t.remove,
                "read_only": t.read_only, "default_attrs": list(t.default_attrs),
                "result_kind": t.result_kind,
                "protected": {"create": self._guard.check(name, "create") is not None,
                              "delete": self._guard.check(name, "delete") is not None,
                              "set": self._guard.check(name, "set") is not None},
            })
        return {"types": types}

    def describe_attributes(self, type: str, parent: Any = None) -> dict[str, Any]:
        """Attributes.GetAll() of the container (or the object for a single object): ID, name, value type and editability of each item, plus a protection flag."""
        r = self._resolve(type)
        parent_key = self._parse_parent(r, parent)

        def run():
            v = self._vissim()
            holder = self._walk(v, r, parent_key)
            if r.registered and r.t.key == rg.KEY_SINGLE:
                # VISSIM 2026: single objects such as NetPara, Simulation, Evaluation and network performance give no Attributes list
                raise ToolError(KIND_NOT_FOUND,
                                f"{type} is a single object, so VISSIM gives no attribute list. Read attribute IDs "
                                f"one by one with get_attributes or see Doc\\Eng\\attribute.xlsx in the VISSIM install folder.")
            try:
                items = holder.Attributes.GetAll()
            except Exception as e:   # noqa: BLE001
                raise self._com_error(e, f"{type} Attributes.GetAll") from e
            out = []
            for a in items or ():
                # Attribute metadata fields in 2026: ID (upper case), Code (the name used with AttValue), Name, ValueType, Editable, Category
                code = getattr(a, "Code", None)
                aid = getattr(a, "ID", None)
                if code is None and aid is None:
                    continue
                code = str(code if code is not None else aid)
                out.append({
                    "id": code,
                    "id_upper": str(aid) if aid is not None else None,
                    "name": getattr(a, "Name", None),
                    "value_type": _plain(getattr(a, "ValueType", None)),
                    "editable": getattr(a, "Editable", None),
                    "category": getattr(a, "Category", None),
                    "protected": (self._guard.check(type, "set", code) is not None
                                  or self._guard.check(type, "set", _base_id(code)) is not None),
                })
            return out

        attrs = self._com.submit(run)
        return {"type": type, "registered": r.registered, "attributes": attrs}

    # ------------------------------------------------------------ queries
    def list_objects(self, type: str, attrs: list[str] | None = None, name_contains: str | None = None,
                     where: dict[str, Any] | None = None, limit: int | None = None, offset: int = 0,
                     parent: Any = None) -> dict[str, Any]:
        """List of rows (no + requested attributes). Reading many objects takes one GetMultipleAttributes call (GetMultiAttValues if unavailable)."""
        r = self._resolve(type)
        want = list(attrs) if attrs else list(r.default_attrs)
        where = dict(where or {})
        if limit is None:
            limit = int(self._cfg.get("list_limit_default", 200))
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ToolError(KIND_BAD_ARGS, f"limit must be an integer >= 0 ({limit!r})")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ToolError(KIND_BAD_ARGS, f"offset must be an integer >= 0 ({offset!r})")
        parent_key = self._parse_parent(r, parent)
        prefix = "-".join(str(p) for p in parent_key)

        # Columns to read: the key attribute first, then requested and filter attributes (no duplicates). A single object has no key (some have no No either).
        single = r.key == rg.KEY_SINGLE
        cols = [] if single else [r.key_attr]
        for a in list(want) + (["Name"] if name_contains else []) + list(where):
            if a not in cols:
                cols.append(a)
        if r.filter_attr is not None and r.filter_attr[0] not in cols:
            cols.append(r.filter_attr[0])

        def run():
            v = self._vissim()
            if single:   # Read attribute by attribute; a failing attribute becomes None plus a warning (same as get_attributes)
                obj = self._walk(v, r, ())
                snap, warns = self._snapshot(obj, tuple(cols))
                return [[snap[c] for c in cols]], warns
            container = self._walk(v, r, parent_key)
            return self._fetch_rows(container, cols), []

        raw, warnings = self._com.submit(run)
        idx = {c: i for i, c in enumerate(cols)}
        needle = name_contains.lower() if name_contains else None
        rows = []
        for rec in raw:
            if r.filter_attr is not None:
                attr, want_val = r.filter_attr
                if bool(rec[idx[attr]]) != bool(want_val):
                    continue
            if needle is not None:
                name = rec[idx["Name"]]
                if name is None or needle not in str(name).lower():
                    continue
            if any(str(rec[idx[a]]) != str(val) for a, val in where.items()):
                continue
            if single:
                no = None
            elif r.key == rg.KEY_COMPOSITE:
                no = f"{prefix}-{rec[idx[r.key_attr]]}"
            else:
                no = rec[idx[r.key_attr]]
            row = {"no": no}
            for a in want:
                row[a] = rec[idx[a]]
            rows.append(row)
        total = len(rows)
        out = {"type": type, "total": total, "limit": limit, "offset": offset,
               "rows": rows[offset:offset + limit]}
        if warnings:
            out["warnings"] = warnings
        return out

    def get_attributes(self, type: str, no: Any = None, attrs: list[str] | None = None) -> dict[str, Any]:
        """Attribute dict of one object. If one attribute fails its value is None, with an entry in warnings."""
        r = self._resolve(type)
        key = self._parse_key(r, no)
        want = tuple(attrs) if attrs else tuple(r.default_attrs)

        def run():
            obj = self._find(self._vissim(), r, key)
            return self._snapshot(obj, want)

        values, warnings = self._com.submit(run)
        return {"type": type, "no": _key_text(key), "attributes": values, "warnings": warnings}

    # ------------------------------------------------------------ attribute changes
    def set_attributes(self, changes: list[dict[str, Any]], stop_on_error: bool = True) -> dict[str, Any]:
        """Apply the items in order. Protection and argument checks all happen before COM (a blocked item never reaches VISSIM).

        Each item carries before (value before applying) and after (value read again afterwards). On failure it
        stops (by default) and leaves what was already applied as it is (no rollback). The response is split into
        applied / failed / skipped, and ok is true only if everything succeeded.
        """
        if not isinstance(changes, list) or not changes:
            raise ToolError(KIND_BAD_ARGS, "changes must be a non-empty list")
        self._require_idle()

        # 1) Checks before COM: parse each item and decide protection
        plan: list[dict[str, Any]] = []
        for i, ch in enumerate(changes):
            entry: dict[str, Any] = {"index": i, "type": None, "no": None, "attr": None, "value": None,
                                     "before": None, "after": None}
            try:
                if not isinstance(ch, dict):
                    raise ToolError(KIND_BAD_ARGS, f"changes[{i}] must be an object")
                missing = [k for k in ("type", "attr", "value") if k not in ch]
                if missing:
                    raise ToolError(KIND_BAD_ARGS, f"changes[{i}]: missing required keys: {', '.join(missing)}")
                entry.update(type=ch["type"], no=ch.get("no"), attr=ch["attr"], value=ch["value"])
                if not isinstance(ch["attr"], str) or not ch["attr"]:
                    raise ToolError(KIND_BAD_ARGS, f"changes[{i}].attr must be a non-empty string")
                r = self._resolve_writable(ch["type"])
                key = self._parse_key(r, ch.get("no"))
                entry["no"] = _key_text(key)
                self._check_guard(r.name, "set", ch["attr"])
                entry["_r"], entry["_key"] = r, key
            except ToolError as e:
                entry["error"] = e.to_dict()
            plan.append(entry)

        applied: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        stopped = False
        for entry in plan:
            r, key = entry.pop("_r", None), entry.pop("_key", None)
            if "error" in entry:
                failed.append(entry)     # protection or argument error; COM was never reached
                continue
            if stopped:
                skipped.append(entry)
                continue
            attr, value = entry["attr"], entry["value"]

            def run(r=r, key=key, attr=attr, value=value):
                obj = self._find(self._vissim(), r, key)
                return self._set_one(obj, attr, value, f"{r.name} {_key_text(key)}")

            try:
                before, after = self._com.submit(run)
            except ToolError as e:
                entry["before"] = e.detail.get("before") if e.detail else None
                entry["error"] = e.to_dict()
                failed.append(entry)
                self._record("set", "set_attributes", r.name, key, attr, entry["before"], None, False,
                             error=e.message)
                if stop_on_error:
                    stopped = True
                continue
            entry["before"], entry["after"] = before, after
            applied.append(entry)
            self._state.note_write()
            self._record("set", "set_attributes", r.name, key, attr, before, after, True)

        return {"ok": not failed and not skipped, "applied": applied, "failed": failed, "skipped": skipped}

    # ------------------------------------------------------------ creation
    def _plan_create_args(self, r: _Resolved, params: dict[str, Any]) -> list[tuple[str, str, Any]]:
        """Check each signature argument as (name, kind, parsed input) before COM. no is filled in later."""
        sig = r.t.create
        plan: list[tuple[str, str, Any]] = []
        for a in sig.args:
            if a.kind == "no":
                plan.append((a.name, a.kind, None))
                continue
            if a.name not in params:
                raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument missing: {a.name} ({a.kind})")
            raw = params[a.name]
            try:
                if a.kind in ("link", "parking_lot"):
                    plan.append((a.name, a.kind, rg._to_int(r.name, raw, a.name)))
                elif a.kind == "lane":
                    # `lane` is a "link-lane" composite key. A separate `link` plus an integer `lane` is accepted too (contract: link, lane, pos)
                    if isinstance(raw, int) and not isinstance(raw, bool) and "link" in params:
                        raw = f"{rg._to_int(r.name, params['link'], 'link')}-{raw}"
                    plan.append((a.name, a.kind, rg.parse_key("Lane", raw)))
                elif a.kind == "pos":
                    plan.append((a.name, a.kind, float(raw)))
                elif a.kind == "int":
                    plan.append((a.name, a.kind, rg._to_int(r.name, raw, a.name)))
                elif a.kind == "wkt":
                    if not isinstance(raw, str) or not raw.strip():
                        raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}: must be a WKT string")
                    plan.append((a.name, a.kind, raw))
                elif a.kind == "list_of":
                    if not isinstance(raw, list):
                        raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}: must be a list")
                    rows = []
                    for j, row in enumerate(raw):
                        if not isinstance(row, dict):
                            raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}[{j}]: must be an object")
                        vals = []
                        for item in a.items:
                            if item not in row:
                                raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}[{j}]: {item} missing")
                            if item in _LIST_ITEM_CONTAINER:
                                vals.append(rg._to_int(r.name, row[item], item))
                            else:
                                vals.append(float(row[item]))
                        rows.append((vals, row))
                    plan.append((a.name, a.kind, rows))
                else:
                    raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}: unknown kind {a.kind!r}")
            except rg.KeyFormatError as e:
                raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}: {e}") from e
            except (TypeError, ValueError) as e:
                raise ToolError(KIND_BAD_ARGS, f"{r.name} create argument {a.name}: {raw!r} ({e})") from e
        return plan

    @staticmethod
    def _post_attrs(r: _Resolved, params: dict[str, Any]) -> tuple[list[tuple[str, Any]], list[str]]:
        """Per-type post-processing: turn the convenience keys of params into a list of VISSIM attributes (ID, value). Returns (list, warnings)."""
        out: list[tuple[str, Any]] = []
        warnings: list[str] = []
        name = r.name
        if name == "VehicleInput":
            if "volume" in params:
                out.append(("Volume(1)", params["volume"]))
            if "veh_comp" in params:
                out.append(("VehComp(1)", params["veh_comp"]))
        elif name in ("DesSpeedDecision", "ReducedSpeedArea"):
            if name == "ReducedSpeedArea" and "length" in params:
                out.append(("Length", params["length"]))
            speeds = params.get("speeds")
            if speeds is not None:
                if not isinstance(speeds, dict):
                    raise ToolError(KIND_BAD_ARGS, f"{name} speeds must be a {{vehicle class: desired speed distribution}} dict")
                for cls, distr in speeds.items():
                    out.append((f"DesSpeedDistr({rg._to_int(name, cls, 'speeds key')})", distr))
        elif name == "VehicleRouteStatic":
            if "rel_flow" in params:
                out.append(("RelFlow(1)", params["rel_flow"]))
        elif name == "ParkingLot":
            if "length" in params:
                out.append(("Length", params["length"]))
        elif name == "VehicleRoutingDecisionParking":
            # Park rate and parking duration distribution are per-time-interval attributes (attribute.xlsx: sub-specifier TimeInterval); set them on the first interval
            if "park_rate" in params:
                out.append(("ParkRate(1)", params["park_rate"]))
            if "park_dur" in params:
                out.append(("ParkDur(1)", params["park_dur"]))
        elif name == "DataCollectionMeasurement":
            if "points" in params:   # list of point numbers -> "1,2,3" (probe: the DataCollectionPoints attribute takes a comma-separated string)
                pts = params["points"]
                if not isinstance(pts, (list, tuple)):
                    raise ToolError(KIND_BAD_ARGS, f"{name} points must be a list of data collection point numbers")
                try:
                    out.append(("DataCollectionPoints", ",".join(str(rg._to_int(name, p, "points")) for p in pts)))
                except rg.KeyFormatError as e:
                    raise ToolError(KIND_BAD_ARGS, str(e)) from e
        if "name" in params:
            out.append(("Name", params["name"]))
        return out, warnings

    def create_object(self, type: str, params: dict[str, Any] | None = None, no: Any = None,
                      attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        """Interpret the registry create signature, call Add<Type>(...), then SetAttValue the post-processing values and attrs in order."""
        params = dict(params or {})
        attrs = dict(attrs or {})
        r = self._resolve_writable(type)
        if r.t.create is None:
            raise ToolError(KIND_BAD_ARGS, f"{type}: create form not verified; {r.t.create_note or 'no create'}")
        # All protection checks before COM
        self._check_guard(r.name, "create")
        post, warnings = self._post_attrs(r, params)
        for a in list(dict(post)) + list(attrs):
            self._check_guard(r.name, "set", a)
        self._require_idle()
        sig = r.t.create
        if not sig.verified:
            warnings.append(f"signature_verified=false: the {sig.method} signature was copied from the official example "
                            f"and has not been confirmed against real COM")
        plan = self._plan_create_args(r, params)
        parent_key: tuple[int, ...] = ()
        if r.parent_parts:
            missing = [p for p in r.parent_parts if p not in params]
            if missing:
                raise ToolError(KIND_BAD_ARGS, f"creating {r.name} requires parent keys: {', '.join(missing)}")
            parent_key = self._parse_parent(r, [params[p] for p in r.parent_parts])
        given_no = None
        if no is not None:
            try:
                given_no = rg._to_int(r.name, no, "no")
            except rg.KeyFormatError as e:
                raise ToolError(KIND_BAD_ARGS, str(e)) from e
        wants_veh_classes = r.name in _VEH_CLASS_DECISIONS and "veh_classes" in params

        def run():
            v = self._vissim()
            container = self._walk(v, r, parent_key)
            # no: the given value, otherwise the existing maximum + 1 (1 if empty)
            new_no = given_no
            if new_no is None:
                existing = self._fetch_rows(container, [r.key_attr])
                nums = [row[0] for row in existing if isinstance(row[0], (int, float)) and not isinstance(row[0], bool)]
                new_no = int(max(nums)) + 1 if nums else 1
            # Resolve arguments (find the referenced objects)
            args: list[Any] = []
            for name, kind, val in plan:
                if kind == "no":
                    args.append(new_no)
                elif kind == "link":
                    args.append(self._item_by_key(v.Net.Links, val, "Link"))
                elif kind == "parking_lot":
                    args.append(self._item_by_key(v.Net.ParkingLots, val, "ParkingLot"))
                elif kind == "lane":
                    link = self._item_by_key(v.Net.Links, val[0], "Link")
                    args.append(self._item_by_key(link.Lanes, val[1], f"Link {val[0]} Lane"))
                elif kind == "list_of":
                    flat: list[Any] = []
                    arg_items = next(a.items for a in sig.args if a.name == name)
                    for vals, _row in val:
                        for item, x in zip(arg_items, vals):
                            if item in _LIST_ITEM_CONTAINER:
                                cont = getattr(v.Net, _LIST_ITEM_CONTAINER[item])
                                flat.append(self._item_by_key(cont, x, item))
                            else:
                                flat.append(x)
                    args.append(flat)
                else:
                    args.append(val)
            method = getattr(container, sig.method, None)
            if not callable(method):
                raise ToolError(KIND_COM_ERROR, f"{r.container} has no {sig.method}")
            try:
                obj = method(*args)
            except ToolError:
                raise
            except Exception as e:   # noqa: BLE001
                raise self._com_error(e, f"{sig.method}({', '.join(str(a) for a in args)})") from e
            if obj is None:
                raise ToolError(KIND_COM_ERROR, f"{sig.method} did not return an object")
            try:
                actual_no = _plain(obj.AttValue(r.key_attr))
            except Exception:   # noqa: BLE001 — if No cannot be read, use the number we passed
                actual_no = new_no
            if isinstance(actual_no, (int, float)) and not isinstance(actual_no, bool):
                actual_no = int(actual_no)
            full_key = (*parent_key, actual_no) if parent_key else actual_no
            what = f"{r.name} {_key_text(full_key)}"
            applied: list[dict[str, Any]] = []
            run_warnings: list[str] = []

            def set_and_note(target, attr, value, label=None):
                before, after = self._set_one(target, attr, value, what)
                applied.append({"no": _key_text(full_key), "attr": label or attr, "value": value,
                                "before": before, "after": after})

            # Per-type post-processing. Even if it fails here the object already exists, so return it and let the caller record it and update counters
            post_error: ToolError | None = None
            try:
                self._post_process(obj, post, wants_veh_classes, params, plan, set_and_note, run_warnings, what)
            except ToolError as e:
                post_error = e
            snap, snap_warn = self._snapshot(obj, r.default_attrs)
            return full_key, snap, applied, run_warnings + snap_warn, post_error

        self._pending_attrs = attrs
        full_key, snap, applied, run_warnings, post_error = self._com.submit(run)
        self._pending_attrs = {}
        warnings.extend(run_warnings)
        self._state.note_write()
        self._record("create", "create_object", r.name, full_key, None, None, snap, True)
        if post_error is not None:
            raise ToolError(post_error.kind,
                            f"{r.name} {_key_text(full_key)} was created but post-processing failed: {post_error.message}",
                            {"no": _key_text(full_key), "applied": applied, **(post_error.detail or {})})
        return {"ok": True, "type": type, "no": _key_text(full_key), "attributes": snap, "applied": applied,
                "signature_verified": sig.verified, "warnings": warnings}

    def _post_process(self, obj, post, wants_veh_classes, params, plan, set_and_note, run_warnings, what) -> None:
        """Attribute writes right after creation (per-type post-processing, veh_classes, relative flows, attrs). Runs inside the COM thread."""
        for attr, value in post:
            set_and_note(obj, attr, value)
        if wants_veh_classes:
            # probe (2026 SP09): a static routing decision has AllVehTypes (bool) and VehClasses (comma-separated string).
            # A parking routing decision uses the same IDs (attribute.xlsx)
            classes = params["veh_classes"]
            set_and_note(obj, "AllVehTypes", False)
            set_and_note(obj, "VehClasses", ",".join(str(c) for c in classes))
        # VehicleComposition: relative flows
        for name, kind, val in plan:
            if kind == "list_of" and name == "relative_flows":
                try:
                    flows = list(obj.VehCompRelFlows.GetAll())
                except Exception as e:   # noqa: BLE001
                    raise self._com_error(e, f"{what} VehCompRelFlows.GetAll") from e
                for j, (_vals, row) in enumerate(val):
                    if "rel_flow" not in row:
                        continue
                    if j >= len(flows):
                        run_warnings.append(f"relative_flows[{j}]: no VehCompRelFlows item, so RelFlow was not applied")
                        continue
                    set_and_note(flows[j], "RelFlow", row["rel_flow"], label=f"VehCompRelFlows[{j + 1}].RelFlow")
        # The attrs dict is applied in order
        for attr, value in self._pending_attrs.items():
            set_and_note(obj, attr, value)

    # ------------------------------------------------------------ deletion
    def delete_object(self, type: str, no: Any = None) -> dict[str, Any]:
        """Return a snapshot (default_attrs) taken before deletion and delete with the registry remove method."""
        r = self._resolve_writable(type)
        if r.t.remove is None:
            raise ToolError(KIND_BAD_ARGS, f"{type}: no remove method is registered")
        self._check_guard(r.name, "delete")
        key = self._parse_key(r, no)
        self._require_idle()

        def run():
            v = self._vissim()
            if isinstance(key, tuple):
                container = self._walk(v, r, key[:-1])
            else:
                container = self._walk(v, r, ())
            obj = self._find(v, r, key)
            snap, warnings = self._snapshot(obj, r.default_attrs)
            method = getattr(container, r.t.remove, None)
            if not callable(method):
                raise ToolError(KIND_COM_ERROR, f"{r.container} has no {r.t.remove}")
            try:
                method(obj)
            except Exception as e:   # noqa: BLE001
                raise self._com_error(e, f"{r.t.remove}({_key_text(key)})") from e
            return snap, warnings

        snap, warnings = self._com.submit(run)
        self._state.note_write()
        self._record("delete", "delete_object", r.name, key, None, snap, None, True)
        return {"ok": True, "type": type, "no": _key_text(key), "before": snap, "warnings": warnings}
