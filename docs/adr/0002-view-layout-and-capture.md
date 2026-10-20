# 0002 — View tools for the MCP server (`set_view`, `capture_view`); the view layout is a copy of the `.layx`

## Context
The user wanted to check a network with background images and 3D models in Vissim 3D, but the server had no tool to view or capture the screen. Operating the user's Vissim window by clicking failed: clicks did not reach the network object pane or menus (an overlay window on screen swallowed them), so even turning on 3D model visibility was impossible. A network saved with background images and 3D models hidden shows no background when simply opened. The user asked to improve the MCP server first, to use screen captures for development checks, and to allow fixing the view layout.

## Findings (Vissim 2026 SP09, a Vissim started with Dispatch like the server's, nothing saved)
- `NetworkWindow.SetAttValue("3D", 0|1)`, `SetCameraPositionAndAngle(x, y, z, yaw, pitch)` and `Screenshot(path, scale)` are accepted (`IGraphics` has no `Screenshot`; `CurrentNetworkWindow` does). Scale 1 → the size of the editor area (2300×1229).
- Showing background images and 3D models, and the online map, are not in COM (`ShowMap`, `Visibility`, `GraphicParameters` etc. are rejected; `Static3DModel` has no visibility attribute either).
- `Vissim.LoadLayout(<.layx path>)` is accepted, and loading a layout with visibility on makes 3D models and background images appear. That layout's online map came on as well. `NetworkWindow.ApplyLayout(path)` is refused with "no layout named …" (it is for named layouts).
- Win32 `PrintWindow(PW_RENDERFULLCONTENT)` captures the whole server Vissim window (2576×1408) as on screen, including 3D, even when other windows cover it.

## Decision
- **`set_view`**: changes the view only — load layout → 2D/3D → 2D `ZoomTo` → 3D `SetCameraPositionAndAngle`. Network objects, the write counter and the `.inpx` are not touched.
- **The view layout is a copy**: with `layout="view"`, the server **only reads** `<name>.layx` next to the network and writes a new copy `<log_root>/layouts/<name>_view.layx` with four attributes changed — background images and 3D models `true`, online map 2D and 3D `false` — then loads it with `LoadLayout` (`layout_xml.write_view_layout`: all other bytes kept; the copy is parsed again and deleted on mismatch; the same path as the original is refused). The online map is turned off so that opening does not make external requests every time. Each load is logged in `changes.jsonl` as `kind=view`.
- **`capture_view`**: PNG in `<log_root>/captures/`. `mode=network` uses COM `Screenshot` (editor picture only, for reports); `mode=window` captures the whole server Vissim window as on screen (for development checks — the same as what the user sees). Window capture uses Win32 GDI through ctypes (`vissim_mcp/screen.py`), not COM, and not pywin32. It waits `capture_settle_s` (default 3 s) before capturing.
- Only the Vissim the server started is captured (PID from the before/after process difference at launch). Vissim windows the user opened in the GUI are not captured (as in [0001](0001-resident-server-own-vissim.md)).

## Alternatives rejected
- **The user saves a view layout once in the GUI (`SaveLayout`)**: no XML writing, but layouts differ per network (camera, window arrangement) and it needs manual work. The user chose the copy.
- **Loading an older network's `.layx`**: visibility comes on, but so does the online map, and it is another network's layout.
- **Editing the `.layx` next to the network in place**: that writes a hand-edited file. Only a copy is written.
- **Operating the GUI by clicking**: clicks sometimes do not land (this case), and checks depend on the state of a person's screen.

## Consequences
- 20 tools (`set_view` and `capture_view` added), `view` added to `changes.jsonl` kinds, tunable `capture_settle_s`. No new `error.kind`.
- Loaded display settings stay in Vissim, so a later `save_network` also writes them into the network's `.layx` (stated in the tool description and contracts).
- Tests `tests/vissim_mcp/test_mcp_view.py` (four attributes in the copy, original unchanged, same path refused, nothing written on unexpected format, call order, argument checks before COM, capture path, `_2` suffix, no PID, no window).
