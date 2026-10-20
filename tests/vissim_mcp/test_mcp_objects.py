"""Object layer: uses fake COM containers to check call order, before/after values, protection and logging for queries, attribute changes, creation and deletion."""
import pytest

from vissim_mcp import state as st
from vissim_mcp.changelog import ChangeLog
from vissim_mcp.comthread import ComThread
from vissim_mcp.errors import ToolError
from vissim_mcp.guard import Guard
from vissim_mcp.objects import Objects

PROTECTED = [
    {"type": "Link", "ops": ["create", "delete", "set"], "attrs": ["*"]},
    {"type": "Connector", "ops": ["create", "delete", "set"], "attrs": ["*"]},
    {"type": "LinkPolyPoint", "ops": ["set"], "attrs": ["*"]},
    {"type": "NetPara", "ops": ["set"],
     "attrs": ["RefPointNet*", "RefPointMap*", "UnitLen*", "TrafReg", "LinkGradBas"]},
]


class FakeComError(Exception):
    """Mimics pywintypes.com_error; its message must be passed up unchanged."""


class FakeAttr:
    """Attribute metadata item in 2026: ID (upper case), Code (AttValue name), Name, ValueType, Editable, Category."""
    def __init__(self, id_, value_type="int", editable=True):
        self.ID = id_.upper()
        self.Code = id_
        self.Name = id_ + " (long)"
        self.ValueType = value_type
        self.Editable = editable
        self.Category = ""


class FakeAttributes:
    def __init__(self, ids):
        self._ids = list(ids)

    def GetAll(self):
        return [FakeAttr(i) for i in self._ids]


class FakeObj:
    def __init__(self, fake, path, attrs, fail_set=()):
        self._fake, self._path, self._attrs = fake, path, dict(attrs)
        self._fail_set = set(fail_set)
        self.Attributes = FakeAttributes(self._attrs)

    def AttValue(self, k):
        self._fake.log.append((self._path, "get", k))
        if k not in self._attrs:
            raise FakeComError(f"AttValue failed: attribute '{k}' unknown on {self._path}")
        return self._attrs[k]

    def SetAttValue(self, k, v):
        self._fake.log.append((self._path, "set", k, v))
        if k in self._fail_set:
            raise FakeComError(f"SetAttValue failed: '{k}' rejected on {self._path}")
        self._attrs[k] = v


class FakeContainer:
    def __init__(self, fake, path, attr_ids=("No", "Name"), key_attr="No"):
        self._fake, self._path, self._key_attr = fake, path, key_attr
        self.items = {}
        self.Attributes = FakeAttributes(attr_ids)

    def ItemByKey(self, k):
        self._fake.log.append((self._path, "ItemByKey", k))
        if k not in self.items:
            raise FakeComError(f"ItemByKey failed: key {k!r} not found in {self._path}")
        return self.items[k]

    def GetAll(self):
        self._fake.log.append((self._path, "GetAll"))
        return list(self.items.values())

    def GetMultipleAttributes(self, attrs):
        self._fake.log.append((self._path, "GetMultipleAttributes", tuple(attrs)))
        return tuple(tuple(o._attrs.get(a) for a in attrs) for o in self.items.values())

    def GetMultiAttValues(self, attr):
        self._fake.log.append((self._path, "GetMultiAttValues", attr))
        return tuple((i + 1, o._attrs.get(attr)) for i, o in enumerate(self.items.values()))

    def add(self, key, **attrs):
        o = FakeObj(self._fake, f"{self._path}[{key}]", {self._key_attr: key, **attrs})
        self.items[key] = o
        return o


