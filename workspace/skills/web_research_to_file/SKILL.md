---
name: web_research_to_file
description: Look something up on the web (search, read pages, compare sources) and save or report the findings, e.g. as a text/markdown/Word file or a note. Load for research, lookup, price check, news, compare or summarise-from-web tasks.
triggers: ["search", "look up", "find out", "research", "google", "news", "price of", "compare", "summarize", "summarise", "weather", "website", "wikipedia"]
tools: ["browser.", "file.", "shell.", "desktop.", "task.", "human.", "skill.", "wait", "clock."]
vision: auto
max_steps: 50
---
# Web research to file

1. `browser.open` a search URL directly: `https://duckduckgo.com/?q=<url-encoded query>` (or a site the task
   names). No sign-in needed; decline cookie banners.
2. Open the 2-3 most relevant results. Use `browser.read` (whole page or one element) to get full text instead
   of scrolling screenshots. `browser.eval` can pull tables:
   `Array.from(document.querySelectorAll('table tr')).map(r => Array.from(r.cells).map(c => c.innerText.trim()).join(' | ')).join('\n')`
3. task.note key facts with their source URL as you go (notes survive across steps).
4. Write the result where asked (`file.write`, e.g. `C:\Users\DELL\Desktop\findings.md`) with a short summary,
   bullet facts and source links. For a Word document, load skill `office_documents`.
5. If the task wants it on screen, open the file (`desktop.open <path>`).

Rules: prefer primary/official sources; give numbers with units and dates; never enter personal data into
sites found during research; don't download files unless the task asks.

## Learned Heuristics (auto-updated)
