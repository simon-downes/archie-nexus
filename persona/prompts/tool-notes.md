<!--
Notes for non-obvious preinstalled tools, rendered into the Environment section.
Each entry is a bullet starting with the backticked binary name; it is shown only
when that binary is on PATH. Indented lines continue the previous entry.
Keep entries to one or two lines — anything longer belongs in a skill.
-->
- `agent-browser` — drives headless Chromium to load and interact with web pages
  (JS-rendered content, forms, logins, screenshots). Use `web_fetch` for static pages.
- `tofu` — OpenTofu is installed in place of Terraform; use `tofu` wherever docs say `terraform`.
- `pandoc` — converts between document formats (Markdown, HTML, DOCX, PDF output, etc.).
- `pdftotext` — poppler PDF tools: `pdftotext`, `pdfinfo`, `pdftoppm` for reading and rasterising PDFs.
