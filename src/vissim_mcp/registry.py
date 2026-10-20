"""Registry: the VISSIM object types the MCP server handles, with container, key format, create signature,
remove method and default attributes.

Create signatures were copied from the official example (PTV Vissim 2026 "Examples Training/COM/Network Objects
Adding/Network Objects Adding.py") and then all confirmed on VISSIM 2026 SP09 with tools/com_probe.py --net
(verified=True, source=SOURCE_PROBE; the four parking create signatures use SOURCE_PROBE_PARKING). Re-check with the
probe when the VISSIM version changes. For an unregistered name get() returns None and the caller treats the type
as read-only.

Container path notation: "Net.Links" is Vissim.Net.Links; in "Net.Links[link].Lanes" the name in brackets is the
leading part of the composite key, i.e. Vissim.Net.Links.ItemByKey(link).Lanes.
"""
from __future__ import annotations

from dataclasses import dataclass

SOURCE_OFFICIAL = "official example"
SOURCE_PROBE = "probe-verified (VISSIM 2026 SP09, 2026-10-12)"
SOURCE_PROBE_PARKING = "probe-verified (VISSIM 2026 SP09, 2026-10-15)"   # four parking create signatures

# Kinds of create arguments
#   no      : object number (0 lets VISSIM pick a free number)
#   link    : link number -> Net.Links.ItemByKey(no)
#   lane    : "link-lane" -> Net.Links.ItemByKey(link).Lanes.ItemByKey(lane)
#   pos     : position on the link (m, float)
#   int     : integer (number of lanes etc.)
#   wkt     : 'LINESTRING(...)' string
#   list_of : list of objects (items gives the item kinds in order, e.g. (veh_type, des_speed_distr) repeated)
#   parking_lot : parking lot number -> Net.ParkingLots.ItemByKey(no)
ARG_KINDS = ("no", "link", "lane", "pos", "int", "wkt", "list_of", "parking_lot")

KEY_NO = "no"                # a single number
KEY_COMPOSITE = "composite"  # composite key "a-b"
KEY_SINGLE = "single"        # single object (no=null)


class KeyFormatError(ValueError):
    """Malformed key."""


@dataclass(frozen=True)
class Arg:
    name: str
    kind: str
    items: tuple[str, ...] = ()   # item kinds repeated for list_of


@dataclass(frozen=True)
class CreateSig:
    method: str
    args: tuple[Arg, ...]
    post: tuple[str, ...] = ()    # attributes / sub-items set separately after creation
    source: str = SOURCE_PROBE
    verified: bool = True


@dataclass(frozen=True)
class ObjType:
    name: str
    container: str
    key: str = KEY_NO
    key_parts: tuple[str, ...] = ()
    filter_attr: tuple[str, object] | None = None   # when one container is split by an attribute value
    create: CreateSig | None = None
    create_note: str = ""                             # why there is no create (none / unverified)
    remove: str | None = None
    default_attrs: tuple[str, ...] = ()
    result_kind: str | None = None
    read_only: bool = False


# Attribute IDs per result kind: the set read when a result query leaves attrs empty
RESULT_ATTRS: dict[str, tuple[str, ...]] = {
    "travel_time": ("TravTm", "Vehs", "DistTrav"),
    "data_collection": ("Vehs", "SpeedAvgArith", "SpeedAvgHarm", "Acceleration", "Length", "Dist",
                        "QueueDelay", "Pers", "OccupRate"),
    "queue": ("QLen", "QLenMax", "QStops"),
    "network_performance": ("DelayAvg", "SpeedAvg", "TravTmTot", "DistTot", "DelayTot",
                            "StopsAvg", "StopsTot", "DelayStopAvg", "DelayStopTot", "VehAct",
                            "VehArr", "DelayLatent", "DemandLatent"),
    "vehicle_input": ("Volume", "DemLatent", "DelayLatent"),
    # Parking: copied from attribute.xlsx (VISSIM 2026 Doc\Eng); IDs and specifiers confirmed with the probe (2026-10-15)
    "parking_lot": ("NumVehsEnter", "NumVehsLeave", "NumVehsPark", "OccupRate", "OccupDurTot", "ParkDurAvg",
                    "ParkDurMax", "ParkSpcBlockRate", "DemLatent", "DelayLatent"),
    "parking_lot_group": ("NumVehsEnter", "NumVehsLeave", "NumVehsPark", "OccupRate", "OccupDurTot", "ParkDurAvg",
                          "ParkDurMax", "ParkSpcBlockRate"),
    "parking_decision": ("NumParkReq", "NumParkReqAccept", "NumParkReqDecl", "ParkReqDeclShare"),
}
# Number of sub-specifiers per result metric, from attribute.xlsx (SubAttr1~3) and the probe.
# Default 3 (run, time interval, vehicle class).
RESULT_ATTR_ARITY: dict[str, int] = {
    "QLen": 2, "QLenMax": 2, "QStops": 2,                      # (run, time interval)
    "DelayLatent": 2, "DemandLatent": 2, "DemLatent": 2,       # (run, time interval)
    "IncidColl": 2, "IncidHarshDec": 2, "IncidTTC": 2,
    "Volume": 1,                                              # (time interval): configured vehicle input volume
}


