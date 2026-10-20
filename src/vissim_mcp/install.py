"""Install and uninstall the `.inpx` double-click launcher.

The install writes to only four places.
- This tool's key under `HKCU\\Software\\Classes\\<.inpx ProgID>` (file icon, default action, one menu entry)
- One desktop shortcut
- One shortcut in the Startup folder (starts the server at login: `pythonw -m vissim_mcp --serve`; if the server is already
  running, that process exits quietly). So that the server is already up when a session opens and Claude's vissim connection does not fail.
- `%LOCALAPPDATA%\\vissim_mcp\\` (launcher icon `icons\\launcher.ico`, install record `install.json`)

HKLM, the VISSIM install folder and menu entries registered by VISSIM are not touched. Uninstall removes only what `install.json` lists.

The registry, folder locations, shortcut creation (PowerShell), the Explorer notification, icon writing, the clock and the pythonw location
all sit behind the `InstallDeps` layer, which tests replace with fakes (tests never touch the real registry, desktop or %LOCALAPPDATA%).
`win32com` is not used: shortcuts go through a PowerShell (WScript.Shell) subprocess and the notification through ctypes.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from . import config as _config

REGISTRY_ROOT = r"HKCU\Software\Classes"
_CLASSES = r"Software\Classes"
EXTENSION = ".inpx"
VERB = "vissim_mcp"
VERB_LABEL = "Open with VISSIM MCP"
SHORTCUT_ARGS = "-m vissim_mcp open"
AUTOSTART_NAME = "VISSIM MCP Server"
AUTOSTART_ARGS = "-m vissim_mcp --serve"
APP_DIR = "vissim_mcp"
ICONS_DIR = "icons"
ICON_NAME = "launcher.ico"
SHORTCUT_ICON_NAME = "shortcut.ico"
RECORD_NAME = "install.json"
RECORD_VERSION = 1

SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000
_CREATE_NO_WINDOW = 0x08000000


class InstallError(RuntimeError):
    """An install step failed (e.g. creating a shortcut). The message is shown to the user as is."""


@dataclass
class Shortcut:
    path: str
    target: str
    arguments: str
    icon: str
    workdir: str


class Registry(Protocol):
    """Registry layer addressed by relative paths under `HKCU\\Software\\Classes` (plus reads of the merged HKCR view)."""

    def hkcr_default(self, subkey: str) -> str | None: ...
    def walk(self, key: str) -> list[tuple[str, dict[str, Any]]]: ...
    def key_exists(self, key: str) -> bool: ...
    def value_exists(self, key: str, name: str) -> bool: ...
    def set_value(self, key: str, name: str, data: str) -> None: ...
    def delete_value(self, key: str, name: str) -> None: ...
    def is_empty(self, key: str) -> bool: ...
    def delete_key(self, key: str) -> None: ...


@dataclass
class InstallDeps:
    registry: Registry
    local_appdata: Callable[[], Path]
    desktop_dir: Callable[[], Path]
    make_shortcut: Callable[[Shortcut], None]
    notify: Callable[[], None]
    write_icon: Callable[[str, Path], Path | None]
    now: Callable[[], str]
    pythonw: Callable[[], str]
    repo_root: Callable[[], Path]
    out: Callable[[str], None] = field(default=print)
    startup_dir: Callable[[], Path] | None = None    # Startup folder. If None, no autostart is added
    write_shortcut_icon: Callable[[str, Path], Path | None] | None = None   # "Vis"/"MCP" shortcut icon. If None or it fails, the launcher icon
    virtualized: Callable[[], str | None] | None = None   # inside an app (MSIX) virtual space: that package folder name. If None, no check
    run_outside: Callable[[list[str]], tuple[int, str]] | None = None   # run a command outside the virtual space (via Explorer) -> (exit code, output)


# ---------- pure helpers ----------

def command_line(pythonw: str) -> str:
    """The double-click command. Both paths are quoted so spaces or non-ASCII characters in the path are fine."""
    return f'"{pythonw}" -m vissim_mcp open "%1"'


def find_pythonw(executable: str | None = None) -> str:
    """pythonw.exe next to the Python that ran the install (or that Python itself if there is none)."""
    p = Path(executable or sys.executable)
    w = p.with_name("pythonw.exe")
    return str(w) if w.is_file() else str(p)


def desktop_from_shell_folder(read: Callable[[], str | None], *, home: Path | None = None) -> Path:
    """The Desktop value of the user shell folders (environment variables expanded, so an OneDrive desktop is honored). Falls back to home\\Desktop."""
    raw = None
    try:
        raw = read()
    except OSError:
        raw = None
    if raw:
        return Path(os.path.expandvars(raw))
    return (home or Path.home()) / "Desktop"


def startup_from_shell_folder(read: Callable[[], str | None], *, appdata: Path | None = None) -> Path:
    """The Startup value of the user shell folders (environment variables expanded). Falls back to %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup."""
    raw = None
    try:
        raw = read()
    except OSError:
        raw = None
    if raw:
        return Path(os.path.expandvars(raw))
    base = appdata or Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def planned_values(progid: str, ico: str, pythonw: str) -> list[tuple[str, str, str]]:
    """(key, value name, data). The key is a relative path under `HKCU\\Software\\Classes`. No value is written at the ProgID root."""
    verb = rf"{progid}\shell\{VERB}"
    return [
        (rf"{progid}\DefaultIcon", "", ico),
        (rf"{progid}\shell", "", VERB),
        (verb, "", VERB_LABEL),
        (verb, "Icon", ico),
        (rf"{verb}\command", "", command_line(pythonw)),
    ]


def planned_keys(progid: str) -> list[str]:
    verb = rf"{progid}\shell\{VERB}"
    return [progid, rf"{progid}\DefaultIcon", rf"{progid}\shell", verb, rf"{verb}\command"]


def _full(key: str) -> str:
    return rf"{REGISTRY_ROOT}\{key}"


def _vname(name: str) -> str:
    return name if name else "(default)"


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _app_dir(deps: InstallDeps) -> Path:
    return Path(deps.local_appdata()) / APP_DIR


def _read_record(path: Path) -> dict:
    """Read install.json. FileNotFoundError if missing, ValueError if corrupt."""
    rec = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rec, dict):
        raise ValueError("not an object")
    return rec


def _save_record(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _foreign(reg: Registry, progid: str, rec: dict | None) -> list[str]:
    """Keys and values under the ProgID that are not in the record (human-readable lines)."""
    keys = {k.lower() for k in (rec or {}).get("registry_keys", [])}
    vals = {(v["key"].lower(), v["name"].lower()) for v in (rec or {}).get("registry_values", [])}
    lines: list[str] = []
    for key, values in reg.walk(progid):
        if key.lower() not in keys:
            lines.append(f"  key {_full(key)}")
            for name, data in values.items():
                lines.append(f"    {_vname(name)} = {data}")
            continue
        for name, data in values.items():
            if (key.lower(), name.lower()) not in vals:
                lines.append(f"  value {_full(key)} : {_vname(name)} = {data}")
    return lines


# ---------- install ----------

def install(cfg: dict, *, deps: InstallDeps | None = None) -> int:
    """Install. 0 on success / 1 refused or failed (configuration error 2 is on the command-line side)."""
    deps = deps or default_deps()
    out, reg = deps.out, deps.registry

    pkg = _virtualized(deps)
    if pkg:
        return _outside(deps, pkg, ["install"] + (["--config", str(cfg["_path"])] if cfg.get("_path") else []))

    try:
        progid = (reg.hkcr_default(EXTENSION) or "").strip()
    except OSError:
        progid = ""
    if not progid:
        out(f"The {EXTENSION} file type is not registered (VISSIM does not seem to be installed). Nothing was written.")
        return 1

    app = _app_dir(deps)
    record_path = app / RECORD_NAME
    old: dict | None = None
    if record_path.exists():
        try:
            old = _read_record(record_path)
        except (OSError, ValueError) as e:
            out(f"Could not read the previous install record ({e}). Checking without a record.")

    foreign = _foreign(reg, progid, old)
    if foreign:
        out(f"Not installing: values not created by this tool already exist ({_full(progid)}):")
        for line in foreign:
            out(line)
        out("Nothing was written.")
        return 1

    exe = str((cfg.get("vissim") or {}).get("exe") or "")
    ico = app / ICONS_DIR / ICON_NAME
    try:
        written = deps.write_icon(exe, ico)
    except Exception as e:  # noqa: BLE001 — any icon failure is a refusal
        out(f"Failed while creating the icon: {e}")
        written = None
    if written is None:
        out(f"Not installing: could not read the icon from the VISSIM executable: {exe or '(no vissim.exe in the config)'}")
        return 1
    ico = _stamped(Path(written))
    out(f"Icon: {ico}")

    pythonw = deps.pythonw()
    rec: dict[str, Any] = {
        "version": RECORD_VERSION,
        "installed_at": deps.now(),
        "progid": progid,
        "pythonw": pythonw,
        "registry_root": REGISTRY_ROOT,
        "registry_keys": [],
        "registry_values": [],
        "shortcut": None,
        "startup_shortcut": None,
        "icon": str(ico),
        "icons_dir": str(ico.parent),
    }
    # Anything from the previous record that is outside this plan (e.g. keys from before the ProgID changed) but still present is carried over into the record, so uninstall removes it.
    keys = planned_keys(progid)
    vals = planned_values(progid, str(ico), pythonw)
    plan_keys = {k.lower() for k in keys}
    plan_vals = {(k.lower(), n.lower()) for k, n, _ in vals}
    carried_keys = [k for k in (old or {}).get("registry_keys", [])
                    if k.lower() not in plan_keys and reg.key_exists(k)]
    carried_vals = [v for v in (old or {}).get("registry_values", [])
                    if (v["key"].lower(), v["name"].lower()) not in plan_vals and reg.value_exists(v["key"], v["name"])]
    rec["registry_keys"] = carried_keys + keys

    def fail(msg: str) -> int:
        _save_record(record_path, rec)
        if rec["registry_values"]:
            _notify(deps)
        out(msg)
        out(f"Recorded what was written: {record_path} (uninstall can remove it)")
        return 1

    rec["registry_values"] = list(carried_vals)
    for key, name, data in vals:
        try:
            reg.set_value(key, name, data)
        except OSError as e:
            return fail(f"Could not write to the registry: {_full(key)} : {_vname(name)} ({e})")
        rec["registry_values"].append({"key": key, "name": name, "data": data})
    out(f"Registry: file icon, double-click action and '{VERB_LABEL}' menu under {_full(progid)}")

    name = str(cfg.get("shortcut_name") or _config.TUNABLE_DEFAULTS["shortcut_name"])
    lnk = Path(deps.desktop_dir()) / f"{name}.lnk"
    prev = (old or {}).get("shortcut")
    if prev and not _same_path(prev, lnk) and Path(prev).is_file() and Path(prev).suffix.lower() == ".lnk":
        try:
            Path(prev).unlink()
            out(f"Removed the previous shortcut: {prev}")
        except OSError as e:
            out(f"Could not remove the previous shortcut: {prev} ({e})")
    lnk_icon = ico
    if deps.write_shortcut_icon is not None:
        try:
            made = deps.write_shortcut_icon(exe, ico.parent / SHORTCUT_ICON_NAME)
            lnk_icon = _stamped(Path(made)) if made else ico
        except Exception as e:  # noqa: BLE001 — if only the shortcut icon fails, use the launcher icon
            out(f"Could not create the shortcut icon (Vis/MCP), using the launcher icon: {e}")
    sc = Shortcut(path=str(lnk), target=pythonw, arguments=SHORTCUT_ARGS, icon=str(lnk_icon),
                  workdir=str(deps.repo_root()))
    try:
        deps.make_shortcut(sc)
    except Exception as e:  # noqa: BLE001 — a shortcut failure is reported and returns 1
        return fail(f"Could not create the desktop shortcut: {e}")
    rec["shortcut"] = str(lnk)
    out(f"Shortcut: {lnk}")

    if deps.startup_dir is not None:
        auto = Path(deps.startup_dir()) / f"{AUTOSTART_NAME}.lnk"
        asc = Shortcut(path=str(auto), target=pythonw, arguments=AUTOSTART_ARGS, icon=str(ico),
                       workdir=str(deps.repo_root()))
        try:
            auto.parent.mkdir(parents=True, exist_ok=True)
            deps.make_shortcut(asc)
        except Exception as e:  # noqa: BLE001 — a failure is reported and returns 1 (what was written is recorded)
            return fail(f"Could not create the Startup shortcut (server autostart at login): {e}")
        rec["startup_shortcut"] = str(auto)
        out(f"Server autostart at login: {auto}")

    _remove_stale_icons(ico.parent, keep={ico, Path(lnk_icon)}, out=out)
    _save_record(record_path, rec)
    out(f"Install record: {record_path}")
    _notify(deps)
    out("Install finished. Double-clicking an .inpx now opens it through the VISSIM MCP server.")
    return 0


def _virtualized(deps: InstallDeps) -> str | None:
    if deps.virtualized is None:
        return None
    try:
        return deps.virtualized()
    except Exception:  # noqa: BLE001 — a detection failure does not block (the actual write will show it)
        return None


def detect_appdata_virtualization(local_appdata: Path | None = None) -> str | None:
    """Check with a test file whether files written to %LOCALAPPDATA% are redirected to `Packages\\<app>\\LocalCache\\Local\\`.

    For commands launched inside the Claude desktop app (a Store-style packaged app), even without package info (GetCurrentPackageFullName fails)
    AppData and HKCU writes went into that app's private space (so the installed icon and association were invisible to Explorer).
    Returns the package folder name if redirected, otherwise None.
    """
    base = Path(local_appdata or _local_appdata())
    name = f"vissim_mcp_probe_{os.getpid()}.tmp"
    probe = base / name
    try:
        probe.write_text("x", encoding="ascii")
    except OSError:
        return None
    try:
        for hit in (base / "Packages").glob(f"*/LocalCache/Local/{name}"):
            return hit.parents[2].name
        return None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _outside(deps: InstallDeps, pkg: str, args: list[str]) -> int:
    """Inside an app virtual space, run the same command outside the app via Explorer and return its output and exit code unchanged."""
    out = deps.out
    if deps.run_outside is None:
        out(f"This console is running inside an app ({pkg}), so writes to the registry and %LOCALAPPDATA% go into that app's private virtual space")
        out("(and do not show up in Explorer). Run it again from a regular console such as Windows Terminal or Command Prompt. Nothing was written.")
        return 1
    out(f"Running inside an app ({pkg}), so running `{' '.join(args)}` outside the app via Explorer.")
    try:
        code, text = deps.run_outside(args)
    except Exception as e:  # noqa: BLE001
        out(f"Could not run it outside the app: {e}. Run it directly from a regular console.")
        return 1
    for line in text.splitlines():
        out(line)
    return code


OUTSIDE_WAIT_S = 120.0


def run_via_explorer(args: list[str], *, python_exe: str | None = None, workdir: Path | None = None,
                     temp_dir: Path | None = None, launch: Callable[[str], None] | None = None,
                     clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None,
                     wait_s: float = OUTSIDE_WAIT_S) -> tuple[int, str]:
    """Write a .cmd containing `python -m vissim_mcp <args>` to a temp folder and run it through `explorer.exe`.

    A process opened by Explorer runs outside the app virtual space (tested: %LOCALAPPDATA% writes from a script opened by Explorer
    landed in the real location). Output goes to a temp log; waits (up to wait_s) until the last line `EXIT=<code>` appears.
    """
    import time
    temp = Path(temp_dir or os.environ.get("TEMP") or Path.home())
    tag = f"vissim_mcp_{args[0]}_{os.getpid()}_{int(time.time())}"
    script, logf = temp / f"{tag}.cmd", temp / f"{tag}.log"
    py = python_exe or sys.executable
    quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
    script.write_text("\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        f'cd /d "{workdir or _config.repo_root()}"',
        "set PYTHONIOENCODING=utf-8",
        f'"{py}" -m vissim_mcp {quoted} > "{logf}" 2>&1',
        f'>> "{logf}" echo EXIT=%errorlevel%',   # redirection first: with "EXIT=0>>" cmd reads the 0 as a handle redirect
        "",
    ]), encoding="utf-8")
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    done = False
    try:
        (launch or (lambda p: subprocess.Popen(["explorer.exe", p])))(str(script))
        t0 = clock()
        while clock() - t0 < wait_s:
            if logf.exists():
                lines = logf.read_text(encoding="utf-8", errors="replace").rstrip().splitlines()
                if lines and lines[-1].startswith("EXIT="):
                    done = True
                    try:
                        code = int(lines[-1][5:].strip())
                    except ValueError:
                        code = 1
                    return code, "\n".join(lines[:-1])
            sleep(0.5)
        return 1, f"Running outside the app did not finish within {wait_s:.0f} seconds (log: {logf})."
    finally:
        # If it finished, delete the script and the log. On timeout the script may still be running, so keep both (to check the log).
        for f in ((script, logf) if done else ()):
            try:
                f.unlink()
            except OSError:
                pass


def _stamped(path: Path) -> Path:
    """Rename `name.ico` to `name_<8-char content hash>.ico`. The Windows icon cache remembers by file path,
    so when the image changes the path must change too for Explorer and shortcuts to read the new image (changing only the content under the same name leaves the old image)."""
    import hashlib
    digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
    target = path.with_name(f"{path.stem}_{digest}{path.suffix}")
    if target != path:
        os.replace(path, target)
    return target


def _remove_stale_icons(folder: Path, keep: set[Path], out: Callable[[str], None]) -> None:
    """Delete old `.ico` files in this tool's icon folder that are not used this time (old suffixes and the unsuffixed name)."""
    keep_norm = {os.path.normcase(str(p)) for p in keep}
    for f in folder.glob("*.ico"):
        if os.path.normcase(str(f)) in keep_norm:
            continue
        try:
            f.unlink()
        except OSError as e:
            out(f"Could not remove the old icon: {f} ({e})")


