"""research-claim-review-gate：記錄研究者對外部調研主張的審查。

`research_catalog_review` 只負責讀與解析（它不能寫、不能替研究者產生或預設
審查——見 test_to_be_research_review_integration 的 ProductionNeverSynthesizesReview
守則）；這支只把研究者在⑦逐條做的決定與理由原樣記下來，綁定到這一次調研的
request digest 與目錄內容，並用解析端同一套結構、綁定、語意檢查驗證，通過才寫。
它不預設任何決定：每一條都必須由呼叫端（研究者的輸入）明確給出。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
import validate_sbp as V

from . import lifecycle
from . import research_catalog_review as CR


def record_decisions(
    run_dir: str | Path,
    catalog: dict[str, Any],
    *,
    catalog_request_digest: str,
    catalog_path: str,
    case_id: str,
    scenario_id: str,
    decisions: list[dict[str, Any]],
    reviewed_at: str,
    filename: str = CR.REVIEW_FILENAME,
) -> dict[str, Any]:
    """同一份目錄先前的衍生機制／條件／問題保留（語意檢查會擋掉跟新決定矛盾的）；
    目錄換了就不帶過來。不呼叫模型、不動調研內容。驗證失敗時 raise
    `CR.ReviewError(CR.INVALID)` 且不寫檔。`filename` 預設是藍圖確認後那一站的
    審查檔；藍圖前的落地研究用自己的檔名（pre-blueprint-implementation-research）。"""
    run_path = Path(run_dir)
    catalog_digest = lifecycle.canonical_content_digest(catalog)
    doc: dict[str, Any] = {
        "schema_version": 1,
        "case_id": case_id,
        "scenario_id": scenario_id,
        "reviewed_at": reviewed_at,
        "reviewed_catalog": {
            "request_digest": catalog_request_digest,
            "catalog_digest": catalog_digest,
            "catalog_path": catalog_path,
        },
        "claim_decisions": [
            {"claim_ref": entry.get("claim_ref"), "decision": entry.get("decision"),
             # A whitespace-only rationale is no rationale.
             "researcher_rationale": entry["researcher_rationale"].strip()
             if isinstance(entry.get("researcher_rationale"), str) else entry.get("researcher_rationale")}
            for entry in decisions if isinstance(entry, dict)
        ],
    }
    try:
        previous = CR.load(run_path, filename=filename)
    except CR.ReviewError:
        previous = None
    if isinstance(previous, dict) and (previous.get("reviewed_catalog") or {}).get("catalog_digest") == catalog_digest:
        for key in ("design_mechanisms", "mechanism_derivation_note", "design_conditions", "design_questions"):
            if key in previous:
                doc[key] = previous[key]
    errors = V.structural_errors(doc, V.load_schema("research-catalog-review"))
    errors += CR.binding_errors(doc, catalog, catalog_request_digest=catalog_request_digest,
                                case_id=case_id, scenario_id=scenario_id)
    errors += CR.semantic_errors(doc, catalog)
    if errors:
        raise CR.ReviewError(CR.INVALID, errors)
    path = run_path / filename
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return doc
