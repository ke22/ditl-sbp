"""Deterministic-content A4 PDF renderer for coding_synthesis delivery views.

The accepted Markdown renderer owns content and section order. Pandoc converts
that presentation to HTML and the repository's existing Playwright/Chromium
runtime prints it to PDF. Chromium may write variable PDF metadata, so
determinism is defined over extracted report content and layout, not raw bytes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import storage_locations

from .delivery import CODING_SYNTHESIS, DeliveryError, DeliveryViewModel
from .delivery_markdown import render_coding_synthesis_markdown


_PAGE_CSS = """
<style>
@page { size: A4 portrait; margin: 17mm 18mm 21mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0;
  color: #20252b;
  background: #fff;
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", "Heiti TC", sans-serif;
  font-size: 10pt;
  line-height: 1.68;
  overflow-wrap: anywhere;
  word-break: normal;
}
h1, h2, h3, h4 {
  color: #172b3a;
  line-height: 1.32;
  break-after: avoid-page;
  page-break-after: avoid;
  overflow-wrap: anywhere;
}
h1 { font-size: 21pt; margin: 0 0 20pt; }
h2 {
  font-size: 15pt;
  margin: 22pt 0 10pt;
  padding-bottom: 4pt;
  border-bottom: 0.6pt solid #bcc9d1;
}
h3 { font-size: 11.5pt; margin: 15pt 0 6pt; }
h2#附錄證據與來源追溯 {
  border-top: 1.2pt solid #899aa5;
  margin-top: 28pt;
  padding-top: 14pt;
}
p { margin: 5pt 0 8pt; orphans: 3; widows: 3; }
ul, ol { margin: 4pt 0 9pt; padding-left: 20pt; }
li { margin: 2pt 0; break-inside: avoid; }
blockquote {
  margin: 6pt 0 8pt;
  padding: 3pt 0 3pt 10pt;
  border-left: 2pt solid #9aaab4;
  color: #34434d;
  break-inside: avoid;
}
blockquote p { margin: 1pt 0; }
code {
  font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
  font-size: 0.86em;
  overflow-wrap: anywhere;
  word-break: normal;
}
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 8pt 0 12pt; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td {
  border: 0.5pt solid #bdc8ce;
  padding: 4pt 5pt;
  vertical-align: top;
  text-align: left;
  overflow-wrap: anywhere;
  word-break: break-word;
}
th { background: #edf2f4; font-weight: 600; }
hr { border: 0; border-top: 0.5pt solid #bdc8ce; margin: 14pt 0; }
@media print {
  a { color: inherit; text-decoration: none; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; }
}
</style>
"""

_FOOTER_TEMPLATE = """
<div style="width:100%;font-family:'Noto Sans TC','PingFang TC',sans-serif;
  font-size:8px;color:#64727a;text-align:center;">
  Coding &amp; Synthesis Report　｜　第 <span class="pageNumber"></span> 頁／共
  <span class="totalPages"></span> 頁
</div>
"""


class PDFRenderError(RuntimeError):
    """A stable, user-readable PDF rendering failure."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _resolve_output_path(output_path: str | Path, data_root: str | Path | None = None) -> Path:
    path = Path(output_path).expanduser().resolve()
    if path.suffix.lower() != ".pdf":
        raise PDFRenderError("pdf_output_must_have_pdf_extension")

    protected_name = storage_locations.protected_canonical_data_kind(
        path, data_root if data_root is not None else Path(__file__).resolve().parents[2]
    )
    if protected_name:
        raise PDFRenderError("pdf_output_cannot_target_canonical_data", protected_name)
    if path.exists() and path.is_dir():
        raise PDFRenderError("pdf_output_path_is_directory")
    return path


def _markdown_to_html(markdown: str) -> str:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise PDFRenderError("pdf_runtime_missing", "Pandoc is required for Markdown conversion")
    command = [
        pandoc,
        "--from=markdown",
        "--to=html5",
        "--standalone",
        "--wrap=none",
        "--metadata=lang:zh-TW",
    ]
    try:
        result = subprocess.run(
            command,
            input=markdown,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise PDFRenderError("pdf_markdown_conversion_failed", type(exc).__name__) from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[:500] or f"exit status {result.returncode}"
        raise PDFRenderError("pdf_markdown_conversion_failed", detail)
    if "</head>" not in result.stdout or "<body" not in result.stdout:
        raise PDFRenderError("pdf_markdown_conversion_invalid_output")
    return result.stdout.replace("</head>", _PAGE_CSS + "\n</head>", 1)


def render_coding_synthesis_pdf(
    view_model: DeliveryViewModel,
    output_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Write the accepted coding_synthesis hierarchy as an A4 portrait PDF.

    Content is obtained exclusively from ``render_coding_synthesis_markdown``;
    this function does not load source files, invoke analysis, or call a model.
    The final file is atomically replaced only after a complete PDF is produced.
    """
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    if view_model.export_id != CODING_SYNTHESIS or view_model.delivery_type != CODING_SYNTHESIS:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", view_model.export_id)

    target = _resolve_output_path(output_path, data_root)
    markdown = render_coding_synthesis_markdown(view_model)
    html = _markdown_to_html(markdown)

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PDFRenderError("pdf_runtime_missing", "Playwright is required for Chromium PDF output") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".tmp.pdf", dir=target.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.set_content(html, wait_until="load")
                    page.pdf(
                        path=str(temporary_path),
                        format="A4",
                        print_background=True,
                        display_header_footer=True,
                        header_template="<div></div>",
                        footer_template=_FOOTER_TEMPLATE,
                        margin={"top": "17mm", "right": "18mm", "bottom": "21mm", "left": "18mm"},
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            raise PDFRenderError("pdf_browser_render_failed", str(exc)[:500]) from exc
        if not temporary_path.is_file() or temporary_path.stat().st_size < 100 or not temporary_path.read_bytes().startswith(b"%PDF-"):
            raise PDFRenderError("pdf_browser_render_invalid_output")
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target