def _notify(deps: InstallDeps) -> None:
    try:
        deps.notify()
    except Exception as e:  # noqa: BLE001 — a notification failure does not affect the result
        deps.out(f"Could not notify Explorer ({e}). It will take effect after you sign out and back in.")


# ---------- uninstall ----------

def uninstall(cfg: dict | None = None, *, deps: InstallDeps | None = None) -> int:
    """Uninstall. 0 on success (or no record) / 1 something could not be removed."""
    deps = deps or default_deps()
    out, reg = deps.out, deps.registry
    pkg = _virtualized(deps)
    if pkg:
        return _outside(deps, pkg, ["uninstall"])
    app = _app_dir(deps)
    record_path = app / RECORD_NAME
    if not record_path.exists():
        out("No install record, so there is nothing to remove.")
        return 0
    try:
        rec = _read_record(record_path)
    except (OSError, ValueError) as e:
        out(f"Could not read the install record, so nothing was removed: {record_path} ({e})")
        return 1

    failed: list[str] = []

    def skip(what: str) -> None:
        out(f"Skipped (already gone): {what}")

    for v in rec.get("registry_values", []):
        key, name = v["key"], v["name"]
        what = f"{_full(key)} : {_vname(name)}"
        if not reg.value_exists(key, name):
            skip(what)
            continue
        try:
            reg.delete_value(key, name)
            out(f"Removed: {what}")
        except OSError as e:
            failed.append(what)
            out(f"Could not remove: {what} ({e})")

    for key in sorted(rec.get("registry_keys", []), key=lambda k: (k.count("\\"), len(k)), reverse=True):
        what = _full(key)
        if not key or key.startswith("."):
            failed.append(what)
            out(f"Not removed (not a key of this tool): {what}")
            continue
        if not reg.key_exists(key):
            skip(what)
            continue
        if not reg.is_empty(key):
            failed.append(what)
            out(f"Kept: {what} — it contains values not created by this tool")
            for k, values in reg.walk(key):
                for n, d in values.items():
                    out(f"    {_full(k)} : {_vname(n)} = {d}")
            continue
        try:
            reg.delete_key(key)
            out(f"Removed: {what}")
        except OSError as e:
            failed.append(what)
            out(f"Could not remove: {what} ({e})")

    for lnk in (rec.get("shortcut"), rec.get("startup_shortcut")):
        if not lnk:
            continue
        p = Path(lnk)
        if p.suffix.lower() != ".lnk":
            failed.append(lnk)
            out(f"Not removed (not a shortcut file): {lnk}")
        elif not p.exists():
            skip(lnk)
        else:
            try:
                p.unlink()
                out(f"Removed: {lnk}")
            except OSError as e:
                failed.append(lnk)
                out(f"Could not remove: {lnk} ({e})")

    icons_dir = rec.get("icons_dir") or (str(Path(rec["icon"]).parent) if rec.get("icon") else None)
    if icons_dir:
        if not _same_path(icons_dir, app / ICONS_DIR):
            failed.append(icons_dir)
            out(f"Not removed (not this tool's icon folder): {icons_dir}")
        elif not Path(icons_dir).exists():
            skip(icons_dir)
        else:
            try:
                shutil.rmtree(icons_dir)
                out(f"Removed: {icons_dir}")
            except OSError as e:
                failed.append(icons_dir)
                out(f"Could not remove: {icons_dir} ({e})")

    _notify(deps)
    if failed:
        out(f"{len(failed)} item(s) could not be removed, so the install record is kept: {record_path}")
        return 1
    try:
        record_path.unlink()
    except OSError as e:
        out(f"Could not remove the install record: {record_path} ({e})")
        return 1
    out(f"Removed: {record_path}")
    out("Uninstall finished. .inpx goes back to VISSIM's original behavior and icon.")
    return 0


