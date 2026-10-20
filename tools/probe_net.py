"""Scratch-network helpers for tools/com_probe.py only (not used by the server).

The probe builds a tiny throwaway network (two links and a connector) as a base for checking COM signatures.
These functions wrap the COM calls it needs and re-export the server's launch/load/save/version helpers.
"""
from __future__ import annotations

from typing import Any, Sequence

from vissim_mcp.vissim_com import ComCapabilityError, launch, load, save, save_as, version  # noqa: F401

Point3 = tuple[float, float, float]


class UnsavedNetworkError(RuntimeError):
    """The Vissim instance has an open network that may be unsaved, so the probe stops."""


def has_open_network(v: Any) -> bool:
    """Is a network open (a file path, or at least one link)? Unknown -> True (conservative)."""
    try:
        if int(v.Net.Links.Count) > 0:
            return True
    except Exception:
        return True
    for attr in ("InputFile", "Filename", "NetFile"):
        try:
            if v.Net.AttValue(attr):
                return True
        except Exception:
            continue
    return False


def ensure_can_open_new(v: Any, force: bool) -> None:
    """Stop unless `force` when a network is open (unknown save state counts as unsaved)."""
    if has_open_network(v) and not force:
        raise UnsavedNetworkError("Vissim has an open network. Make sure it is saved, then run again with --force")


def new_network(v: Any) -> None:
    try:
        v.New()
    except Exception as e:
        raise ComCapabilityError(f"Vissim.New() failed: {e}") from e


def wkt_linestring(pts: Sequence[Point3], with_z: bool) -> str:
    # Vissim 2026 rejects "LINESTRING Z(...)" but accepts a three-coordinate "LINESTRING(x y z, ...)".
    if with_z:
        return "LINESTRING(" + ", ".join(f"{x:.3f} {y:.3f} {z:.2f}" for x, y, z in pts) + ")"
    return "LINESTRING(" + ", ".join(f"{x:.3f} {y:.3f}" for x, y, _ in pts) + ")"


def add_link(v: Any, no: int, pts: Sequence[Point3], lane_widths: Sequence[float], name: str) -> tuple[str, float | None]:
    """Create a link. Returns ('z' | '2d+z', Length2D computed by Vissim).

    'z' = the 3D WKT was accepted; '2d+z' = created in 2D, then the Z offset of each point was written.
    """
    links = v.Net.Links
    widths = [float(w) for w in lane_widths]
    try:
        link = links.AddLink(int(no), wkt_linestring(pts, with_z=True), widths)
        mode = "z"
    except Exception as e_z:
        try:
            link = links.AddLink(int(no), wkt_linestring(pts, with_z=False), widths)
        except Exception as e2:
            raise ComCapabilityError(f"AddLink({no}) failed: 3D={e_z}; 2D={e2}") from e2
        items = link.LinkPolyPts.GetAll()
        if len(items) != len(pts):
            raise ComCapabilityError(f"AddLink({no}): LinkPolyPts count {len(items)} != {len(pts)}")
        for item, (_, _, z) in zip(items, pts):
            item.SetAttValue("ZOffset", float(z))
        mode = "2d+z"
    try:
        link.SetAttValue("Name", name)
    except Exception as e:
        raise ComCapabilityError(f"Link({no}).Name failed: {e}") from e
    try:
        length = float(link.AttValue("Length2D"))
    except Exception:
        length = None
    return mode, length


def add_connector(v: Any, no: int, from_no: int, from_lanes: Sequence[int], from_pos: float,
                  to_no: int, to_lanes: Sequence[int], to_pos: float, name: str) -> None:
    """AddConnector(Key, FromLane, FromPos, ToLane, ToPos, NumLanes, "LINESTRING EMPTY").

    from_lanes/to_lanes are consecutive lane numbers of equal length (1 = rightmost). The connector starts at the
    lowest lane and spans NumLanes to the left.
    """
    if len(from_lanes) != len(to_lanes) or not from_lanes:
        raise ValueError("from_lanes and to_lanes must be non-empty and of equal length")
    fl, tl = sorted(int(i) for i in from_lanes), sorted(int(i) for i in to_lanes)
    links = v.Net.Links
    try:
        from_link = links.ItemByKey(int(from_no))
        to_link = links.ItemByKey(int(to_no))
        conn = links.AddConnector(int(no), from_link.Lanes.ItemByKey(fl[0]), float(from_pos),
                                  to_link.Lanes.ItemByKey(tl[0]), float(to_pos), len(fl), "LINESTRING EMPTY")
        if conn is None:
            raise ComCapabilityError("AddConnector returned no object")
    except ComCapabilityError:
        raise
    except Exception as e:
        raise ComCapabilityError(f"AddConnector({no}: {from_no}->{to_no}) failed: {e}") from e
    try:
        conn.SetAttValue("Name", name)
    except Exception as e:
        raise ComCapabilityError(f"Connector({no}).Name failed: {e}") from e
