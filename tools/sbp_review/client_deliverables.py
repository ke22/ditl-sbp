"""Client-facing deliverables: the workbench screens themselves, printed to PDF.

2026-10-02 researcher ruling: what goes to a client is what the researcher sees
on screen (coding, JTBD + HMW, Persona Matrix + Persona, CJM, service blueprint), not an
evidence list. Each item is printed by headless Chromium from the workbench's
own print mode (``ui.html`` ``printDeliverableMode``), one PDF per item. This
module owns which items exist, when each can be offered, and the printing; the
content and its look stay in the UI.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

CLIENT_DELIVERABLES = (
    {"id": "client_coding", "print": "coding", "label": "開放／主軸／選擇編碼", "filename": "coding",
     "requires": (("axial_coding.yaml",),)},
    {"id": "client_jtbd_hmw", "print": "jtbd_hmw", "label": "JTBD＋HMW", "filename": "jtbd-hmw",
     "requires": (("narrative_job_selection.yaml", "jtbd.yaml"), ("primary_hmw_selection.yaml",))},
    {"id": "client_persona", "print": "persona", "label": "Persona Matrix＋Persona", "filename": "persona",
     "requires": (("persona_content.yaml",),)},
    {"id": "client_cjm", "print": "cjm", "label": "顧客旅程圖", "filename": "customer-journey-map",
     "requires": (("journey.yaml", "journey_proposal.yaml"),)},
    {"id": "client_blueprint", "print": "blueprint", "label": "服務藍圖", "filename": "service-blueprint",
     "requires": (("service_graph.yaml",),)},
)
CLIENT_DELIVERABLE_BY_ID = {item["id"]: item for item in CLIENT_DELIVERABLES}

_A4_SCALE = 0.72  # the 960px print page fits A4 portrait inside 12mm margins
_READY = "document.documentElement.dataset.printReady==='1'||!!document.documentElement.dataset.printError"


class ClientDeliverableError(RuntimeError):
    pass


def available(run_dir: Path) -> list[dict]:
    """Items whose station has content in this run, in menu order."""
    out = []
    for item in CLIENT_DELIVERABLES:
        if all(any((run_dir / name).is_file() for name in group) for group in item["requires"]):
            out.append({"id": item["id"], "label": item["label"], "formats": ["pdf"]})
    return out


def print_url(base_url: str, item_id: str, case_id: str, run_name: str) -> str:
    query = urllib.parse.urlencode({"print": CLIENT_DELIVERABLE_BY_ID[item_id]["print"],
                                    "case": case_id, "run": run_name})
    return f"{base_url.rstrip('/')}/?{query}"


BROWSER_MISSING_MESSAGE = "輸出 PDF 需要的瀏覽器元件還沒裝好，請重新執行 ./install.sh 後再輸出。"


def render_pdfs(base_url: str, case_id: str, run_name: str, item_ids: list[str],
                cookie: str | None = None, timeout_ms: int = 90000) -> list[tuple[str, bytes]]:
    """Print each item's screen to PDF bytes, as (filename, body) pairs."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ClientDeliverableError("這台機器沒有安裝 PDF 列印需要的 Playwright。") from exc
    rendered = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:  # never show Playwright's own text or local paths
            raise ClientDeliverableError(BROWSER_MISSING_MESSAGE) from exc
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 1000},
                                          extra_http_headers={"Cookie": cookie} if cookie else None)
            for item_id in item_ids:
                item = CLIENT_DELIVERABLE_BY_ID[item_id]
                page = context.new_page()
                try:
                    page.goto(print_url(base_url, item_id, case_id, run_name), wait_until="domcontentloaded")
                    page.wait_for_function(_READY, timeout=timeout_ms)
                    data = page.evaluate("({...document.documentElement.dataset})")
                    if data.get("printError"):
                        raise ClientDeliverableError(f"「{item['label']}」無法輸出：{data['printError']}")
                    page.emulate_media(media="screen")
                    if data.get("printFit"):
                        # The journey map is one wide poster page, as wide as the table.
                        body = page.pdf(width=f"{int(data['printWidth']) + 2}px",
                                        height=f"{int(data['printHeight']) + 2}px",
                                        print_background=True, page_ranges="1")
                    else:
                        body = page.pdf(format="A4", print_background=True, scale=_A4_SCALE,
                                        margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
                except PlaywrightError as exc:
                    raise ClientDeliverableError(f"「{item['label']}」列印失敗或逾時，請再試一次。") from exc
                finally:
                    page.close()
                rendered.append((f"{item['filename']}.pdf", body))
        finally:
            browser.close()
    return rendered