def result_arity(attr: str) -> int:
    return RESULT_ATTR_ARITY.get(attr, 3)


def _no() -> Arg:
    return Arg("no", "no")


_TYPES: tuple[ObjType, ...] = (
    ObjType("Link", "Net.Links", filter_attr=("IsConn", False),
            create=CreateSig("AddLink", (_no(), Arg("wkt", "wkt"), Arg("lane_widths", "list_of", ("width",)))),
            remove="RemoveLink",
            default_attrs=("No", "Name", "NumLanes", "Length2D", "LinkBehavType", "DisplayType")),
    ObjType("Connector", "Net.Links", filter_attr=("IsConn", True),
            create=CreateSig("AddConnector", (_no(), Arg("from_lane", "lane"), Arg("from_pos", "pos"),
                                              Arg("to_lane", "lane"), Arg("to_pos", "pos"),
                                              Arg("num_lanes", "int"), Arg("wkt", "wkt"))),
            remove="RemoveLink",
            default_attrs=("No", "Name", "NumLanes", "Length2D", "FromLink", "ToLink")),
    ObjType("Lane", "Net.Links[link].Lanes", key=KEY_COMPOSITE, key_parts=("link", "lane"),
            create_note="no create (the number of lanes is handled on the link)",
            default_attrs=("Index", "Width", "BlockedVehClasses")),
    ObjType("LinkPolyPoint", "Net.Links[link].LinkPolyPts", key=KEY_COMPOSITE, key_parts=("link", "index"),
            create_note="read-only", read_only=True,
            default_attrs=("Index", "X", "Y", "ZOffset")),
    ObjType("NetPara", "Net.NetPara", key=KEY_SINGLE,
            default_attrs=("RefPointNetX", "RefPointNetY", "RefPointMapX", "RefPointMapY",
                           "UnitLenShort", "UnitLenLong", "TrafReg", "LinkGradBas")),
    ObjType("VehicleType", "Net.VehicleTypes", create_note="no create (reference data)",
            default_attrs=("No", "Name", "Category", "Model2D3DDistr")),
    ObjType("DesSpeedDistribution", "Net.SpeedDistributions", create_note="no create (reference data)",   # 2026 container name (the official example's DesSpeedDistributions does not exist)
            default_attrs=("No", "Name", "LowerBound", "UpperBound")),
    # Time distributions (parking duration etc.): the Net.TimeDistributions container and writing Mean/StdDev of a
    # normal distribution were confirmed with tools/com_probe.py (2026-10-15). Default attributes are only No and Name,
    # which exist for every distribution form (an empirical distribution may have no Mean/StdDev).
    ObjType("TimeDistribution", "Net.TimeDistributions", create_note="no create (reference data); only the values of an existing distribution can be changed",
            default_attrs=("No", "Name")),
    ObjType("VehicleClass", "Net.VehicleClasses", create_note="no create (reference data)",
            default_attrs=("No", "Name", "VehTypes")),
    ObjType("VehicleComposition", "Net.VehicleCompositions",
            create=CreateSig("AddVehicleComposition",
                             (_no(), Arg("relative_flows", "list_of", ("veh_type", "des_speed_distr"))),
                             post=("VehCompRelFlows[*].RelFlow",)),
            remove="RemoveVehicleComposition",
            default_attrs=("No", "Name")),
    ObjType("VehicleInput", "Net.VehicleInputs",
            create=CreateSig("AddVehicleInput", (_no(), Arg("link", "link")),
                             post=("Volume(1)", "VehComp(1)")),
            remove="RemoveVehicleInput",
            default_attrs=("No", "Name", "Link", "Volume(1)", "VehComp(1)")),
    ObjType("VehicleRoutingDecisionStatic", "Net.VehicleRoutingDecisionsStatic",
            create=CreateSig("AddVehicleRoutingDecisionStatic", (_no(), Arg("link", "link"), Arg("pos", "pos"))),
            remove="RemoveVehicleRoutingDecisionStatic",
            default_attrs=("No", "Name", "Link", "Pos", "AllVehTypes", "VehClasses")),
    ObjType("VehicleRouteStatic", "Net.VehicleRoutingDecisionsStatic[decision].VehRoutSta",
            key=KEY_COMPOSITE, key_parts=("decision", "route"),
            create=CreateSig("AddVehicleRouteStatic", (_no(), Arg("dest_link", "link"), Arg("dest_pos", "pos")),
                             post=("RelFlow(1)",)),
            remove="RemoveVehicleRouteStatic",
            default_attrs=("No", "Name", "DestLink", "DestPos", "RelFlow(1)")),
    ObjType("DesSpeedDecision", "Net.DesSpeedDecisions",
            create=CreateSig("AddDesSpeedDecision", (_no(), Arg("lane", "lane"), Arg("pos", "pos")),
                             post=("DesSpeedDistr(vehicle class)",)),
            remove="RemoveDesSpeedDecision",
            default_attrs=("No", "Name", "Lane", "Pos", "TimeFrom", "TimeTo")),
    ObjType("ReducedSpeedArea", "Net.ReducedSpeedAreas",
            create=CreateSig("AddReducedSpeedArea", (_no(), Arg("lane", "lane"), Arg("pos", "pos")),
                             post=("Length", "DesSpeedDistr(vehicle class)")),
            remove="RemoveReducedSpeedArea",
            # Decel and DesSpeedDistr can only be read with a vehicle-class sub-attribute (attribute.xlsx SubAttr1; the
            # probe of 2026-10-18 rejected "Decel"). Class numbers differ per network, so they are not in the default
            # set: read them through attrs, e.g. "Decel(10)".
            default_attrs=("No", "Name", "Lane", "Pos", "Length", "TimeFrom", "TimeTo")),
    ObjType("DataCollectionPoint", "Net.DataCollectionPoints",
            create=CreateSig("AddDataCollectionPoint", (_no(), Arg("lane", "lane"), Arg("pos", "pos"))),
            remove="RemoveDataCollectionPoint",
            default_attrs=("No", "Name", "Lane", "Pos")),
    ObjType("DataCollectionMeasurement", "Net.DataCollectionMeasurements",
            create=CreateSig("AddDataCollectionMeasurement", (_no(),), post=("DataCollectionPoints",)),
            remove="RemoveDataCollectionMeasurement",
            default_attrs=("No", "Name", "DataCollectionPoints"),
            result_kind="data_collection"),
    ObjType("VehicleTravelTimeMeasurement", "Net.VehicleTravelTimeMeasurements",
            create=CreateSig("AddVehicleTravelTimeMeasurement",
                             (_no(), Arg("from_link", "link"), Arg("from_pos", "pos"),
                              Arg("to_link", "link"), Arg("to_pos", "pos"))),
            remove="RemoveVehicleTravelTimeMeasurement",
            default_attrs=("No", "Name", "StartLink", "StartPos", "EndLink", "EndPos", "Dist"),
            result_kind="travel_time"),
    ObjType("QueueCounter", "Net.QueueCounters",
            create=CreateSig("AddQueueCounter", (_no(), Arg("link", "link"), Arg("pos", "pos"))),
            remove="RemoveQueueCounter",
            default_attrs=("No", "Name", "Link", "Pos"),
            result_kind="queue"),
    # Parking: the signatures were taken from the official example (Network Objects Adding.py, lines 194, 210, 225) and
    # the COM type library (Vissim260.exe) and confirmed with the probe (creation, attribute writes, deletion and the
    # parking-space Index key, on a copy of the official "Multi-Storey Car Park" example).
    # Per-floor aisles and ramps (links, connectors) and automatic parking-space placement (Car park creator) are out
    # of scope: draw them in VISSIM, then handle them here.
    ObjType("ParkingLotGroup", "Net.ParkLotGrps",
            create=CreateSig("AddParkingLotGroup", (_no(),), source=SOURCE_PROBE_PARKING),
            create_note="membership is set through the EvalGrps attribute on the parking lot",
            remove="RemoveParkingLotGroup",
            default_attrs=("No", "Name"),
            result_kind="parking_lot_group"),
    ObjType("ParkingLot", "Net.ParkingLots",
            create=CreateSig("AddParkingLot", (_no(), Arg("lane", "lane"), Arg("pos", "pos")),
                             post=("Length",), source=SOURCE_PROBE_PARKING),
            remove="RemoveParkingLot",
            default_attrs=("No", "Name", "Type", "Lane", "Pos", "Length", "Capacity", "Attrac", "InitOccup",
                           "EvalGrps"),
            result_kind="parking_lot"),
    ObjType("ParkingSpace", "Net.ParkingLots[lot].ParkSpcs", key=KEY_COMPOSITE, key_parts=("lot", "index"),
            create_note="read-only (VISSIM creates parking spaces from the parking lot Length and LenPerSpc)", read_only=True,
            default_attrs=("Index", "ParkingLot")),
    ObjType("VehicleRoutingDecisionParking", "Net.VehicleRoutingDecisionsParking",
            create=CreateSig("AddVehicleRoutingDecisionParking", (_no(), Arg("link", "link"), Arg("pos", "pos")),
                             post=("ParkRate(1)", "ParkDur(1)"), source=SOURCE_PROBE_PARKING),
            remove="RemoveVehicleRoutingDecisionParking",
            default_attrs=("No", "Name", "Link", "Pos", "AllVehTypes", "VehClasses", "FullOccupBehav",
                           "RouteChoiceMeth"),
            result_kind="parking_decision"),
    ObjType("VehicleRouteParking", "Net.VehicleRoutingDecisionsParking[decision].VehRoutPark",
            key=KEY_COMPOSITE, key_parts=("decision", "route"),
            create=CreateSig("AddVehicleRouteParking", (_no(), Arg("parking_lot", "parking_lot")),
                             source=SOURCE_PROBE_PARKING),
            remove="RemoveVehicleRouteParking",
            default_attrs=("No", "Name", "ParkLot", "RelFlowSameParkLot")),
    ObjType("Simulation", "Simulation", key=KEY_SINGLE,
            default_attrs=("SimPeriod", "SimRes", "RandSeed", "NumRuns", "UseMaxSimSpeed", "SimSec")),
    ObjType("Evaluation", "Evaluation", key=KEY_SINGLE,
            default_attrs=("DataCollCollectData", "VehTravTmsCollectData", "QueuesCollectData",
                           "VehNetPerfCollectData")),
    ObjType("VehicleNetworkPerformanceMeasurement", "Net.VehicleNetworkPerformanceMeasurement",
            key=KEY_SINGLE,
            # Every attribute of this object is a result metric, so none can be read without specifiers (run, time
            # interval, vehicle class) (attribute.xlsx; there is no No/Name either). The default set is current run, last
            # interval, all vehicle types; use get_results for a per-interval table.
            default_attrs=tuple(f"{a}(Current,Last,All)" for a in RESULT_ATTRS["network_performance"][:5]),
            result_kind="network_performance"),
)