class FakeVissim:
    """A VISSIM that mimics the container paths of the registry."""

    def __init__(self):
        self.log = []
        self.Net = type("Net", (), {})()
        n = self.Net
        n.Links = FakeContainer(self, "Links", ("No", "Name", "NumLanes", "Length2D", "IsConn"))
        for no, is_conn, name in ((10001, False, "Main St NB"), (10002, False, "Main St SB"), (10003, True, "")):
            lk = n.Links.add(no, Name=name, NumLanes=2, Length2D=500.0, IsConn=is_conn)
            lk.Lanes = FakeContainer(self, f"Links[{no}].Lanes", ("Index", "Width", "BlockedVehClasses"), "Index")
            for i in (1, 2):
                lk.Lanes.add(i, Width=3.5, BlockedVehClasses="")
            lk.LinkPolyPts = FakeContainer(self, f"Links[{no}].LinkPolyPts", ("Index", "X", "Y"), "Index")
            lk.LinkPolyPts.add(1, X=0.0, Y=0.0)
        n.VehicleTypes = FakeContainer(self, "VehicleTypes")
        n.VehicleTypes.add(100, Name="Car")
        n.VehicleTypes.add(200, Name="HGV")
        n.SpeedDistributions = FakeContainer(self, "SpeedDistributions")   # 2026 container name
        n.SpeedDistributions.add(40, Name="40")
        n.SpeedDistributions.add(100, Name="100")
        n.VehicleInputs = FakeContainer(self, "VehicleInputs", ("No", "Name", "Link", "Volume(1)", "VehComp(1)"))
        n.VehicleInputs.add(1, Name="EntranceA", Link=10001, **{"Volume(1)": 1200, "VehComp(1)": 1})
        n.VehicleInputs.add(2, Name="EntranceB", Link=10002, **{"Volume(1)": 800, "VehComp(1)": 1})
        n.VehicleInputs.add(5, Name="ramp", Link=10002, **{"Volume(1)": 300, "VehComp(1)": 1})
        n.VehicleCompositions = FakeContainer(self, "VehicleCompositions")
        n.DesSpeedDecisions = FakeContainer(self, "DesSpeedDecisions", ("No", "Name", "Lane", "Pos"))
        n.ReducedSpeedAreas = FakeContainer(self, "ReducedSpeedAreas", ("No", "Name", "Lane", "Pos", "Length"))
        n.QueueCounters = FakeContainer(self, "QueueCounters", ("No", "Name", "Link", "Pos"))
        n.QueueCounters.add(7, Name="q", Link=10001, Pos=100.0)
        n.VehicleRoutingDecisionsStatic = FakeContainer(self, "VRDS", ("No", "Name", "Link", "Pos", "AllVehTypes", "VehClasses"))
        d = n.VehicleRoutingDecisionsStatic.add(1, Name="Branch", Link=10001, Pos=50.0, AllVehTypes=True)
        d.VehRoutSta = FakeContainer(self, "VRDS[1].VehRoutSta", ("No", "Name", "DestLink", "DestPos", "RelFlow(1)"))
        d.VehRoutSta.add(1, Name="", DestLink=10002, DestPos=400.0, **{"RelFlow(1)": 1.0})
        # Parking: group 1, parking lot 1 (2 parking spaces), parking routing decision 1 (route 1 -> parking lot 1)
        n.ParkLotGrps = FakeContainer(self, "ParkLotGrps")
        n.ParkLotGrps.add(1, Name="Floor 1")
        n.ParkingLots = FakeContainer(self, "ParkingLots", ("No", "Name", "Type", "Lane", "Pos", "Length", "Capacity",
                                                            "Attrac", "InitOccup", "EvalGrps"))
        lot = n.ParkingLots.add(1, Name="A", Type="REALPARKINGSPACES", Lane="10001-1", Pos=300.0, Length=10.0,
                                Capacity=2, Attrac=0.0, InitOccup=0, EvalGrps="1")
        lot.ParkSpcs = FakeContainer(self, "ParkingLots[1].ParkSpcs", ("Index", "ParkingLot"), "Index")
        for i in (1, 2):
            lot.ParkSpcs.add(i, ParkingLot=1)
        n.VehicleRoutingDecisionsParking = FakeContainer(self, "VRDP", ("No", "Name", "Link", "Pos", "AllVehTypes",
                                                                        "VehClasses"))
        dp = n.VehicleRoutingDecisionsParking.add(1, Name="Entry", Link=10001, Pos=20.0, AllVehTypes=True,
                                                  VehClasses="")
        dp.VehRoutPark = FakeContainer(self, "VRDP[1].VehRoutPark", ("No", "Name", "ParkLot", "RelFlowSameParkLot"))
        dp.VehRoutPark.add(1, Name="", ParkLot=1, RelFlowSameParkLot=1.0)
        n.NetPara = FakeObj(self, "NetPara", {"RefPointNetX": 0.0, "RefPointNetY": 0.0, "UnitLenShort": 0,
                                               "Name": "net"})
        # Network performance measurement: every attribute is a result metric, so only names with specifiers can be read (bare names and No are rejected, attribute.xlsx)
        n.VehicleNetworkPerformanceMeasurement = FakeObj(self, "VehNetPerf", {
            "DelayAvg(Current,Last,All)": 12.5, "SpeedAvg(Current,Last,All)": 88.0,
            "TravTmTot(Current,Last,All)": 3600.0, "DistTot(Current,Last,All)": 90000.0,
            "DelayTot(Current,Last,All)": 450.0})
        # Container not in the registry (read-only)
        n.SignalControllers = FakeContainer(self, "SignalControllers")
        n.SignalControllers.add(3, Name="SC3")
        self._wire_add_remove()

    def _wire_add_remove(self):
        n, fake = self.Net, self

        def add_vehicle_input(no, link):
            fake.log.append(("VehicleInputs", "AddVehicleInput", no, link))
            return n.VehicleInputs.add(no, Name="", Link=link._attrs["No"], **{"Volume(1)": 0, "VehComp(1)": 1})

        def add_des_speed_decision(no, lane, pos):
            fake.log.append(("DesSpeedDecisions", "AddDesSpeedDecision", no, lane, pos))
            o = n.DesSpeedDecisions.add(no, Name="", Lane=lane._path, Pos=pos)
            o._attrs["DesSpeedDistr(10)"] = None
            o._attrs["DesSpeedDistr(20)"] = None
            return o

        def add_reduced_speed_area(no, lane, pos):
            fake.log.append(("ReducedSpeedAreas", "AddReducedSpeedArea", no, lane, pos))
            o = n.ReducedSpeedAreas.add(no, Name="", Lane=lane._path, Pos=pos, Length=10.0)
            o._attrs["DesSpeedDistr(10)"] = None
            return o

        def add_vehicle_composition(no, flat):
            fake.log.append(("VehicleCompositions", "AddVehicleComposition", no, list(flat)))
            o = n.VehicleCompositions.add(no, Name="")
            o.VehCompRelFlows = FakeContainer(fake, f"VehicleCompositions[{no}].VehCompRelFlows", ("RelFlow",))
            for i in range(0, len(flat), 2):
                o.VehCompRelFlows.add(i // 2 + 1, RelFlow=1.0, VehType=flat[i]._attrs["No"],
                                      DesSpeedDistr=flat[i + 1]._attrs["No"])
            return o

        def add_queue_counter(no, link, pos):
            fake.log.append(("QueueCounters", "AddQueueCounter", no, link, pos))
            return n.QueueCounters.add(no, Name="", Link=link._attrs["No"], Pos=pos)

        def add_vrds(no, link, pos):
            fake.log.append(("VRDS", "AddVehicleRoutingDecisionStatic", no, link, pos))
            o = n.VehicleRoutingDecisionsStatic.add(no, Name="", Link=link._attrs["No"], Pos=pos, AllVehTypes=True,
                                                    VehClasses="")
            o.VehRoutSta = FakeContainer(fake, f"VRDS[{no}].VehRoutSta", ("No", "Name", "DestLink", "DestPos", "RelFlow(1)"))
            return o

        def add_route(decision):
            def _add(no, dest_link, dest_pos):
                fake.log.append((f"VRDS[{decision}].VehRoutSta", "AddVehicleRouteStatic", no, dest_link, dest_pos))
                return n.VehicleRoutingDecisionsStatic.items[decision].VehRoutSta.add(
                    no, Name="", DestLink=dest_link._attrs["No"], DestPos=dest_pos, **{"RelFlow(1)": 1.0})
            return _add

        def add_parking_lot_group(no):
            fake.log.append(("ParkLotGrps", "AddParkingLotGroup", no))
            return n.ParkLotGrps.add(no, Name="")

        def add_parking_lot(no, lane, pos):
            fake.log.append(("ParkingLots", "AddParkingLot", no, lane, pos))
            o = n.ParkingLots.add(no, Name="", Type="REALPARKINGSPACES", Lane=lane._path, Pos=pos, Length=0.0,
                                  Capacity=0, Attrac=0.0, InitOccup=0, EvalGrps="")
            o.ParkSpcs = FakeContainer(fake, f"ParkingLots[{no}].ParkSpcs", ("Index", "ParkingLot"), "Index")
            return o

        def add_vrdp(no, link, pos):
            fake.log.append(("VRDP", "AddVehicleRoutingDecisionParking", no, link, pos))
            o = n.VehicleRoutingDecisionsParking.add(no, Name="", Link=link._attrs["No"], Pos=pos, AllVehTypes=True,
                                                     VehClasses="", **{"ParkRate(1)": 0.0, "ParkDur(1)": None})
            o.VehRoutPark = FakeContainer(fake, f"VRDP[{no}].VehRoutPark", ("No", "Name", "ParkLot"))
            return o

        def add_park_route(decision):
            def _add(no, lot):
                fake.log.append((f"VRDP[{decision}].VehRoutPark", "AddVehicleRouteParking", no, lot))
                return n.VehicleRoutingDecisionsParking.items[decision].VehRoutPark.add(
                    no, Name="", ParkLot=lot._attrs["No"], RelFlowSameParkLot=1.0)
            return _add

        def remover(container, name):
            def _remove(obj):
                fake.log.append((container._path, name, obj._path))
                key = obj._attrs[container._key_attr]
                del container.items[key]
            return _remove

        n.VehicleInputs.AddVehicleInput = add_vehicle_input
        n.VehicleInputs.RemoveVehicleInput = remover(n.VehicleInputs, "RemoveVehicleInput")
        n.DesSpeedDecisions.AddDesSpeedDecision = add_des_speed_decision
        n.DesSpeedDecisions.RemoveDesSpeedDecision = remover(n.DesSpeedDecisions, "RemoveDesSpeedDecision")
        n.ReducedSpeedAreas.AddReducedSpeedArea = add_reduced_speed_area
        n.VehicleCompositions.AddVehicleComposition = add_vehicle_composition
        n.VehicleCompositions.RemoveVehicleComposition = remover(n.VehicleCompositions, "RemoveVehicleComposition")
        n.QueueCounters.AddQueueCounter = add_queue_counter
        n.QueueCounters.RemoveQueueCounter = remover(n.QueueCounters, "RemoveQueueCounter")
        n.VehicleRoutingDecisionsStatic.AddVehicleRoutingDecisionStatic = add_vrds
        vrs = n.VehicleRoutingDecisionsStatic.items[1].VehRoutSta
        vrs.AddVehicleRouteStatic = add_route(1)
        vrs.RemoveVehicleRouteStatic = remover(vrs, "RemoveVehicleRouteStatic")
        n.Links.RemoveLink = remover(n.Links, "RemoveLink")
        n.ParkLotGrps.AddParkingLotGroup = add_parking_lot_group
        n.ParkLotGrps.RemoveParkingLotGroup = remover(n.ParkLotGrps, "RemoveParkingLotGroup")
        n.ParkingLots.AddParkingLot = add_parking_lot
        n.ParkingLots.RemoveParkingLot = remover(n.ParkingLots, "RemoveParkingLot")
        n.VehicleRoutingDecisionsParking.AddVehicleRoutingDecisionParking = add_vrdp
        vrp = n.VehicleRoutingDecisionsParking.items[1].VehRoutPark
        vrp.AddVehicleRouteParking = add_park_route(1)
        vrp.RemoveVehicleRouteParking = remover(vrp, "RemoveVehicleRouteParking")

    # ---- check helpers ----
    def calls(self, *ops):
        return [e for e in self.log if e[1] in ops]

    def touched(self, path_prefix):
        return [e for e in self.log if str(e[0]).startswith(path_prefix)]


