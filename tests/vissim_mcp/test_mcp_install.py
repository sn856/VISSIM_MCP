"""Install and uninstall: only a fake registry layer and temporary folders are used (the real registry, desktop and %LOCALAPPDATA% stay untouched)."""
import base64
import json
from pathlib import Path

import pytest

from vissim_mcp import install as inst

PROGID = "Vissim input file"
EXE = r"C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe"
PYW = r"C:\Py\pythonw.exe"
REPO = r"C:\Repo"


class FakeRegistry:
    """Imitates only HKCU\\Software\\Classes (case-insensitive) plus the default values of the merged HKCR view."""

    def __init__(self, hkcr=None):
        self.hkcr = dict(hkcr or {})
        self.keys = {}            # lower path -> [orig path, {lower name: (orig name, data)}]
        self.fail_delete = set()  # keys/values whose deletion fails (lower key or (lower key, lower name))
        self.writes = 0

    # test setup helpers
    def put(self, key, name, data):
        self._ensure(key)
        self.keys[key.lower()][1][name.lower()] = (name, data)

    def _ensure(self, key):
        parts = key.split("\\")
        for i in range(1, len(parts) + 1):
            p = "\\".join(parts[:i])
            self.keys.setdefault(p.lower(), [p, {}])

    def values(self, key):
        k = self.keys.get(key.lower())
        return None if k is None else {n: d for n, d in k[1].values()}

    # layer contract
    def hkcr_default(self, subkey):
        return self.hkcr.get(subkey.lower())

    def walk(self, key):
        lk = key.lower()
        out = []
        for p, (orig, vals) in sorted(self.keys.items()):
            if p == lk or p.startswith(lk + "\\"):
                out.append((orig, {n: d for n, d in vals.values()}))
        return out

    def key_exists(self, key):
        return key.lower() in self.keys

    def value_exists(self, key, name):
        k = self.keys.get(key.lower())
        return k is not None and name.lower() in k[1]

    def set_value(self, key, name, data):
        self.writes += 1
        self.put(key, name, data)

    def delete_value(self, key, name):
        if (key.lower(), name.lower()) in self.fail_delete:
            raise OSError("access denied")
        del self.keys[key.lower()][1][name.lower()]

    def is_empty(self, key):
        lk = key.lower()
        if self.keys[lk][1]:
            return False
        return not any(p.startswith(lk + "\\") for p in self.keys)

    def delete_key(self, key):
        if key.lower() in self.fail_delete:
            raise OSError("access denied")
        assert self.is_empty(key)
        del self.keys[key.lower()]


def _h(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(data).hexdigest()[:8]


class World:
    def __init__(self, tmp_path, *, progid=PROGID, icon_ok=True):
        self.reg = FakeRegistry({".inpx": progid} if progid else {})
        self.lad = tmp_path / "LocalAppData"
        self.desk = tmp_path / "OneDrive" / "Desktop"
        self.desk.mkdir(parents=True)
        self.startup = tmp_path / "Roaming" / "Startup"
        self.shortcuts = []
        self.notified = 0
        self.icon_calls = []
        self.icon_ok = icon_ok
        self.shortcut_icon_fail = False
        self.lines = []
        self.clock = "2026-10-18T20:00:00"

    def _icon(self, exe, path):
        self.icon_calls.append((exe, Path(path)))
        if not self.icon_ok:
            return None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"ICO")
        return Path(path)

    def _shortcut_icon(self, exe, path):
        self.icon_calls.append(("shortcut", exe, Path(path)))
        if self.shortcut_icon_fail:
            raise RuntimeError("drawing failed")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"ICO2")
        return Path(path)

    def _shortcut(self, sc):
        self.shortcuts.append(sc)
        Path(sc.path).write_bytes(b"LNK")

    def _notify(self):
        self.notified += 1

    def deps(self):
        return inst.InstallDeps(
            registry=self.reg,
            local_appdata=lambda: self.lad,
            desktop_dir=lambda: self.desk,
            make_shortcut=self._shortcut,
            notify=self._notify,
            write_icon=self._icon,
            now=lambda: self.clock,
            pythonw=lambda: PYW,
            repo_root=lambda: Path(REPO),
            out=self.lines.append,
            startup_dir=lambda: self.startup,
            write_shortcut_icon=self._shortcut_icon,
        )

    @property
    def ico(self):
        """The launcher icon with the content tag the install adds (fake content b"ICO")."""
        return self.lad / "vissim_mcp" / "icons" / f"launcher_{_h(b'ICO')}.ico"

    @property
    def ico_raw(self):
        """The path the install hands over for writing the icon (before the tag is added)."""
        return self.lad / "vissim_mcp" / "icons" / "launcher.ico"

    @property
    def shortcut_ico(self):
        return self.lad / "vissim_mcp" / "icons" / f"shortcut_{_h(b'ICO2')}.ico"

    @property
    def record_path(self):
        return self.lad / "vissim_mcp" / "install.json"

    @property
    def lnk(self):
        return self.desk / "VISSIM MCP.lnk"

    @property
    def auto(self):
        return self.startup / "VISSIM MCP Server.lnk"

    def text(self):
        return "\n".join(self.lines)


