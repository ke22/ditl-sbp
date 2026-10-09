"""Deterministic A2 landscape PDF renderer for As-is Service Blueprint."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import storage_locations

from .delivery import DeliveryError, DeliveryViewModel
from .delivery_as_is_blueprint import AS_IS_BLUEPRINT, LEGACY_BLUEPRINT_TITLE, SBP_BLUEPRINT_TITLE
from .delivery_as_is_blueprint_markdown import render_as_is_blueprint_markdown


_PAGE_CSS = """
<style>
@page { size: A2 landscape; margin: 13mm 14mm 17mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0;
  max-width: none;
  padding: 0;
  color: #20252b;
  background: #fff;
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", "Heiti TC", sans-serif;
  font-size: 9.1pt;
  line-height: 1.55;
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
h1 { font-size: 22pt; margin: 0 0 17pt; }
h2 {
  font-size: 15pt;
  margin: 18pt 0 8pt;
  padding-bottom: 4pt;
  border-bottom: 0.6pt solid #bcc9d1;
}
h3 { font-size: 12pt; margin: 13pt 0 5pt; }
h4 { font-size: 10.5pt; margin: 10pt 0 5pt; }
p { margin: 4pt 0 7pt; orphans: 3; widows: 3; }
ul, ol { margin: 3pt 0 8pt; padding-left: 18pt; }
li { margin: 1pt 0; break-inside: avoid; }
code {
  font-family: "SFMono-Regular", "Menlo", "Consolas", monospace;
  font-size: 0.84em;
  overflow-wrap: anywhere;
}
table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 7pt 0 10pt; font-size: 8pt; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td {
  border: 0.5pt solid #bdc8ce;
  padding: 3.5pt 4pt;
  vertical-align: top;
  text-align: left;
  overflow-wrap: anywhere;
  word-break: break-word;
}
th { background: #edf2f4; font-weight: 600; }
h2#issues-gaps-canonical-graph-markers, h2#relations-provenance-notes, h2#conflicts-limitations, h2#availability {
  border-top: 1.2pt solid #899aa5;
  margin-top: 24pt;
  padding-top: 11pt;
}
@media print {
  a { color: inherit; text-decoration: none; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; }
}
</style>
"""

_FOOTER_TEMPLATE = """
<div style="width:100%;font-family:'Noto Sans TC','PingFang TC',sans-serif;
  font-size:8px;color:#64727a;text-align:center;">
  {title}　｜　第 <span class="pageNumber"></span> 頁／共
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


def _title(view_model: DeliveryViewModel) -> str:
    title = (view_model.to_dict().get("metadata") or {}).get("title")
    return title if title in {LEGACY_BLUEPRINT_TITLE, SBP_BLUEPRINT_TITLE} else LEGACY_BLUEPRINT_TITLE


def render_as_is_blueprint_pdf(
    view_model: DeliveryViewModel,
    output_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Write the normalized As-is hierarchy as an A2 landscape PDF."""
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    if view_model.export_id != AS_IS_BLUEPRINT or view_model.delivery_type != AS_IS_BLUEPRINT:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", view_model.export_id)

    target = _resolve_output_path(output_path, data_root)
    html = _markdown_to_html(render_as_is_blueprint_markdown(view_model))
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
                        format="A2",
                        landscape=True,
                        print_background=True,
                        display_header_footer=True,
                        header_template="<div></div>",
                        footer_template=_FOOTER_TEMPLATE.replace("{title}", _title(view_model)),
                        margin={"top": "13mm", "right": "14mm", "bottom": "17mm", "left": "14mm"},
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