class Env:
    def __init__(self, tmp_path):
        self.vissim = FakeVissim()
        self.cfg = {"list_limit_default": 200}
        self.state = st.ServerState(port=8766)
        self.state.set_network("C:/runs/x/network.inpx")
        self.com = ComThread(retry_count=0, sleep=lambda s: None)
        self.com.start()
        self.changelog = ChangeLog(tmp_path / "mcp")
        self.guard = Guard(PROTECTED)
        self.getter_calls = 0

        def getter():
            self.getter_calls += 1
            return self.vissim
        self.objects = Objects(self.cfg, self.state, self.com, self.changelog, self.guard, getter)

    def close(self):
        self.com.stop()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def _err(exc_info):
    return exc_info.value.kind, str(exc_info.value)


# ---------------------------------------------------------------- type and attribute descriptions

def test_list_object_types_returns_the_registry_and_a_protection_summary(env):
    r = env.objects.list_object_types()
    by = {t["type"]: t for t in r["types"]}
    assert by["VehicleInput"]["container"] == "Net.VehicleInputs"
    assert by["VehicleInput"]["create"]["method"] == "AddVehicleInput"
    assert by["VehicleInput"]["create"]["verified"] is True   # confirmed with the probe (2026 SP09)
    assert [a["kind"] for a in by["VehicleInput"]["create"]["args"]] == ["no", "link"]
    assert by["VehicleInput"]["protected"] == {"create": False, "delete": False, "set": False}
    assert by["Link"]["protected"] == {"create": True, "delete": True, "set": True}
    assert by["NetPara"]["protected"]["set"] is True and by["NetPara"]["key"] == "single"
    assert by["Lane"]["create"] is None and by["Lane"]["key_parts"] == ["link", "lane"]
    assert by["DataCollectionMeasurement"]["create"]["method"] == "AddDataCollectionMeasurement"
    assert by["DataCollectionMeasurement"]["create"]["post"] == ["DataCollectionPoints"]
    assert env.getter_calls == 0   # does not call COM


def test_describe_attributes_uses_GetAll_and_shows_protection(env):
    r = env.objects.describe_attributes("VehicleInput")
    by = {a["id"]: a for a in r["attributes"]}
    assert set(by) == {"No", "Name", "Link", "Volume(1)", "VehComp(1)"}
    assert by["Name"]["protected"] is False
    assert by["Name"]["name"] == "Name (long)" and by["Name"]["id_upper"] == "NAME"
    assert by["Name"]["value_type"] == "int" and by["Name"]["editable"] is True and by["Name"]["category"] == ""
    r = env.objects.describe_attributes("Link")
    by = {a["id"]: a for a in r["attributes"]}
    assert by["NumLanes"]["protected"] is True and by["Length2D"]["protected"] is True
    # VISSIM 2026 gives no attribute list for single objects (NetPara etc.) -> not_found plus a hint
    with pytest.raises(ToolError) as ei:
        env.objects.describe_attributes("NetPara")
    assert ei.value.kind == "not_found" and "attribute.xlsx" in ei.value.message


def test_describe_attributes_missing_fields_are_None(env):
    class Bare:
        ID = "X"
    env.vissim.Net.QueueCounters.Attributes.GetAll = lambda: [Bare()]
    r = env.objects.describe_attributes("QueueCounter")
    a = r["attributes"][0]
    assert a["id"] == "X" and a["name"] is None and a["value_type"] is None and a["editable"] is None


def test_describe_attributes_composite_key_selects_the_parent_via_parent(env):
    r = env.objects.describe_attributes("Lane", parent="10001")
    assert {a["id"] for a in r["attributes"]} == {"Index", "Width", "BlockedVehClasses"}
    with pytest.raises(ToolError) as ei:
        env.objects.describe_attributes("Lane")
    assert ei.value.kind == "bad_args"


# ---------------------------------------------------------------- listing and queries

