# 0003 — A launcher routes `.inpx` double-clicks through the MCP server; COM cannot attach to a Vissim you start yourself, so every open goes through the server

## Context
The user wanted any Vissim they open to be controllable from chat (COM) at all times. The first question was: "to use COM, I have to add `-automation` to the Vissim shortcut, right?" (an earlier bridge's README said an `-automation` Vissim can be attached after 10–30 s). [0001](0001-resident-server-own-vissim.md) put "attach to a window the user started" out of scope, but the saved record of the test behind it lacked that step's output. The test was repeated and the design settled.

## Findings (Vissim 2026 SP09)
- **`-automation`**: on a Vissim started with `Vissim260.exe -automation`, `GetActiveObject` every 5 s for 90 s → all `MK_E_UNAVAILABLE`; no Vissim entry in the ROT.
- **`Dispatch` does not attach either**: with the server running and an `-automation` Vissim (empty window) started 30 s earlier, `open_network` did not use that window and started a new Vissim (`launched=true`, warning "1 Vissim besides the server's").
- **One server, several Vissims**: two `Dispatch` calls in one process and one thread → two Vissims, each with a different file; alternating reads and writes did not mix. Released references closed them within seconds. Each window uses a licence, and with one COM thread a busy window blocks the other.
- **Open file path**: top-level Vissim object `AttValue("WorkingFolder")` + `AttValue("InputFile")` (an empty Vissim has `InputFile=''`). Not on `Net`.
- **`LoadNet` discards unsaved changes without asking** (after renaming a link, `LoadNet` of another file took 9.2 s, no dialog, original file unchanged).
- When the server lets go (quit or crash), its Vissim closes — "Vissim stays after the server stops" is not possible.

## Decision
- **Launcher**: `python -m vissim_mcp open [path]`. `install` changes the default `.inpx` double-click action to it (per-user `HKCU\Software\Classes\<ProgID>` only; Vissim's own menu entries stay on right-click) and creates a desktop shortcut without a path (server + empty Vissim). The launcher starts the server if it is off and opens the file in the server's Vissim through `POST /admin/open`. `install` also creates a Startup folder shortcut for the server (`pythonw -m vissim_mcp --serve`), so the server runs from sign-in and the AI client's connection does not fail when a session opens. `uninstall` removes only what `install.json` lists.
- **Server : Vissim stays 1 : 1**. If another file is open, the launcher asks every time ([Discard and open] / [Cancel], warning that on-screen edits are lost too). For the same file it does not reload; it only brings the window to the front. One open at a time (mutex). If another Vissim is running, it says so and opens. [Open in plain Vissim] is offered **only when the server or Vissim could not start at all**.
- **Following the open file**: on each tool or admin call the server reads the open file once (no periodic polling) and follows a file changed from the Vissim menu. After the file changes through the menu or the launcher, **the first write tool** is refused with `network_changed` (once per server) — to make the conversation aware that the file changed behind its back.
- **Outside changes**: if the open file's modification time changes outside the server, it is logged as `external_change` and the response carries a warning — the assistant first asks the user whether they saved from the Vissim window.
- **`open_network`**: if a file is open or content is drawn (links without a file), `discard` is needed even with zero writes. A rule the server does not enforce: the assistant sets `discard=true` only after asking the user and getting an answer.
- **Shutdown**: tray "Quit server and Vissim". With an open file: [Save and quit / Quit without saving (recovery copy) / Cancel]; if another Vissim is running, ask once more before saving; with no answer, a recovery copy. With drawn content only: OK / Cancel. A running simulation is stopped first.
- **Icons**: launcher and `.inpx` = the Vissim icon with its background colour inverted. Desktop shortcut = the same inverted shape with white "Vis"/"MCP" on two lines. Tray = inverted background with one white letter "V" (Segoe UI Bold). Two lines of text were first tried in the tray, but at 16 px they were too small; `pystray` also loaded the icon at 32 px and blurred it when scaling down, so the tray icon is drawn at tray-slot size and loaded directly. Icons are generated from the installed Vissim exe at run time and not stored in the repo. Launcher icon files live in `%LOCALAPPDATA%\vissim_mcp\icons\` (unaffected by cleaning `runs/`).

## Alternatives rejected
- **`-automation` on Vissim shortcuts and file associations**: does not attach, per the tests above. Not to be revived.
- **One server with several Vissims**: possible, but the user chose simplicity (licences, waiting on one COM thread, and every one of the 20 tools would need a target). Not to be revived.
- **Adding "Open with MCP" to the right-click menu only**: the user chose to change the default action.
- **Auto-install when the server starts**: the user chose explicit install and uninstall commands (they change the registry).
- **Confirm only when the conversation made changes**: the server cannot count on-screen edits and `LoadNet` drops them without asking, so it always confirms.
- **First-write refusal per conversation (a new path-confirmation argument)**: the user chose once per server (assumes one conversation).
- **Enforcing `discard` with a dialog on the server's screen**: the user chose to keep it as a rule for the assistant.
- **Always offering [Open in plain Vissim] on failure**: when the server is busy, the same file may be open in the server, and two windows would overwrite each other — offered only when nothing could start.
- **Icons in the repo or in `runs/mcp/icons/`**: no PTV material goes into the public repo, and cleaning `runs/` would break file icons.
- **Black text on the tray icon**: the user chose white text (after comparing drafts).
