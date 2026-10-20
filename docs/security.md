# Security and data handling

## Authentication and network exposure

- No user authentication (a local, single-user tool). No calls to external services.
- The only network traffic is the MCP server's local HTTP on **127.0.0.1** (default port 8766). There is no config key to bind another host. Any process on the same PC can connect, so do not run the server on a shared PC.
- Do not turn off the AI client's per-tool approval prompts for this server.
- Vissim COM is local inter-process communication (`Vissim.Vissim.26`).
- No secrets. The config holds no keys or passwords.

## Data passed to AI clients

- Tool responses contain what the AI client asked for: file paths of the open network, object attributes, results, change log entries, and screen captures (`capture_view` writes PNG files and returns their paths). Whatever the client reads is sent to that client's model provider under the client's own terms.
- Do not open networks or capture screens you are not allowed to share with the AI provider.
- `server.log`, `changes.jsonl` and `launcher.log` contain full file paths. Check them before sending them to anyone.

## Write scope

- **The server writes to the network file only on request.** It writes the **one** `.inpx` open in the server's Vissim (whether opened from the conversation, the launcher, or the Vissim menu), and only when the `save_network` tool is called or "Save and quit" is chosen in the quit dialog. There is no auto-save.
- After the open file changes (Vissim menu or launcher), the first write or save is refused once (`network_changed`).
- Before saving, a `.bak` copy is made next to the file. For a file under a `protected_roots` folder, the copy goes to `<log_root>/backup/` instead, so data folders do not fill up with copies.
- If a Vissim process other than the server's exists, or it cannot be determined, saving is refused unless `force` is given.
- The protected list blocks creating, deleting and reshaping links and connectors, poly point writes, and changes to reference point, units, traffic side and gradient basis. No argument turns it off; only the config changes it.
- Everything else the server writes goes under `log_root` (default `runs/mcp/`): logs, backups, recovery copies, view layout copies, captures, `launcher.log`. View layout copies never touch the original `.layx`.
- **Install (`python -m vissim_mcp install`)** writes only to the user account:
  - `%LOCALAPPDATA%\vissim_mcp\` (launcher and shortcut icons `icons\*.ico` with a content hash in the name; install record `install.json`),
  - this tool's keys under `HKCU\Software\Classes\<.inpx ProgID>` (`DefaultIcon`, `shell` default value, `shell\vissim_mcp`),
  - one desktop shortcut and one Startup folder shortcut (server at sign-in).
  
  It does not touch HKLM, the Vissim install folder, or the menu entries Vissim registered. If the user-account side already holds values this tool did not create, install refuses. `uninstall` removes only what `install.json` lists.
- The launcher (`open`) writes only `<log_root>/launcher.log`.
- `tools/mcp_call.py` writes no files; the tools it calls follow the server's rules.
- `tools/com_probe.py` creates an empty new network and saves nothing, or opens a saved network given with `--net` and saves nothing. Its extra options open and save only per-run copies under `runs/mcp/scratch/`.
- Icons are generated from the installed Vissim exe at run time. No PTV material is stored in the repo.

## Logs

- `<log_root>/server.log`: start, stop, Vissim launch and loss, tool calls, errors.
- `<log_root>/changes.jsonl`: open, save, attribute changes, create, delete, run, recovery, loss — each entry says what changed, from which value to which, whether it succeeded, and the error text.
- Edits the user makes directly in the Vissim window are not logged. Two cases are detected on the next tool call: a save from the server's Vissim window (Ctrl+S) changes the file's modification time and is logged as `kind=external_change` (no `.bak` is made), and opening another file from the Vissim menu is logged as `kind=file_switch`.
- `runs/` is excluded from git.
