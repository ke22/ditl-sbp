"""Issue funnel audit (purpose-aware-research-calibration, task 4.2).

For researcher issue keywords (e.g. 恆隆行「永續」「回收」, MUJI「品牌」「無印」)
count and list the hits in five layers of one run:

1. open codes -- name and description;
2. axial categories -- category name, label and discriminating basis;
3. selective themes -- name, core sentence, insight and key insights;
4. HMW -- statement;
5. Persona axes -- label and both poles (the selected pair is marked).

The evidence pool is counted as a reference row so a reader can see whether a
keyword was in the transcripts at all before it thins out. Read-only; no model.

Usage: python3 -m sbp_gen.audits.issue_funnel_audit --run DIR --keywords 永續,回收 --case 恆隆行 [--run ... --keywords ... --case ...] --out FILE.md
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from sbp_gen.audits import write_report

LAYERS = ("open", "axial", "selective", "hmw", "axes")
LAYER_TEXT = {"evidence": "（參考）證據池", "open": "開放編碼", "axial": "主軸類別", "selective": "選擇主題",
              "hmw": "HMW 框法", "axes": "Persona 分群軸"}


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def _join(*parts) -> str:
    out = []
    for part in parts:
        if isinstance(part, list):
            out.extend(str(x.get("text") if isinstance(x, dict) else x) for x in part)
        elif part:
            out.append(str(part))
    return "／".join(out)


def layer_items(run_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Each layer as (label, searchable text); a missing file is an empty layer marked None."""
    run_dir = Path(run_dir)
    items: dict[str, list[tuple[str, str]] | None] = {}
    evidence = _load(run_dir / "evidence.yaml")
    items["evidence"] = None if evidence is None else [(e.get("id", ""), str(e.get("text") or "")) for e in evidence if isinstance(e, dict)]
    open_doc = _load(run_dir / "open_coding.yaml")
    items["open"] = None if open_doc is None else [
        (c.get("name", ""), _join(c.get("name"), c.get("description"))) for c in open_doc.get("concepts") or [] if isinstance(c, dict)]
    axial = _load(run_dir / "axial_coding.yaml")
    items["axial"] = None if axial is None else [
        (c.get("category_name") or c.get("label") or c.get("id", ""),
         _join(c.get("category_name"), c.get("label"), c.get("discriminating_basis")))
        for c in axial.get("categories") or [] if isinstance(c, dict)]
    selective = _load(run_dir / "selective_themes.yaml")
    items["selective"] = None if selective is None else [
        (t.get("name", ""), _join(t.get("name"), t.get("core_sentence"), t.get("insight"), t.get("key_insights")))
        for t in selective.get("themes") or [] if isinstance(t, dict)]
    hmw = _load(run_dir / "hmw.yaml")
    items["hmw"] = None if hmw is None else [
        (h.get("statement", ""), str(h.get("statement") or "")) for h in hmw.get("entries") or [] if isinstance(h, dict)]
    matrix = _load(run_dir / "persona_matrix.yaml")
    selection = _load(run_dir / "segmentation_axis_selection.yaml") or {}
    chosen = set(selection.get("axis_ids") or [])
    items["axes"] = None if matrix is None else [
        (f"{a.get('label', '')}{'（已選）' if a.get('id') in chosen else ''}",
         _join(a.get("label"), a.get("pole_low"), a.get("pole_high")))
        for a in matrix.get("axes") or [] if isinstance(a, dict)]
    return items


def audit_run(run_dir: Path, keywords: list[str], case: str = "") -> dict:
    items = layer_items(run_dir)
    hits = {}
    for layer, entries in items.items():
        if entries is None:
            hits[layer] = None
            continue
        hits[layer] = {kw: [label for label, text in entries if kw in text] for kw in keywords}
    return {"run": Path(run_dir).name, "case": case, "keywords": keywords, "hits": hits,
            "sizes": {layer: (None if entries is None else len(entries)) for layer, entries in items.items()}}


def missing_layers(result: dict, keyword: str) -> list[str]:
    """The layers (of those present in the run) where the keyword does not appear."""
    return [layer for layer in LAYERS if result["hits"][layer] is not None and not result["hits"][layer][keyword]]


def render(results: list[dict], title: str, note: str = "") -> str:
    lines = [f"# {title}", "",
             "purpose-aware-research-calibration 任務 4.2。唯讀審計，沒有呼叫模型，也沒有寫入任何 run。", ""]
    if note:
        lines += [note, ""]
    lines += [
             "每一層數「文字裡出現關鍵詞的項目」有幾個，並列出命中的項目。證據池只是參考：看關鍵詞在訪談裡有沒有被提到，再看它在哪一層開始變少或消失。",
             "命中是字面比對，同義說法（例如「環保」之於「永續」）不會算進來。", ""]
    for r in results:
        lines += [f"## {r['case'] or r['run']}", "", f"run：`{r['run']}`", "",
                  "| 層 | 項目數 | " + " | ".join(r["keywords"]) + " |",
                  "|---|---:|" + "---:|" * len(r["keywords"])]
        for layer in ("evidence",) + LAYERS:
            size = r["sizes"][layer]
            if r["hits"][layer] is None:
                lines.append(f"| {LAYER_TEXT[layer]} | 沒有這一層 | " + " | ".join("—" for _ in r["keywords"]) + " |")
                continue
            lines.append(f"| {LAYER_TEXT[layer]} | {size} | " + " | ".join(str(len(r["hits"][layer][kw])) for kw in r["keywords"]) + " |")
        lines.append("")
        for kw in r["keywords"]:
            gone = "、".join(LAYER_TEXT[layer] for layer in missing_layers(r, kw))
            lines.append(f"- 「{kw}」：{f'沒有出現在：{gone}' if gone else '每一層都有'}")
        for layer in LAYERS:
            if not r["hits"][layer]:
                continue
            for kw in r["keywords"]:
                found = r["hits"][layer][kw]
                if found:
                    lines += ["", f"**{LAYER_TEXT[layer]}・{kw}**", ""] + [f"- {label}" for label in found]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="issue_funnel_audit")
    p.add_argument("--run", action="append", type=Path, required=True)
    p.add_argument("--keywords", action="append", required=True, help="comma-separated, one per --run")
    p.add_argument("--case", action="append", default=[])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--title", default="研究者議題漏斗審計")
    p.add_argument("--note", default="", help="why these runs were chosen")
    args = p.parse_args(argv)
    if len(args.keywords) != len(args.run):
        p.error("each --run needs its own --keywords")
    cases = args.case + [""] * (len(args.run) - len(args.case))
    results = [audit_run(run, [k.strip() for k in kws.split(",") if k.strip()], case)
               for run, kws, case in zip(args.run, args.keywords, cases)]
    write_report(args.out, render(results, args.title, args.note))
    for r in results:
        row = "、".join(f"{LAYER_TEXT[layer]} {sum(len(v) for v in r['hits'][layer].values()) if r['hits'][layer] else '—'}" for layer in LAYERS)
        print(f"{r['case'] or r['run']}：{row}")
    print(f"報告：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