def test_list_objects_makes_one_GetMultipleAttributes_call(env):
    r = env.objects.list_objects("VehicleInput")
    assert len(env.vissim.calls("GetMultipleAttributes")) == 1
    assert env.vissim.calls("get") == []    # no AttValue loop per object
    assert r["total"] == 3 and [row["no"] for row in r["rows"]] == [1, 2, 5]
    assert set(r["rows"][0]) == {"no", "No", "Name", "Link", "Volume(1)", "VehComp(1)"}
    assert r["rows"][0]["Volume(1)"] == 1200


def test_list_objects_requested_attributes_only(env):
    r = env.objects.list_objects("VehicleInput", attrs=["Name"])
    assert set(r["rows"][0]) == {"no", "Name"}
    call = env.vissim.calls("GetMultipleAttributes")[0]
    assert call[2][0] == "No"    # the first column is always No


def test_list_objects_name_contains_where_limit_offset_total(env):
    r = env.objects.list_objects("VehicleInput", name_contains="Entrance")
    assert r["total"] == 2 and [row["no"] for row in r["rows"]] == [1, 2]
    r = env.objects.list_objects("VehicleInput", name_contains="RAMP")   # case-insensitive
    assert [row["no"] for row in r["rows"]] == [5]
    r = env.objects.list_objects("VehicleInput", where={"Link": "10002"})
    assert r["total"] == 2 and [row["no"] for row in r["rows"]] == [2, 5]
    r = env.objects.list_objects("VehicleInput", where={"Link": 10002}, name_contains="ramp")
    assert [row["no"] for row in r["rows"]] == [5]
    r = env.objects.list_objects("VehicleInput", limit=1, offset=1)
    assert r["total"] == 3 and [row["no"] for row in r["rows"]] == [2]
    assert r["limit"] == 1 and r["offset"] == 1


def test_list_objects_default_limit_comes_from_config(env):
    env.cfg["list_limit_default"] = 2
    r = env.objects.list_objects("VehicleInput")
    assert r["total"] == 3 and len(r["rows"]) == 2 and r["limit"] == 2


def test_list_objects_links_and_connectors_are_split_by_IsConn(env):
    r = env.objects.list_objects("Link", attrs=["Name"])
    assert [row["no"] for row in r["rows"]] == [10001, 10002]
    r = env.objects.list_objects("Connector", attrs=["Name"])
    assert [row["no"] for row in r["rows"]] == [10003]
    assert "IsConn" not in r["rows"][0]


def test_list_objects_falls_back_to_GetMultiAttValues_without_GetMultipleAttributes(env):
    env.vissim.Net.VehicleInputs.GetMultipleAttributes = None   # pretend this container has no fast read
    r = env.objects.list_objects("VehicleInput", attrs=["Name"])
    assert [row["Name"] for row in r["rows"]] == ["EntranceA", "EntranceB", "ramp"]
    assert [e[2] for e in env.vissim.calls("GetMultiAttValues")] == ["No", "Name"]
    assert env.vissim.calls("get") == []


def test_list_objects_composite_key_lists_items_under_parent_as_composite_key_strings(env):
    r = env.objects.list_objects("Lane", parent="10001")
    assert [row["no"] for row in r["rows"]] == ["10001-1", "10001-2"]
    assert r["rows"][0]["Width"] == 3.5
    with pytest.raises(ToolError) as ei:
        env.objects.list_objects("Lane")
    assert ei.value.kind == "bad_args"


def test_list_objects_reads_an_unregistered_type_from_the_Net_container(env):
    r = env.objects.list_objects("SignalControllers")
    assert r["total"] == 1 and r["rows"][0]["no"] == 3 and r["rows"][0]["Name"] == "SC3"
    with pytest.raises(ToolError) as ei:
        env.objects.list_objects("Nope")
    assert ei.value.kind == "not_found"


def test_get_attributes_default_attributes_and_a_failed_attribute_gives_null_and_a_warning(env):
    r = env.objects.get_attributes("VehicleInput", 1)
    assert r["no"] == 1 and r["attributes"]["Name"] == "EntranceA" and r["attributes"]["Volume(1)"] == 1200
    assert r["warnings"] == []
    r = env.objects.get_attributes("VehicleInput", "1", attrs=["Name", "missing_attr"])
    assert r["attributes"] == {"Name": "EntranceA", "missing_attr": None}
    assert len(r["warnings"]) == 1 and "missing_attr" in r["warnings"][0]


def test_get_attributes_missing_object_is_not_found(env):
    with pytest.raises(ToolError) as ei:
        env.objects.get_attributes("VehicleInput", 99)
    kind, msg = _err(ei)
    assert kind == "not_found" and "99" in msg


def test_get_attributes_composite_key_and_single_object(env):
    r = env.objects.get_attributes("Lane", "10001-2")
    assert r["no"] == "10001-2" and r["attributes"]["Index"] == 2
    r = env.objects.get_attributes("VehicleRouteStatic", "1-1")
    assert r["no"] == "1-1" and r["attributes"]["DestLink"] == 10002
    r = env.objects.get_attributes("NetPara", None, attrs=["RefPointNetX", "Name"])
    assert r["no"] is None and r["attributes"] == {"RefPointNetX": 0.0, "Name": "net"}
    with pytest.raises(ToolError) as ei:
        env.objects.get_attributes("Lane", "10001")
    assert ei.value.kind == "bad_args"


def test_reads_are_allowed_while_the_simulation_runs(env):
    env.state.set_simulation(state=st.SIM_RUNNING)
    assert env.objects.get_attributes("VehicleInput", 1)["attributes"]["Name"] == "EntranceA"
    assert env.objects.list_objects("VehicleInput")["total"] == 3


# ---------------------------------------------------------------- attribute changes

def test_set_attributes_before_after_values_and_log(env):
    r = env.objects.set_attributes([{"type": "VehicleInput", "no": 1, "attr": "Volume(1)", "value": 1500}])
    assert r["ok"] is True
    assert len(r["applied"]) == 1 and r["failed"] == [] and r["skipped"] == []
    item = r["applied"][0]
    assert item["before"] == 1200 and item["after"] == 1500 and item["no"] == 1
    # read before applying -> write -> read again
    assert env.vissim.touched("VehicleInputs[1]") == [
        ("VehicleInputs[1]", "get", "Volume(1)"),
        ("VehicleInputs[1]", "set", "Volume(1)", 1500),
        ("VehicleInputs[1]", "get", "Volume(1)"),
    ]
    assert env.state.snapshot()["network"]["unsaved_writes"] == 1
    rows = env.changelog.read()
    assert len(rows) == 1
    assert rows[0]["kind"] == "set" and rows[0]["tool"] == "set_attributes"
    assert rows[0]["type"] == "VehicleInput" and rows[0]["no"] == 1 and rows[0]["attr"] == "Volume(1)"
    assert rows[0]["before"] == 1200 and rows[0]["after"] == 1500 and rows[0]["ok"] is True
    assert rows[0]["network_path"] == "C:/runs/x/network.inpx"


