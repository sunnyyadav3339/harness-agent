---
name: files_and_terminal
description: Organise, find, rename, move, zip, convert and inspect files and folders, and run command-line tools with PowerShell. Load for file management, cleanup, batch rename, search, archive, system info or script tasks.
triggers: ["file", "files", "folder", "downloads", "rename", "move", "copy", "zip", "unzip", "organize", "organise", "clean up", "find", "search files", "terminal", "powershell", "command", "script", "disk", "convert"]
vision: never
max_steps: 50
---
# Files and terminal (PowerShell first)

- Use `shell.run` (PowerShell) for anything touching many files; use `file.*` for reading or writing single text files.
- Home is `C:\Users\DELL`; Downloads `C:\Users\DELL\Downloads`; Desktop may live under OneDrive. Check with
  `[Environment]::GetFolderPath('Desktop')`.
- Always **look before you change**: list what will be affected first (`Get-ChildItem ... | Select Name,Length,LastWriteTime`),
  then act, then list again to verify.
- Deleting asks the human for approval (policy). Prefer moving to a folder like `_to_delete` when the task allows.

## Handy commands
- Find: `Get-ChildItem -Path $HOME -Recurse -Filter *.pdf -ErrorAction SilentlyContinue | Select -First 50 FullName`
- Organise by extension:
  `Get-ChildItem $dir -File | ForEach-Object { $d = Join-Path $dir ($_.Extension.TrimStart('.').ToUpper()); New-Item -ItemType Directory -Force $d | Out-Null; Move-Item $_.FullName $d }`
- Batch rename with numbering: `$i=1; Get-ChildItem $dir *.jpg | Sort Name | % { Rename-Item $_.FullName ("photo_{0:D3}.jpg" -f $i++) }`
- Zip / unzip: `Compress-Archive -Path $src\* -DestinationPath $out.zip -Force`; `Expand-Archive $zip -DestinationPath $dir -Force`
- Sizes: `Get-ChildItem $dir -Recurse -File | Measure-Object Length -Sum`
- Text search: `Select-String -Path $dir\*.txt -Pattern 'keyword'`
- System info: `Get-ComputerInfo | Select OsName,OsVersion,CsTotalPhysicalMemory`; disk: `Get-PSDrive -PSProvider FileSystem`
- Download a file: `Invoke-WebRequest -Uri $url -OutFile $path` (only from sources the task names)
- Python is available: `python -c "..."` or write a .py file with file.write and run it.

## Verification
- Show the final state (listing or counts) in task.done. If the task is about a folder, open it with
  `desktop.open <folder>` at the end so the result is visible.

## Learned Heuristics (auto-updated)