# ---------- real layer (tests do not use it) ----------

class WinRegistry:
    """Writes only under HKCU\\Software\\Classes via winreg. HKCR is read-only (merged view)."""

    @staticmethod
    def _path(key: str) -> str:
        return rf"{_CLASSES}\{key}" if key else _CLASSES

    def hkcr_default(self, subkey: str) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, subkey) as k:
                val, _typ = winreg.QueryValueEx(k, "")
        except FileNotFoundError:
            return None
        return val if isinstance(val, str) else None

    def walk(self, key: str) -> list[tuple[str, dict[str, Any]]]:
        import winreg
        out: list[tuple[str, dict[str, Any]]] = []

        def visit(rel: str) -> None:
            try:
                k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._path(rel))
            except FileNotFoundError:
                return
            with k:
                n_sub, n_val, _ = winreg.QueryInfoKey(k)
                values = {}
                for i in range(n_val):
                    name, data, _t = winreg.EnumValue(k, i)
                    values[name] = data
                subs = [winreg.EnumKey(k, i) for i in range(n_sub)]
            out.append((rel, values))
            for s in subs:
                visit(rf"{rel}\{s}")

        visit(key)
        return out

    def key_exists(self, key: str) -> bool:
        import winreg
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._path(key)).Close()
            return True
        except FileNotFoundError:
            return False

    def value_exists(self, key: str, name: str) -> bool:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._path(key)) as k:
                winreg.QueryValueEx(k, name)
            return True
        except FileNotFoundError:
            return False

    def set_value(self, key: str, name: str, data: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self._path(key), 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, data)

    def delete_value(self, key: str, name: str) -> None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._path(key), 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)

    def is_empty(self, key: str) -> bool:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._path(key)) as k:
            n_sub, n_val, _ = winreg.QueryInfoKey(k)
        return n_sub == 0 and n_val == 0

    def delete_key(self, key: str) -> None:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self._path(key))   # fails if there are subkeys (left as is)


def _read_user_shell_folder(name: str) -> str | None:
    import winreg
    sub = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            val, _t = winreg.QueryValueEx(k, name)
    except FileNotFoundError:
        return None
    return val if isinstance(val, str) else None


def _read_user_shell_desktop() -> str | None:
    return _read_user_shell_folder("Desktop")


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def shortcut_script(sc: Shortcut) -> str:
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(sc.path)})",
        f"$s.TargetPath = {_ps_quote(sc.target)}",
        f"$s.Arguments = {_ps_quote(sc.arguments)}",
        f"$s.IconLocation = {_ps_quote(sc.icon + ',0')}",
        f"$s.WorkingDirectory = {_ps_quote(sc.workdir)}",
        "$s.Save()",
    ])


def _powershell_exe() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _run_subprocess(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=60, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    raw = (r.stdout or b"") + (r.stderr or b"")
    try:
        text = raw.decode("mbcs", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return r.returncode, text


def powershell_shortcut(sc: Shortcut, *, run: Callable[[list[str]], tuple[int, str]] | None = None) -> None:
    """Create a .lnk with WScript.Shell (PowerShell subprocess; -EncodedCommand keeps quotes and non-ASCII text safe)."""
    run = run or _run_subprocess
    enc = base64.b64encode(shortcut_script(sc).encode("utf-16-le")).decode("ascii")
    rc, text = run([_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-EncodedCommand", enc])
    if rc != 0:
        raise InstallError(f"PowerShell exit code {rc}: {text.strip()}")
    if not Path(sc.path).is_file():
        raise InstallError(f"The shortcut file was not created: {sc.path}")


def shell_notify() -> None:
    import ctypes
    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


def _write_icon(exe: str, path: Path) -> Path | None:
    from . import icons
    return icons.write_launcher_ico(exe, path)


def _write_shortcut_icon(exe: str, path: Path) -> Path | None:
    from . import icons
    return icons.write_shortcut_ico(exe, path)


def default_deps(out: Callable[[str], None] = print) -> InstallDeps:
    """The real system layer. Merely constructing it neither reads nor writes anything."""
    return InstallDeps(
        registry=WinRegistry(),
        local_appdata=_local_appdata,
        desktop_dir=lambda: desktop_from_shell_folder(_read_user_shell_desktop),
        make_shortcut=powershell_shortcut,
        notify=shell_notify,
        write_icon=_write_icon,
        now=lambda: datetime.now().astimezone().isoformat(timespec="seconds"),
        pythonw=find_pythonw,
        repo_root=_config.repo_root,
        out=out,
        startup_dir=lambda: startup_from_shell_folder(lambda: _read_user_shell_folder("Startup")),
        write_shortcut_icon=_write_shortcut_icon,
        virtualized=detect_appdata_virtualization,
        run_outside=run_via_explorer,
    )
