# Operations

## Prerequisites

- Windows 11, Python 3.12 64-bit (`python` on PATH), PTV Vissim 2026 with an active licence (COM ProgID `Vissim.Vissim.26`).
- An MCP client that supports HTTP transport (for example Claude Code).

## Install (once)

```powershell
cd <repo root>
python -m pip install -e ".[dev]"     # Pillow, pywin32, mcp, pystray, pytest
python -m pytest -q                    # all must pass; Vissim is not needed
```

Without the install step, `python -m vissim_mcp` fails to find the module outside the repo (pytest works without it).

## Config

`config/vissim_mcp.json`: port (8766), protected list, `protected_roots`. Use `--config` for another file. Every key is described in [contracts](contracts.md#config-configvissim_mcpjson). There are no environment variables; all values come from the config file.

## Running the server

The server stays resident in the tray and holds the one Vissim it started. AI client sessions connect to the local address from any folder and only send commands. When a session ends, the server and Vissim stay.

```powershell
# 0) Once — COM check (PC with Vissim 2026; empty new network, nothing saved; output in runs\mcp\probe_<time>.txt)
python tools\com_probe.py --net <saved .inpx>   # registry create signatures, simulation, evaluation, result attributes, ROT
                                                # (without --net: create and attributes only, simulation skipped)
python tools\com_probe.py --rot-automation      # optional: does GetActiveObject attach to an -automation window (up to 90 s; that window is closed afterwards)

# 1) Once — register with Claude Code at user scope (visible from sessions in any folder)
claude mcp add --scope user --transport http vissim http://127.0.0.1:8766/mcp

# 2) Start of work — start the server (one command, no console, stays in the tray)
python -m vissim_mcp
python -m vissim_mcp status                     # status (JSON). Vissim starts on the first command

# 3) End of work — tray menu "Quit server and VISSIM", or
python -m vissim_mcp stop                       # if a file is open: Save and quit / Quit without saving / Cancel dialog
                                                # (no answer in 60 s → recovery copy in runs\mcp\recovery\), then closes with Vissim
```

- Typical conversation: "open `<path>`" → query, change, create → "run it" → look at results → **"save"** (only then does the file change; a `.bak` copy is made first). For a file under a `protected_roots` folder, the `.bak` goes to `runs\mcp\backup\`.
- Refusals and what to do:
  - A file is open (even with zero changes from the conversation) and another file is requested → the assistant should ask first → "save" or "discard and open" (`discard` — Vissim also drops on-screen edits without asking).
  - First write after the file changed through the Vissim menu or a double-click → refused once (`network_changed`) → confirm it is the right file, then repeat.
  - Save refused because another Vissim window is open → close that window, or "save anyway" (`force`).
  - A simulation is running → "stop" first.
  - Link, connector or reference point changes → protected list, not possible (adjust only in the config).
- If the user closes the Vissim window, the next command reports that Vissim is gone, and unsaved changes are lost. The next "open" starts a new Vissim.
- Running a simulation makes Vissim create `<name>.results\` (result DB) and `<name>.err` next to the network — Vissim's own behaviour. Read aggregated results with `get_results`. `.att` files appear only when exported from Vissim or when raw data output is on.
- Do not run a second Vissim bridge that starts its own Vissim at the same time; each instance uses a licence.

### Double-click opens through the server (launcher, [adr/0003](adr/0003-launcher-through-server.md))

COM cannot attach to a Vissim you start yourself. So install the launcher, and every `.inpx` open goes through the server.

```powershell
# Started from inside the Claude desktop app (chat or app terminal), it re-runs itself outside the app through Explorer
# (writes from inside the app go to app-private storage)
python -m vissim_mcp install       # once: .inpx double-click → launcher, file icon (green inverted), desktop 'VISSIM MCP' shortcut, server at sign-in
python -m vissim_mcp uninstall     # undo (removes only what install.json lists — back to Vissim's own action and icon)
```

- **Double-click**: starts the server if it is off ("opening" window) and opens the file in the server's Vissim. If another file is open it asks [Discard and open] / [Cancel]; for the same file it only brings the window to the front. One file at a time. If another Vissim is running, it says so and opens anyway.
- **Server at sign-in**: the Startup folder shortcut starts the server (tray "V" icon) at sign-in. Vissim starts on the first command, so no licence is used until then. The AI client's connection therefore does not fail when a session opens (if you stop the server during a session, that session must reconnect).
- **Desktop 'VISSIM MCP'**: server + empty Vissim. If you then open a file from the Vissim menu, the server follows it (the first write after the change is refused once, to confirm the file).
- **Open without COM**: right-click → "Open with PTV Vissim 2026" (Vissim's own entry). If the server or Vissim could not start at all, the failure dialog offers [Open with plain VISSIM].
- Saving with Ctrl+S in the server's Vissim window leaves no `.bak`. The next tool call logs an outside change (`external_change`) and the assistant should ask whether you saved on screen.
- **Run `install` again** when the Python location changed (double-click gives a Windows error) or after reinstalling or updating Vissim broke the association. If you chose "Always use this app" for `.inpx` in Windows, that choice wins — reset the default to Vissim (ProgID `Vissim input file`) under "Open with".
- If `install` refuses because of "keys or values this tool did not create" (for example `install.json` was deleted by hand and only the keys remain), inspect and delete the keys under `HKCU\Software\Classes\Vissim input file` in the Registry Editor, then run `install` again.
- If the config file is broken, a double-click shows a "config error" message box and exits (there is no console).
- Launcher log: `runs\mcp\launcher.log` (one line per run). Icons and install record: `%LOCALAPPDATA%\vissim_mcp\`.
- Logs: `runs\mcp\server.log`, `runs\mcp\changes.jsonl` (what changed, from which value to which), tray menu "Open log folder".

### Calling tools without the AI client

If the client's connection to the server drops while the server is still in the tray, call the same tools from the command line ([contracts](contracts.md#command-line-mcp-client-toolsmcp_callpy)):

```powershell
python tools\mcp_call.py list
python tools\mcp_call.py call vissim_status
python tools\mcp_call.py call get_attributes --args "{\"type\": \"Link\", \"no\": 1, \"attrs\": [\"Length2D\"]}"
```

## Cleaning up output

- `runs/` is not tracked by git. The tool never deletes anything there. Delete old `runs\mcp\captures\`, `layouts\`, `recovery\`, `backup\` contents and probe outputs by hand when no longer needed.

## Troubleshooting

- `runs\mcp\server.log` and `python -m vissim_mcp status`.
- "Already running": the tray icon is there, or an earlier process (`pythonw.exe`) is left over — end it with `stop`. A ghost process without a server: `tasklist /FI "IMAGENAME eq pythonw.exe"`, then `taskkill /PID <pid> /F`.
- Several Vissim windows: do not tell them apart by title. See [engineering-notes](engineering-notes.md#server-process).
