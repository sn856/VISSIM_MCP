"""Protection-list decisions — the gatekeeper that keeps coordinates and reference points unchanged. There is no argument to turn it off."""
import inspect

import pytest

from vissim_mcp import config as mc
from vissim_mcp.guard import Guard, ProtectedError

INITIAL = [
    {"type": "Link", "ops": ["create", "delete", "set"], "attrs": ["*"]},
    {"type": "Connector", "ops": ["create", "delete", "set"], "attrs": ["*"]},
    {"type": "LinkPolyPoint", "ops": ["set"], "attrs": ["*"]},
    {"type": "NetPara", "ops": ["set"],
     "attrs": ["RefPointNet*", "RefPointMap*", "UnitLen*", "TrafReg", "LinkGradBas"]},
]


@pytest.fixture
def guard():
    return Guard(INITIAL)


def test_link_set_blocks_every_attribute_when_attrs_is_star(guard):
    for attr in ("Name", "NumLanes", "Points3D", "LinkBehavType", "Anything"):
        err = guard.check("Link", "set", attr)
        assert isinstance(err, ProtectedError), attr
        assert "Link" in str(err) and "set" in str(err) and attr in str(err)
        assert "protected" in str(err)


def test_link_create_and_delete_blocked(guard):
    assert isinstance(guard.check("Link", "create"), ProtectedError)
    assert isinstance(guard.check("Link", "delete"), ProtectedError)
    assert isinstance(guard.check("Connector", "create"), ProtectedError)
    assert isinstance(guard.check("Connector", "delete"), ProtectedError)


def test_vehicle_input_create_passes(guard):
    assert guard.check("VehicleInput", "create") is None
    assert guard.check("VehicleInput", "set", "Volume(1)") is None
    assert guard.check("VehicleInput", "delete") is None


def test_netpara_reference_points_blocked_by_prefix_pattern(guard):
    for attr in ("RefPointNetX", "RefPointNetY", "RefPointMapX", "RefPointMapY",
                 "UnitLenShort", "UnitLenLong", "TrafReg", "LinkGradBas"):
        err = guard.check("NetPara", "set", attr)
        assert isinstance(err, ProtectedError), attr
        assert "NetPara" in str(err) and attr in str(err)


def test_netpara_other_attributes_pass(guard):
    assert guard.check("NetPara", "set", "Name") is None
    assert guard.check("NetPara", "set", "TrafRegX") is None     # only an exact match, not a prefix
    assert guard.check("NetPara", "set", "NetParaUnitLen") is None   # a prefix pattern matches with startswith only


def test_lane_blocked_veh_classes_and_background_pass(guard):
    assert guard.check("Lane", "set", "BlockedVehClasses") is None
    assert guard.check("Lane", "set", "Width") is None
    assert guard.check("BackgroundImage", "create") is None
    assert guard.check("BackgroundImage", "set", "Pos") is None
    assert guard.check("Static3DModel", "delete") is None


def test_link_poly_point_only_set_blocked(guard):
    assert isinstance(guard.check("LinkPolyPoint", "set", "X"), ProtectedError)
    # operations not in ops are not blocked (read-only handling is the registry's job)
    assert guard.check("LinkPolyPoint", "delete") is None


def test_empty_attrs_lets_set_pass_and_blocks_only_the_operation():
    g = Guard([{"type": "VehicleInput", "ops": ["delete", "set"], "attrs": []}])
    assert g.check("VehicleInput", "set", "Volume(1)") is None
    assert isinstance(g.check("VehicleInput", "delete"), ProtectedError)


def test_set_without_attribute_is_blocked_for_types_with_protected_attributes(guard):
    assert isinstance(guard.check("NetPara", "set"), ProtectedError)
    assert guard.check("VehicleInput", "set") is None


def test_require_raises(guard):
    with pytest.raises(ProtectedError, match="Link"):
        guard.require("Link", "set", "Name")
    guard.require("VehicleInput", "create")   # passes: nothing happens


def test_unknown_operation_raises_value_error(guard):
    with pytest.raises(ValueError):
        guard.check("Link", "fly")


def test_multiple_entries_for_the_same_type_are_merged():
    g = Guard([{"type": "X", "ops": ["set"], "attrs": ["A*"]},
               {"type": "X", "ops": ["delete"], "attrs": []}])
    assert isinstance(g.check("X", "set", "AB"), ProtectedError)
    assert isinstance(g.check("X", "delete"), ProtectedError)
    assert g.check("X", "set", "B") is None
    assert g.check("X", "create") is None


def test_no_argument_turns_protection_off():
    params = inspect.signature(Guard.__init__).parameters
    assert set(params) == {"self", "protected"}
    params = inspect.signature(Guard.check).parameters
    assert set(params) == {"self", "type", "op", "attr"}


def test_guard_built_from_repo_config_gives_the_same_decisions():
    g = Guard(mc.load_config()["protected"])
    assert isinstance(g.check("NetPara", "set", "RefPointNetX"), ProtectedError)
    assert isinstance(g.check("Link", "set", "WktPolyline"), ProtectedError)   # alignment geometry
    assert isinstance(g.check("Link", "set", "numlanes"), ProtectedError)      # case-insensitive
    assert isinstance(g.check("Connector", "set", "FromLink"), ProtectedError)
    assert g.check("Link", "set", "Name") is None                                # attributes that are not geometry are open
    assert g.check("VehicleInput", "create") is None
