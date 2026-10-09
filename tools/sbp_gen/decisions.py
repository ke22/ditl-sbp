"""案子層級的判斷軌跡索引——`decisions.yaml`。

跟 `stages.py` 不同：這裡的函式全部只讀既有檔案、不呼叫模型、不產生新內容，
只是把六個候選機制（核心類別／Persona維度／主要Job／主要HMW／目標族群軸／
主要目標）分散在各個 `runs/` 目錄裡的選擇結果，掃描、比對、整理成一份案子
層級的索引。見 TARGET-ARCHITECTURE.md 決定十一；目標族群軸是
add-segmentation-axis-designation 補上的第五個框架點，主要目標是
add-target-segmentation-designation 補上、取代原本核心目標對象的第六個
框架點（quadrant-members-are-designer-assigned 移除了核心目標對象本身的
機制與畫面，框架點回到六個）。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import yaml

from . import gates

# build_decisions 呼叫 scan_decision 的六組固定參數，順序即輸出的六個框架點鍵名。
# 見 design.md「Implementation Contract」的參數表。
_DECISION_POINTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("core_category", "selective_coding_state", "selective_coding.yaml",
     "core_category_candidates_state", "core_category_candidates.yaml"),
    # rebuild-stage-two-as-cards-modal-and-pair-picker：②不再提供「挑一個核心
    # 類別」，它提供的決定是挑一組敘事與顧客任務的配對，所以②那一站的已決定
    # 狀態改讀這一筆。上面的 core_category 那筆原樣留著、照舊掃它一直在掃的
    # 檔案——把它改指向配對檔會留下一個叫 core_category 卻在回答敘事問題的
    # 鍵，那正是這個專案清理過一次的那種不實宣告。代價是索引暫時比導覽多一
    # 個框架點，由併站那支結清。
    ("narrative_job", "narrative_job_selection_state", "narrative_job_selection.yaml",
     "narrative_job_candidates_state", "narrative_job_candidates.yaml"),
    ("persona_dimensions", "persona_dimensions_selection_state", "persona_dimensions_selection.yaml",
     "persona_dimension_candidates_state", "persona_dimension_candidates.yaml"),
    ("primary_job", "primary_job_selection_state", "primary_job_selection.yaml",
     "primary_job_candidates_state", "primary_job_candidates.yaml"),
    ("primary_hmw", "primary_hmw_selection_state", "primary_hmw_selection.yaml",
     "primary_hmw_candidates_state", "primary_hmw_candidates.yaml"),
    ("segmentation_axes", "segmentation_axis_selection_state", "segmentation_axis_selection.yaml",
     "segmentation_axis_candidates_state", "segmentation_axis_candidates.yaml"),
    ("primary_target", "target_segmentation_selection_state", "target_segmentation_selection.yaml",
     "target_segmentation_candidates_state", "target_segmentation_candidates.yaml"),
)


def find_runs_for_case(case_id: str, runs_root: Path) -> list[Path]:
    """回傳 `runs_root` 底下 `case_id` 相符的 run 目錄，依 `case.yaml` 的
    mtime 由新到舊排序（不是目錄名稱——2026-09-08 修正：舊版依目錄名稱字串
    排序，「由新到舊」只有在每個 run 目錄名稱都以 ISO 日期開頭時才成立；
    瀏覽器建立的 run 是 `<CaseLabel>-<date>-<epoch-ms>`，CJK 案名也一律排在
    ASCII 數字前面，兩者都會讓比實際更舊的 run 排到最前面，
    `scan_decision` 因此回報一個已經被取代的舊選擇。`case.yaml` 的 mtime
    跟 `run_summary()` 顯示給研究者看的「已決定（來自 X 的分析結果）」用的
    是同一個訊號來源，兩邊不會對不上）。

    任何一個子目錄的 `case.yaml` 讀不到、不是合法 YAML、或 `case_id` 對不上，
    都靜默排除——多數 `runs/` 子目錄本來就屬於別的案子，這不是錯誤。
    """
    if not runs_root.is_dir():
        return []

    matches: list[tuple[float, str, Path]] = []
    for entry in runs_root.iterdir():
        if not entry.is_dir():
            continue
        case_path = entry / "case.yaml"
        if not case_path.is_file():
            continue
        try:
            case_data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(case_data, dict):
            continue
        if case_data.get("case_id") != case_id:
            continue
        try:
            mtime = case_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        # 目錄名稱只在 mtime 打平手時當決定性排序鍵（tiebreak），不是主要依據。
        matches.append((mtime, entry.name, entry))

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _mtime, _name, entry in matches]


def scan_decision(
    case_id: str,
    state_key: str,
    selection_filename: str,
    candidates_state_key: str,
    candidates_filename: str,
    ordered_runs: list[Path],
    *,
    gate_name: str | None = None,
) -> dict:
    """在（新到舊排序的）`ordered_runs` 裡找第一個完成這個框架點選擇的 run。

    `case_id` 只是為了跟 `find_runs_for_case` 介面對稱而保留的參數，這裡不使用——
    `ordered_runs` 已經是篩過 `case_id` 的結果。

    對沒有 `gate_name` 的四個框架點,「完成」的權威判斷維持原樣:run.json 的
    `state_key == "complete"`,*而且* 對應的選擇檔真的存在於磁碟上。

    `gate_name` 給的是 `tools/sbp_gen/gates.py` DEPENDENCY_MAP 裡的關卡名稱
    (`segmentation_axes`、`target_segmentation`)——rebuild-to-next-human-gate
    改成讀 ledger 的關卡狀態(`resolve_gate_state(...) == "confirmed"`),沒有
    `.gates/` 目錄的舊 run 會在第一次讀取時自動從既有選擇檔合成 legacy import,
    效果等同原本「state 完成且檔案存在」的判斷,但不再依賴一個可能跟磁碟檔案
    對不上的狀態旗標。兩者缺一都視為這個 run 沒有回答這個框架點,改看下一個
    (較舊的)run。找不到任何一個滿足的 run 就回傳未決定。
    """
    del case_id  # 介面對稱用，見上方 docstring
    now = dt.datetime.now().isoformat(timespec="seconds")
    for run_dir in ordered_runs:
        run_json_path = run_dir / "run.json"
        run_state: dict = {}
        if run_json_path.is_file():
            try:
                loaded = json.loads(run_json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                loaded = None
            if isinstance(loaded, dict):
                run_state = loaded
            elif gate_name is None:
                # 沒有 gate_name 的四個框架點仍然依賴 run_state 判斷完成，
                # run.json 讀不到或形狀不對就當這個 run 沒回答，跟改動前一致。
                continue
        elif gate_name is None:
            continue

        selection_path = run_dir / selection_filename
        if not selection_path.is_file():
            continue
        if gate_name is not None:
            if gates.resolve_gate_state(run_dir, gate_name, now=now) != "confirmed":
                continue
        elif run_state.get(state_key) != "complete":
            continue

        result = {
            "status": "decided",
            "run": run_dir.name,
            "selection_file": f"{run_dir.name}/{selection_filename}",
        }

        candidates_path = run_dir / candidates_filename
        if run_state.get(candidates_state_key) == "complete" and candidates_path.is_file():
            result["candidates_file"] = f"{run_dir.name}/{candidates_filename}"

        return result

    return {"status": "undecided"}


def build_decisions(case_dir: Path, runs_root: Path) -> dict:
    """組出案子層級的 `decisions.yaml` 內容——不寫檔，只回傳資料。"""
    case_path = case_dir / "case.yaml"
    try:
        case_data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        raise ValueError(f"讀取或解析 {case_path} 失敗：{exc}") from exc

    if not isinstance(case_data, dict) or not case_data.get("case_id"):
        raise ValueError(f"{case_path} 沒有有效的 case_id 欄位。")

    case_id = case_data["case_id"]
    ordered_runs = find_runs_for_case(case_id, runs_root)

    # rebuild-to-next-human-gate: these two points' "decided" question is
    # answered by gates.py's ledger (see scan_decision's gate_name param),
    # not the run.json state flag every other point still uses.
    gate_name_by_point = {"segmentation_axes": "segmentation_axes", "primary_target": "target_segmentation"}

    decisions = {
        point: scan_decision(
            case_id, state_key, selection_filename, candidates_state_key, candidates_filename, ordered_runs,
            gate_name=gate_name_by_point.get(point),
        )
        for point, state_key, selection_filename, candidates_state_key, candidates_filename in _DECISION_POINTS
    }

    return {
        "schema_version": 1,
        "case_id": case_id,
        "decisions": decisions,
    }