def _cfg(**over):
    cfg = {"vissim": {"exe": EXE}, "shortcut_name": "VISSIM MCP"}
    cfg.update(over)
    return cfg


def _nothing_written(w):
    assert w.reg.writes == 0
    assert not w.record_path.exists()
    assert w.shortcuts == []
    assert w.notified == 0


# ---------- install refused ----------

def test_progid_missing_refuses_and_writes_nothing(tmp_path):
    w = World(tmp_path, progid=None)
    assert inst.install(_cfg(), deps=w.deps()) == 1
    _nothing_written(w)
    assert w.icon_calls == []
    assert not w.lad.exists()
    assert "VISSIM" in w.text()


def test_progid_blank_refuses(tmp_path):
    w = World(tmp_path, progid="  ")
    assert inst.install(_cfg(), deps=w.deps()) == 1
    _nothing_written(w)


def test_foreign_values_refuse_with_listing(tmp_path):
    w = World(tmp_path)
    w.reg.put(PROGID + r"\shell\open\command", "", "notepad.exe %1")
    w.reg.put(PROGID + r"\DefaultIcon", "", r"C:\x.ico")
    before = json.dumps(w.reg.keys, sort_keys=True)
    assert inst.install(_cfg(), deps=w.deps()) == 1
    assert json.dumps(w.reg.keys, sort_keys=True) == before
    assert w.icon_calls == []
    assert not w.lad.exists()
    assert w.shortcuts == [] and w.notified == 0
    t = w.text()
    assert r"shell\open\command" in t
    assert "notepad.exe %1" in t
    assert "DefaultIcon" in t


def test_foreign_value_inside_our_recorded_key_refuses(tmp_path):
    w = World(tmp_path)
    assert inst.install(_cfg(), deps=w.deps()) == 0
    w.reg.put(PROGID + r"\shell\vissim_mcp", "Extended", "")
    w.lines.clear()
    assert inst.install(_cfg(), deps=w.deps()) == 1
    assert "Extended" in w.text()


def test_icon_failure_refuses_and_writes_nothing_else(tmp_path):
    w = World(tmp_path, icon_ok=False)
    assert inst.install(_cfg(), deps=w.deps()) == 1
    assert w.icon_calls == [(EXE, w.ico_raw)]
    assert w.reg.writes == 0
    assert not w.record_path.exists()
    assert w.shortcuts == [] and w.notified == 0
    assert "icon" in w.text()


# ---------- normal install ----------

