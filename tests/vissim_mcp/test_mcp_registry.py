"""Registry: per-type container, key format, create signature (official example) and composite-key parsing."""
import pytest

from vissim_mcp import registry as rg


def test_unregistered_name_is_none():
    assert rg.get("PedestrianInput") is None
    assert rg.get("") is None


def test_all_create_signatures_are_probe_verified():
    for name in rg.names():
        t = rg.get(name)
        if t.create is not None:
            assert t.create.source in (rg.SOURCE_PROBE, rg.SOURCE_PROBE_PARKING), name
            assert t.create.verified is True, name
            assert all(a.kind in rg.ARG_KINDS for a in t.create.args), name


def test_simple_key_parsing():
    assert rg.parse_key("VehicleInput", 3) == 3
    assert rg.parse_key("VehicleInput", "3") == 3
    with pytest.raises(rg.KeyFormatError):
        rg.parse_key("VehicleInput", "a")
    with pytest.raises(rg.KeyFormatError):
        rg.parse_key("VehicleInput", None)


def test_composite_key_parsing():
    assert rg.parse_key("Lane", "10001-2") == (10001, 2)
    assert rg.parse_key("LinkPolyPoint", "10001-5") == (10001, 5)
    assert rg.parse_key("VehicleRouteStatic", "1-2") == (1, 2)
    assert rg.parse_key("Lane", (10001, 2)) == (10001, 2)


@pytest.mark.parametrize("bad", ["10001", "10001-", "-2", "10001-2-3", "a-b", 5, None, ""])
def test_composite_key_errors(bad):
    with pytest.raises(rg.KeyFormatError) as ei:
        rg.parse_key("Lane", bad)
    assert "Lane" in str(ei.value)


def test_single_object_key_is_null():
    assert rg.parse_key("NetPara", None) is None
    assert rg.parse_key("Simulation", None) is None
    with pytest.raises(rg.KeyFormatError):
        rg.parse_key("NetPara", 1)


def test_key_of_unregistered_type_is_an_error():
    with pytest.raises(rg.KeyFormatError, match="unregistered"):
        rg.parse_key("Nope", 1)


def test_link_and_connector_share_a_container_and_differ_by_IsConn():
    link, conn = rg.get("Link"), rg.get("Connector")
    assert link.container == conn.container == "Net.Links"
    assert link.filter_attr == ("IsConn", False)
    assert conn.filter_attr == ("IsConn", True)
    assert link.remove == conn.remove == "RemoveLink"


def test_lane_and_point_are_composite_keys_under_a_link():
    lane = rg.get("Lane")
    assert lane.key == "composite" and lane.key_parts == ("link", "lane")
    assert lane.container == "Net.Links[link].Lanes"
    assert lane.create is None
    pt = rg.get("LinkPolyPoint")
    assert pt.key_parts == ("link", "index") and pt.read_only is True
    assert pt.container == "Net.Links[link].LinkPolyPts"


