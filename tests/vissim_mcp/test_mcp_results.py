"""Results: specifier assembly, interval=all rows, not_found, result file listing and the .att parser (no VISSIM needed, synthetic fixtures)."""
import os
from pathlib import Path

import pytest

from vissim_mcp import comthread as ct
from vissim_mcp import results as rs
from vissim_mcp import state as st
from vissim_mcp.config import TUNABLE_DEFAULTS
from vissim_mcp.errors import ToolError
from vissim_mcp.registry import RESULT_ATTRS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mcp"


class FakeComError(Exception):
    pass


class FakeMeasurement:
    """Looks AttValue(specifier) up in a dict. None if absent (the case where VISSIM returns "no value")."""

    def __init__(self, no, name, values=None, raise_on=()):
        self.values = {"No": no, "Name": name}
        self.values.update(values or {})
        self.raise_on = set(raise_on)
        self.asked = []

    def AttValue(self, name):
        self.asked.append(name)
        if name in self.raise_on:
            raise FakeComError(f"AttValue failed: {name}")
        return self.values.get(name)


class FakeContainer:
    def __init__(self, items):
        self.items = {m.values["No"]: m for m in items}

    def ItemByKey(self, no):
        if no not in self.items:
            raise FakeComError(f"ItemByKey failed: {no}")
        return self.items[no]

    def GetAll(self):
        return list(self.items.values())


class FakeVissim:
    def __init__(self, **containers):
        self.Net = type("N", (), {})()
        for k, v in containers.items():
            setattr(self.Net, k, v)


@pytest.fixture
def com():
    t = ct.ComThread(retry_count=0, retry_interval_s=0.0)
    t.start()
    yield t
    t.stop()


@pytest.fixture
def cfg():
    return dict(TUNABLE_DEFAULTS)


def _make(cfg, com, vissim, path="C:/runs/sample/network.inpx"):
    state = st.ServerState(8766)
    state.set_network(path)
    return rs.Results(cfg, state, com, lambda: vissim), state


def _tt(no, name, intervals):
    vals = {}
    for i, (tt, vehs) in enumerate(intervals, start=1):
        vals[f"TravTm(Current,{i},All)"] = tt
        vals[f"Vehs(Current,{i},All)"] = vehs
        vals[f"DistTrav(Current,{i},All)"] = 1500.0
    vals["TravTm(Avg,Avg,All)"] = sum(t for t, _ in intervals) / len(intervals)
    vals["Vehs(Avg,Avg,All)"] = 80
    vals["DistTrav(Avg,Avg,All)"] = 1500.0
    return FakeMeasurement(no, name, vals)


# ---- get_results ----

def test_specifier_has_the_form_metric_run_interval_vehicle_class(cfg, com):
    m = _tt(1, "A", [(10.0, 5)])
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m])))
    out = r.get_results("travel_time", nos=[1], run="Avg", interval="Avg", veh_class="All")
    assert "TravTm(Avg,Avg,All)" in m.asked
    assert "Vehs(Avg,Avg,All)" in m.asked and "DistTrav(Avg,Avg,All)" in m.asked
    assert out["rows"] == [{"no": 1, "name": "A", "interval": "Avg", "TravTm": 10.0, "Vehs": 80,
                            "DistTrav": 1500.0}]
    assert out["attrs"] == list(RESULT_ATTRS["travel_time"])
    assert "note" in out and "vehicle-class" in out["note"]


def test_interval_all_returns_rows_until_values_run_out(cfg, com):
    m1 = _tt(1, "A", [(10.0, 5), (12.0, 6), (11.0, 7)])
    m2 = _tt(2, "B", [(20.0, 1)])
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m1, m2])))
    out = r.get_results("travel_time")
    rows = out["rows"]
    assert [(row["no"], row["interval"]) for row in rows] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    assert rows[1]["TravTm"] == 12.0 and rows[1]["Vehs"] == 6
    assert out["count"] == 4
    # the 4th interval is asked for (all None) and then it stops
    assert "TravTm(Current,4,All)" in m1.asked and "TravTm(Current,5,All)" not in m1.asked


def test_interval_all_limit_is_the_tunable_result_max_intervals(cfg, com):
    cfg["result_max_intervals"] = 3
    assert TUNABLE_DEFAULTS["result_max_intervals"] == 500
    vals = {f"TravTm(Current,{i},All)": float(i) for i in range(1, 50)}
    m = FakeMeasurement(1, "A", vals)
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m])))
    out = r.get_results("travel_time")
    assert [row["interval"] for row in out["rows"]] == [1, 2, 3]
    assert out["interval_limit_reached"] is True


