"""研究者對現況藍圖上 AI 推論的判斷（2026-09-29 研究者裁定）。

三種判斷都把推論留在圖上，不刪除：

- plausible（合理）：AI 推論、研究者接受；可以當目前現況建模的依據。
- needs_adjustment（需要調整）：待修正；不進下游。
- insufficient_evidence（證據不足）：仍標成 AI 推論；不算已確認的現況，
  也不當 To-be 的依據。

判斷存在 run 目錄自己的 `inference_reviews.yaml`，不寫進 `sbp_draft.yaml`
（那是呈現用的投影，不能拿來當下游的輸入）。沒有判斷的推論維持原本的行為；
舊的 `muted` 旗標只代表「看過、先不提醒」，不會被當成合理。
"""
from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path

import yaml

FILE = "inference_reviews.yaml"
DECISIONS = ("plausible", "needs_adjustment", "insufficient_evidence")
# 這兩種判斷的推論不給 To-be 當依據。
EXCLUDED_DOWNSTREAM = frozenset({"needs_adjustment", "insufficient_evidence"})
TARGETS = ("node", "relation")


class InferenceReviewRejected(ValueError):
    pass


def load(run_dir: Path) -> dict:
    """{"node": {id: review}, "relation": {id: review}}；讀不到就是空的。"""
    path = Path(run_dir) / FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {target: {k: v for k, v in (data.get(target) or {}).items()
                     if isinstance(k, str) and isinstance(v, dict) and v.get("decision") in DECISIONS}
            for target in TARGETS}


def decision(reviews: dict, target: str, item_id: str) -> str | None:
    return ((reviews.get(target) or {}).get(item_id) or {}).get("decision")


def record(run_dir: Path, target: object, item_id: object, value: object, note: object = None) -> dict:
    """寫入一筆判斷；value 是 None 就清掉這筆（回到尚未判斷）。"""
    if target not in TARGETS:
        raise InferenceReviewRejected("不認得要判斷的是哪一種項目。")
    if not isinstance(item_id, str) or not item_id:
        raise InferenceReviewRejected("沒有指定要判斷哪一項。")
    if value is not None and value not in DECISIONS:
        raise InferenceReviewRejected("不認得這個判斷。")
    reviews = load(run_dir)
    if value is None:
        reviews[target].pop(item_id, None)
    else:
        entry = {"decision": value, "decided_at": dt.datetime.now().isoformat(timespec="seconds")}
        if isinstance(note, str) and note.strip():
            entry["note"] = note.strip()
        reviews[target][item_id] = entry
    path = Path(run_dir) / FILE
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump({"schema_version": 1, **reviews}, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)
    return reviews


def _is_inference(claim: object) -> bool:
    return isinstance(claim, dict) and (claim.get("source") == "inference" or claim.get("inferred") is True)


def graph_for_downstream(graph: dict, reviews: dict) -> dict:
    """給 To-be 的現況圖：拿掉被判「需要調整」或「證據不足」的推論。

    關係：推論關係被這樣判就拿掉。節點：拿掉它的推論 claim；拿掉後一條 claim
    都不剩，節點與連到它的關係一起拿掉。有證據的內容一律不動。
    """
    out = copy.deepcopy(graph)
    dropped_nodes: set[str] = set()
    nodes = []
    for node in out.get("nodes") or []:
        if not isinstance(node, dict):
            nodes.append(node)
            continue
        if decision(reviews, "node", node.get("id", "")) in EXCLUDED_DOWNSTREAM:
            node["claims"] = [c for c in node.get("claims") or [] if not _is_inference(c)]
            if not node["claims"]:
                dropped_nodes.add(node.get("id"))
                continue
        nodes.append(node)
    out["nodes"] = nodes
    relations = []
    for relation in out.get("relations") or []:
        if not isinstance(relation, dict):
            relations.append(relation)
            continue
        ends = set(relation.get("endpoints") or [relation.get("from"), relation.get("to")])
        if ends & dropped_nodes:
            continue
        if relation.get("source") == "inference" and \
                decision(reviews, "relation", relation.get("id", "")) in EXCLUDED_DOWNSTREAM:
            continue
        relations.append(relation)
    out["relations"] = relations
    if isinstance(out.get("markers"), list):
        out["markers"] = [m for m in out["markers"] if not (isinstance(m, dict) and m.get("node_ref") in dropped_nodes)]
    return out
