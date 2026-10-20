# 0001 — A resident MCP server drives Vissim from chat: it owns the one Vissim it started, blocks only a protected list, and writes files only on an explicit save

## Context
For calibration (matching demand, tuning speeds) and for comparing alternatives, the user wants to give instructions in plain language and have the AI assistant operate Vissim directly. Three constraints collide:
1. A Vissim started over COM closes when the process that started it ends, and a Vissim started from the GUI cannot be attached to (Vissim 2026, see [engineering-notes](../engineering-notes.md)).
2. The files involved are hand-edited working files. A tool that writes them freely can cause losses that cannot be undone.
3. An earlier tray bridge re-attached on every request with `GetActiveObject`, missed the running Vissim on ROT timing, and after Vissim closed its one-second polling produced hundreds of warnings. Reconnect workers, back-off and cool-downs were bolted on as a result.

## Decision
- **A resident server whose lifetime is separate from the AI client session** (tray, HTTP on 127.0.0.1, official `mcp` package) holds **the one Vissim it started**. Sessions only send commands (several sessions may connect; calls are serialized). Vissim starts on the first command (`Dispatch` only, never `GetActiveObject`). If it disappears, that is reported once with no automatic reconnect, and the server does not poll it for status.
- **COM runs on one dedicated thread only** (instead of per-thread proxies through the Global Interface Table). Simulations run in `SimBreakAt` chunks with `RunContinuous`, not `RunSingleStep` per step.
- **Wide scope, narrow protection**: unlimited queries; attribute changes; creating (vehicle inputs, vehicle compositions, static routes, desired speed decisions, reduced speed areas, evaluation devices) and deleting objects. Only a **protected list** is blocked (creating, deleting and reshaping links and connectors, `LinkPolyPoint` writes, NetPara reference point, units, traffic side and gradient basis). There is no allow list. **No argument turns the protected list off.** For links and connectors only the geometry and structure attributes found by the probe (`Wkt*`, `Length*`, `NumLanes`, `IsConn`, `From*`, `To*`, `zOffset*`, `Gradient`, `Level`) are blocked; name, display and similar attributes stay open.
- **Files are written only by a `save_network` call** (no auto-save). The target is the one `.inpx` the user named in the conversation, including working files and files under data folders. Before saving, a `.bak` (for files under `protected_roots`, the backup goes to `<log_root>/backup/`). If a Vissim process other than the server's exists or cannot be ruled out, saving is refused without `force`. Opening another file over unsaved changes is refused without `discard`. Every write returns before/after values and is logged in `changes.jsonl`.
- **Shutdown** (tray quit item and `stop` command alike): if there are unsaved changes, ask (close / cancel); on close, or with no answer (default 60 s), leave a recovery copy in `<log_root>/recovery/` and then close.
- Port 8766, chosen not to clash with the earlier bridge (8765).
- The starting point for create signatures is the official example script installed with Vissim; the final word is the signature that `tools/com_probe.py` made work on 2026. A rejected signature is not replaced by a guess.

## Alternatives rejected
- **A stdio MCP server per session**: when the session ends, Vissim closes and the work is cut off. The user required the server to outlive sessions.
- **Attaching to a Vissim window the user started**: earlier notes disagreed (one said it cannot attach; the earlier bridge's notes said `-automation` attaches after 10–30 s). A probe re-check showed that neither a `Dispatch` nor an `-automation` Vissim appeared in the ROT within 120 s (Vissim 2026 SP09). There is no way to attach, so the server handles only the Vissim it started.
- **Allow list**: every new attribute needed during calibration would require a config edit. The user chose the protected list.
- **Auto-save, or always saving to a new file**: rejected by the user (unintended overwrites, file management burden).
- **Per-thread COM proxies through the GIT**: one dedicated thread is simpler for a server that serializes calls anyway, and the earlier bridge struggled to stabilize that approach.
- **`RunSingleStep` per step**: tens of thousands of COM round trips and redraws for a one-hour simulation.
- **The server reading observed traffic data for comparison**: scope creep. The assistant does the comparison in the conversation.
- **Implementing the tray directly with pywin32**: more code. `pystray` is used.

## Consequences
- Package `src/vissim_mcp/`, config `config/vissim_mcp.json`, logs under `runs/mcp/`, check script `tools/com_probe.py`, external packages `mcp` and `pystray`.
- Network communication changes from "none" to "local HTTP on 127.0.0.1 only".
- Now impossible: creating, deleting or reshaping links and connectors through MCP, changing the reference point, and any file change without a save call.
- Edits the user makes directly in the Vissim window cannot be counted by the server and are not included in the "unsaved changes" count (stated in responses and docs).
- Future candidates: time-dependent changes (apply attributes at break points), batch scenario runs with result tables, attaching to user windows, help with comparing against observed data.