def test_nos_selects_only_those_and_a_missing_number_is_not_found(cfg, com):
    m1 = _tt(1, "A", [(10.0, 5)])
    m2 = _tt(2, "B", [(20.0, 1)])
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m1, m2])))
    out = r.get_results("travel_time", nos=[2], interval=1)
    assert [row["no"] for row in out["rows"]] == [2]
    with pytest.raises(ToolError) as ei:
        r.get_results("travel_time", nos=[9])
    assert ei.value.kind == "not_found" and "9" in ei.value.message


def test_all_none_values_give_not_found_with_a_reason(cfg, com):
    m = FakeMeasurement(1, "A")
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m])))
    with pytest.raises(ToolError) as ei:
        r.get_results("travel_time")
    assert ei.value.kind == "not_found" and "evaluation" in ei.value.message
    with pytest.raises(ToolError) as ei:
        r.get_results("travel_time", interval="Avg")
    assert ei.value.kind == "not_found"


def test_container_per_kind_and_queue_specifier_has_two_items(cfg, com):
    q = FakeMeasurement(5, "Q", {"QLen(Current,1)": 12.5, "QLenMax(Current,1)": 40.0, "QStops(Current,1)": 3})
    d = FakeMeasurement(7, "D", {"Vehs(Current,1,All)": 100, "SpeedAvgArith(Current,1,All)": 95.0})
    vi = FakeMeasurement(3, "VI", {"Volume(1)": 1200, "DemLatent(Current,1)": 3})
    v = FakeVissim(QueueCounters=FakeContainer([q]), DataCollectionMeasurements=FakeContainer([d]),
                 VehicleInputs=FakeContainer([vi]))
    r, _ = _make(cfg, com, v)
    out = r.get_results("queue", interval=1)
    assert out["rows"][0]["QLen"] == 12.5 and out["rows"][0]["QLenMax"] == 40.0
    assert "QLen(Current,1)" in q.asked and not any("All" in a for a in q.asked)
    out = r.get_results("data_collection", interval=1)
    assert out["rows"][0]["Vehs"] == 100 and out["rows"][0]["SpeedAvgArith"] == 95.0
    assert out["rows"][0]["Acceleration"] is None       # a missing metric stays None
    out = r.get_results("vehicle_input", interval=1)
    assert out["rows"][0]["Volume"] == 1200 and out["rows"][0]["DemLatent"] == 3
    assert "Volume(1)" in vi.asked and "DemLatent(Current,1)" in vi.asked    # the number of specifiers differs per metric
    assert "attr_ids_verified" not in out


def test_parking_result_containers_and_specifiers_for_three_kinds(cfg, com):
    lot = FakeMeasurement(700, "B1", {"OccupRate(Current,1,All)": 0.75, "NumVehsPark(Current,1,All)": 12,
                              "DemLatent(Current,1)": 2})
    grp = FakeMeasurement(2, "Floor 2", {"OccupRate(Current,1,All)": 0.5, "NumVehsEnter(Current,1,All)": 30})
    dec = FakeMeasurement(1, "Entry", {"NumParkReq(Current,1,All)": 40, "ParkReqDeclShare(Current,1,All)": 0.1})
    r, _ = _make(cfg, com, FakeVissim(ParkingLots=FakeContainer([lot]), ParkLotGrps=FakeContainer([grp]),
                                     VehicleRoutingDecisionsParking=FakeContainer([dec])))
    out = r.get_results("parking_lot", nos=[700], interval=1)
    assert out["rows"][0]["OccupRate"] == 0.75 and out["rows"][0]["NumVehsPark"] == 12
    assert out["rows"][0]["DemLatent"] == 2 and "DemLatent(Current,1)" in lot.asked   # two specifiers
    assert "attr_ids_verified" not in out
    out = r.get_results("parking_lot_group", interval=1)
    assert out["rows"][0]["no"] == 2 and out["rows"][0]["NumVehsEnter"] == 30
    assert out["attrs"] == list(RESULT_ATTRS["parking_lot_group"])
    out = r.get_results("parking_decision", interval=1)
    assert out["rows"][0]["NumParkReq"] == 40 and out["rows"][0]["ParkReqDeclShare"] == 0.1


def test_network_performance_is_a_single_object(cfg, com):
    np_ = FakeMeasurement(None, None, {"DelayAvg(Current,1,All)": 30.0, "SpeedAvg(Current,1,All)": 88.0,
                               "DelayAvg(Current,2,All)": 31.0})
    r, _ = _make(cfg, com, FakeVissim(VehicleNetworkPerformanceMeasurement=np_))
    out = r.get_results("network_performance")
    assert [row["interval"] for row in out["rows"]] == [1, 2]
    assert out["rows"][0]["no"] is None and out["rows"][0]["DelayAvg"] == 30.0
    assert set(out["attrs"]) == set(RESULT_ATTRS["network_performance"])


