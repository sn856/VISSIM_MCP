"""Thin Vissim COM helpers: launch, open, save, version. The server uses only these (no `GetActiveObject`).

When COM refuses, ComCapabilityError carries Vissim's own message up unchanged (no silent workarounds).
"""
from __future__ import annotations

from typing import Any


class ComCapabilityError(RuntimeError):
    """A COM method or attribute is missing or rejected the input. Stop and report."""


def launch(progid: str) -> tuple[Any, str]:
    """Always start a new Vissim (Dispatch only, never GetActiveObject). Returns (object, "Dispatch:<progid>").

    The server attaches only to the Vissim it launched, never to a Vissim the user opened in the GUI.
    A Vissim started by Dispatch closes when our process ends (COM server lifetime).
    """
    import pythoncom  # noqa: F401  (COM initialisation)
    import win32com.client as wc
    try:
        v = wc.Dispatch(progid)
        return v, f"Dispatch:{progid}"
    except Exception as e:
        raise ComCapabilityError(f"Failed to launch Vissim ({progid}): {e}") from e


def save_as(v: Any, path: str) -> None:
    try:
        v.SaveNetAs(str(path))
    except Exception as e:
        raise ComCapabilityError(f"SaveNetAs({path}) failed: {e}") from e


def save(v: Any) -> None:
    try:
        v.SaveNet()
    except Exception as e:
        raise ComCapabilityError(f"SaveNet() failed: {e}") from e


def load(v: Any, path: str) -> None:
    try:
        v.LoadNet(str(path), False)
    except Exception as e:
        raise ComCapabilityError(f"LoadNet({path}) failed: {e}") from e


def version(v: Any) -> str:
    for attr in ("VersionStr", "Version"):
        try:
            val = v.AttValue(attr)
            if val:
                return str(val)
        except Exception:
            continue
    return ""
