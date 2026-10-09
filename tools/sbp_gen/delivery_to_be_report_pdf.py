"""Deterministic A4 PDF renderer for the ``to_be_report`` delivery view."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import storage_locations

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_to_be_report import TO_BE_REPORT
from .delivery_to_be_report_markdown import render_to_be_report_markdown


_PAGE_CSS = """
<style>
@page { size: A4 portrait; margin: 16mm 17mm 20mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0;
  color: #20252b;
  background: #fff;
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", "Heiti TC", sans-serif;
  font-size: 9.5pt;
  line-height: 1.62;
  overflow-wrap: anywhere;
  word-break: normal;
}
h1, h2, h3, h4 {
  color: #172b3a;
  line-height: 1.3;
  break-after: avoid-page;
  page-break-after: avoid;
  overflow-wrap: anywhere;
}
h1 { font-size: 21pt; margin: 0 0 18pt; }
h2 { font-size: 15pt; margin: 21pt 0 9pt; padding-bottom: 4pt; border-bottom: 0.6pt solid #bcc9d1; }
h3 { font-size: 12pt; margin: 16pt 0 6pt; }
h4 { font-size: 10.5pt; margin: 12pt 0 5pt; }
p { margin: 5pt 0 8pt; orphans: 3; widows: 3; }
ul, ol { margin: 4pt 0 9pt; padding-left: 19pt; }
li { margin: 2pt 0; break-inside: avoid; }
code { font-family: "SFMono-Regular", "Menlo", "Consolas", monospace; font-size: 0.84em; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 7pt 0 11pt; font-size: 7.8pt; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td { border: 0.5pt solid #bdc8ce; padding: 3.5pt 4pt; vertical-align: top; text-align: left; overflow-wrap: anywhere; word-break: break-word; }
th { background: #edf2f4; font-weight: 600; }
blockquote { margin: 6pt 0 8pt; padding: 3pt 0 3pt 10pt; border-left: 2pt solid #9aaab4; color: #34434d; break-inside: avoid; }
blockquote p { margin: 1pt 0; }
h2#design-drivers-select, h2#issue-coverage-unsupported-areas, h2#risks-limitations-unresolved, h2#availability, h2#appendix-external-research-decisions { border-top: 1.1pt solid #899aa5; margin-top: 25pt; padding-top: 10pt; }
@media print { a { color: inherit; text-decoration: none; } pre { white-space: pre-wrap; overflow-wrap: anywhere; } }
</style>
"""

_FOOTER_TEMPLATE = """
<div style="width:100%;font-family:'Noto Sans TC','PingFang TC',sans-serif;font-size:8px;color:#64727a;text-align:center;">
  To-be Service Report　｜　第 <span class="pageNumber"></span> 頁／共 <span class="totalPages"></span> 頁
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
    result = subprocess.run(
        [pandoc, "--from=markdown", "--to=html5", "--standalone", "--wrap=none", "--metadata=lang:zh-TW"],
        input=markdown,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()[:500] or f"exit status {result.returncode}"
        raise PDFRenderError("pdf_markdown_conversion_failed", detail)
    if "</head>" not in result.stdout or "<body" not in result.stdout:
        raise PDFRenderError("pdf_markdown_conversion_invalid_output")
    return result.stdout.replace("</head>", _PAGE_CSS + "\n</head>", 1)


def render_to_be_report_pdf(
    view_model: DeliveryViewModel,
    output_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Write the normalized To-be hierarchy as a readable A4 portrait PDF."""
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    if view_model.export_id != TO_BE_REPORT or view_model.delivery_type != TO_BE_REPORT:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", view_model.export_id)
    target = _resolve_output_path(output_path, data_root)
    html = _markdown_to_html(render_to_be_report_markdown(view_model))
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PDFRenderError("pdf_runtime_missing", "Playwright is required for Chromium PDF output") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp.pdf", dir=target.parent)
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
                        margin={"top": "16mm", "right": "17mm", "bottom": "20mm", "left": "17mm"},
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