def test_set_attributes_after_is_the_value_read_again(env):
    vi = env.vissim.Net.VehicleInputs.items[1]
    orig = vi.SetAttValue

    def rounding_set(k, v):
        orig(k, round(v))    # pretend VISSIM stored a changed value
    vi.SetAttValue = rounding_set
    r = env.objects.set_attributes([{"type": "VehicleInput", "no": 1, "attr": "Volume(1)", "value": 1500.7}])
    assert r["applied"][0]["after"] == 1501 and r["applied"][0]["value"] == 1500.7


def test_set_attributes_rejects_only_protected_items_which_never_reach_COM(env):
    r = env.objects.set_attributes([
        {"type": "VehicleInput", "no": 1, "attr": "Name", "value": "A"},
        {"type": "NetPara", "no": None, "attr": "RefPointNetX", "value": 1.0},
        {"type": "Link", "no": 10001, "attr": "Name", "value": "x"},
        {"type": "VehicleInput", "no": 2, "attr": "Name", "value": "B"},
    ])
    assert r["ok"] is False
    assert [i["index"] for i in r["applied"]] == [0, 3]
    assert [i["index"] for i in r["failed"]] == [1, 2]
    assert all(i["error"]["kind"] == "protected" for i in r["failed"])
    assert "RefPointNetX" in r["failed"][0]["error"]["message"]
    assert r["skipped"] == []
    assert env.vissim.touched("NetPara") == []
    assert env.vissim.touched("Links") == []
    assert env.vissim.Net.VehicleInputs.items[2]._attrs["Name"] == "B"
    assert [row["no"] for row in env.changelog.read()] == [1, 2]


def test_set_attributes_protection_check_also_matches_the_ID_before_the_parenthesis(env):
    g = Guard([{"type": "VehicleInput", "ops": ["set"], "attrs": ["Volume"]}])
    env.objects = Objects(env.cfg, env.state, env.com, env.changelog, g, lambda: env.vissim)
    r = env.objects.set_attributes([{"type": "VehicleInput", "no": 1, "attr": "Volume(1)", "value": 1}])
    assert r["failed"][0]["error"]["kind"] == "protected"
    assert env.vissim.touched("VehicleInputs") == []


def test_set_attributes_second_failure_keeps_the_first_and_skips_the_rest(env):
    env.vissim.Net.VehicleInputs.items[2]._fail_set.add("Name")
    r = env.objects.set_attributes([
        {"type": "VehicleInput", "no": 1, "attr": "Name", "value": "A"},
        {"type": "VehicleInput", "no": 2, "attr": "Name", "value": "B"},
        {"type": "VehicleInput", "no": 5, "attr": "Name", "value": "C"},
    ])
    assert r["ok"] is False
    assert [i["index"] for i in r["applied"]] == [0]
    assert [i["index"] for i in r["failed"]] == [1]
    assert r["failed"][0]["error"]["kind"] == "com_error"
    assert "SetAttValue failed: 'Name' rejected" in r["failed"][0]["error"]["message"]   # COM message unchanged
    assert r["failed"][0]["before"] == "EntranceB"
    assert [i["index"] for i in r["skipped"]] == [2]
    assert env.vissim.Net.VehicleInputs.items[1]._attrs["Name"] == "A"    # not rolled back
    assert env.vissim.Net.VehicleInputs.items[5]._attrs["Name"] == "ramp"
    assert env.vissim.touched("VehicleInputs[5]") == []
    rows = env.changelog.read()
    assert [(row["no"], row["ok"]) for row in rows] == [(1, True), (2, False)]
    assert "rejected" in rows[1]["error"]
    assert env.state.snapshot()["network"]["unsaved_writes"] == 1


def test_set_attributes_continues_when_stop_on_error_is_False(env):
    env.vissim.Net.VehicleInputs.items[2]._fail_set.add("Name")
    r = env.objects.set_attributes([
        {"type": "VehicleInput", "no": 1, "attr": "Name", "value": "A"},
        {"type": "VehicleInput", "no": 2, "attr": "Name", "value": "B"},
        {"type": "VehicleInput", "no": 5, "attr": "Name", "value": "C"},
    ], stop_on_error=False)
    assert [i["index"] for i in r["applied"]] == [0, 2]
    assert [i["index"] for i in r["failed"]] == [1] and r["skipped"] == []
    assert env.vissim.Net.VehicleInputs.items[5]._attrs["Name"] == "C"


def test_set_attributes_missing_object_is_a_not_found_item(env):
    r = env.objects.set_attributes([{"type": "VehicleInput", "no": 99, "attr": "Name", "value": "A"}])
    assert r["failed"][0]["error"]["kind"] == "not_found"
    assert r["failed"][0]["before"] is None


def test_set_attributes_composite_key_lane(env):
    r = env.objects.set_attributes([{"type": "Lane", "no": "10001-2", "attr": "BlockedVehClasses", "value": "20"}])
    assert r["ok"] is True
    item = r["applied"][0]
    assert item["no"] == "10001-2" and item["before"] == "" and item["after"] == "20"
    assert ("Links[10001].Lanes", "ItemByKey", 2) in env.vissim.log
    assert env.changelog.read()[0]["no"] == "10001-2"


def test_set_attributes_unregistered_and_read_only_types_are_bad_args(env):
    r = env.objects.set_attributes([
        {"type": "SignalControllers", "no": 3, "attr": "Name", "value": "x"},
        {"type": "LinkPolyPoint", "no": "10001-1", "attr": "X", "value": 1.0},
    ], stop_on_error=False)
    kinds = [i["error"]["kind"] for i in r["failed"]]
    assert kinds[0] == "bad_args"
    assert kinds[1] == "bad_args"    # read-only types are filtered out before the protection check
    assert env.vissim.log == []


def test_set_attributes_malformed_argument_items(env):
    r = env.objects.set_attributes([
        {"type": "VehicleInput", "no": "a", "attr": "Name", "value": "x"},
        {"type": "VehicleInput", "no": 1, "value": "x"},
    ], stop_on_error=False)
    assert [i["error"]["kind"] for i in r["failed"]] == ["bad_args", "bad_args"]
    assert env.vissim.log == []
    with pytest.raises(ToolError) as ei:
        env.objects.set_attributes([])
    assert ei.value.kind == "bad_args"


def test_set_attributes_while_the_simulation_runs_gives_sim_running(env):
    env.state.set_simulation(state=st.SIM_RUNNING)
    with pytest.raises(ToolError) as ei:
        env.objects.set_attributes([{"type": "VehicleInput", "no": 1, "attr": "Name", "value": "A"}])
    assert ei.value.kind == "sim_running"
    assert env.vissim.log == []
    env.state.set_simulation(state=st.SIM_STOPPING)
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("VehicleInput", {"link": 10001})
    assert ei.value.kind == "sim_running"
    with pytest.raises(ToolError) as ei:
        env.objects.delete_object("VehicleInput", 1)
    assert ei.value.kind == "sim_running"
    assert env.vissim.log == []


