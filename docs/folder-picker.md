# Folder picker (macOS)

`/api/browse-folder` opens a native folder dialog on the machine running the bridge.

## Behavior

- **macOS:** uses `osascript` in a subprocess (avoids AppKit/Tk crashes inside the bridge process).
- **Windows / Linux:** unchanged Tkinter `askdirectory` dialog.
- **Cancel:** returns `{ "path": "", "cancelled": true }` — not an error.
- **Failure / headless:** returns a structured `{ "code", "error", "message" }` and tells the UI to paste a path. Set `ACCURETTA_NO_GUI=1` to skip the dialog entirely.
- **Remote clients:** loopback-only. Tailscale or LAN peers get HTTP 403 and must paste a path into the text field.

Selected folder paths are not written to bridge logs by default.
