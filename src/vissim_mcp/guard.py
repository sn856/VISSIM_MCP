"""Protection list — a gatekeeper that compares type, operation and attribute as strings and blocks before any COM call.

This layer keeps coordinates and reference points unchanged. There is no argument or setting that turns protection off.
Entry format: {type, ops: [create|delete|set], attrs: [...]}
  - attrs ["*"]  -> every attribute of that type
  - attrs []     -> no attribute protection (operation only)
  - a trailing * as in "RefPointNet*" is a prefix pattern (startswith)
"""
from __future__ import annotations

from typing import Any

from .config import PROTECTED_OPS


class ProtectedError(PermissionError):
    """An operation blocked by the protection list."""


class Guard:
    def __init__(self, protected: list[dict[str, Any]]):
        # Entries for the same type are merged (blocked if any one of them blocks).
        self._ops: dict[str, set[str]] = {}
        self._attrs: dict[str, list[str]] = {}
        for e in protected:
            t = e["type"]
            self._ops.setdefault(t, set()).update(e.get("ops", ()))
            self._attrs.setdefault(t, []).extend(e.get("attrs", ()))

    @staticmethod
    def _attr_matches(pattern: str, attr: str) -> bool:
        """Case-insensitive (VISSIM's AttValue is too). A trailing `*` is a prefix pattern."""
        p, a = pattern.lower(), attr.lower()
        if p == "*":
            return True
        if p.endswith("*"):
            return a.startswith(p[:-1])
        return a == p

    def check(self, type: str, op: str, attr: str | None = None) -> ProtectedError | None:
        """None if allowed; if blocked, a ProtectedError carrying the reason (returned, not raised)."""
        if op not in PROTECTED_OPS:
            raise ValueError(f"unknown operation: {op!r} (must be one of {', '.join(PROTECTED_OPS)})")
        ops = self._ops.get(type)
        if not ops or op not in ops:
            return None
        if op in ("create", "delete"):
            return ProtectedError(
                f"Blocked by the protection list: {type} {op}. Adjust the `protected` setting if needed.")
        patterns = self._attrs.get(type, [])
        if not patterns:
            return None
        if attr is None:
            return ProtectedError(
                f"Blocked by the protection list: {type} set — cannot be changed without an attribute name "
                f"(protected attributes: {', '.join(patterns)}). Adjust the `protected` setting if needed.")
        for pat in patterns:
            if self._attr_matches(pat, attr):
                return ProtectedError(
                    f"Blocked by the protection list: {type} set {attr} (rule {pat!r}). "
                    f"Adjust the `protected` setting if needed.")
        return None

    def require(self, type: str, op: str, attr: str | None = None) -> None:
        """Raises ProtectedError if blocked."""
        err = self.check(type, op, attr)
        if err is not None:
            raise err