# ---------------------------------------------------------------- creation

def test_create_VehicleInput_signature_order_and_post_processing(env):
    r = env.objects.create_object("VehicleInput", {"link": 10002, "volume": 900, "veh_comp": 1, "name": "New entrance"},
                                  no=9)
    assert r["ok"] is True and r["no"] == 9 and r["type"] == "VehicleInput"
    assert r["signature_verified"] is True and not any("signature" in w for w in r["warnings"])
    add = env.vissim.calls("AddVehicleInput")[0]
    assert add[2] == 9 and add[3] is env.vissim.Net.Links.items[10002]
    sets = [(e[2], e[3]) for e in env.vissim.touched("VehicleInputs[9]") if e[1] == "set"]
    assert sets == [("Volume(1)", 900), ("VehComp(1)", 1), ("Name", "New entrance")]
    assert r["attributes"]["No"] == 9 and r["attributes"]["Volume(1)"] == 900 and r["attributes"]["Name"] == "New entrance"
    by = {a["attr"]: a for a in r["applied"]}
    assert by["Volume(1)"]["before"] == 0 and by["Volume(1)"]["after"] == 900
    rows = env.changelog.read()
    assert len(rows) == 1 and rows[0]["kind"] == "create" and rows[0]["no"] == 9
    assert rows[0]["after"]["Volume(1)"] == 900 and rows[0]["before"] is None
    assert env.state.snapshot()["network"]["unsaved_writes"] == 1


def test_create_without_no_uses_max_plus_1(env):
    r = env.objects.create_object("VehicleInput", {"link": 10001})
    assert r["no"] == 6
    r = env.objects.create_object("VehicleInput", {"link": 10001})
    assert r["no"] == 7
    r = env.objects.create_object("VehicleComposition", {"relative_flows": [{"veh_type": 100, "des_speed_distr": 40}]})
    assert r["no"] == 1    # 1 for an empty container


def test_create_DesSpeedDecision_lane_object_and_pos_then_DesSpeedDistr(env):
    r = env.objects.create_object("DesSpeedDecision", {"lane": "10001-2", "pos": 120.5, "speeds": {10: 100, 20: 40}})
    add = env.vissim.calls("AddDesSpeedDecision")[0]
    lane = env.vissim.Net.Links.items[10001].Lanes.items[2]
    assert add[2] == 1 and add[3] is lane and add[4] == 120.5
    sets = [(e[2], e[3]) for e in env.vissim.touched("DesSpeedDecisions[1]") if e[1] == "set"]
    assert sets == [("DesSpeedDistr(10)", 100), ("DesSpeedDistr(20)", 40)]
    assert r["attributes"]["Pos"] == 120.5


def test_create_ReducedSpeedArea_length(env):
    r = env.objects.create_object("ReducedSpeedArea", {"lane": "10002-1", "pos": 10, "length": 25.0, "speeds": {"10": 40}})
    sets = [(e[2], e[3]) for e in env.vissim.touched("ReducedSpeedAreas[1]") if e[1] == "set"]
    assert sets == [("Length", 25.0), ("DesSpeedDistr(10)", 40)]
    assert r["attributes"]["Length"] == 25.0


def test_list_objects_ReducedSpeedArea_default_attributes_are_read_in_one_call(env):
    """GetMultipleAttributes fails as a whole if even one column is rejected, so the default set must not contain Decel, which needs a specifier."""
    env.objects.create_object("ReducedSpeedArea", {"lane": "10002-1", "pos": 10, "length": 25.0})
    c = env.vissim.Net.ReducedSpeedAreas
    plain = c.GetMultipleAttributes

    def strict(attrs):   # VISSIM 2026 SP09 wording (probe of 2026-10-18)
        if "Decel" in attrs:
            raise FakeComError("The subattribute no. 1 of type 'Vehicle Class' of the attribute 'DECEL()' is invalid: "
                               "Sub-attribute not specified")
        return plain(attrs)
    c.GetMultipleAttributes = strict

    r = env.objects.list_objects("ReducedSpeedArea")
    assert r["total"] == 1 and r["rows"][0]["Length"] == 25.0
    with pytest.raises(ToolError) as ei:      # requesting the bare name directly gives com_error with the COM message unchanged
        env.objects.list_objects("ReducedSpeedArea", attrs=["Decel"])
    assert ei.value.kind == "com_error" and "Sub-attribute not specified" in str(ei.value)


def test_network_performance_default_attributes_are_read_with_specifiers(env):
    """VISSIM rejects bare names such as DelayAvg. The default set carries (Current,Last,All)."""
    r = env.objects.get_attributes("VehicleNetworkPerformanceMeasurement")
    assert r["warnings"] == [] and r["attributes"]["DelayAvg(Current,Last,All)"] == 12.5
    r = env.objects.list_objects("VehicleNetworkPerformanceMeasurement")
    assert r["total"] == 1 and "warnings" not in r
    assert r["rows"][0] == {"no": None, "DelayAvg(Current,Last,All)": 12.5, "SpeedAvg(Current,Last,All)": 88.0,
                            "TravTmTot(Current,Last,All)": 3600.0, "DistTot(Current,Last,All)": 90000.0,
                            "DelayTot(Current,Last,All)": 450.0}
    # a single object has no key column, so No is not read
    assert ("VehNetPerf", "get", "No") not in env.vissim.log


def test_list_objects_read_failure_of_a_single_object_is_reported_as_a_warning(env):
    """It does not silently return only None (same warnings as get_attributes)."""
    r = env.objects.list_objects("VehicleNetworkPerformanceMeasurement", attrs=["DelayAvg"])
    assert r["rows"] == [{"no": None, "DelayAvg": None}]
    assert len(r["warnings"]) == 1 and r["warnings"][0].startswith("DelayAvg: ")


def test_create_VehicleComposition_flat_list_and_RelFlow(env):
    r = env.objects.create_object("VehicleComposition", {
        "relative_flows": [{"veh_type": 100, "des_speed_distr": 100, "rel_flow": 0.9},
                           {"veh_type": 200, "des_speed_distr": 40, "rel_flow": 0.1}],
        "name": "Car90"}, no=3)
    add = env.vissim.calls("AddVehicleComposition")[0]
    vt, ds = env.vissim.Net.VehicleTypes.items, env.vissim.Net.SpeedDistributions.items
    assert add[2] == 3 and add[3] == [vt[100], ds[100], vt[200], ds[40]]
    flows = env.vissim.Net.VehicleCompositions.items[3].VehCompRelFlows.items
    assert flows[1]._attrs["RelFlow"] == 0.9 and flows[2]._attrs["RelFlow"] == 0.1
    assert r["no"] == 3 and r["attributes"]["Name"] == "Car90"
    by = {a["attr"]: a for a in r["applied"]}
    assert by["VehCompRelFlows[1].RelFlow"]["before"] == 1.0 and by["VehCompRelFlows[1].RelFlow"]["after"] == 0.9
    assert by["VehCompRelFlows[2].RelFlow"]["after"] == 0.1
    assert by["Name"]["after"] == "Car90"


