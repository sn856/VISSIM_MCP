"""Change log (<log_root>/changes.jsonl) and server log (<log_root>/server.log).

In changes.jsonl each line is one entry:
  {at, server_start_id, tool, network_path, type, no, attr, before, after, ok, error}
`at` is ISO8601 (with time zone, clock.now_iso). Extra fields may be added.
read() returns entries in file order (not newest first) and keeps only those at or after `since` (an ISO string).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .clock import now_iso

ENTRY_FIELDS = ("at", "server_start_id", "tool", "network_path", "type", "no", "attr",
                "before", "after", "ok", "error")
ENTRY_KINDS = ("open", "save", "set", "create", "delete", "run_simulation", "recovery", "vissim_lost", "view",
               "file_switch", "external_change")

CHANGES_FILE = "changes.jsonl"
SERVER_LOG_FILE = "server.log"
LOGGER_NAME = "vissim_mcp"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 5


def _parse_at(text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError) as e:
        raise ValueError(f"timestamp must be ISO8601: {text!r}") from e
    if dt.tzinfo is None:
        dt = dt.astimezone()   # with no time zone, treat it as this PC's local time
    return dt


class ChangeLog:
    def __init__(self, log_root: str | Path):
        self.root = Path(log_root)

    @property
    def path(self) -> Path:
        return self.root / CHANGES_FILE

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append one entry as one line. Creates the folder if missing. Returns the filled-in entry."""
        if not entry.get("tool"):
            raise ValueError("a change-log entry must have a tool")
        if "kind" in entry and entry["kind"] not in ENTRY_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ENTRY_KINDS)} ({entry['kind']!r})")
        row: dict[str, Any] = {k: None for k in ENTRY_FIELDS}
        row.update(entry)
        if not row.get("at"):
            row["at"] = now_iso()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    def read(self, since: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Return entries in file order. Only those at or after `since`; if there are more than `limit`, the last `limit`."""
        since_dt = _parse_at(since) if since else None
        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue   # skip corrupt lines (e.g. a write that was cut off)
                if since_dt is not None:
                    try:
                        if _parse_at(row.get("at")) < since_dt:
                            continue
                    except ValueError:
                        continue
                rows.append(row)
        if limit is not None and limit >= 0 and len(rows) > limit:
            rows = rows[-limit:]
        return rows


def setup_logging(log_root: str | Path, level: int = logging.INFO) -> logging.Logger:
    """Return a logger with a rotating UTF-8 file handler on <log_root>/server.log. Calling it repeatedly still leaves one handler."""
    root = Path(log_root)
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    target = (root / SERVER_LOG_FILE).resolve()
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == target:
            return logger
    handler = RotatingFileHandler(target, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger
