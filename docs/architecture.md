# Architecture

## Overview

```
MCP client session (any folder) ──HTTP 127.0.0.1:8766 /mcp──▶ vissim_mcp server (resident, tray)
                                                                │  Runtime.lock: one tool call at a time
                                                                ▼
                session ─ objects ─ simulation ─ results ─ view      (layers raise ToolError on failure)
                        └──── ComThread (one dedicated thread) ────▶ Vissim 2026 COM (the instance the server started with Dispatch)
                                guard (protected list, before COM)   state (shared state)   changelog (<log_root>/changes.jsonl)
```

- One server per PC (port check plus a named mutex), one Vissim (started on the first command), at most one open network. Several sessions may connect, but calls are serialized. Every response carries `state` (open file, unsaved write count, last write time, simulation state), so each session can see what the others did.
- Three threads: tray (main thread, `pystray`), HTTP (secondary thread, HTTP transport from the official `mcp` package), COM (dedicated). The HTTP thread puts work on the COM thread's queue and waits. The chunked simulation loop also runs on the COM thread, one chunk at a time.
- The server never attaches to a Vissim the user started. It owns the instance it launched ([adr/0001](adr/0001-resident-server-own-vissim.md)).

## Request trace (tool call `set_attributes`)

1. The HTTP thread receives the request and passes it to `Runtime.run_tool`, which takes the lock (waits until the previous call ends).
2. `run_tool` runs the open-file check (`session.check_current`) and, for write tools, the "file changed" check (`network_changed`).
3. `objects.set_attributes`: for each item, resolve type and key through the registry and call `guard.require(type, "set", attr)`. A protected attribute marks only that item as `protected`; it never reaches Vissim. If the simulation is `running` or `stopping`, the whole call fails with `sim_running`.
4. The remaining items go to the COM thread in order: `AttValue` (before) → `SetAttValue` → `AttValue` (after). Each success calls `state.note_write()` and appends one line to `changes.jsonl`.
5. Response `{ok, applied, failed, skipped, state}`. The file has not changed. When the user says "save", `save_network` makes a `.bak` copy and then calls `SaveNet`.

## Double-click launcher ([adr/0003](adr/0003-launcher-through-server.md))

```
.inpx double-click / desktop shortcut 'VISSIM MCP' ──▶ pythonw -m vissim_mcp open [path]   (launcher, no console, one at a time via mutex)
      │ GET /admin/status (if no server: start --serve and wait)
      │ POST /admin/open {path, discard, expect_*} ──▶ Runtime.admin_open ─▶ session.check_current → opened / front / launched
      ▼
  confirm / notice / failure dialogs (tkinter) → bring the server's Vissim window to the front (by pid)
```

- On each tool or admin call the server runs `session.check_current` once. It reads the file open in the server's Vissim (`WorkingFolder` + `InputFile`) and its modification time. It follows a file opened from the Vissim menu (`file_switch`; sets the "changed" flag, so the next write is refused with `network_changed`) and records saves made outside the server (`external_change`). There is no periodic polling.
- `install`/`uninstall` (per-user registry, `%LOCALAPPDATA%\vissim_mcp\`, desktop and Startup shortcuts) and `icons` (read the Vissim exe icon and derive the launcher, shortcut and tray icons with ctypes) also run outside the server process.

## Shutdown

Tray "Quit server and VISSIM" or `python -m vissim_mcp stop` (`/admin/shutdown`) → `Runtime.request_quit` (holding the lock):

1. If a simulation is running, stop it first.
2. Check the open file.
   - Open file: Save and quit / Quit without saving / Cancel. If another Vissim is running, ask once more before saving.
   - Drawn content only (links but no file): OK / Cancel.
   - Neither: no prompt.
3. Save, or write a recovery copy to `<log_root>/recovery/` (always the last step).
4. Release COM references, stop threads, exit the process. Vissim closes with it.

## Module map (`src/vissim_mcp/`)

| Module | Role | Depends on |
|---|---|---|
| `config` | Load `config/vissim_mcp.json`, required keys, tunable defaults, repo root from the package location | none |
| `errors` | The ten failure kinds and `ToolError` | none |
| `clock` | Timestamp format (ISO 8601 with time zone) | none |
| `registry` | Object registry (type → container, key format, create signature, remove method, default attributes, result metrics) | none |
| `guard` | Protected-list checks (type, operation, attribute; `*` and prefix patterns) | config |
| `comthread` | Dedicated COM thread (queue, busy retry, exception passing) | config (pythoncom imported lazily) |
| `vissim_com` | Start Vissim (Dispatch only), load, save, version | none (win32com imported lazily) |
| `layout_xml` | View layout copy (four display attributes in `.layx`, original unchanged) | none |
| `state` | Shared state (server, Vissim, network, simulation) and the `snapshot` used in responses | clock |
| `changelog` | `changes.jsonl` and `server.log` | clock |
| `session` | Start Vissim, detect loss, open-file check (follow, changed flag, outside changes), open, launcher open (`admin_open`), save, save on quit, backup, recovery copy, other Vissim processes | state, comthread, changelog, errors, vissim_com, clock |
| `objects` | Query, set attributes, create, delete (before/after values) | registry, guard, state, comthread, changelog, errors |
| `simulation` | Simulation and evaluation settings, chunked run and stop, status | state, comthread, changelog, errors, config |
| `results` | Result attribute tables, `.att` files in the `.results\` folder | registry, state, comthread, errors, config |
| `view` | View settings (layout copy, 2D/3D, zoom, camera) and screen capture (COM `Screenshot` or whole window) | state, comthread, changelog, errors, config, screen, layout_xml |
| `screen` | Find the server's Vissim window and capture it with `PrintWindow` (ctypes Win32 GDI, not COM) | Pillow |
| `server` | `Runtime` (layers, lock, response shape, pre-call open-file check, first-write refusal, shutdown and its dialogs), 20 tools, admin routes (`/admin/status[?refresh=1]`, `/admin/shutdown`, `/admin/open`) | all of the above + mcp |
| `tray` | Tray icon (server icon, green "V" fallback) and menu | changelog, icons + pystray, Pillow |
| `icons` | Read icon 0 of the Vissim exe (ctypes), colour-inverted launcher icon, "Vis/MCP" shortcut icon, "V" tray icon, multi-size `.ico` | Pillow |
| `launcher` | Double-click launcher (`open`): mutex, find/start the server, `/admin/open`, confirm/notice/failure dialogs (tkinter), bring window to front, `launcher.log` | config, server (admin route constants and `port_free` only — calls go over HTTP) |
| `install` | `install`/`uninstall`: ProgID, per-user registry, icons, shortcuts (PowerShell), `install.json`; re-runs itself through Explorer when started inside a packaged app's virtualized storage | config, icons |
| `__main__` | Command line (start / `--serve` / status / stop / open / install / uninstall) | server, tray, config, launcher, install, changelog |

## Tools (`tools/`)

| File | Role |
|---|---|
| `com_probe.py` | One-off COM check on a PC with Vissim 2026. Confirms that the registry's create signatures, remove methods and result attributes are accepted. Saves nothing to user networks. Output goes to `<log_root>/probe_<YYYYMMDD-HHMMSS>.txt`. |
| `probe_net.py` | Scratch-network helpers used only by `com_probe.py` (not used by the server). |
| `mcp_call.py` | Command-line MCP client. Calls the server's tools over HTTP when the AI client's connection is down. Writes no files. |

## External dependencies

- **PTV Vissim 2026** (COM ProgID `Vissim.Vissim.26`). Observations in this repo were made on 2026 SP09.
- Python 3.12+ packages: `mcp` (2.x, HTTP transport), `pystray` (tray), `Pillow`, `pywin32`; `pytest` for development.
- An MCP client that supports HTTP transport, for example Claude Code, registered at `http://127.0.0.1:8766/mcp`.

## Boundaries

- Network traffic is HTTP on 127.0.0.1 only. There is no config key to bind another host. Any process on the same PC can connect, so do not run the server on a shared PC.
- The server writes to: the one `.inpx` open in the server's Vissim (only on a `save_network` call or "Save and quit" in the quit dialog, after a `.bak` copy), and `log_root` (logs, backups, recovery copies, view layouts, captures, `launcher.log`). Vissim itself writes the simulation result folder next to the network.
- The install command writes to: `%LOCALAPPDATA%\vissim_mcp\`, this tool's keys under the `.inpx` ProgID in the per-user registry, one desktop shortcut, and one Startup folder shortcut (starts the server at sign-in). `uninstall` removes only what `install.json` lists.
- Links and connectors cannot be created, deleted or reshaped through MCP, and reference point, units, traffic side and gradient basis cannot be changed (protected list; no argument turns it off).
