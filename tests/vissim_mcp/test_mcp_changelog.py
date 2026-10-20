"""Change log — one entry per line in changes.jsonl, the since filter, and server log setup."""
import json
import logging

import pytest

from vissim_mcp import changelog as cl


@pytest.fixture
def log(tmp_path):
    return cl.ChangeLog(tmp_path / "missing_dir" / "mcp")


def test_creates_missing_folder_one_line_per_entry_utf8(log):
    e = log.append({"tool": "set", "server_start_id": "s1", "network_path": "C:/x.inpx",
                    "type": "VehicleInput", "no": 1, "attr": "Volume(1)", "before": 500,
                    "after": 800, "ok": True, "error": None})
    assert e["at"] and "T" in e["at"]
    log.append({"tool": "open", "network_path": "C:/café.inpx", "ok": True})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert log.path.name == "changes.jsonl"
    first = json.loads(lines[0])
    assert set(first) >= set(cl.ENTRY_FIELDS)
    assert first["attr"] == "Volume(1)" and first["before"] == 500 and first["after"] == 800
    second = json.loads(lines[1])
    assert second["type"] is None and second["no"] is None     # missing fields are filled with null
    assert "café.inpx" in lines[1]                                # ensure_ascii=False


def test_explicit_at_is_kept(log):
    e = log.append({"tool": "save", "at": "2026-10-12T10:00:00+09:00", "ok": True})
    assert e["at"] == "2026-10-12T10:00:00+09:00"


def test_missing_tool_is_an_error(log):
    with pytest.raises(ValueError, match="tool"):
        log.append({"ok": True})


def test_entry_kind_constants():
    assert set(cl.ENTRY_KINDS) == {"open", "save", "set", "create", "delete", "run_simulation",
                                   "recovery", "vissim_lost", "view", "file_switch", "external_change"}


def test_read_returns_file_order_with_since_filter(log):
    for i, at in enumerate(("2026-10-12T10:00:00+09:00", "2026-10-12T10:00:05+09:00",
                            "2026-10-12T10:00:10+09:00")):
        log.append({"tool": "set", "at": at, "no": i, "ok": True})
    rows = log.read()
    assert [r["no"] for r in rows] == [0, 1, 2]
    rows = log.read(since="2026-10-12T10:00:05+09:00")
    assert [r["no"] for r in rows] == [1, 2]
    rows = log.read(since="2026-10-12T01:00:06+00:00")   # comparison works across time zones
    assert [r["no"] for r in rows] == [2]
    assert log.read(since="2026-10-13T00:00:00+09:00") == []


def test_read_limit_returns_the_last_n_in_file_order(log):
    for i in range(5):
        log.append({"tool": "set", "no": i, "ok": True})
    assert [r["no"] for r in log.read(limit=2)] == [3, 4]


def test_missing_file_gives_empty_list(log):
    assert log.read() == []


def test_corrupt_lines_are_skipped_and_the_rest_returned(log):
    log.append({"tool": "set", "no": 0, "ok": True})
    with log.path.open("a", encoding="utf-8") as f:
        f.write("{corrupt line\n")
    log.append({"tool": "set", "no": 1, "ok": True})
    assert [r["no"] for r in log.read()] == [0, 1]


def test_invalid_since_is_an_error(log):
    with pytest.raises(ValueError):
        log.read(since="yesterday")


def test_setup_logging_adds_rotating_handler_on_server_log(tmp_path):
    root = tmp_path / "mcp"
    logger = cl.setup_logging(root)
    logger.info("start %s", "café")
    for h in logger.handlers:
        h.flush()
    p = root / "server.log"
    assert p.is_file()
    assert "start café" in p.read_text(encoding="utf-8")
    kinds = [type(h).__name__ for h in logger.handlers]
    assert "RotatingFileHandler" in kinds
    # calling it twice does not stack handlers
    again = cl.setup_logging(root)
    assert again is logger
    assert sum(1 for h in logger.handlers if type(h).__name__ == "RotatingFileHandler") == 1
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)
    logging.getLogger(cl.LOGGER_NAME).handlers.clear()
