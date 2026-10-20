# Contracts — commands, config, tools, logs

Tool names, arguments, `error.kind` values and log formats in this document are contracts. Change them together with this document and the tests.
Paths under `runs/mcp/` are the default `log_root` (relative to the repo root).

## Command line (`python -m vissim_mcp`)

| Command | Behaviour | Exit code |
|---|---|---|
| `python -m vissim_mcp [--config <path>]` | Starts tray + server as a console-less child (`pythonw`) and returns at once. If the port is in use: "already running" | 0 / 1 port in use / 2 config error |
| `python -m vissim_mcp --serve [--config] [--no-tray]` | Runs in this process (internal; `--no-tray` serves HTTP only, for checks) | 0 / 1 port in use / 2 |
| `python -m vissim_mcp status [--config]` | Prints the `/admin/status` result (JSON) | 0 / 1 no server / 2 |
| `python -m vissim_mcp stop [--config]` | `/admin/shutdown` — same procedure as the tray quit item (see "Shutdown procedure") | 0 request accepted / 1 no server / 2 |
| `python -m vissim_mcp open [<path>] [--config]` | Launcher (see "Double-click launcher"). Called by a double-click (`pythonw … open "%1"`) and by the desktop shortcut (no path) | 0 opened, brought to front, or cancelled by the user / 1 failure (including when a notice was shown) / 2 config error |
| `python -m vissim_mcp install [--config]` | Installs the double-click association, `.inpx` file icon and shortcuts (see "Install and uninstall") | 0 / 1 refused or failed / 2 config error |
| `python -m vissim_mcp uninstall` | Removes only what `install.json` lists (runs without a config) | 0 done or no record / 1 something could not be removed, or `install.json` is broken |

Default config path = `config/vissim_mcp.json` in the repo root (derived from the package location). Relative paths in the config (`log_root`) are relative to the repo root.

## Config `config/vissim_mcp.json`

Required:
- `vissim.progid` — `"Vissim.Vissim.26"`.
- `port` — initial value 8766.
- `protected` — list of `{type, ops: [create|delete|set], attrs: [...]}`. `attrs: ["*"]` means every attribute, `[]` means no attribute protection, a trailing `*` is a prefix pattern (`"RefPointNet*"`).
- `protected_roots` — list of folders. A `.bak` for a file under one of them goes to `<log_root>/backup/` instead of next to the file. May be empty.

Tunable (defaults in `vissim_mcp/config.py`):

| Key | Default | Meaning |
|---|---|---|
| `vissim.exe` | default install path of Vissim 2026 (`…\PTV Vissim 2026\Exe\Vissim260.exe`) | icon source for tray, launcher and shortcut |
| `log_root` | `"runs/mcp"` | logs, backups, recovery copies, layouts, captures |
| `quick_mode_default` | `true` | `run_simulation` default |
| `sim_chunk_s` | `60` | simulation chunk length (s) |
| `quit_prompt_timeout_s` | `60` | quit dialog timeout (s) |
| `list_limit_default` | `200` | `list_objects` default `limit` |
| `com_retry_count` | `5` | retries when Vissim rejects a call as busy |
| `com_retry_interval_s` | `0.5` | interval between those retries |
| `result_max_intervals` | `500` | upper bound for `get_results(interval="all")` |
| `capture_settle_s` | `3.0` | wait before a screen capture |
| `launcher_server_wait_s` | `30` | how long the launcher waits for a server it started |
| `launcher_open_timeout_s` | `600` | launcher timeout for `/admin/open` |
| `shortcut_name` | `"VISSIM MCP"` | desktop shortcut name |

The host is fixed at 127.0.0.1 (no config key). A missing required key fails with a message naming the key(s), exit code 2.

Initial protected list (attribute IDs are case-insensitive; trailing `*` is a prefix):
- `Link` and `Connector`: `ops: [create, delete, set]`, `attrs: ["Wkt*", "Length*", "NumLanes", "IsConn", "FromLink", "FromPos", "ToLink", "ToPos", "zOffset*", "Gradient", "Level"]` (geometry and structure only — `Name`, `LinkBehavType`, `DisplayType` and similar stay open).
- `LinkPolyPoint`: `ops: [set]`, `attrs: ["*"]`.
- `NetPara`: `ops: [set]`, `attrs: ["RefPointNet*", "RefPointMap*", "UnitLen*", "TrafReg", "LinkGradBas"]`.