def test_create_signatures_follow_the_official_example():
    vi = rg.get("VehicleInput").create
    assert vi.method == "AddVehicleInput"
    assert [a.kind for a in vi.args] == ["no", "link"]
    assert "Volume(1)" in vi.post and "VehComp(1)" in vi.post

    vc = rg.get("VehicleComposition").create
    assert vc.method == "AddVehicleComposition"
    assert [a.kind for a in vc.args] == ["no", "list_of"]
    assert vc.args[1].items == ("veh_type", "des_speed_distr")

    rd = rg.get("VehicleRoutingDecisionStatic").create
    assert rd.method == "AddVehicleRoutingDecisionStatic"
    assert [a.kind for a in rd.args] == ["no", "link", "pos"]

    rt = rg.get("VehicleRouteStatic")
    assert rt.container == "Net.VehicleRoutingDecisionsStatic[decision].VehRoutSta"
    assert rt.key_parts == ("decision", "route")
    assert rt.create.method == "AddVehicleRouteStatic"
    assert [a.kind for a in rt.create.args] == ["no", "link", "pos"]
    assert "RelFlow(1)" in rt.create.post

    for name in ("DesSpeedDecision", "ReducedSpeedArea", "DataCollectionPoint"):
        sig = rg.get(name).create
        assert sig.method == f"Add{name}", name
        assert [a.kind for a in sig.args] == ["no", "lane", "pos"], name

    tt = rg.get("VehicleTravelTimeMeasurement").create
    assert tt.method == "AddVehicleTravelTimeMeasurement"
    assert [a.kind for a in tt.args] == ["no", "link", "pos", "link", "pos"]

    qc = rg.get("QueueCounter").create
    assert qc.method == "AddQueueCounter"
    assert [a.kind for a in qc.args] == ["no", "link", "pos"]

    lk = rg.get("Link").create
    assert lk.method == "AddLink" and [a.kind for a in lk.args] == ["no", "wkt", "list_of"]
    cn = rg.get("Connector").create
    assert cn.method == "AddConnector"
    assert [a.kind for a in cn.args] == ["no", "lane", "pos", "lane", "pos", "int", "wkt"]


def test_parking_signatures_follow_the_official_example_and_type_library():
    g = rg.get("ParkingLotGroup")
    assert g.container == "Net.ParkLotGrps" and g.create.method == "AddParkingLotGroup"
    assert [a.kind for a in g.create.args] == ["no"] and g.result_kind == "parking_lot_group"
    lot = rg.get("ParkingLot")
    assert lot.create.method == "AddParkingLot" and [a.kind for a in lot.create.args] == ["no", "lane", "pos"]
    assert lot.result_kind == "parking_lot"
    spc = rg.get("ParkingSpace")
    assert spc.container == "Net.ParkingLots[lot].ParkSpcs" and spc.key_parts == ("lot", "index")
    assert spc.read_only is True and spc.create is None
    dec = rg.get("VehicleRoutingDecisionParking")
    assert dec.create.method == "AddVehicleRoutingDecisionParking"
    assert [a.kind for a in dec.create.args] == ["no", "link", "pos"]
    assert dec.result_kind == "parking_decision"
    rt = rg.get("VehicleRouteParking")
    assert rt.container == "Net.VehicleRoutingDecisionsParking[decision].VehRoutPark"
    assert rt.key_parts == ("decision", "route")
    assert [a.kind for a in rt.create.args] == ["no", "parking_lot"]
    assert rg.parse_key("ParkingSpace", "700-3") == (700, 3)
    for name in ("ParkingLotGroup", "ParkingLot", "VehicleRoutingDecisionParking", "VehicleRouteParking"):
        assert rg.get(name).remove == f"Remove{name}", name
        assert rg.get(name).create.source == rg.SOURCE_PROBE_PARKING, name
    assert rg.RESULT_ATTRS["parking_decision"] == ("NumParkReq", "NumParkReqAccept", "NumParkReqDecl",
                                                   "ParkReqDeclShare")
    assert {"OccupRate", "NumVehsPark", "ParkDurAvg"} <= set(rg.RESULT_ATTRS["parking_lot_group"])
    assert "DemLatent" in rg.RESULT_ATTRS["parking_lot"] and "DemLatent" not in rg.RESULT_ATTRS["parking_lot_group"]


def test_data_collection_measurement_is_created_with_a_number_and_points_are_an_attribute():
    t = rg.get("DataCollectionMeasurement")
    assert t.create.method == "AddDataCollectionMeasurement" and [a.kind for a in t.create.args] == ["no"]
    assert t.create.post == ("DataCollectionPoints",)
    assert t.result_kind == "data_collection"


def test_desired_speed_distribution_container_has_the_2026_name():
    assert rg.get("DesSpeedDistribution").container == "Net.SpeedDistributions"


def test_time_distribution_only_changes_values_and_has_no_create():
    t = rg.get("TimeDistribution")
    assert t.container == "Net.TimeDistributions" and t.create is None and t.remove is None
    assert t.default_attrs == ("No", "Name") and not t.read_only


