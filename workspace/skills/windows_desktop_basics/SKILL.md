---
name: windows_desktop_basics
description: How to operate Windows 11 desktop apps reliably - launching apps, File Explorer, open/save dialogs, window switching, Settings pages, system dialogs. Load for any task involving native desktop applications.
triggers: ["open app", "desktop", "file explorer", "notepad", "paint", "calculator", "settings", "window", "save as", "screenshot of", "folder"]
vision: always
max_steps: 60
---
# Windows desktop basics

## Launching and switching
- Prefer `desktop.open` with the app or target: "notepad", "calc", "mspaint", "explorer", "excel", "winword",
  "powerpnt", "outlook", a file path (opens in its default app), a folder path, or a URI like "ms-settings:display".
- After launching, the new window needs ~1-2 s. If it isn't in the foreground, use `desktop.focus(title_contains=...)`.
- `desktop.windows` lists open windows; use it when unsure what's open.
- Close a window: `desktop.hotkey("alt+f4")`. If a "Save changes?" prompt appears, answer according to the task (default: Don't Save only if the task didn't ask to keep changes).

## Working inside apps
- Prefer UI element ids from the UIA list. If the list is empty (canvas or custom apps), click by screenshot coordinates.
- Keyboard shortcuts are faster and more reliable than menus: ctrl+s save, ctrl+shift+s / F12 save as, ctrl+o open,
  ctrl+n new, ctrl+a select all, ctrl+c/v copy/paste, ctrl+z undo, ctrl+f find, ctrl+p print, win+e Explorer.
- To type long or unicode text, use `desktop.type` (it pastes via the clipboard automatically).

## Open / Save dialogs
- In the File name box, type the FULL path (e.g. `C:\Users\DELL\Desktop\report.txt`) and press Enter; that is
  faster than navigating folders. If "already exists - replace?" appears, confirm only if the task wants overwriting.
- Verify the file was written: `file.list` the folder or `shell.run("Test-Path '<path>'")`.

## File Explorer
- Navigate by typing a path into the address bar: click it (or alt+d), type the path, Enter.
- For bulk file operations (rename many, move by type, find), use `shell.run` with PowerShell instead of clicking.

## Settings
- Open pages directly with URIs: ms-settings:display, ms-settings:sound, ms-settings:bluetooth,
  ms-settings:network-wifi, ms-settings:defaultapps, ms-settings:dateandtime, ms-settings:personalization-background.
- Change a system or security setting only when the task explicitly asks for it; otherwise only read it.

## Verification
- After each meaningful action, confirm from the screenshot or element list that the expected window/state appeared.
- When the task is done, leave the result visible on screen (the judges are watching) and call task.done with what changed.

## Learned Heuristics (auto-updated)