def test_install_writes_exact_registry_values(tmp_path):
    w = World(tmp_path)
    assert inst.install(_cfg(), deps=w.deps()) == 0
    ico = str(w.ico)
    assert w.reg.values(PROGID + r"\DefaultIcon") == {"": ico}
    assert w.reg.values(PROGID + r"\shell") == {"": "vissim_mcp"}
    assert w.reg.values(PROGID + r"\shell\vissim_mcp") == {"": "Open with VISSIM MCP", "Icon": ico}
    assert w.reg.values(PROGID + r"\shell\vissim_mcp\command") == {
        "": '"C:\\Py\\pythonw.exe" -m vissim_mcp open "%1"'}
    # No value is written at the ProgID root (so VISSIM's display name is not hidden)
    assert w.reg.values(PROGID) == {}
    # Nothing is written outside this tool's five keys
    assert sorted(w.reg.keys) == sorted(k.lower() for k in (
        PROGID, PROGID + r"\DefaultIcon", PROGID + r"\shell",
        PROGID + r"\shell\vissim_mcp", PROGID + r"\shell\vissim_mcp\command"))


def test_install_icon_written_from_cfg_exe(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    assert w.icon_calls == [(EXE, w.ico_raw), ("shortcut", EXE, w.ico.parent / "shortcut.ico")]
    assert w.ico.is_file()
    # No server icon file is created (only the launcher one and the shortcut one)
    assert sorted(p.name for p in w.ico.parent.iterdir()) == sorted([w.ico.name, w.shortcut_ico.name])


def test_install_shortcut_spec(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    assert len(w.shortcuts) == 2
    sc = w.shortcuts[0]
    assert Path(sc.path) == w.lnk
    assert sc.target == PYW
    assert sc.arguments == "-m vissim_mcp open"
    assert Path(sc.icon) == w.shortcut_ico                    # the desktop shortcut uses the "Vis"/"MCP" icon
    assert Path(sc.workdir) == Path(REPO)


def test_install_shortcut_name_is_tunable(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(shortcut_name="Traffic VISSIM"), deps=w.deps())
    assert Path(w.shortcuts[0].path) == w.desk / "Traffic VISSIM.lnk"


def test_install_json_records_everything(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    rec = json.loads(w.record_path.read_text(encoding="utf-8"))
    assert rec["progid"] == PROGID
    assert rec["pythonw"] == PYW
    assert rec["installed_at"] == w.clock
    assert Path(rec["icon"]) == w.ico
    assert Path(rec["icons_dir"]) == w.ico.parent
    assert Path(rec["shortcut"]) == w.lnk
    assert rec["registry_root"] == r"HKCU\Software\Classes"
    assert sorted(rec["registry_keys"]) == sorted([
        PROGID, PROGID + r"\DefaultIcon", PROGID + r"\shell",
        PROGID + r"\shell\vissim_mcp", PROGID + r"\shell\vissim_mcp\command"])
    vals = {(v["key"], v["name"]): v["data"] for v in rec["registry_values"]}
    assert vals == {
        (PROGID + r"\DefaultIcon", ""): str(w.ico),
        (PROGID + r"\shell", ""): "vissim_mcp",
        (PROGID + r"\shell\vissim_mcp", ""): "Open with VISSIM MCP",
        (PROGID + r"\shell\vissim_mcp", "Icon"): str(w.ico),
        (PROGID + r"\shell\vissim_mcp\command", ""): '"C:\\Py\\pythonw.exe" -m vissim_mcp open "%1"',
    }


def test_install_notifies_explorer_once(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    assert w.notified == 1


def test_install_twice_is_idempotent(tmp_path):
    w = World(tmp_path)
    assert inst.install(_cfg(), deps=w.deps()) == 0
    keys1 = json.dumps(w.reg.keys, sort_keys=True)
    rec1 = json.loads(w.record_path.read_text(encoding="utf-8"))
    w.clock = "2026-10-19T09:00:00"
    assert inst.install(_cfg(), deps=w.deps()) == 0
    assert json.dumps(w.reg.keys, sort_keys=True) == keys1
    rec2 = json.loads(w.record_path.read_text(encoding="utf-8"))
    assert rec2["installed_at"] == "2026-10-19T09:00:00"
    rec1.pop("installed_at"), rec2.pop("installed_at")
    assert rec1 == rec2
    assert w.notified == 2
    assert w.lnk.is_file()


def test_reinstall_after_partial_uninstall_keys_keeps_record_complete(tmp_path):
    """After one recorded key has disappeared, installing again recreates that key and records it."""
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    w.reg.delete_value(PROGID + r"\DefaultIcon", "")
    w.reg.delete_key(PROGID + r"\DefaultIcon")
    assert inst.install(_cfg(), deps=w.deps()) == 0
    rec = json.loads(w.record_path.read_text(encoding="utf-8"))
    assert PROGID + r"\DefaultIcon" in rec["registry_keys"]
    assert w.reg.values(PROGID + r"\DefaultIcon") == {"": str(w.ico)}


def test_reinstall_with_other_shortcut_name_removes_old_shortcut(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    old = w.lnk
    inst.install(_cfg(shortcut_name="New name"), deps=w.deps())
    assert not old.exists()
    assert (w.desk / "New name.lnk").is_file()


def test_shortcut_failure_returns_1_and_keeps_record_for_uninstall(tmp_path):
    w = World(tmp_path)

    def boom(sc):
        raise inst.InstallError("shortcut failed")

    d = w.deps()
    d.make_shortcut = boom
    assert inst.install(_cfg(), deps=d) == 1
    rec = json.loads(w.record_path.read_text(encoding="utf-8"))
    assert rec["registry_keys"]          # the registry entries already written are recorded so uninstall can remove them
    assert rec.get("shortcut") in (None, "")
    assert "shortcut failed" in w.text()
    assert inst.uninstall(deps=w.deps()) == 0
    assert w.reg.keys == {}


# ---------- uninstall ----------

def test_uninstall_without_record_returns_0(tmp_path):
    w = World(tmp_path)
    assert inst.uninstall(deps=w.deps()) == 0
    assert "No install record" in w.text()
    assert w.notified == 0


def test_uninstall_removes_only_recorded_items(tmp_path):
    w = World(tmp_path)
    w.reg.put("Other.Prog\\shell\\open\\command", "", "x.exe")       # someone else's sibling key
    inst.install(_cfg(), deps=w.deps())
    w.reg.put(PROGID + "_sibling", "", "not ours")                     # someone else's sibling key created after the install
    (w.desk / "other shortcut.lnk").write_bytes(b"x")
    (w.lad / "vissim_mcp" / "launcher.log").write_text("x", encoding="utf-8")
    w.notified = 0
    assert inst.uninstall(deps=w.deps()) == 0
    assert w.reg.values("Other.Prog\\shell\\open\\command") == {"": "x.exe"}
    assert w.reg.values(PROGID + "_sibling") == {"": "not ours"}
    assert not w.reg.key_exists(PROGID)
    assert not w.lnk.exists()
    assert (w.desk / "other shortcut.lnk").exists()
    assert not w.ico.parent.exists()
    assert not w.record_path.exists()
    assert (w.lad / "vissim_mcp" / "launcher.log").exists()
    assert w.notified == 1


def test_uninstall_keeps_recorded_key_that_gained_foreign_value(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    w.reg.put(PROGID + r"\shell", "Extra", "foreign value")
    assert inst.uninstall(deps=w.deps()) == 1
    assert w.reg.values(PROGID + r"\shell") == {"Extra": "foreign value"}
    assert not w.reg.key_exists(PROGID + r"\shell\vissim_mcp")
    assert w.record_path.exists()          # kept so uninstall can be run again
    assert "Extra" in w.text() or r"\shell" in w.text()


def test_uninstall_skips_missing_items_and_reports(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    w.lnk.unlink()
    w.reg.delete_value(PROGID + r"\shell\vissim_mcp", "Icon")
    w.reg.delete_value(PROGID + r"\DefaultIcon", "")
    w.reg.delete_key(PROGID + r"\DefaultIcon")
    import shutil
    shutil.rmtree(w.ico.parent)
    w.lines.clear()
    assert inst.uninstall(deps=w.deps()) == 0
    assert w.reg.keys == {}
    assert not w.record_path.exists()
    t = w.text()
    assert "already gone" in t
    assert "VISSIM MCP.lnk" in t
    assert "DefaultIcon" in t


def test_uninstall_delete_failure_returns_1_and_keeps_record(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    w.reg.fail_delete.add(((PROGID + r"\shell\vissim_mcp\command").lower(), ""))
    assert inst.uninstall(deps=w.deps()) == 1
    assert w.record_path.exists()
    assert w.reg.value_exists(PROGID + r"\shell\vissim_mcp\command", "")
    assert w.notified == 2                  # install 1 + uninstall 1 (something was removed, so it notifies)
    # Once the fault is fixed, uninstall can finish
    w.reg.fail_delete.clear()
    assert inst.uninstall(deps=w.deps()) == 0
    assert w.reg.keys == {}


def test_uninstall_corrupt_record_returns_1_and_deletes_nothing(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    w.record_path.write_text("{corrupt", encoding="utf-8")
    keys = json.dumps(w.reg.keys, sort_keys=True)
    assert inst.uninstall(deps=w.deps()) == 1
    assert json.dumps(w.reg.keys, sort_keys=True) == keys
    assert w.lnk.exists()


def test_uninstall_refuses_icons_dir_outside_own_folder(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    victim = tmp_path / "victim"
    victim.mkdir()
    rec = json.loads(w.record_path.read_text(encoding="utf-8"))
    rec["icons_dir"] = str(victim)
    w.record_path.write_text(json.dumps(rec), encoding="utf-8")
    assert inst.uninstall(deps=w.deps()) == 1
    assert victim.exists()


def test_uninstall_cfg_argument_is_optional(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    assert inst.uninstall(_cfg(), deps=w.deps()) == 0


# ---------- pure parts of the real layer (system untouched) ----------

def test_command_line_quotes_pythonw_and_target():
    assert inst.command_line(r"C:\가 나\pythonw.exe") == '"C:\\가 나\\pythonw.exe" -m vissim_mcp open "%1"'   # non-ASCII (Korean) folder name kept as Unicode test data


def test_find_pythonw_prefers_pythonw_next_to_python(tmp_path):
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    assert inst.find_pythonw(str(py)) == str(py)          # python.exe if there is no pythonw
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert inst.find_pythonw(str(py)) == str(tmp_path / "pythonw.exe")


def test_powershell_shortcut_script_and_runner(tmp_path):
    lnk = tmp_path / "it's 바탕" / "VISSIM MCP.lnk"   # non-ASCII (Korean) folder name kept as Unicode test data
    lnk.parent.mkdir()
    calls = []

    def run(argv):
        calls.append(argv)
        lnk.write_bytes(b"LNK")
        return 0, ""

    sc = inst.Shortcut(path=str(lnk), target=PYW, arguments="-m vissim_mcp open",
                       icon=r"C:\L\launcher.ico", workdir=REPO)
    inst.powershell_shortcut(sc, run=run)
    argv = calls[0]
    assert argv[0].lower().endswith("powershell.exe")
    assert "-EncodedCommand" in argv
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "WScript.Shell" in script
    assert "it''s 바탕" in script                     # single-quote escaping
    assert f"TargetPath = '{PYW}'" in script
    assert "Arguments = '-m vissim_mcp open'" in script
    assert r"IconLocation = 'C:\L\launcher.ico,0'" in script
    assert f"WorkingDirectory = '{REPO}'" in script
    assert ".Save()" in script


def test_powershell_shortcut_failure_raises(tmp_path):
    sc = inst.Shortcut(path=str(tmp_path / "x.lnk"), target=PYW, arguments="", icon="i", workdir=REPO)
    with pytest.raises(inst.InstallError):
        inst.powershell_shortcut(sc, run=lambda argv: (1, "error"))
    with pytest.raises(inst.InstallError):          # reported success but the file is missing
        inst.powershell_shortcut(sc, run=lambda argv: (0, ""))


def test_desktop_from_shell_folder_expands_env(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\u")
    assert inst.desktop_from_shell_folder(lambda: r"%USERPROFILE%\OneDrive\Desktop") == Path(
        r"C:\Users\u\OneDrive\Desktop")
    assert inst.desktop_from_shell_folder(lambda: None, home=Path(r"C:\Users\u")) == Path(r"C:\Users\u\Desktop")


def test_default_deps_is_lazy_and_not_called_by_tests():
    """The default layer does nothing to the system just by being constructed (it reads only when a value is requested)."""
    d = inst.default_deps()
    assert callable(d.notify) and callable(d.make_shortcut)


# ---------- server autostart at login ----------

def test_install_adds_startup_shortcut_for_server(tmp_path):
    w = World(tmp_path)
    assert inst.install(_cfg(), deps=w.deps()) == 0
    sc = w.shortcuts[1]
    assert Path(sc.path) == w.auto and w.auto.is_file()
    assert sc.target == PYW and sc.arguments == "-m vissim_mcp --serve"
    assert Path(sc.icon) == w.ico and Path(sc.workdir) == Path(REPO)
    rec = json.loads(w.record_path.read_text(encoding="utf-8"))
    assert Path(rec["startup_shortcut"]) == w.auto


def test_uninstall_removes_startup_shortcut(tmp_path):
    w = World(tmp_path)
    inst.install(_cfg(), deps=w.deps())
    assert inst.uninstall(deps=w.deps()) == 0
    assert not w.auto.exists() and not w.lnk.exists() and not w.record_path.exists()


def test_startup_shortcut_failure_is_recorded_for_uninstall(tmp_path):
    w = World(tmp_path)
    real = w._shortcut

    def flaky(sc):
        if "Startup" in sc.path:
            raise inst.InstallError("blocked")
        real(sc)
    w._shortcut = flaky
    assert inst.install(_cfg(), deps=w.deps()) == 1
    rec = json.loads(w.record_path.read_text(encoding="utf-8"))
    assert rec["startup_shortcut"] is None and Path(rec["shortcut"]) == w.lnk
    assert "autostart" in w.text()
    w._shortcut = real
    assert inst.uninstall(deps=w.deps()) == 0 and not w.lnk.exists()


def test_no_startup_dir_skips_autostart(tmp_path):
    w = World(tmp_path)
    d = w.deps()
    d.startup_dir = None
    assert inst.install(_cfg(), deps=d) == 0
    assert len(w.shortcuts) == 1
    assert json.loads(w.record_path.read_text(encoding="utf-8"))["startup_shortcut"] is None


def test_startup_from_shell_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roam"))
    assert inst.startup_from_shell_folder(lambda: r"%APPDATA%\X\Startup") == tmp_path / "Roam" / "X" / "Startup"
    assert inst.startup_from_shell_folder(lambda: None) == (
        tmp_path / "Roam" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")



def test_shortcut_icon_failure_falls_back_to_launcher_icon(tmp_path):
    w = World(tmp_path)
    w.shortcut_icon_fail = True
    assert inst.install(_cfg(), deps=w.deps()) == 0
    assert Path(w.shortcuts[0].icon) == w.ico
    assert "Vis/MCP" in w.text()



def test_icon_file_names_carry_content_tag_and_old_ones_go(tmp_path):
    """The Windows icon cache remembers by path -> when the image changes the file name changes too and the old file is deleted."""
    w = World(tmp_path)
    assert inst.install(_cfg(), deps=w.deps()) == 0
    first = sorted(p.name for p in w.ico.parent.iterdir())
    real = w._icon

    def other(exe, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"ICO-NEW")
        w.icon_calls.append((exe, Path(path)))
        return Path(path)
    w._icon = other
    assert inst.install(_cfg(), deps=w.deps()) == 0
    names = sorted(p.name for p in w.ico.parent.iterdir())
    new_launcher = f"launcher_{_h(b'ICO-NEW')}.ico"
    assert new_launcher in names and w.ico.name not in names and len(names) == 2
    assert names != first
    assert w.reg.values(PROGID + r"\DefaultIcon") == {"": str(w.ico.parent / new_launcher)}
    w._icon = real


# ---------- install refused inside an app (package) virtual space ----------

def test_install_inside_virtualized_app_without_runner_refuses(tmp_path):
    w = World(tmp_path)
    d = w.deps()
    d.virtualized = lambda: "Claude_pzs8sxrjxfjjc"
    assert inst.install(_cfg(), deps=d) == 1
    _nothing_written(w)
    assert w.icon_calls == [] and not w.lad.exists()
    assert "regular console" in w.text() and "Claude_pzs8sxrjxfjjc" in w.text()


def test_install_inside_virtualized_app_runs_outside(tmp_path):
    w = World(tmp_path)
    d = w.deps()
    d.virtualized = lambda: "Claude_x"
    calls = []
    d.run_outside = lambda args: calls.append(args) or (0, "Install finished.")
    assert inst.install(_cfg(_path=r"C:\cfg\vissim_mcp.json"), deps=d) == 0
    assert calls == [["install", "--config", r"C:\cfg\vissim_mcp.json"]]
    _nothing_written(w)                                     # nothing is written in this process (the virtual space)
    assert "Install finished." in w.text()


def test_uninstall_inside_virtualized_app_runs_outside(tmp_path):
    w = World(tmp_path)
    d = w.deps()
    d.virtualized = lambda: "Claude_x"
    d.run_outside = lambda args: (1, "Could not remove: x")
    assert inst.uninstall(deps=d) == 1
    assert "Could not remove: x" in w.text()


def test_run_via_explorer_writes_script_and_reads_result(tmp_path):
    launched = []

    def fake_launch(script):
        launched.append(script)
        text = Path(script).read_text(encoding="utf-8")
        assert "-m vissim_mcp install --config" in text and "chcp 65001" in text
        assert ' echo EXIT=%errorlevel%' in text and "%errorlevel%>>" not in text   # the 0>> trap
        log = Path(script).with_suffix(".log")
        log.write_text("Install finished." + chr(10) + "EXIT=0" + chr(10), encoding="utf-8")   # imitates the process outside the app
    code, out = inst.run_via_explorer(["install", "--config", r"C:\a b\c.json"], python_exe=r"C:\Py\python.exe",
                                      workdir=Path(REPO), temp_dir=tmp_path, launch=fake_launch,
                                      clock=lambda: 0.0, sleep=lambda s: None)
    assert code == 0 and out == "Install finished."
    assert len(launched) == 1 and not list(tmp_path.iterdir())   # script and log are deleted when finished


def test_run_via_explorer_timeout_keeps_files(tmp_path):
    t = [0.0]

    def sleep(s):
        t[0] += s
    code, out = inst.run_via_explorer(["uninstall"], python_exe="py.exe", workdir=Path(REPO), temp_dir=tmp_path,
                                      launch=lambda p: None, clock=lambda: t[0], sleep=sleep, wait_s=2.0)
    assert code == 1 and "did not finish" in out
    assert len(list(tmp_path.glob("*.cmd"))) == 1


def test_detect_appdata_virtualization(tmp_path):
    lad = tmp_path / "Local"
    lad.mkdir()
    assert inst.detect_appdata_virtualization(lad) is None
    assert not list(lad.glob("vissim_mcp_probe_*"))         # the probe file is deleted


def test_detector_failure_does_not_block(tmp_path):
    w = World(tmp_path)
    d = w.deps()

    def boom():
        raise RuntimeError("x")
    d.virtualized = boom
    assert inst.install(_cfg(), deps=d) == 0
