---
name: office_documents
description: Create, edit, format and export Microsoft Excel, Word and PowerPoint documents (installed, Office16), including saving as PDF. Load for spreadsheet, document, report, slide or PDF-export tasks.
triggers: ["excel", "spreadsheet", "xlsx", "csv", "word", "docx", "document", "powerpoint", "pptx", "slides", "pdf", "chart", "table", "report"]
vision: always
max_steps: 80
---
# Office documents (Excel / Word / PowerPoint)

Two routes. Pick per step:
1. **Data route (fast, exact):** produce content with shell or Python, e.g. write a CSV with `file.write`,
   or drive Office through COM from PowerShell. Best for many cells, formulas, bulk formatting and PDF export.
2. **GUI route (visible):** operate the app with desktop tools. Use it when the task is about using the app,
   for visual checks, and to leave the final result open on screen.
A good default: build with the data route, then open the file in the app (`desktop.open <path>`) and verify visually.

## Excel via PowerShell COM (robust patterns)
```powershell
$xl = New-Object -ComObject Excel.Application; $xl.Visible = $true
$wb = $xl.Workbooks.Add(); $ws = $wb.Worksheets.Item(1)
$ws.Cells.Item(1,1) = 'Name'; $ws.Range('A1:C1').Font.Bold = $true
$ws.UsedRange.Columns.AutoFit() | Out-Null
$wb.SaveAs('C:\Users\DELL\Desktop\out.xlsx', 51)          # 51 = xlsx
$wb.ExportAsFixedFormat(0, 'C:\Users\DELL\Desktop\out.pdf') # 0 = PDF
```
- Open an existing file: `$wb = $xl.Workbooks.Open('C:\path\file.xlsx')`.
- A CSV opens directly in Excel: `desktop.open C:\path\data.csv`.
- Charts: `$ch = $ws.Shapes.AddChart2(201, 51).Chart; $ch.SetSourceData($ws.Range('A1:B10'))`.

## Word via COM
```powershell
$w = New-Object -ComObject Word.Application; $w.Visible = $true
$d = $w.Documents.Add(); $d.Content.Text = "Title`r`nBody text"
$d.Paragraphs.Item(1).Range.Style = 'Heading 1'
$d.SaveAs2('C:\Users\DELL\Desktop\doc.docx'); $d.ExportAsFixedFormat('C:\Users\DELL\Desktop\doc.pdf', 17)
```

## PowerPoint via COM
```powershell
$p = New-Object -ComObject PowerPoint.Application; $pres = $p.Presentations.Add()
$s = $pres.Slides.Add(1, 1); $s.Shapes.Item(1).TextFrame.TextRange.Text = 'Title'
$pres.SaveAs('C:\Users\DELL\Desktop\deck.pptx'); $pres.SaveAs('C:\Users\DELL\Desktop\deck.pdf', 32)
```

## GUI tips
- Excel Name Box (left of the formula bar) jumps to a cell: click it, type `B2`, Enter.
- Enter a formula by typing it starting with `=` and pressing Enter.
- Export as PDF: File > Save As (or F12), choose "PDF" in the type dropdown, type the full path, Save.
- First launch may show a sign-in or "What's new" dialog. Close it (Esc or its close button) and continue.

## Verification
- Confirm the output file exists (file.list / Test-Path) and, for data, read back a few cells or the CSV.
- Leave the final document open and visible. Report the saved path(s) in task.done.

## Learned Heuristics (auto-updated)
