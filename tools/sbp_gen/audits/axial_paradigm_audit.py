"""Axial paradigm audit (purpose-aware-research-calibration, task 4.1).

For every category of a run's axial coding, label every paradigm-model item
(conditions, actions, consequences):

- visible_in_concepts: at least half of the item's character bigrams appear in
  the names and descriptions of the category's open codes;
- added_by_model: fewer than half do -- the item says something the concepts
  the model was given do not;
- empty: the list has no item (counted once per list).

A category without `concept_refs` (older method) is compared with the open
codes that share at least one evidence id with it; the report says so.

Character bigrams stand in for content words because Chinese has no spaces;
the rule is deliberately simple and is stated in the report so a reader can
judge each label. Read-only: nothing under the run directory is written.

Usage: python3 -m sbp_gen.audits.axial_paradigm_audit --run DIR [--run DIR ...] --out FILE.md
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

from sbp_gen.audits import write_report

FIELDS = (("conditions", "條件"), ("actions", "行動／互動"), ("consequences", "結果"))
THRESHOLD = 0.5
_CJK_OR_WORD = re.compile(r"[一-鿿A-Za-z0-9]")


def bigrams(text: str) -> set[str]:
    chars = [c for c in str(text or "") if _CJK_OR_WORD.match(c)]
    return {a + b for a, b in zip(chars, chars[1:])}


def item_text(item) -> str:
    return str(item.get("text") or "") if isinstance(item, dict) else str(item or "")


def label_item(text: str, concept_text: str) -> tuple[str, float]:
    grams = bigrams(text)
    if not grams:
        return "empty", 0.0
    pool = bigrams(concept_text)
    ratio = len(grams & pool) / len(grams)
    return ("visible_in_concepts" if ratio >= THRESHOLD else "added_by_model"), ratio


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def audit_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    axial = _load(run_dir / "axial_coding.yaml") or {}
    concepts = [c for c in (_load(run_dir / "open_coding.yaml") or {}).get("concepts") or [] if isinstance(c, dict)]
    by_id = {c.get("id"): c for c in concepts}
    rows, totals, fallback = [], {"visible_in_concepts": 0, "added_by_model": 0, "empty": 0}, False
    for category in axial.get("categories") or []:
        if not isinstance(category, dict):
            continue
        refs = category.get("concept_refs") or []
        if refs:
            members = [by_id[r] for r in refs if r in by_id]
        else:
            fallback = True
            evidence = set(category.get("evidence_refs") or [])
            members = [c for c in concepts if evidence & set(c.get("evidence_refs") or [])]
        pool = " ".join(f"{c.get('name') or ''} {c.get('description') or ''}" for c in members)
        for field, field_label in FIELDS:
            items = category.get(field) or []
            if not items:
                totals["empty"] += 1
                rows.append({"category": category.get("category_name") or category.get("label") or category.get("id"),
                             "field": field_label, "text": "", "label": "empty", "ratio": 0.0, "basis": ""})
                continue
            for item in items:
                label, ratio = label_item(item_text(item), pool)
                totals[label] += 1
                rows.append({"category": category.get("category_name") or category.get("label") or category.get("id"),
                             "field": field_label, "text": item_text(item), "label": label, "ratio": ratio,
                             "basis": item.get("basis", "") if isinstance(item, dict) else ""})
    return {"run": run_dir.name, "categories": len(axial.get("categories") or []), "concepts": len(concepts),
            "rows": rows, "totals": totals, "fallback": fallback}


LABEL_TEXT = {"visible_in_concepts": "概念裡看得到", "added_by_model": "補出來的", "empty": "空白"}


def render(results: list[dict], title: str) -> str:
    lines = [f"# {title}", "",
             "purpose-aware-research-calibration 任務 4.1。唯讀審計，沒有呼叫模型，也沒有寫入任何 run。", "",
             "## 判定規則", "",
             f"- **概念裡看得到**：這一格文字的雙字組（兩個相鄰的中文字或英數字）至少 {int(THRESHOLD * 100)}% 出現在這個類別所含開放編碼的名稱與描述裡。",
             "- **補出來的**：不到一半——這一格說了模型收到的概念裡沒有的東西。可能是有證據的綜合，也可能是補寫，要人看。",
             "- **空白**：這一組沒有任何一格，每組算一次。",
             "- 舊方法的類別沒有記下包含哪些開放編碼，改用「和類別共用至少一則證據的開放編碼」比對，表上會註明。",
             "- 中文沒有詞界，雙字組是「實詞」的替代。換句話說的綜合會拉低重疊，所以總覽另列重疊落在 25–49% 的格數，這些最可能只是改寫，不是新內容。",
             "- 這是粗篩，不是判決：數字告訴研究者先看哪幾格，每一格要不要算補寫，仍由研究者判斷。", ""]
    lines += ["## 總覽", "", "| run | 類別 | 開放編碼 | 概念裡看得到 | 補出來的 | 其中重疊 25–49% | 空白 | 比對方式 |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in results:
        t = r["totals"]
        near = sum(1 for row in r["rows"] if row["label"] == "added_by_model" and row["ratio"] >= 0.25)
        lines.append(f"| {r['run']} | {r['categories']} | {r['concepts']} | {t['visible_in_concepts']} | {t['added_by_model']} | {near} | {t['empty']} | "
                     f"{'共用證據的概念（舊方法）' if r['fallback'] else '類別記下的開放編碼'} |")
    for r in results:
        lines += ["", f"## {r['run']}", "", "| 類別 | 組 | 內容 | 判定 | 重疊 | 模型標的依據 |", "|---|---|---|---|---:|---|"]
        for row in r["rows"]:
            text = row["text"].replace("|", "／")
            lines.append(f"| {row['category']} | {row['field']} | {text} | {LABEL_TEXT[row['label']]} | "
                         f"{row['ratio']:.0%} | {row['basis'] or '—'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="axial_paradigm_audit")
    p.add_argument("--run", action="append", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--title", default="主軸典範模型審計")
    args = p.parse_args(argv)
    results = [audit_run(run) for run in args.run]
    write_report(args.out, render(results, args.title))
    for r in results:
        t = r["totals"]
        print(f"{r['run']}: 看得到 {t['visible_in_concepts']}、補出來 {t['added_by_model']}、空白 {t['empty']}")
    print(f"報告：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
