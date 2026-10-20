"""vissim_mcp config loader — required keys, defaults, and paths relative to the repository root."""
import json
from pathlib import Path

import pytest

from vissim_mcp import config as mc

MINIMAL = {
    "vissim": {"progid": "Vissim.Vissim.26"},
    "port": 8766,
    "protected": [],
    "protected_roots": ["D:\\background_data"],
}


def _write(tmp_path, data):
    p = tmp_path / "vissim_mcp.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_missing_required_key_error_names_the_key(tmp_path):
    data = dict(MINIMAL)
    del data["port"]
    del data["protected_roots"]
    with pytest.raises(mc.ConfigError) as ei:
        mc.load_config(_write(tmp_path, data))
    assert "port" in str(ei.value) and "protected_roots" in str(ei.value)


def test_nested_required_key_vissim_progid(tmp_path):
    data = dict(MINIMAL)
    data["vissim"] = {}
    with pytest.raises(mc.ConfigError, match="vissim.progid"):
        mc.load_config(_write(tmp_path, data))


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(mc.ConfigError):
        mc.load_config(tmp_path / "missing.json")


def test_tunable_key_defaults_live_in_one_place(tmp_path):
    cfg = mc.load_config(_write(tmp_path, MINIMAL))
    for k, v in mc.TUNABLE_DEFAULTS.items():
        if k == "log_root":
            continue
        assert cfg[k] == v
    assert cfg["quick_mode_default"] is True
    assert cfg["sim_chunk_s"] == 60
    assert cfg["quit_prompt_timeout_s"] == 60
    assert cfg["list_limit_default"] == 200
    assert cfg["com_retry_count"] == 5
    assert cfg["com_retry_interval_s"] == 0.5
    assert cfg["vissim"]["exe"].endswith("Vissim260.exe")
    assert cfg["vissim"]["progid"] == "Vissim.Vissim.26"


def test_no_host_config_key_only_a_constant(tmp_path):
    cfg = mc.load_config(_write(tmp_path, MINIMAL))
    assert "host" not in cfg
    assert mc.HOST == "127.0.0.1"


def test_repo_root_is_derived_from_the_package():
    root = mc.repo_root()
    assert (root / "src" / "vissim_mcp" / "config.py").is_file()
    assert (root / "pyproject.toml").is_file()


def test_relative_log_root_becomes_absolute_against_repo_root(tmp_path, monkeypatch):
    # It must be the same from any folder — changing cwd does not change the result.
    monkeypatch.chdir(tmp_path)
    cfg = mc.load_config(_write(tmp_path, MINIMAL))
    p = Path(cfg["log_root"])
    assert p.is_absolute()
    assert p == (mc.repo_root() / "runs" / "mcp").resolve()


def test_absolute_log_root_is_kept(tmp_path):
    data = dict(MINIMAL)
    data["log_root"] = str(tmp_path / "logs")
    cfg = mc.load_config(_write(tmp_path, data))
    assert Path(cfg["log_root"]) == (tmp_path / "logs").resolve()


def test_protected_entry_format_validation(tmp_path):
    data = dict(MINIMAL)
    data["protected"] = [{"type": "Link", "ops": ["fly"], "attrs": []}]
    with pytest.raises(mc.ConfigError, match="protected"):
        mc.load_config(_write(tmp_path, data))
    data["protected"] = [{"type": "Link", "ops": ["set"]}]   # attrs missing
    with pytest.raises(mc.ConfigError, match="attrs"):
        mc.load_config(_write(tmp_path, data))
    data["protected"] = "Link"   # not a list
    with pytest.raises(mc.ConfigError, match="protected"):
        mc.load_config(_write(tmp_path, data))


def test_port_must_be_an_integer(tmp_path):
    data = dict(MINIMAL)
    data["port"] = "8766"
    with pytest.raises(mc.ConfigError, match="port"):
        mc.load_config(_write(tmp_path, data))


def test_repo_config_file_loads_with_the_right_initial_values():
    cfg = mc.load_config()
    assert cfg["port"] == 8766
    assert cfg["protected_roots"] == []
    types = {e["type"]: e for e in cfg["protected"]}
    assert set(types) == {"Link", "Connector", "LinkPolyPoint", "NetPara"}
    assert types["Link"]["ops"] == ["create", "delete", "set"]
    assert types["Link"]["attrs"][:4] == ["Wkt*", "Length*", "NumLanes", "IsConn"]   # geometry and structure attributes narrowed down by probing
    assert types["Connector"]["ops"] == ["create", "delete", "set"]
    assert types["Connector"]["attrs"] == types["Link"]["attrs"]
    assert types["LinkPolyPoint"]["ops"] == ["set"] and types["LinkPolyPoint"]["attrs"] == ["*"]
    assert types["NetPara"]["ops"] == ["set"]
    assert types["NetPara"]["attrs"] == ["RefPointNet*", "RefPointMap*", "UnitLen*", "TrafReg", "LinkGradBas"]
    assert cfg["_path"] == str((mc.repo_root() / "config" / "vissim_mcp.json").resolve())


def test_exe_default_is_vissim_2026_install_path():
    assert mc.VISSIM_DEFAULTS["exe"].endswith(r"PTV Vissim 2026\Exe\Vissim260.exe")