def test_result_metric_sub_specifier_count():
    assert rg.result_arity("TravTm") == 3 and rg.result_arity("QLen") == 2 and rg.result_arity("Volume") == 1
    assert rg.result_arity("DelayLatent") == 2 and rg.result_arity("DemLatent") == 2


def test_types_without_create():
    for name in ("VehicleType", "DesSpeedDistribution", "VehicleClass", "Lane", "LinkPolyPoint"):
        assert rg.get(name).create is None, name


def test_remove_method_naming_rule():
    for name in ("VehicleInput", "VehicleComposition", "QueueCounter", "DesSpeedDecision",
                 "VehicleRoutingDecisionStatic", "VehicleRouteStatic"):
        assert rg.get(name).remove == f"Remove{name}", name
    for name in ("NetPara", "Simulation", "Evaluation", "VehicleNetworkPerformanceMeasurement",
                 "Lane", "LinkPolyPoint"):
        assert rg.get(name).remove is None, name


def test_single_objects():
    for name, container in (("NetPara", "Net.NetPara"), ("Simulation", "Simulation"),
                            ("Evaluation", "Evaluation"),
                            ("VehicleNetworkPerformanceMeasurement",
                             "Net.VehicleNetworkPerformanceMeasurement")):
        t = rg.get(name)
        assert t.key == "single" and t.container == container, name


def test_result_metric_sets():
    assert {"TravTm", "Vehs", "DistTrav"} <= set(rg.RESULT_ATTRS["travel_time"])
    assert {"Vehs", "SpeedAvgArith", "SpeedAvgHarm", "Acceleration", "Length", "QueueDelay"} <= set(rg.RESULT_ATTRS["data_collection"])
    assert {"QLen", "QLenMax", "QStops"} <= set(rg.RESULT_ATTRS["queue"])
    assert {"DelayAvg", "SpeedAvg", "TravTmTot", "DistTot", "VehAct", "VehArr"} <= set(rg.RESULT_ATTRS["network_performance"])
    assert "Vehs" not in rg.RESULT_ATTRS["network_performance"]
    assert rg.RESULT_ATTRS["vehicle_input"] == ("Volume", "DemLatent", "DelayLatent")
    assert rg.get("VehicleTravelTimeMeasurement").result_kind == "travel_time"
    assert rg.get("QueueCounter").result_kind == "queue"
    assert rg.get("VehicleNetworkPerformanceMeasurement").result_kind == "network_performance"
    assert rg.get("VehicleInput").result_kind is None


def test_default_attribute_sets_are_not_empty():
    for name in rg.names():
        assert rg.get(name).default_attrs, name


# Attributes whose SubAttr1 is the vehicle class in attribute.xlsx (VISSIM 2026 Doc\Eng): VISSIM rejects them when read
# without a specifier (probe 2026-10-18: ReducedSpeedArea "Decel" -> "Sub-attribute not specified"). Class numbers
# differ per network.
_VEH_CLASS_SUBATTR = {
    "ReducedSpeedArea": ("Decel", "DesSpeedDistr"),
    "DesSpeedDecision": ("DesSpeedDistr",),
}


def test_default_attributes_do_not_contain_bare_names_that_need_a_vehicle_class_specifier():
    for name, ids in _VEH_CLASS_SUBATTR.items():
        attrs = rg.get(name).default_attrs
        for a in ids:
            assert a not in attrs, (name, a)
    assert rg.get("ReducedSpeedArea").default_attrs == ("No", "Name", "Lane", "Pos", "Length", "TimeFrom", "TimeTo")


def test_network_performance_default_attributes_all_carry_the_three_specifiers():
    """attribute.xlsx: this object has no attribute without specifiers (no No/Name either)."""
    attrs = rg.get("VehicleNetworkPerformanceMeasurement").default_attrs
    assert attrs == tuple(f"{a}(Current,Last,All)" for a in ("DelayAvg", "SpeedAvg", "TravTmTot", "DistTot", "DelayTot"))