Nothing else is blocked.

## Client registration and admin routes

Claude Code (user scope, visible from any folder):

```powershell
claude mcp add --scope user --transport http vissim http://127.0.0.1:8766/mcp
```

The MCP server name is `vissim`; the MCP endpoint path is `/mcp`.

Admin routes (plain local HTTP, not MCP):
- `GET /admin/status` — same body as `vissim_status` (from memory, no COM). With `?refresh=1` (also `true`, `yes`) it first runs the open-file check inside the serialized call, then adds `refreshed` (bool), `refresh_note` (Vissim absent, simulation running, a COM error text, a loss text, or `null`) and `warnings`.
- `POST /admin/shutdown` — starts the shutdown procedure on a separate thread. Returns `{accepted, note}`.
- `POST /admin/open` — for the launcher only. Body `{path: string|null, discard: bool = false, expect_open_path?: string|null, expect_unsaved_writes?: int}`. Serialized like a tool, same response shape `{ok, …, state}`. Order:
  1. Simulation `running` or `stopping` → `sim_running`.
  2. Open-file check.
  3. `path = null`: launch Vissim if absent (`action = "launched"`), else `"front"`.
  4. With `path`: file missing → `not_found`. Same as the open file (absolute path, case-insensitive) → `"front"` (not reloaded, `discard` ignored). Another open file or drawn content and `discard = false` → `unsaved_changes` (detail `{unsaved_writes, open_path, drawn_links?}`). `discard = true` but an `expect_*` value differs from the current value → nothing is discarded; `unsaved_changes` again with the new values. Otherwise open it (`"opened"`, sets the changed flag, `changes.jsonl` `kind=open`, `tool=admin_open`, `previous_path`, `changed_by="launcher"`).
  - Success: `{ok, action, path, pid, launched, counts (when opened), warnings[]}`. Failure to launch Vissim: `vissim_absent` with `detail.launch_failed = true`. Malformed body: `bad_args`.

## Tools — common rules

Every tool returns a JSON object with `ok` and `state`.

`state = {vissim{running, version}, network{path, unsaved_writes, last_write_at, counts}, simulation{state, run_id, progress_s}, server_start_id}`.

Failure: `ok = false`, `error = {kind, message[, detail]}`, with

`kind ∈ protected · no_network · unsaved_changes · vissim_absent · other_vissim_running · sim_running · com_error · not_found · bad_args · network_changed` (10 kinds).

Vissim/COM error text is passed through in `message`. There is no silent fallback. An unexpected exception inside a layer is reported as `com_error` with the exception type in the message.

`type` is a registry name (below). `no` is a number, a composite key string (lane `"link-lane"`, static or parking route `"decision-route"`, poly point `"link-index"`, parking space `"lot-index"`), or `null` for a single object. Type names not in the registry are allowed for read tools only, as the container `Net.<name>`.

## Tools (20)