_BY_NAME: dict[str, ObjType] = {t.name: t for t in _TYPES}


def names() -> tuple[str, ...]:
    return tuple(_BY_NAME)


def get(type_name: str) -> ObjType | None:
    """ObjType for a registered type, otherwise None (the caller treats it as read-only)."""
    return _BY_NAME.get(type_name)


def _to_int(type_name: str, text, what: str) -> int:
    if isinstance(text, bool):
        raise KeyFormatError(f"{type_name} key {what}: must be an integer ({text!r})")
    if isinstance(text, int):
        return text
    if isinstance(text, str) and text.strip().isdigit():
        return int(text.strip())
    raise KeyFormatError(f"{type_name} key {what}: must be an integer ({text!r})")


def parse_key(type_name: str, key):
    """Parse a key according to the type.

    Number key -> int, composite key "link-lane" -> (int, int) tuple, single object -> None (giving a key is an error).
    """
    t = get(type_name)
    if t is None:
        raise KeyFormatError(f"unregistered type: {type_name!r}")
    if t.key == KEY_SINGLE:
        if key is not None:
            raise KeyFormatError(f"{type_name} is a single object and takes no key (no=null) ({key!r})")
        return None
    if t.key == KEY_NO:
        if key is None:
            raise KeyFormatError(f"{type_name} key is missing (no)")
        return _to_int(type_name, key, "no")
    # composite key
    fmt = "-".join(t.key_parts)
    if isinstance(key, (tuple, list)):
        parts = list(key)
    elif isinstance(key, str):
        parts = key.split("-")
    else:
        raise KeyFormatError(f"{type_name} key must have the form \"{fmt}\" ({key!r})")
    if len(parts) != len(t.key_parts):
        raise KeyFormatError(f"{type_name} key must have the form \"{fmt}\" ({key!r})")
    return tuple(_to_int(type_name, p, name) for p, name in zip(parts, t.key_parts))