def test_create_VehicleRoutingDecisionStatic_veh_classes(env):
    r = env.objects.create_object("VehicleRoutingDecisionStatic", {"link": 10001, "pos": 80, "veh_classes": [10, 20]})
    assert r["no"] == 2
    sets = [(e[2], e[3]) for e in env.vissim.touched("VRDS[2]") if e[1] == "set"]
    assert ("AllVehTypes", False) in sets
    assert ("VehClasses", "10,20") in sets                      # probe: comma-separated string


def test_create_VehicleRouteStatic_composite_parent_and_rel_flow(env):
    r = env.objects.create_object("VehicleRouteStatic", {"decision": 1, "dest_link": 10002, "dest_pos": 300, "rel_flow": 0.4})
    assert r["no"] == "1-2"
    add = env.vissim.calls("AddVehicleRouteStatic")[0]
    assert add[0] == "VRDS[1].VehRoutSta" and add[2] == 2 and add[3] is env.vissim.Net.Links.items[10002] and add[4] == 300.0
    sets = [(e[2], e[3]) for e in env.vissim.touched("VRDS[1].VehRoutSta[2]") if e[1] == "set"]
    assert sets == [("RelFlow(1)", 0.4)]
    assert env.changelog.read()[0]["no"] == "1-2"


def test_create_attrs_dict_is_applied_with_SetAttValue_in_order(env):
    r = env.objects.create_object("QueueCounter", {"link": 10001, "pos": 10}, attrs={"Name": "Queue", "Pos": 12.0})
    sets = [(e[2], e[3]) for e in env.vissim.touched("QueueCounters[8]") if e[1] == "set"]
    assert sets == [("Name", "Queue"), ("Pos", 12.0)]
    assert r["attributes"]["Pos"] == 12.0


def test_create_protected_type_gives_protected_without_COM(env):
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("Link", {"wkt": "LINESTRING(0 0, 1 1)", "lane_widths": [{"width": 3.5}]})
    assert ei.value.kind == "protected"
    assert env.vissim.log == []
    # if attrs contains a protected attribute, nothing is created at all
    g = Guard([{"type": "QueueCounter", "ops": ["set"], "attrs": ["Pos"]}])
    env.objects = Objects(env.cfg, env.state, env.com, env.changelog, g, lambda: env.vissim)
    env.vissim.log.clear()
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("QueueCounter", {"link": 10001, "pos": 1}, attrs={"Pos": 2})
    assert ei.value.kind == "protected"
    assert env.vissim.calls("AddQueueCounter") == []


def test_create_type_without_a_signature_is_bad_args(env):
    for name, params in (("Lane", {}), ("VehicleType", {}), ("Nope", {})):
        with pytest.raises(ToolError) as ei:
            env.objects.create_object(name, params)
        assert ei.value.kind == "bad_args", name
    assert env.vissim.log == []


def test_create_missing_and_malformed_arguments(env):
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("VehicleInput", {})
    assert ei.value.kind == "bad_args" and "link" in str(ei.value)
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("DesSpeedDecision", {"lane": "10001", "pos": 1})
    assert ei.value.kind == "bad_args"
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("DesSpeedDecision", {"lane": "10001-1", "pos": "far"})
    assert ei.value.kind == "bad_args"
    assert env.vissim.calls("AddVehicleInput", "AddDesSpeedDecision") == []


def test_create_missing_link_is_not_found(env):
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("VehicleInput", {"link": 77777})
    assert ei.value.kind == "not_found" and "77777" in str(ei.value)
    assert env.vissim.calls("AddVehicleInput") == []


def test_create_com_rejection_becomes_com_error_with_the_message_unchanged(env):
    def boom(no, link):
        raise FakeComError("AddVehicleInput: Invalid key 0x80020009")
    env.vissim.Net.VehicleInputs.AddVehicleInput = boom
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("VehicleInput", {"link": 10001})
    assert ei.value.kind == "com_error" and "Invalid key 0x80020009" in str(ei.value)
    assert env.changelog.read() == []
    assert env.state.snapshot()["network"]["unsaved_writes"] == 0


# ---------------------------------------------------------------- parking

def test_create_ParkingLot_lane_and_position_then_Length(env):
    r = env.objects.create_object("ParkingLot", {"link": 10002, "lane": 1, "pos": 250, "length": 30.0,
                                                 "name": "B1"})
    add = env.vissim.calls("AddParkingLot")[0]
    assert add[2] == 2 and add[3] is env.vissim.Net.Links.items[10002].Lanes.items[1] and add[4] == 250.0
    sets = [(e[2], e[3]) for e in env.vissim.touched("ParkingLots[2]") if e[1] == "set"]
    assert sets == [("Length", 30.0), ("Name", "B1")]
    assert r["no"] == 2 and r["attributes"]["Length"] == 30.0 and r["attributes"]["Type"] == "REALPARKINGSPACES"
    assert r["signature_verified"] is True   # confirmed with the probe (2026-10-15)


def test_create_ParkingLotGroup_number_only(env):
    r = env.objects.create_object("ParkingLotGroup", {"name": "Floor 2"})
    assert env.vissim.calls("AddParkingLotGroup") == [("ParkLotGrps", "AddParkingLotGroup", 2)]
    assert r["no"] == 2 and r["attributes"] == {"No": 2, "Name": "Floor 2"}


def test_create_VehicleRoutingDecisionParking_park_rate_park_duration_vehicle_classes(env):
    r = env.objects.create_object("VehicleRoutingDecisionParking",
                                  {"link": 10001, "pos": 5, "park_rate": 100.0, "park_dur": 3, "veh_classes": [10]})
    add = env.vissim.calls("AddVehicleRoutingDecisionParking")[0]
    assert add[2] == 2 and add[3] is env.vissim.Net.Links.items[10001] and add[4] == 5.0
    sets = [(e[2], e[3]) for e in env.vissim.touched("VRDP[2]") if e[1] == "set"]
    assert sets == [("ParkRate(1)", 100.0), ("ParkDur(1)", 3), ("AllVehTypes", False), ("VehClasses", "10")]
    assert r["no"] == 2


