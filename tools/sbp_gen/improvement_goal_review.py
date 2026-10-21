"""add-improvement-goal-review-stage：站⑩的改善目標確認/改寫。

`決定一`（openspec/TARGET-ARCHITECTURE.md）：`as_is_research.py` 的
`missing_improvement_goal` 閘門要留著，只是它擋的值要從站①（`case.yaml` 的
研究前直覺）換成站⑩（這裡：研究者看過 HMW 之後確認或改寫的值）。`決定六`：
站①的原始目標要能繼續當比對用的證偽基準，不能被這裡的改寫覆蓋掉。

這裡走既有四個框架點選擇檔（`persona_dimensions_selection.yaml` 等）的輕量
CLI 寫法——單一 YAML、無伺服器、無內容指紋過期偵測——不是 CJM／現況藍圖那套
`review_state` + `reviewed_artifact_fingerprint` 的網頁確認機制：那套機制綁定
桌面工作台前端，站⑩現在還沒有前端。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import validate_sbp as V  # noqa: E402

REVIEW_FILENAME = "improvement_goal_review.yaml"


def read_original_goal(run_dir: Path) -> str:
    """讀 `case.yaml` 的原始 `improvement_goal`，逐字（去頭尾空白、內部空白
    正規化）。`case.yaml` 不存在或讀不出來時回傳空字串——`--show` 仍然要能
    告訴研究者「研究前根本沒填」，這本身就是有意義的資訊，不是錯誤。
    """
    case_path = run_dir / "case.yaml"
    if not case_path.exists():
        return ""
    try:
        case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return ""
    if not isinstance(case, dict):
        return ""
    return " ".join(str(case.get("improvement_goal") or "").split())


def read_hmw_statements(run_dir: Path) -> list[str]:
    """讀 `hmw.yaml` 的全部 `entries[].statement`，不篩選、不排序——決定三
    「事實層永遠用完整證據」的精神同樣適用於這裡要攤開給研究者比對的陳述：
    漏掉任何一條都會讓比對本身不完整。`hmw.yaml` 不存在或讀不出來時回傳
    空清單。
    """
    hmw_path = run_dir / "hmw.yaml"
    if not hmw_path.exists():
        return []
    try:
        doc = yaml.safe_load(hmw_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(doc, dict):
        return []
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return []
    return [
        str(entry.get("statement") or "")
        for entry in entries
        if isinstance(entry, dict) and entry.get("statement")
    ]


def confirmation_errors(doc: dict) -> list[str]:
    """驗證一份候選 `improvement_goal_review.yaml` 文件——結構檢查交給既有
    的 `V.structural_errors`（schema 的 `operative_goal.minLength: 1` 已經
    擋掉空字串），這裡只補 schema 表達不了的事：`operative_goal` 去頭尾空白
    後仍然是空字串（只有空白字元）的情況，minLength 對這種輸入算「非空」，
    schema 擋不住。空清單代表通過驗證。
    """
    errors = V.structural_errors(doc, V.load_schema("improvement-goal-review"))
    if isinstance(doc, dict):
        operative_goal = doc.get("operative_goal")
        if isinstance(operative_goal, str) and operative_goal.strip() == "" and operative_goal != "":
            errors.append("[improvement_goal_review] operative_goal 不可只有空白字元")
    return errors


def read_operative_goal(run_dir: Path) -> str | None:
    """讀 `improvement_goal_review.yaml` 的 `operative_goal`——`as_is_research.py`
    的閘門實際呼叫的函式。檔案不存在、讀不出來、不是物件、或 `operative_goal`
    不是非空白字串時一律回傳 `None`，代表「還沒確認」，讓呼叫端統一用同一句
    `missing_improvement_goal` 拒絕，不分辨是哪一種缺失——研究者的補救方法
    都一樣：跑一次 `--confirm-file`。
    """
    review_path = run_dir / REVIEW_FILENAME
    if not review_path.exists():
        return None
    try:
        doc = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(doc, dict):
        return None
    operative_goal = doc.get("operative_goal")
    if not isinstance(operative_goal, str):
        return None
    normalized = " ".join(operative_goal.split())
    return normalized if normalized else None


def confirm(run_dir: Path, operative_goal: object, framing_changed_note: object = None) -> tuple[dict, list[str]]:
    """confirm-improvement-goal-save-only：唯一的改善目標寫入——CLI 的
    `--confirm-file` 與工作台⑦都呼叫這一支，同一個概念只有一條寫入路徑。

    組出 `improvement_goal_review.yaml`（研究前目標與 HMW 陳述是確認當下的
    逐字快照），驗證通過才寫檔並在 run.json 記 `improvement_goal_review_state`；
    不通過時不寫檔，把失敗記進 run.json。不呼叫模型、不動 case.yaml。
    回傳 (文件, 錯誤清單)。
    """
    from . import run_lock

    with run_lock.hold(run_dir):
        return _confirm(run_dir, operative_goal, framing_changed_note)


def _confirm(run_dir: Path, operative_goal: str, framing_changed_note) -> tuple[dict, list[str]]:
    from . import run_files

    run_json_path = run_dir / "run.json"
    run_state = run_files.read_state(run_json_path)
    case_id = run_dir.name
    case_path = run_dir / "case.yaml"
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        if isinstance(case, dict):
            case_id = case.get("case_id") or case_id
    doc = {
        "schema_version": 1,
        "case_id": case_id,
        "original_goal": read_original_goal(run_dir),
        "hmw_statements": read_hmw_statements(run_dir),
        "operative_goal": operative_goal,
    }
    if framing_changed_note is not None:
        doc["framing_changed_note"] = framing_changed_note
    errors = confirmation_errors(doc)
    if errors:
        run_state["improvement_goal_review_state"] = "failed"
        run_state["improvement_goal_review_failure"] = "; ".join(errors)
    else:
        path = run_dir / REVIEW_FILENAME
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(path)
        run_state["improvement_goal_review_state"] = "complete"
        run_state.pop("improvement_goal_review_failure", None)
    run_files.write_json_atomic(run_json_path, run_state, trailing_newline=False)
    return doc, errors