def test_invalid_kind_run_interval_and_vehicle_class_are_bad_args(cfg, com):
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([])))
    for kw in ({"kind": "foo"}, {"kind": "travel_time", "run": "Mean"},
               {"kind": "travel_time", "interval": "First"}, {"kind": "travel_time", "veh_class": "HGV"},
               {"kind": "travel_time", "nos": "1"}):
        with pytest.raises(ToolError) as ei:
            r.get_results(**kw)
        assert ei.value.kind == "bad_args", kw


def test_run_number_and_vehicle_class_number_accept_integers(cfg, com):
    m = FakeMeasurement(1, "A", {"TravTm(2,Last,20)": 9.0})
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m])))
    out = r.get_results("travel_time", run=2, interval="Last", veh_class=20)
    assert out["rows"][0]["TravTm"] == 9.0
    assert "TravTm(2,Last,20)" in m.asked


def test_com_rejection_becomes_com_error_with_the_message_unchanged(cfg, com):
    m = FakeMeasurement(1, "A", raise_on=("TravTm(Current,1,All)",))
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m])))
    with pytest.raises(ToolError) as ei:
        r.get_results("travel_time", interval=1)
    assert ei.value.kind == "com_error" and "AttValue failed: TravTm(Current,1,All)" in ei.value.message


def test_no_network_gives_no_network(cfg, com):
    def getter():
        raise ToolError("no_network", "no open network")

    r = rs.Results(cfg, st.ServerState(8766), com, getter)
    with pytest.raises(ToolError) as ei:
        r.get_results("travel_time")
    assert ei.value.kind == "no_network"


# ---- list_result_files ----

def _results_dir(tmp_path):
    net = tmp_path / "run1" / "network.inpx"
    net.parent.mkdir(parents=True)
    net.write_text("<network/>", encoding="utf-8")
    d = net.parent / "network.results"
    d.mkdir()
    return net, d


def test_result_file_list_is_newest_first(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    old = d / "old_Travel Times.att"
    new = d / "new_Data Collection.att"
    old.write_bytes(b"$VISION\n")
    new.write_bytes(b"$VISION\n" * 3)
    (d / "ignore.txt").write_text("x", encoding="utf-8")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_100, 1_700_000_100))
    r, _ = _make(cfg, com, None, path=str(net))
    out = r.list_result_files()
    assert out["folder"] == str(d)
    assert [f["name"] for f in out["files"]] == [new.name, old.name]
    assert out["files"][0]["size"] == 24 and out["files"][1]["size"] == 8
    assert "T" in out["files"][0]["modified_at"]
    assert out.get("note") is None


def test_missing_results_folder_gives_empty_list_and_note(cfg, com, tmp_path):
    r, _ = _make(cfg, com, None, path=str(tmp_path / "run2" / "network.inpx"))
    out = r.list_result_files()
    assert out["files"] == [] and "results" in out["note"]


def test_result_file_list_without_network_gives_no_network(cfg, com):
    r = rs.Results(cfg, st.ServerState(8766), com, lambda: None)
    with pytest.raises(ToolError) as ei:
        r.list_result_files()
    assert ei.value.kind == "no_network"


# ---- read_result_file / .att parser ----