def test_create_VehicleRouteParking_passes_the_parking_lot_object(env):
    r = env.objects.create_object("VehicleRouteParking", {"decision": 1, "parking_lot": 1})
    add = env.vissim.calls("AddVehicleRouteParking")[0]
    assert add[0] == "VRDP[1].VehRoutPark" and add[2] == 2 and add[3] is env.vissim.Net.ParkingLots.items[1]
    assert r["no"] == "1-2" and r["attributes"]["ParkLot"] == 1


def test_create_VehicleRouteParking_missing_parking_lot_is_not_found_without_COM_creation(env):
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("VehicleRouteParking", {"decision": 1, "parking_lot": 99})
    assert ei.value.kind == "not_found" and "ParkingLot 99" in str(ei.value)
    assert env.vissim.calls("AddVehicleRouteParking") == []
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("VehicleRouteParking", {"decision": 1, "parking_lot": "A"})
    assert ei.value.kind == "bad_args"


def test_parking_spaces_are_read_only_composite_keys_under_a_parking_lot(env):
    r = env.objects.list_objects("ParkingSpace", parent=1)
    assert [row["no"] for row in r["rows"]] == ["1-1", "1-2"]
    assert env.objects.get_attributes("ParkingSpace", "1-2")["attributes"] == {"Index": 2, "ParkingLot": 1}
    out = env.objects.set_attributes([{"type": "ParkingSpace", "no": "1-1", "attr": "Index", "value": 3}])
    assert out["ok"] is False and out["failed"][0]["error"]["kind"] == "bad_args"
    with pytest.raises(ToolError) as ei:
        env.objects.create_object("ParkingSpace", {})
    assert ei.value.kind == "bad_args"


def test_parking_lot_attributes_batch_change(env):
    env.objects.create_object("ParkingLot", {"lane": "10001-2", "pos": 400})
    out = env.objects.set_attributes([
        {"type": "ParkingLot", "no": 1, "attr": "Attrac", "value": 100.0},
        {"type": "ParkingLot", "no": 2, "attr": "Attrac", "value": 90.0},
        {"type": "ParkingLot", "no": 2, "attr": "EvalGrps", "value": "1"},
    ])
    assert out["ok"] is True and [a["after"] for a in out["applied"]] == [100.0, 90.0, "1"]


def test_delete_parking_route_composite_key_and_parking_lot(env):
    r = env.objects.delete_object("VehicleRouteParking", "1-1")
    assert r["before"]["ParkLot"] == 1
    assert env.vissim.calls("RemoveVehicleRouteParking") == [
        ("VRDP[1].VehRoutPark", "RemoveVehicleRouteParking", "VRDP[1].VehRoutPark[1]")]
    r = env.objects.delete_object("ParkingLot", 1)
    assert r["before"]["Name"] == "A" and 1 not in env.vissim.Net.ParkingLots.items


# ---------------------------------------------------------------- deletion

def test_delete_snapshot_Remove_and_log(env):
    r = env.objects.delete_object("VehicleInput", 2)
    assert r["ok"] is True and r["no"] == 2
    assert r["before"] == {"No": 2, "Name": "EntranceB", "Link": 10002, "Volume(1)": 800, "VehComp(1)": 1}
    assert env.vissim.calls("RemoveVehicleInput") == [("VehicleInputs", "RemoveVehicleInput", "VehicleInputs[2]")]
    assert 2 not in env.vissim.Net.VehicleInputs.items
    rows = env.changelog.read()
    assert rows[0]["kind"] == "delete" and rows[0]["no"] == 2 and rows[0]["before"]["Name"] == "EntranceB"
    assert rows[0]["after"] is None
    assert env.state.snapshot()["network"]["unsaved_writes"] == 1


def test_delete_composite_key(env):
    r = env.objects.delete_object("VehicleRouteStatic", "1-1")
    assert r["no"] == "1-1" and r["before"]["DestLink"] == 10002
    assert env.vissim.calls("RemoveVehicleRouteStatic") == [
        ("VRDS[1].VehRoutSta", "RemoveVehicleRouteStatic", "VRDS[1].VehRoutSta[1]")]
    assert env.changelog.read()[0]["no"] == "1-1"


def test_delete_without_a_Remove_method_gives_com_error(env):
    env.vissim.Net.QueueCounters.RemoveQueueCounter = None
    with pytest.raises(ToolError) as ei:
        env.objects.delete_object("QueueCounter", 7)
    assert ei.value.kind == "com_error" and "RemoveQueueCounter" in str(ei.value)
    assert 7 in env.vissim.Net.QueueCounters.items


def test_delete_protected_types_and_types_without_delete(env):
    with pytest.raises(ToolError) as ei:
        env.objects.delete_object("Link", 10001)
    assert ei.value.kind == "protected"
    with pytest.raises(ToolError) as ei:
        env.objects.delete_object("Connector", 10003)
    assert ei.value.kind == "protected"
    for name, no in (("NetPara", None), ("Lane", "10001-1"), ("VehicleType", 100), ("Nope", 1)):
        with pytest.raises(ToolError) as ei:
            env.objects.delete_object(name, no)
        assert ei.value.kind == "bad_args", name
    assert env.vissim.log == []


def test_delete_missing_object_is_not_found(env):
    with pytest.raises(ToolError) as ei:
        env.objects.delete_object("VehicleInput", 99)
    assert ei.value.kind == "not_found"
    assert env.changelog.read() == []


def test_delete_com_rejection_gives_com_error(env):
    def boom(obj):
        raise FakeComError("RemoveVehicleInput: object in use")
    env.vissim.Net.VehicleInputs.RemoveVehicleInput = boom
    with pytest.raises(ToolError) as ei:
        env.objects.delete_object("VehicleInput", 1)
    assert ei.value.kind == "com_error" and "object in use" in str(ei.value)
    assert env.state.snapshot()["network"]["unsaved_writes"] == 0


# ---------------------------------------------------------------- common

def test_no_network_passes_the_getter_ToolError_through(env):
    def no_net():
        raise ToolError("no_network", "no open network")
    env.objects = Objects(env.cfg, env.state, env.com, env.changelog, env.guard, no_net)
    with pytest.raises(ToolError) as ei:
        env.objects.get_attributes("VehicleInput", 1)
    assert ei.value.kind == "no_network"


def test_COM_calls_run_on_the_COM_thread(env):
    import threading
    seen = []
    orig = env.vissim.Net.VehicleInputs.GetMultipleAttributes

    def spy(attrs):
        seen.append(threading.current_thread().name)
        return orig(attrs)
    env.vissim.Net.VehicleInputs.GetMultipleAttributes = spy
    env.objects.list_objects("VehicleInput")
    assert seen == ["vissim-com"]


def test_no_argument_switches_protection_off():
    import inspect
    for fn in (Objects.set_attributes, Objects.create_object, Objects.delete_object):
        params = set(inspect.signature(fn).parameters)
        assert not {"force", "unsafe", "skip_guard", "no_guard"} & params, fn.__name__