| Tool | Arguments | Behaviour and return |
|---|---|---|
| `vissim_status` | — | `server{version, started_at, port, server_start_id, config_path}`, `vissim{running, version, launched_at, pid}`, `network{path, unsaved_writes, last_write_at, counts}`, `simulation{state, run_id, progress_s, period_s, run_index, num_runs, started_at, error}`, `other_vissim_processes` (excluding the server's own; `null` if unknown) |
| `open_network` | `path`, `discard=false` | Launches Vissim if absent (always a new instance, window visible). Missing file → `not_found`. **If a file is open or there is drawn content (links but no file)**, it fails with `unsaved_changes` unless `discard` — even with zero writes, even for the same file (detail `{unsaved_writes, open_path, drawn_links?}`; the message says to set `discard=true` only after asking the user — `LoadNet` drops on-screen edits without asking). An empty Vissim opens without asking. Opening resets the write counter and clears the changed flag. Returns `path, counts{Link, Connector, VehicleInput, …}, launched, discarded_writes, warnings[]` (warning when another Vissim process exists or it cannot be determined) |
| `save_network` | `path=null`, `force=false` | No network → `no_network`. Another Vissim process (not the server's) or unknown → `other_vissim_running` unless `force`. No `path`: `.bak` copy (`<name>_<YYYYMMDD-HHMMSS>.bak.inpx` next to the file, or in `<log_root>/backup/` for files under `protected_roots`; same second → `_2`) → save in place. With `path`: back up the target only if it exists → save as → later saves go to the new file (`path_changed=true`, `note`). Returns `saved_path, backup_path, writes_flushed, path_changed, note` |
| `list_object_types` | — | Per type: container, key format, whether it can be created (`verified`, `source`), create arguments, protection |
| `describe_attributes` | `type`, `parent=null` | Attribute metadata of the container: `attributes[{id (name used in AttValue), id_upper, name, value_type, editable, category, protected}]` (missing fields `null`). Composite-key types need `parent` (the parent number). Single objects (`NetPara`, `Simulation`, `Evaluation`, network performance) → `not_found`, because Vissim gives no list for them (see `attribute.xlsx` in the Vissim install) |
| `list_objects` | `type`, `attrs=[]`, `name_contains=null`, `where={}`, `limit=<config>`, `offset=0`, `parent=null` | `total`, `rows[{no, …}]`. Empty `attrs` → the type's default attributes. `name_contains` matches part of `Name` (case-insensitive); `where` compares attribute values as strings. For single objects (`no=null`) each attribute is read separately; failed ones are `null` with `warnings` (for multi-object types one rejected column fails the call with `com_error`) |
| `get_attributes` | `type`, `no=null`, `attrs=[]` | `attributes{}` (attributes that fail to read are `null`, with `warnings`) |
| `set_attributes` | `changes=[{type, no, attr, value}]`, `stop_on_error=true` | `attr` is the Vissim attribute ID as is (`Volume(1)`, `Cont(2)`, `RelFlow(1)`, `DesSpeedDistr(10)`). Protected and malformed items fail before COM; the rest are applied in order; by default it stops at the first failure that reached COM. Each item has `before` and `after` (re-read). No rollback. Overall `ok` only when every item succeeded. Returns `applied, failed, skipped`. During a simulation → `sim_running` |
| `create_object` | `type`, `params{}`, `no=null`, `attrs={}` | Creates with the registry signature (`no` omitted → max + 1) → type-specific post-steps → applies `attrs`. Returns `no`, snapshot `attributes`, `applied[{attr, before, after}]`, `signature_verified` (with a warning when false). Types without a create signature in the registry → `bad_args`. Protection checks run before COM |
| `delete_object` | `type`, `no` | Returns the snapshot before deletion, then calls `Remove<Type>` |
| `configure_simulation` | `period_s`, `resolution`, `random_seed`, `num_runs`, `seed_increment`, `start_time` (all optional) | Before/after values of `Simulation` attributes `SimPeriod`, `SimRes`, `RandSeed`, `NumRuns`, `RandSeedIncr`, `StartTm` |
| `configure_evaluation` | `data_collection{collect, write_file, interval_s, from_s, to_s}`, `travel_times{…}`, `queues{…}`, `network_performance{…}`, `parking_lots{…}`, `parking_lot_groups{…}`, `parking_decisions{…}`, `parking_spaces{…}` | Before/after values of `Evaluation` attributes (`<group>CollectData`, `Interval`, `FromTime`, `ToTime`; parking group prefixes `ParkLots`, `ParkLotGrps`, `ParkRoutDecs`, `ParkSpcs`). `write_file` has no per-group attribute in 2026, so it only adds a warning. `collect` must be on for `get_results` |
| `run_simulation` | `quick_mode=null` (config default), `num_runs=null` | Starts in the background and returns at once `{run_id, period_s, chunk_s}`. Each chunk (`sim_chunk_s`) is `SimBreakAt` → `RunContinuous`; progress is `state.simulation.progress_s` at chunk boundaries. Ends as `finished` or `failed` (with text). Already running → `sim_running` |
| `stop_simulation` | — | Stops at the next chunk boundary (`stopping` → `idle`). Not running → `bad_args` |
| `get_results` | `kind ∈ data_collection · travel_time · queue · network_performance · vehicle_input · parking_lot · parking_lot_group · parking_decision`, `nos=[]`, `run="Current"` (or a number, `Avg`, `StdDev`, `Min`, `Max`), `interval="all"` (or a number, `Last`, `Avg`, `Total`, `Min`, `Max`), `veh_class="All"` (or a class number) | Table read from result attributes `metric(run, interval, class)`: `rows[{no, name, interval, metric…}]`. Metrics per kind: see "Result metrics". Ratio metrics (`OccupRate`, `…Rate`, `…Share`) are 0–1. All values missing → `not_found` with a reason. Upper bound `result_max_intervals` |
| `list_result_files` | — | `.att` files in `<network folder>\<name>.results\` (name, size, modified time, newest first). No folder → empty list + `note`. Collecting aggregated results alone does not create `.att` files (use `get_results`); `.att` files exist only when exported from Vissim or when raw data output is on |
| `read_result_file` | `name`, `max_rows=1000`, `where={}` | `.att` → `columns, rows, row_count, truncated, meta`. `..`, absolute paths, non-`.att` names → `bad_args` |
| `get_change_log` | `since=null` (ISO 8601), `limit=200` | `entries[]` (format below), `path` |
| `set_view` | `layout=null` (`view` or `network`), `mode=null` (`2d` or `3d`), `zoom=null` (`[x0, y0, x1, y1]`, 2D), `camera=null` (`{x, y, z, yaw, pitch}`, 3D, network coordinates in m and degrees) | Changes the view only (network objects, write counter and `.inpx` unchanged). Order: layout → 2D/3D → `ZoomTo` → `SetCameraPositionAndAngle`. `layout="view"`: reads `<name>.layx` next to the network and writes a fresh copy `<log_root>/layouts/<name>_view.layx` with background images and 3D models shown and the online map off, then `LoadLayout` ([adr/0002](adr/0002-view-layout-and-capture.md)). `layout="network"`: loads the original `.layx` as is. No `.layx` → `not_found`; unexpected format → `bad_args`. `zoom` with `camera`, or either one in conflict with `mode` → `bad_args` (before COM). Returns `mode` (current 2d/3d), `layout{name, path, source, changed, note}`, `zoom`, `camera`, `warnings`. A layout load is logged as `kind=view`. Loaded display settings stay in Vissim, so a later `save_network` also writes them to the network's `.layx`. Not refused during a simulation |
| `capture_view` | `mode="network"` (or `window`), `label=null` (letters, digits, `-`, `_`, up to 60), `scale=1.0` (0.1–4, network only), `settle_s=<config>` (0–60) | Waits `settle_s`, then writes `<log_root>/captures/<YYYYMMDD-HHMMSS>[_label].png` (same name → `_2`). `network`: COM `NetworkWindow.Screenshot(path, scale)` — the network editor picture only (for reports). `window`: the whole server Vissim window as on screen (Win32 `PrintWindow`, works even when covered — for development checks). Server Vissim PID unknown or no window → `not_found`. Returns `path, mode, size[w, h]` (+ `pid`), `warnings` (window was restored from minimized; image has a single colour = maybe not drawn yet) |

### Result metrics (`get_results`)

| `kind` | Metrics |
|---|---|
| `travel_time` | `TravTm, Vehs, DistTrav` |
| `data_collection` | `Vehs, SpeedAvgArith, SpeedAvgHarm, Acceleration, Length, Dist, QueueDelay, Pers, OccupRate` |
| `queue` | `QLen, QLenMax, QStops` (two sub-attributes: run, interval) |
| `network_performance` | `DelayAvg, SpeedAvg, TravTmTot, DistTot, DelayTot, StopsAvg, StopsTot, DelayStopAvg, DelayStopTot, VehAct, VehArr, DelayLatent, DemandLatent` |
| `vehicle_input` | `Volume` (interval only), `DemLatent, DelayLatent` (two sub-attributes) |
| `parking_lot` | `NumVehsEnter, NumVehsLeave, NumVehsPark, OccupRate, OccupDurTot, ParkDurAvg, ParkDurMax, ParkSpcBlockRate, DemLatent, DelayLatent` |
| `parking_lot_group` | the `parking_lot` set without `DemLatent` and `DelayLatent` (8) |
| `parking_decision` | `NumParkReq, NumParkReqAccept, NumParkReqDecl, ParkReqDeclShare` |

Parking space results: pass the sub-attributes directly, for example `get_attributes("ParkingSpace", "lot-index", ["OccupRate(Current,Last,All)"])`.

## Registry (object types)

| `type` | Container | Key | Create `params` | Remove |
|---|---|---|---|---|
| `Link` / `Connector` | `Net.Links` | number | (protected — no create, delete or geometry writes) | — |
| `Lane` | `Links[link].Lanes` | `"link-lane"` | none (attribute changes only, e.g. `BlockedVehClasses`) | — |
| `LinkPolyPoint` | `Links[link].LinkPolyPts` | `"link-index"` | read only | — |
| `NetPara` | `Net.NetPara` | single | reference point, units, traffic side, gradient basis protected | — |
| `VehicleType` / `DesSpeedDistribution` (container `Net.SpeedDistributions`) / `VehicleClass` | the matching container | number | none | — |
| `TimeDistribution` | `Net.TimeDistributions` | number | none (values of existing distributions only — normal `Mean`/`StdDev` writes confirmed by probe; default attributes `No`, `Name`) | — |
| `VehicleComposition` | `Net.VehicleCompositions` | number | `relative_flows=[{veh_type, des_speed_distr, rel_flow}]`, `name` | `RemoveVehicleComposition` |
| `VehicleInput` | `Net.VehicleInputs` | number | `link`, `volume` (→ `Volume(1)`), `veh_comp` (→ `VehComp(1)`), `name` | `RemoveVehicleInput` |
| `VehicleRoutingDecisionStatic` | `Net.VehicleRoutingDecisionsStatic` | number | `link`, `pos`, `name`, `veh_classes` | `RemoveVehicleRoutingDecisionStatic` |
| `VehicleRouteStatic` | `…[decision].VehRoutSta` | `"decision-route"` | `decision`, `dest_link`, `dest_pos`, `rel_flow` (→ `RelFlow(1)`) | `RemoveVehicleRouteStatic` |
| `DesSpeedDecision` | `Net.DesSpeedDecisions` | number | `link`, `lane`, `pos`, `speeds={class: distribution}` (→ `DesSpeedDistr(class)`), `name` | `RemoveDesSpeedDecision` |
| `ReducedSpeedArea` | `Net.ReducedSpeedAreas` | number | `link`, `lane`, `pos`, `length`, `speeds`, `name` | `RemoveReducedSpeedArea` |
| `DataCollectionPoint` | `Net.DataCollectionPoints` | number | `link`, `lane`, `pos` | `RemoveDataCollectionPoint` |
| `DataCollectionMeasurement` | `Net.DataCollectionMeasurements` | number | `points=[point numbers…]` (→ `DataCollectionPoints` comma string), `name` | `RemoveDataCollectionMeasurement` |
| `VehicleTravelTimeMeasurement` | `Net.VehicleTravelTimeMeasurements` | number | `from_link`, `from_pos`, `to_link`, `to_pos`, `name` | `RemoveVehicleTravelTimeMeasurement` |
| `QueueCounter` | `Net.QueueCounters` | number | `link`, `pos`, `name` | `RemoveQueueCounter` |
| `ParkingLotGroup` | `Net.ParkLotGrps` | number | `name` (membership is the lot's `EvalGrps`, a comma string of group numbers) | `RemoveParkingLotGroup` |
| `ParkingLot` | `Net.ParkingLots` | number | `link`, `lane`, `pos`, `length` (→ `Length`), `name` — Vissim sets the number of spaces from `Length` and `LenPerSpc` (≥ 1 m). Set `Type`, `Capacity`, `Attrac`, `InitOccup`, `EvalGrps` etc. through `attrs` or `set_attributes` | `RemoveParkingLot` |
| `ParkingSpace` | `ParkingLots[lot].ParkSpcs` | `"lot-index"` | read only | — |
| `VehicleRoutingDecisionParking` | `Net.VehicleRoutingDecisionsParking` | number | `link`, `pos`, `park_rate` (→ `ParkRate(1)`, **0–1**: 0.5 = 50 %), `park_dur` (→ `ParkDur(1)`, time distribution number), `veh_classes`, `name` | `RemoveVehicleRoutingDecisionParking` |
| `VehicleRouteParking` | `…Parking[decision].VehRoutPark` | `"decision-route"` | `decision`, `parking_lot` (lot number → object) | `RemoveVehicleRouteParking` |
| `Simulation` / `Evaluation` / `VehicleNetworkPerformanceMeasurement` | single | `null` | through the settings and result tools. Every attribute of the network performance measurement is a result metric (needs sub-attributes, has no `No`/`Name`), so its default attributes are five metrics with sub-attributes such as `DelayAvg(Current,Last,All)`; use `get_results` for per-interval tables | — |

All create signatures were confirmed on Vissim 2026 SP09 by `tools/com_probe.py` (`source` and `verified` in `list_object_types`). Building floors and ramps (links, connectors — protected) and automatic parking space layout (Vissim Car park creator — not in COM) are drawn in Vissim; lots, groups, decisions, routes and evaluation are then handled through MCP. `DesSpeedDecision` and `ReducedSpeedArea` have time-window attributes `TimeFrom` and `TimeTo`; lane closure (`Lane.BlockedVehClasses`) has no time window.

## Open-file check, changed flag, outside changes ([adr/0003](adr/0003-launcher-through-server.md))

- **Open file** = `AttValue("WorkingFolder")` + `AttValue("InputFile")` on the top-level Vissim object of the server's instance (empty means none). **Drawn content** = no open file but at least one link.
- **The open-file check** runs once per call for the 19 tools other than `vissim_status`, for `/admin/open` and for `/admin/status?refresh=1`. No periodic polling. Skipped when Vissim is absent or a simulation is running. Loss of Vissim triggers the usual loss handling; other COM failures only warn (the recorded file is trusted). If this check finds that Vissim is gone, that call reports `vissim_absent` (`lost_writes`) once, whatever the tool (including `get_change_log` and `list_object_types`); `open_network` and `/admin/open` only warn and start a new Vissim.
- If the file differs from the recorded one, the server follows it: updates path and object counts, resets the session write count, logs `file_switch`, adds `warnings`, and sets the **changed flag**.
- **Changed flag**: set when the file changes from the Vissim menu or the launcher opens another file; cleared by `open_network`, a Vissim restart, or the first refused write (`front` keeps it). While it is set, the first write tool to arrive (`set_attributes`, `create_object`, `delete_object`, `configure_simulation`, `configure_evaluation`, `run_simulation`, `save_network`) is refused before COM with `network_changed` (detail `{previous_path, current_path, changed_by: "vissim_menu"|"launcher"}`). Once per server, not per session. Read tools pass.
- **Modification-time baseline** is updated right after open, follow and `save_network`. If the file's time differs, the server logs `external_change`, updates the baseline, resets the session write count, and adds a warning that the open file changed outside the server and the user should be asked whether they saved from the Vissim window. A save from the Vissim window (Ctrl+S) leaves no `.bak`.

## Shutdown procedure (tray quit item, `stop`, `/admin/shutdown`)

1. If a simulation is running, stop it and wait (up to `sim_chunk_s` + 30 s). If it does not stop, skip the dialogs, try a recovery copy only, and exit.
2. After the open-file check:
   - Open file: Yes / No / Cancel dialog (`quit_prompt_timeout_s`, default button Cancel). Yes = save in place (`save_network` rules: `.bak`, `kind=save`, `tool=quit`; if another Vissim exists or it cannot be determined, a second Yes/No dialog asks whether to save anyway — No or timeout → recovery copy; save failure → recovery copy). No = recovery copy. Cancel = keep running. Timeout = recovery copy.
   - Drawn content only: OK / Cancel dialog (no save button; OK or timeout → recovery copy).
   - Neither: exit without asking.
   - If the check fails, content is assumed to exist.
3. The recovery copy is made last, just before exit (`SaveNetAs` changes the file open in Vissim). Then the session is closed and the process exits. Other tool and admin calls wait during this procedure.

## Double-click launcher (`open`)

- Mutex `Local\vissim_mcp_launcher`. If already held: a notice that another file is being opened and only one can open at a time.
- No path or not `.inpx` → notice, exit 1. Shows an "opening" window (topmost, no close button).
- Finds the server with `GET /admin/status`. If none, starts `--serve` without a console and waits up to `launcher_server_wait_s`. If `other_vissim_processes` is ≥ 1 or `null`, shows an OK-only notice and continues.
- `POST /admin/open` (`launcher_open_timeout_s`).
  - `opened`, `front`, `launched` → bring the server's Vissim window (pid) to the front (failures ignored).
  - `unsaved_changes` → dialog [Discard and open] [Cancel] (default Cancel; shows the open file or "no file — drawn content in the Vissim window", the file to open, the session change count, and that on-screen edits are lost too). Discard and open → retry with `discard=true` and `expect_*` (asks again if the state changed meanwhile).
  - `sim_running` → notice.
  - **Could not start at all** (server wait exceeded, server start failed, `launch_failed`) → failure dialog [Open with plain VISSIM] (without a path: [Start plain VISSIM]) [Close].
  - Other failures (open timeout, `com_error`, `not_found`, connection lost, server busy) → [Retry] [Cancel].

## Install and uninstall (`install` / `uninstall`)

- Inside a packaged app's virtualized storage (a command started from the Claude desktop app; a test file in `%LOCALAPPDATA%` is redirected to `Packages\*\LocalCache\Local\`), `install` and `uninstall` write nothing from that process. They write the same command to a temporary `.cmd`, run it outside the app through `explorer.exe`, and return its output and exit code (up to 120 s).
- Install:
  - Reads the `.inpx` ProgID (merged HKCR view; on Vissim 2026 `Vissim input file`). None → 1.
  - If `HKCU\Software\Classes\<ProgID>` holds keys or values not in this tool's record → refuse (1).
  - Inverts the colours of icon 0 of `vissim.exe` and writes `%LOCALAPPDATA%\vissim_mcp\icons\launcher_<hash>.ico` (seven sizes, 16–256; the 8-character hash comes from the content, because the Windows icon cache remembers paths; older `.ico` files are removed). Cannot read the icon → 1.
  - Registry: `DefaultIcon` = that `.ico`; `shell` default value `vissim_mcp`; `shell\vissim_mcp` = the "Open with VISSIM MCP" verb (with an `Icon` value); `shell\vissim_mcp\command` = `"<pythonw.exe>" -m vissim_mcp open "%1"`. Vissim's own menu entries stay (right-click).
  - Desktop (user shell folder) `<shortcut_name>.lnk`: target `pythonw.exe -m vissim_mcp open`, icon `icons\shortcut_<hash>.ico` (inverted background with white "Vis"/"MCP" on two lines; falls back to the launcher icon), start folder = repo root.
  - **Server at sign-in**: Startup folder (user shell folder `Startup`) shortcut for the server: target `pythonw.exe -m vissim_mcp --serve` (if a server is already running, the new process exits at once).
  - Notifies Explorer of the association change.
- `install.json` = `{version, installed_at, progid, pythonw, registry_root, registry_keys[], registry_values[], shortcut, startup_shortcut, icon, icons_dir}`. Re-installing overwrites only this tool's keys.
- Uninstall removes only what the record lists: values, keys (if empty), both shortcuts (desktop and Startup), `icons\`, `install.json`. Already-missing items are skipped and reported. A key to which someone else added values is kept and the exit code is 1 (record kept, so uninstall can run again). No record → "no install record", 0.
- Icons: launcher and file icon = the Vissim icon with only the background inverted (`#D653D3` → `#29AC2C`); the white text stays. Desktop shortcut = the same inverted shape with white "Vis" (top) and "MCP" (bottom), Segoe UI Semibold. Tray = the same inverted background with one white "V" (Segoe UI Bold, about 70 % of the shape height), drawn at tray-slot size (`SM_CXSMICON`) and loaded from memory or a temporary `.ico`; fallback is a green circle with V. No icon images are stored in the repo.

## Logs, backups, recovery (`<log_root>`, default `runs/mcp/`)

- `changes.jsonl` — one entry per line `{at (ISO 8601 with time zone), server_start_id, tool, kind, network_path, type, no, attr, before, after, ok, error, …}`. `kind ∈ open · save · set · create · delete · run_simulation · recovery · vissim_lost · view · file_switch · external_change`.
  - `view` — layout load by `set_view`: `layout`, `layout_path`, `source_layx`, `changed`.
  - `file_switch` — followed a file opened from the Vissim menu: `previous_path`, `current_path`, `changed_by="vissim_menu"`, `dropped_writes`.
  - `external_change` — the open file's modification time changed outside the server (probably a save from the Vissim window): `before_mtime`, `after_mtime`, `note`.
  - Launcher open: `kind=open`, `tool=admin_open`, `previous_path`, `changed_by="launcher"`.
  - Save from the quit dialog: `kind=save`, `tool=quit`.
- `launcher.log` — one JSON line per launcher run `{time, pid, path, answers, choices, warnings, vissim_pid, exit}` (write failures ignored).
- `server.log` — UTF-8, rotating (5 MB × 5).
- `backup/<name>_<YYYYMMDD-HHMMSS>.bak.inpx` — pre-save copy of a file under `protected_roots` (other files get the `.bak` next to them).
- `recovery/<name>_<YYYYMMDD-HHMMSS>.inpx` — recovery copy at shutdown when a file was open (regardless of write count); `recovery/drawn_<YYYYMMDD-HHMMSS>.inpx` when there was only drawn content.
- `layouts/<name>_view.layx` — view layout copy made by `set_view(layout="view")` (rewritten on every call; the original `.layx` is unchanged).
- `captures/<YYYYMMDD-HHMMSS>[_label].png` — `capture_view` images.
- `probe_<YYYYMMDD-HHMMSS>[_label].txt` — `tools/com_probe.py` output.
- Simulation result files (`.att`, result DB) are written by Vissim itself to `<name>.results\` next to the network, not by this tool.

## COM probe (`tools/com_probe.py`)

Run once on a PC with Vissim 2026, and again after a Vissim version change. It never saves the user's network.

```
python tools/com_probe.py [--progid Vissim.Vissim.26] [--net <saved .inpx>] [--force] [--rot-automation] [--out <path>] [--label <name>]
```

- Without `--net`: creates an empty new network (stops if Vissim already has an open network, unless `--force`), checks every create signature, remove method, default attributes, lane, simulation and evaluation attribute in the registry. Simulation and result items are skipped (an unsaved network blocks `RunContinuous`).
- With `--net`: opens that saved network instead of `New()` and also runs the simulation and result items. Nothing is saved.
- `--rot-automation`: also starts Vissim with `-automation` and checks whether `GetActiveObject` can attach (it cannot on 2026 SP09).
- Each item prints OK or rejected with Vissim's message; a rejection does not stop the run. Only the registry's signatures are tried — no guessed alternatives.
- Extra option group (any of these replaces the default items): `--scenario <copy folder> [--plain <copy .inpx>]`, `--signal <FIXEDTIME copy .inpx>`, `--signal-simple <FIXEDTIMESIMPLE copy .inpx>`, `--intervals [--net <saved .inpx>]`, `--step-timeout <s>` (default 300). These take only copies under `runs/mcp/scratch/`, recopy them per run to `runs/mcp/scratch/<time>/`, and open and save only those. A step over the timeout ends only the Vissim the probe started. Findings are in [engineering-notes](engineering-notes.md).
- Output: `<log_root>/probe_<YYYYMMDD-HHMMSS>[_label].txt` (or `--out`).

## Command-line MCP client (`tools/mcp_call.py`)

A fallback for calling the server's tools when the AI client's connection is down (the server still runs in the tray).

```
python tools/mcp_call.py [--port <n>] list
python tools/mcp_call.py [--port <n>] call <tool> [--args '<JSON object>']
python tools/mcp_call.py [--port <n>] call --file calls.json [--stop-on-error]
```

- Address fixed at 127.0.0.1; port from `--port`, then `port` in `config/vissim_mcp.json`, then 8766.
- `--file` format: `[["tool", {args}], …]` or `[{"tool": "tool", "args": {args}}, …]`. Continues after an `ok=false` response unless `--stop-on-error`.
- Output: UTF-8, the server's JSON response text as is.
- Exit codes: 0 all calls succeeded (JSON response and `ok` not false), 1 a failure response, 2 argument error, 3 could not reach the server.
- Writes no files. The tool called decides what changes; write tools such as `save_network` are passed through, so call only what the user asked for. The server's protected list and save rules apply as usual.
