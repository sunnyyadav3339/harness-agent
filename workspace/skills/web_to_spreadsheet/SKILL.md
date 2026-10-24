---
name: web_to_spreadsheet
description: Extract structured data (a table, list or prices) from a web page into a CSV/Excel file, optionally with a chart or PDF export, and show it in Excel. Load for scrape-to-Excel, tabulate, export data or make-a-chart-from-website tasks.
triggers: ["table to excel", "into excel", "into a spreadsheet", "scrape", "extract data", "export to excel", "tabulate", "chart from", "table from", "from this page", "from the website", "web page", "wikipedia"]
vision: auto
max_steps: 60
---
# Web to spreadsheet

1. Open the page (`browser.open`). Find the data on the observation or with `browser.read`.
2. Extract it in one go with `browser.eval`, returning CSV text. Example for an HTML table:
   ```js
   (() => { const t = document.querySelector('table'); return Array.from(t.rows).map(r => Array.from(r.cells).map(c => '"' + c.innerText.trim().replace(/"/g, '""') + '"').join(',')).join('\n'); })()
   ```
   For lists or cards, map the repeated elements to fields in the same way. Check the row count matches the page.
3. `file.write` the CSV (UTF-8) to the requested path, default `C:\Users\DELL\Desktop\<topic>.csv`.
4. For .xlsx, formatting, charts or PDF: use the COM patterns in skill `office_documents` (load it).
   Numbers must be numbers: strip currency symbols and commas before writing numeric columns.
5. Open the result in Excel (`desktop.open <path>`) and verify the headers and first rows on screen.
6. task.done with the path(s), row count and a one-line summary of the data.

## Learned Heuristics (auto-updated)
