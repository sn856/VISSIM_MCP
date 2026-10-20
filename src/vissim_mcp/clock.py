"""Timestamp format for the `at` field of logs and state (ISO 8601, with time zone, to the second)."""
from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
