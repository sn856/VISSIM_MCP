"""Loader for the vissim_mcp config file (config/vissim_mcp.json).

A missing required key raises a ConfigError naming the key. Defaults for tunable keys live in this one file only.
The host is fixed at 127.0.0.1 and has no config key (local, single user).
Relative paths in the config (log_root) are made absolute against the repository root, so the result is the same from any working folder.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Missing required key or malformed value."""


HOST = "127.0.0.1"          # fixed — no config key
DEFAULT_PORT = 8766         # initial value in config/vissim_mcp.json (the key is required, so this is not used as a default)

REQUIRED_KEYS = (
    "vissim",
    "port",
    "protected",
    "protected_roots",
)

PROTECTED_OPS = ("create", "delete", "set")

# Tunable keys and their defaults — in this one place only.
TUNABLE_DEFAULTS: dict[str, Any] = {
    "log_root": "runs/mcp",
    "quick_mode_default": True,
    "sim_chunk_s": 60,
    "quit_prompt_timeout_s": 60,
    "list_limit_default": 200,
    "result_max_intervals": 500,
    "com_retry_count": 5,
    "com_retry_interval_s": 0.5,
    "capture_settle_s": 3.0,     # capture_view mode=window: time to wait for VISSIM to finish drawing before capturing
    "launcher_server_wait_s": 30,      # launcher: maximum time to wait for /admin/status to answer after starting the server
    "launcher_open_timeout_s": 600,    # launcher: time limit for the /admin/open request
    "shortcut_name": "VISSIM MCP",     # install: desktop shortcut name (the part before .lnk)
}
# Vissim executable (default install path of PTV Vissim 2026).
VISSIM_DEFAULTS: dict[str, Any] = {"exe": r"C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe"}

# keys to make absolute against the repository root
_PATH_KEYS = ("log_root",)


def repo_root() -> Path:
    """Repository root derived from the package location (src/vissim_mcp/config.py -> two levels up)."""
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return repo_root() / "config" / "vissim_mcp.json"


def _check_protected(items: Any) -> None:
    if not isinstance(items, list):
        raise ConfigError("protected: must be a list")
    for i, e in enumerate(items):
        where = f"protected[{i}]"
        if not isinstance(e, dict):
            raise ConfigError(f"{where}: must be an object")
        missing = [k for k in ("type", "ops", "attrs") if k not in e]
        if missing:
            raise ConfigError(f"{where}: missing required keys: {', '.join(missing)}")
        if not isinstance(e["type"], str) or not e["type"]:
            raise ConfigError(f"{where}.type: must be a non-empty string")
        if not isinstance(e["ops"], list) or any(op not in PROTECTED_OPS for op in e["ops"]):
            raise ConfigError(f"{where}.ops: must be chosen from {', '.join(PROTECTED_OPS)}")
        if not isinstance(e["attrs"], list) or any(not isinstance(a, str) for a in e["attrs"]):
            raise ConfigError(f"{where}.attrs: must be a list of strings")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read the config file, validate it, apply defaults and return it as a dict.

    Without `path`, uses config/vissim_mcp.json at the repository root.
    """
    p = Path(path) if path is not None else default_config_path()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file JSON error: {p}: {e}") from e
    if not isinstance(cfg, dict):
        raise ConfigError(f"config file must be an object: {p}")

    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ConfigError(f"missing required keys: {', '.join(missing)}")

    if not isinstance(cfg["vissim"], dict):
        raise ConfigError("vissim: must be an object")
    if "progid" not in cfg["vissim"]:
        raise ConfigError("missing required key: vissim.progid")
    if isinstance(cfg["port"], bool) or not isinstance(cfg["port"], int) or not (0 < cfg["port"] < 65536):
        raise ConfigError("port: must be an integer from 1 to 65535")
    _check_protected(cfg["protected"])
    if not isinstance(cfg["protected_roots"], list) or any(not isinstance(r, str) for r in cfg["protected_roots"]):
        raise ConfigError("protected_roots: must be a list of strings")

    for k, v in TUNABLE_DEFAULTS.items():
        cfg.setdefault(k, v)
    for k, v in VISSIM_DEFAULTS.items():
        cfg["vissim"].setdefault(k, v)

    root = repo_root()
    for k in _PATH_KEYS:
        rel = Path(cfg[k])
        cfg[k] = str((rel if rel.is_absolute() else root / rel).resolve())
    cfg["_path"] = str(p.resolve())
    return cfg
