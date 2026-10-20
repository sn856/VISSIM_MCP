"""Failure kinds for tool responses (the common `error.kind`). Every layer raises failures with this exception and the server turns them into `ok=false`."""
from __future__ import annotations

KIND_PROTECTED = "protected"
KIND_NO_NETWORK = "no_network"
KIND_UNSAVED_CHANGES = "unsaved_changes"
KIND_VISSIM_ABSENT = "vissim_absent"
KIND_OTHER_VISSIM_RUNNING = "other_vissim_running"
KIND_SIM_RUNNING = "sim_running"
KIND_COM_ERROR = "com_error"
KIND_NOT_FOUND = "not_found"
KIND_BAD_ARGS = "bad_args"
KIND_NETWORK_CHANGED = "network_changed"   # first write after the file open in the server's VISSIM changed
ERROR_KINDS = (KIND_PROTECTED, KIND_NO_NETWORK, KIND_UNSAVED_CHANGES, KIND_VISSIM_ABSENT,
               KIND_OTHER_VISSIM_RUNNING, KIND_SIM_RUNNING, KIND_COM_ERROR, KIND_NOT_FOUND, KIND_BAD_ARGS,
               KIND_NETWORK_CHANGED)


class ToolError(Exception):
    """kind (one of the constants above) + a human-readable message (VISSIM/COM error text is carried verbatim) + optional detail."""

    def __init__(self, kind: str, message: str, detail: dict | None = None):
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown error.kind: {kind!r}")
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d
