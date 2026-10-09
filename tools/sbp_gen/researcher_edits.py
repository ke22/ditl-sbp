"""Record a researcher's structural judgement into an As-is graph.

The researcher adjudicates structure — whether a card sits in the right step
and lane, whether a line connects the right two things. Verifying that a claim
matches its cited transcript is not that work; it belongs to the pipeline.
So nothing here asks for a reason, a supporting excerpt, or a ruling on
whether existing evidence still applies.

The shape is not invented here. `schema/service-action-graph.schema.json`
already defines `source: researcher`, already forbids it from carrying an
`evidence_ref`, and already requires it to be marked `inferred: true`;
`validate_sbp` already enforces all three. What has never existed is any
path that produces one.
"""
from __future__ import annotations

import copy
from typing import Any


class JudgementRejected(ValueError):
    """The judgement cannot be recorded. Carries a researcher-facing message."""


# `service_role` 與 `layer`／`actor_boundary`／`visibility` 是一一對應的，規則寫在
# `prompts/structure.md` 裡給模型看。研究者拖一張卡片到別的泳道 = 四個欄位同時變，
# 但畫面上絕對不能問他這四個欄位——那是 schema 內部詞彙，專案規則明令不得上畫面。
# 他只看到泳道的中文名，對應在這裡發生。
ROLE_BY_LANE = {
    "顧客行動": "customer",
    "前台行動": "frontstage_delivery",
    "後台行動": "backstage_delivery",
    "支援流程": "internal_support",
}

FIELDS_BY_ROLE = {
    "customer": {"actor_boundary": "customer", "visibility": "customer_visible"},
    "frontstage_delivery": {"actor_boundary": "provider", "visibility": "customer_visible"},
    "backstage_delivery": {"actor_boundary": "provider", "visibility": "customer_invisible"},
    "internal_support": {"actor_boundary": "included_support", "visibility": "customer_invisible"},
    "external_dependency": {"actor_boundary": "external_dependency", "visibility": "customer_invisible"},
    "out_of_scope": {"actor_boundary": "out_of_scope", "visibility": "not_applicable"},
}

# `實體證據` 是 artifact node 專用的泳道，而 artifact node 依 schema 不得帶
# actor_boundary／visibility。研究者不能把一個動作拖進去，也不能把物件拖出來。
ARTIFACT_LANE = "實體證據"


def _node(graph: dict, node_id: str) -> dict:
    for node in graph.get("nodes") or []:
        if node.get("id") == node_id:
            return node
    raise JudgementRejected(f"這張圖上沒有這一格（{node_id}）")


def _researcher_claim(text: str) -> dict:
    """A claim in the shape the schema demands of a researcher judgement."""
    return {
        "text": text,
        "source": "researcher",
        "inferred": True,          # 研究者的判斷不是觀察到的事實
        "scope": "individual_case",
        # 刻意沒有 evidence_ref：schema 明令 researcher claim 不得帶它
    }


def move_node(graph: dict, node_id: str, *, step: int | None = None,
              layer: str | None = None) -> dict:
    """Move a card to another journey step, another lane, or both.

    Position in a service blueprint is not decoration: the column is the
    journey step and the row is the lane, so moving a card is always a claim
    about the service. It is recorded as one.
    """
    if step is None and layer is None:
        raise JudgementRejected("沒有指定要搬到哪裡")
    result = copy.deepcopy(graph)
    node = _node(result, node_id)

    moved = []
    if step is not None:
        if not isinstance(step, int) or step < 1:
            raise JudgementRejected("步驟必須是從 1 開始的整數")
        node["step"] = step
        moved.append(f"步驟 {step}")

    if layer is not None:
        if layer == ARTIFACT_LANE or node.get("kind") == "artifact":
            raise JudgementRejected("實體證據不能跟服務動作互相搬移")
        role = ROLE_BY_LANE.get(layer)
        if role is None:
            raise JudgementRejected(f"沒有這一條泳道（{layer}）")
        node["layer"] = layer
        node["service_role"] = role
        node.update(FIELDS_BY_ROLE[role])
        moved.append(layer)

    node.setdefault("claims", []).append(
        _researcher_claim(f"研究者把這個動作放在{'、'.join(moved)}。"))
    return result


def _relation(graph: dict, relation_id: str) -> dict:
    for relation in graph.get("relations") or []:
        if relation.get("id") == relation_id:
            return relation
    raise JudgementRejected(f"這張圖上沒有這條線（{relation_id}）")


def _next_relation_id(graph: dict) -> str:
    taken = {r.get("id") for r in graph.get("relations") or []}
    n = 1
    while f"R-r{n}" in taken:
        n += 1
    return f"R-r{n}"


def add_relation(graph: dict, from_id: str, to_id: str, *, relation_type: str = "sequence") -> dict:
    """Draw a relation the researcher asserts.

    Nobody said this in an interview, so it carries no evidence reference and
    `source: researcher` — which is exactly what the interface must state
    before the researcher commits it.
    """
    if from_id == to_id:
        raise JudgementRejected("一條線不能連到自己")
    result = copy.deepcopy(graph)
    _node(result, from_id)
    _node(result, to_id)
    for existing in result.get("relations") or []:
        if existing.get("from") == from_id and existing.get("to") == to_id:
            raise JudgementRejected("這兩格之間已經有一條線了")
    result.setdefault("relations", []).append({
        "id": _next_relation_id(result),
        "type": relation_type,
        "source": "researcher",
        "from": from_id,
        "to": to_id,
        # 刻意沒有 evidence_refs：schema 只在 source 是 evidence 時要求它
    })
    return result


def remove_relation(graph: dict, relation_id: str) -> dict:
    result = copy.deepcopy(graph)
    _relation(result, relation_id)
    result["relations"] = [r for r in result["relations"] if r.get("id") != relation_id]
    return result


def redirect_relation(graph: dict, relation_id: str, *, from_id: str | None = None,
                      to_id: str | None = None) -> dict:
    """Point an existing relation at a different card.

    The line was drawn from evidence, but the researcher says it lands in the
    wrong place. Redirecting makes the endpoint their assertion, so the
    relation stops claiming transcript support it no longer has.
    """
    if from_id is None and to_id is None:
        raise JudgementRejected("沒有指定要改連到哪裡")
    result = copy.deepcopy(graph)
    relation = _relation(result, relation_id)
    if "endpoints" in relation:
        raise JudgementRejected("這條線沒有方向，不能改端點")
    if from_id is not None:
        _node(result, from_id)
        relation["from"] = from_id
    if to_id is not None:
        _node(result, to_id)
        relation["to"] = to_id
    if relation.get("from") == relation.get("to"):
        raise JudgementRejected("一條線不能連到自己")
    relation["source"] = "researcher"
    relation.pop("evidence_refs", None)
    return result
