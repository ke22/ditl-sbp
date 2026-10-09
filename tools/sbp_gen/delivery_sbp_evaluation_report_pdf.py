"""A4 portrait PDF for ``sbp_evaluation_report``, plus the Research-station
export dispatch: new runs render the SBP Evaluation Report, legacy runs the
unchanged To-be Service Report."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import stages
from .delivery import DeliveryError, DeliveryViewModel, _read_yaml
from .delivery_sbp_evaluation_report import (
    SBP_EVALUATION_REPORT, SBP_EVALUATION_REPORT_TITLE, build_sbp_evaluation_report_view,
)
from .delivery_sbp_evaluation_report_markdown import render_sbp_evaluation_report_markdown
from .delivery_to_be_report import build_to_be_report_view
from .delivery_to_be_report_markdown import render_to_be_report_markdown
from .delivery_to_be_report_pdf import (
    _FOOTER_TEMPLATE, PDFRenderError, _markdown_to_html, _resolve_output_path, render_to_be_report_pdf,
)


def render_sbp_evaluation_report_pdf(
    view_model: DeliveryViewModel,
    output_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> Path:
    if not isinstance(view_model, DeliveryViewModel):
        raise DeliveryError("renderer_invalid_input", "delivery_view", "expected DeliveryViewModel")
    if view_model.export_id != SBP_EVALUATION_REPORT or view_model.delivery_type != SBP_EVALUATION_REPORT:
        raise DeliveryError("renderer_unsupported_delivery_type", "delivery_view", view_model.export_id)
    target = _resolve_output_path(output_path, data_root)
    html = _markdown_to_html(render_sbp_evaluation_report_markdown(view_model))
    footer = _FOOTER_TEMPLATE.replace("To-be Service Report", SBP_EVALUATION_REPORT_TITLE)
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
                        path=str(temporary_path), format="A4", print_background=True,
                        display_header_footer=True, header_template="<div></div>", footer_template=footer,
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


def _sbp_workflow(run_dir: str | Path) -> bool:
    path = Path(run_dir) / "journey.yaml"
    return path.is_file() and stages.journey_uses_solution_selection(_read_yaml(path, "confirmed_journey"))


def build_research_report_view(run_dir: str | Path) -> DeliveryViewModel:
    """The Research-station export: SBP Evaluation Report for a new run,
    the unchanged To-be Service Report for a legacy run."""
    if _sbp_workflow(run_dir):
        return build_sbp_evaluation_report_view(run_dir)
    return build_to_be_report_view(run_dir)


def render_research_report_markdown(view_model: DeliveryViewModel) -> str:
    if view_model.export_id == SBP_EVALUATION_REPORT:
        return render_sbp_evaluation_report_markdown(view_model)
    return render_to_be_report_markdown(view_model)


def render_research_report_pdf(view_model: DeliveryViewModel, output_path: str | Path, *,
                               data_root: str | Path | None = None) -> Path:
    if view_model.export_id == SBP_EVALUATION_REPORT:
        return render_sbp_evaluation_report_pdf(view_model, output_path, data_root=data_root)
    return render_to_be_report_pdf(view_model, output_path, data_root=data_root)