def test_att_parser_header_columns_rows_and_BOM(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    src = FIXTURES / "travel_times_sample.att"
    (d / src.name).write_bytes(src.read_bytes())
    r, _ = _make(cfg, com, None, path=str(net))
    out = r.read_result_file(src.name)
    assert out["table"] == "VEHICLETRAVELTIMEMEASUREMENTEVALUATION"
    assert out["columns"] == ["SIMRUN", "TIMEINT", "VEHICLETRAVELTIMEMEASUREMENT", "TRAVTM(ALL)", "VEHS(ALL)",
                              "DISTTRAV(ALL)"]
    assert len(out["rows"]) == 6 and out["row_count"] == 6 and out["truncated"] is False
    assert out["rows"][0] == [1, "0-900", 1, 123.45, 80, 1500.0]
    assert out["rows"][3][3] is None                     # an empty cell is None
    assert out["rows"][5][0] == "AVG"
    assert any("Table: Vehicle Travel Time Results" in m for m in out["meta"])
    assert any("Synthetic sample" in m and "M\u00fcller" in m for m in out["meta"])   # non-ASCII UTF-8 under a BOM
    assert out["encoding"] == "utf-8"


def test_att_parser_treats_TABLE_line_as_header_text(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    src = FIXTURES / "data_collection_sample.att"
    (d / src.name).write_bytes(src.read_bytes())
    r, _ = _make(cfg, com, None, path=str(net))
    out = r.read_result_file(src.name)
    assert out["table"] == "DATACOLLECTIONMEASUREMENTEVALUATION"
    assert out["columns"][3] == "VEHS(ALL)" and len(out["rows"]) == 3
    assert any(m.startswith("$TABLE:") for m in out["meta"])


def test_where_and_max_rows_truncated(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    src = FIXTURES / "travel_times_sample.att"
    (d / src.name).write_bytes(src.read_bytes())
    r, _ = _make(cfg, com, None, path=str(net))
    out = r.read_result_file(src.name, where={"SIMRUN": "1", "TIMEINT": "0-900"})
    assert len(out["rows"]) == 2 and out["row_count"] == 2
    assert [row[2] for row in out["rows"]] == [1, 2]
    out = r.read_result_file(src.name, where={"VEHICLETRAVELTIMEMEASUREMENT": 1})   # compared as strings even when a number is given
    assert out["row_count"] == 4
    out = r.read_result_file(src.name, max_rows=2)
    assert len(out["rows"]) == 2 and out["row_count"] == 6 and out["truncated"] is True
    with pytest.raises(ToolError) as ei:
        r.read_result_file(src.name, where={"missing_col": "1"})
    assert ei.value.kind == "bad_args"


def test_cp949_fallback_decoding(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    # Korean comment on purpose: simulates a cp949-encoded file written by a Korean-locale VISSIM
    body = "$VISION\n* Comment: 한글 주석\n$QUEUECOUNTEREVALUATION:SIMRUN;TIMEINT;QUEUECOUNTER;QLEN\n1;0-900;1;3.5\n"
    (d / "q.att").write_bytes(body.encode("cp949"))
    r, _ = _make(cfg, com, None, path=str(net))
    out = r.read_result_file("q.att")
    assert out["encoding"] == "cp949" and any("한글 주석" in m for m in out["meta"])
    assert out["rows"] == [[1, "0-900", 1, 3.5]]


def test_file_name_check_parent_path_absolute_path_extension(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    (d / "ok.att").write_text("$VISION\n$T:A;B\n1;2\n", encoding="utf-8")
    (tmp_path / "run1" / "outside.att").write_text("$VISION\n$T:A\n1\n", encoding="utf-8")
    r, _ = _make(cfg, com, None, path=str(net))
    assert r.read_result_file("ok.att")["rows"] == [[1, 2]]
    for bad in ("../outside.att", str(tmp_path / "run1" / "outside.att"), "..\\outside.att", "sub/x.att",
                "ok.txt", ""):
        with pytest.raises(ToolError) as ei:
            r.read_result_file(bad)
        assert ei.value.kind == "bad_args", bad
    with pytest.raises(ToolError) as ei:
        r.read_result_file("missing.att")
    assert ei.value.kind == "not_found"


def test_missing_column_header_line_is_bad_args(cfg, com, tmp_path):
    net, d = _results_dir(tmp_path)
    (d / "broken.att").write_text("$VISION\n* only comments\n", encoding="utf-8")
    r, _ = _make(cfg, com, None, path=str(net))
    with pytest.raises(ToolError) as ei:
        r.read_result_file("broken.att")
    assert ei.value.kind == "bad_args"


def test_interval_all_stops_at_a_com_error_for_an_out_of_range_interval(cfg, com):
    """VISSIM 2026 rejects a non-existent time interval with a 'Time Interval ... does not match' COM error instead of returning None."""
    class RejectingMeasurement(FakeMeasurement):
        def AttValue(self, spec):
            if ",3," in spec:
                raise RuntimeError("The subattribute no. 2 '3' of type 'Time Interval' of the attribute 'TRAVTM(CURRENT,3,ALL)' is invalid: Object 3 does not match")
            return super().AttValue(spec)
    m = RejectingMeasurement(1, "T", {"TravTm(Current,1,All)": 10.0, "Vehs(Current,1,All)": 5, "DistTrav(Current,1,All)": 100.0,
                              "TravTm(Current,2,All)": 12.0, "Vehs(Current,2,All)": 6, "DistTrav(Current,2,All)": 100.0})
    r, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([m])))
    out = r.get_results("travel_time")
    assert [row["interval"] for row in out["rows"]] == [1, 2]
    # an error from the first interval is raised as is
    class RejectAll(FakeMeasurement):
        def AttValue(self, spec):
            raise RuntimeError("The subattribute no. 2 '1' of type 'Time Interval' of the attribute 'TRAVTM(CURRENT,1,ALL)' is invalid: Object 1 does not match")
    r2, _ = _make(cfg, com, FakeVissim(VehicleTravelTimeMeasurements=FakeContainer([RejectAll(2, "U", {})])))
    with pytest.raises(ToolError) as ei:
        r2.get_results("travel_time")
    assert ei.value.kind == "com_error"
