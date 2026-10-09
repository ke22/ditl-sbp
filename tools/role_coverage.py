"""role_coverage.py — 從證據池與案例邊界算角色覆蓋度，取代手寫的 data_profile.yaml。

以終為始分析（見 HANDOFF 2026-08-20 第十一次 session）發現：`metrics_chongzheng.md` 的 GT
比對顯示，AI 產出最大的兩個失分項都不是生成品質問題，其中一個是角色覆蓋度——「這格的動作
是客服行政做的，但證據池裡沒有任何一條客服行政的第一手說法」，這句話目前唯一的來源是研究
團隊手寫的 `data_profile.yaml`。這支工具把其中「案例邊界已經明確列出的角色」那部分機械化。

## 這支工具算得出來、算不出來的事

算得出來：
- `roles_covered`：`evidence.yaml` 裡每個角色有幾條證據、來自哪些原始檔案。完全機械，
  不需要模型判斷。
- `roles_checked_absent`：`case.yaml` 的 `customers` 與 `external_partners[].role`
  是研究團隊已經明確列出「這個服務理論上牽涉哪些角色」的結構化清單。拿它跟 `roles_covered`
  比對，缺的就是缺口；同時對每個缺角色做一次證據全文的字串比對，看看有沒有被其他人提到過。

算不出來（老實列出，不假裝算得到）：
- **內部角色的缺口**（例如客服行政、系統紀錄）。`case.yaml` 目前只結構化「顧客」跟「外部
  夥伴」，服務提供者內部有哪些角色分工，沒有結構化清單可以比對——這部分仍要研究團隊人工
  盤點，或等 action-graph 架構的 `actor` 欄位補上結構化角色標記（那是另一條並行工作線在做
  的事，這支工具刻意不依賴它，才能現在就用）。
- **同一角色在兩份檔案裡用不同稱呼，而且這件事比想像中常發生、後果比想像中嚴重。**
  `case.yaml` 的 `customers` 是自然語言描述（例如「Honeywell 空氣清淨機使用者」），
  `evidence.yaml` 的 `role` 是訪談角色標籤（例如「顧客走查受訪者」）——兩者常常是同一群人，
  字面完全對不上。實測過真的會發生**假陰性**：恆隆行案例裡「顧客走查受訪者」有 10 條證據，
  是這個案例證據最多的角色，但因為 `case.yaml` 寫的是「Honeywell 空氣清淨機使用者」，字串
  比對找不到，工具會報「完全沒被提及」——這句話是錯的，顧客其實被訪得最多。
  **這不是可以靠改良比對演算法解決的小瑕疵。** 換過幾種字元重疊的比對方式都測過，「顧客走查
  受訪者」跟「Honeywell 空氣清淨機使用者」共用的字元幾乎是零，字面比對這條路本身就走不通，
  需要語意理解才能判斷「這兩個詞在講同一群人」——這正是這支工具刻意不做模型呼叫要付出的代價。
  所以下面的輸出**不對「有沒有覆蓋」下結論**，只列出兩份原始清單，比對交給看報告的人自己做。


用法：

    python3 tools/role_coverage.py \\
        --evidence cases/長庚富康巴士/evidence_chongzheng_verbatim.yaml \\
        --case cases/長庚富康巴士/case.yaml

    # 順便看這次生成的 claims 有沒有真的引用到某角色的證據（不是只是「有沒有這條證據」，
    # 是「這條證據有沒有被用在最後的圖上」）
    python3 tools/role_coverage.py --evidence ... --case ... --draft runs/xxx/sbp_draft.yaml

    # 機器可讀輸出（給前端或其他工具接線用）
    python3 tools/role_coverage.py --evidence ... --case ... --json
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import yaml


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def covered_roles(evidence: list[dict]) -> dict[str, dict]:
    """每個角色有幾條證據、來自哪些原始檔案。完全從 evidence.yaml 算，不依賴任何手寫檔案。"""
    by_role: dict[str, list[dict]] = defaultdict(list)
    for e in evidence:
        role = e.get("role")
        if role:
            by_role[role].append(e)
    return {
        role: {
            "n_evidence": len(items),
            "sources": sorted({i.get("source", "") for i in items if i.get("source")}),
            "evidence_ids": sorted(i["id"] for i in items if i.get("id")),
        }
        for role, items in by_role.items()
    }


def find_mentions(role: str, evidence: list[dict], context: int = 20) -> list[dict]:
    """在別人的證據原文裡找這個角色名稱的字串比對命中。

    只搜尋 text 欄位（受訪者原話），不搜尋 note（那是研究團隊自己的分析註解，
    不是證據本身——跟 stages.py 的 EVAL_ONLY_FIELDS 同一個理由，不能把答案先告訴自己）。
    """
    hits = []
    for e in evidence:
        text = e.get("text", "")
        idx = text.find(role)
        if idx < 0:
            continue
        start = max(0, idx - context)
        end = min(len(text), idx + len(role) + context)
        snippet = text[start:end]
        hits.append({
            "evidence_id": e.get("id"),
            "speaker_role": e.get("role"),
            "snippet": ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else ""),
        })
    return hits


def boundary_role_check(case: dict, evidence: list[dict]) -> list[dict]:
    """拿 case.yaml 結構化列出的角色（customers／external_partners）跟 roles_covered 比對。

    **這個函式只做字串是否相等，不判斷「這兩個詞是不是在講同一群人」。** `exact_match=False`
    只代表「roles_covered 裡沒有一模一樣的字串」，不代表這個角色真的沒有證據——`customers`
    常常是自然語言描述，跟 `evidence.yaml` 的訪談角色標籤字面對不上是常態，不是例外
    （見檔頭「顧客走查受訪者」vs「Honeywell 空氣清淨機使用者」的真實案例）。回傳結果一律
    要連同完整的 `roles_covered` 清單一起看，由人自己判斷有沒有同一群人換了稱呼——這支工具
    不下「缺」或「不缺」的結論。
    """
    covered = covered_roles(evidence)
    boundary: list[tuple[str, str]] = []
    for c in case.get("customers") or []:
        boundary.append((c, "customer"))
    for p in case.get("external_partners") or []:
        role = p.get("role") if isinstance(p, dict) else None
        if role:
            treatment = p.get("treatment", "")
            boundary.append((role, f"external_partner（{treatment}）" if treatment else "external_partner"))

    out = []
    for role, why in boundary:
        if role in covered:
            out.append({"role": role, "why": why, "exact_match": True, **covered[role]})
            continue
        mentions = find_mentions(role, evidence)
        out.append({
            "role": role,
            "why": why,
            "exact_match": False,
            "exact_string_mentioned_elsewhere": bool(mentions),
            "mentions": mentions,
        })
    return out


def citation_coverage(evidence: list[dict], draft: dict) -> dict[str, dict]:
    """這次生成的圖裡，每個角色的證據有幾條真的被引用、幾條晾在旁邊沒用到。

    「有證據」不等於「證據被用上」——這個數字回答的是後者，是 roles_covered 之外
    的另一個問題。
    """
    role_by_id = {e["id"]: e.get("role") for e in evidence if e.get("id")}
    cited_ids: set[str] = set()
    for cell in draft.get("cells") or []:
        for claim in cell.get("claims") or []:
            ref = claim.get("evidence_ref")
            if ref:
                cited_ids.add(ref)

    covered = covered_roles(evidence)
    out = {}
    for role, info in covered.items():
        role_ids = set(info["evidence_ids"])
        cited = role_ids & cited_ids
        out[role] = {
            "n_evidence": len(role_ids),
            "n_cited": len(cited),
            "cited_evidence_ids": sorted(cited),
            "uncited_evidence_ids": sorted(role_ids - cited),
        }
    return out


def _print_report(covered, boundary, citation):
    print("== 角色覆蓋度：兩份原始清單，比對交給你自己做 ==\n")
    print("有拿到（來自 evidence.yaml，逐條算出來的，跟下面 case.yaml 的角色清單沒有互相比對過）")
    for role, info in sorted(covered.items(), key=lambda kv: -kv[1]["n_evidence"]):
        print(f"  {role}：{info['n_evidence']} 條 · {'、'.join(info['sources'])}")

    no_match = [b for b in boundary if not b["exact_match"]]
    if no_match:
        print("\ncase.yaml 列出、但找不到一模一樣角色名稱的（不代表真的沒訪到——"
              "請對照上面「有拿到」清單，自己判斷會不會是同一群人換了稱呼）")
        for b in no_match:
            tag = "同一個字串在別人的證據原文裡出現過（不保證是同一個意思）" \
                if b["exact_string_mentioned_elsewhere"] else "這個字串沒在任何證據原文裡出現過"
            print(f"  {b['role']}（{b['why']}）：{tag}")
            for m in b["mentions"][:3]:
                print(f"      {m['evidence_id']}（{m['speaker_role']} 說）：{m['snippet']}")
            if len(b["mentions"]) > 3:
                print(f"      ⋯還有 {len(b['mentions']) - 3} 條")

    matched = [b for b in boundary if b["exact_match"]]
    if matched:
        print("\ncase.yaml 列出、字面上找到一模一樣角色名稱的")
        for b in matched:
            print(f"  {b['role']}（{b['why']}）：{b['n_evidence']} 條")

    checked_roles = {b["role"] for b in boundary}
    print(f"\n（這次只檢查了 case.yaml 明確列出的 {len(checked_roles)} 個角色："
          f"{'、'.join(sorted(checked_roles))}——內部角色沒有結構化清單可比對，這支工具查不到，"
          f"見檔頭說明。字面找不到不等於真的沒有，務必人工核對「有拿到」那份清單。）")

    if citation:
        print("\n== 這次生成，每個角色的證據有沒有真的被用上 ==\n")
        for role, info in sorted(citation.items(), key=lambda kv: -kv[1]["n_evidence"]):
            print(f"  {role}：{info['n_cited']}/{info['n_evidence']} 條被引用")
            if info["uncited_evidence_ids"]:
                print(f"      沒用到：{'、'.join(info['uncited_evidence_ids'])}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--evidence", required=True, type=Path)
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--draft", type=Path, help="選填：算這次生成有沒有真的引用到每個角色的證據")
    p.add_argument("--json", action="store_true", help="輸出機器可讀 JSON，不印報告")
    args = p.parse_args()

    evidence = _load(args.evidence) or []
    case = _load(args.case) or {}

    covered = covered_roles(evidence)
    boundary = boundary_role_check(case, evidence)
    citation = citation_coverage(evidence, _load(args.draft)) if args.draft else None

    if args.json:
        print(json.dumps(
            {"roles_covered": covered, "roles_boundary_check": boundary, "citation_coverage": citation},
            ensure_ascii=False, indent=2,
        ))
    else:
        _print_report(covered, boundary, citation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
