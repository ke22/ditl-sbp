"""CJM coverage audit (purpose-aware-research-calibration, task 4.3).

Compares two journey files of one run -- by default the confirmed
`journey.yaml` and the candidate `journey_proposal.yaml` -- step by step on the
primary scenario: evidence count, distinct sources (sessions S1..Sn) and, where
speaker attribution is confirmed, the participants. Lists the steps that rest
on a single source and the steps with no counterpart in the other file -- a
counterpart shares at least a quarter of its evidence (Jaccard) or has a
similar title, because the same step is usually reworded. The target Persona's sources are shown as a reference, so a reader
can see whether a journey reaches beyond the people the Persona covers.

Read-only: journey files and the run are never written; no model is called.

Usage: python3 -m sbp_gen.audits.cjm_coverage_audit --run DIR [--a journey.yaml --b journey_proposal.yaml] --out FILE.md
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import yaml

from sbp_gen import source_attribution
from sbp_gen.audits import write_report

TITLE_MATCH = 0.4
EVIDENCE_MATCH = 0.25


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def _bigrams(text: str) -> set[str]:
    chars = [c for c in str(text or "") if not c.isspace()]
    return {a + b for a, b in zip(chars, chars[1:])}


def title_similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return 0.0 if not x or not y else 2 * len(x & y) / (len(x) + len(y))


def primary_steps(journey: dict) -> tuple[dict, list[dict]]:
    scenarios = [s for s in journey.get("scenarios") or [] if isinstance(s, dict)]
    primary = next((s for s in scenarios if s.get("id") == journey.get("primary_scenario")), scenarios[0] if scenarios else {})
    return primary, [s for s in primary.get("steps") or [] if isinstance(s, dict)]


def _step_refs(step: dict) -> list[str]:
    refs = list(step.get("evidence_refs") or [])
    for action in step.get("customer_actions") or []:
        if isinstance(action, dict):
            refs.extend(action.get("evidence_refs") or [])
    return list(dict.fromkeys(refs))


def coverage(steps: list[dict], evidence: list, attribution: dict | None) -> list[dict]:
    by_id = {e.get("id"): e for e in evidence if isinstance(e, dict)}
    codes = source_attribution.session_codes(evidence)
    rows = []
    for step in steps:
        refs = _step_refs(step)
        items = [by_id[r] for r in refs if r in by_id]
        sessions = sorted({codes[str(i.get("source"))] for i in items if i.get("source")}, key=lambda s: int(s[1:]))
        people = sorted({k for k in (source_attribution.person_key(i, attribution) for i in items) if k}) if attribution else []
        rows.append({"id": step.get("id", ""), "label": step.get("label", ""), "refs": len(refs), "ref_ids": set(refs),
                     "missing": len(refs) - len(items), "sessions": sessions, "people": people})
    return rows


def persona_sessions(run_dir: Path, evidence: list) -> list[str] | None:
    confirmed = _load(run_dir / "persona_confirmed.yaml")
    matrix = _load(run_dir / "persona_matrix.yaml")
    if not isinstance(confirmed, dict) or not isinstance(matrix, dict):
        return None
    members = {m for p in (confirmed.get("persona_content") or {}).get("personas") or [] for m in p.get("member_subject_ids") or []}
    refs = {r for s in matrix.get("subjects") or [] if s.get("id") in members for r in s.get("evidence_refs") or []}
    codes = source_attribution.session_codes(evidence)
    sources = {e.get("source") for e in evidence if isinstance(e, dict) and e.get("id") in refs}
    return sorted({codes[str(s)] for s in sources if s}, key=lambda s: int(s[1:]))


def session_share(rows: list[dict], evidence: list) -> dict[str, int]:
    """How many step citations fall on each session (a unit cited by two steps counts twice)."""
    by_id = {e.get("id"): e for e in evidence if isinstance(e, dict)}
    codes = source_attribution.session_codes(evidence)
    counts = Counter(codes[str(by_id[r].get("source"))] for row in rows for r in row["ref_ids"]
                     if r in by_id and by_id[r].get("source"))
    return dict(sorted(counts.items(), key=lambda kv: int(kv[0][1:])))


def share_text(share: dict[str, int]) -> str:
    total = sum(share.values())
    return "、".join(f"{k} {v * 100 // total}%" for k, v in share.items()) if total else "—"


def baseline(run_dir: Path, name: str = "journey.yaml") -> dict:
    """A journey from an earlier run, summarised by session share only -- its
    evidence ids are not comparable with another run's, so no step matching."""
    run_dir = Path(run_dir)
    evidence = _load(run_dir / "evidence.yaml") or []
    scenario, steps = primary_steps(_load(run_dir / name) or {})
    rows = coverage(steps, evidence, source_attribution.load(run_dir))
    return {"run": run_dir.name, "file": name, "scenario": scenario.get("label", ""), "steps": len(rows),
            "sources": source_attribution.session_codes(evidence), "share": session_share(rows, evidence),
            "persona_sessions": persona_sessions(run_dir, evidence)}


def evidence_overlap(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def unmatched_steps(a: list[dict], b: list[dict]) -> list[tuple[str, str, float, float]]:
    """Steps in `a` with no counterpart in `b`: no step there shares EVIDENCE_MATCH
    of the evidence and none has a title at TITLE_MATCH. Each comes with the
    step sharing most evidence, that overlap and the best title similarity."""
    out = []
    for row in a:
        overlap, closest = max(((evidence_overlap(row["ref_ids"], o["ref_ids"]), o["label"]) for o in b), default=(0.0, ""))
        title = max((title_similarity(row["label"], o["label"]) for o in b), default=0.0)
        if overlap < EVIDENCE_MATCH and title < TITLE_MATCH:
            out.append((row["label"], closest, overlap, title))
    return out


def audit(run_dir: Path, a_name: str = "journey.yaml", b_name: str = "journey_proposal.yaml") -> dict:
    run_dir = Path(run_dir)
    evidence = _load(run_dir / "evidence.yaml") or []
    attribution = source_attribution.load(run_dir)
    result = {"run": run_dir.name, "files": (a_name, b_name), "attribution": attribution is not None,
              "sources": source_attribution.session_codes(evidence), "persona_sessions": persona_sessions(run_dir, evidence)}
    for key, name in (("a", a_name), ("b", b_name)):
        doc = _load(run_dir / name) or {}
        scenario, steps = primary_steps(doc)
        rows = coverage(steps, evidence, attribution)
        result[key] = {"scenario": scenario.get("label", ""), "rows": rows,
                       "other_scenarios": [(s.get("label", ""), len(s.get("steps") or [])) for s in doc.get("scenarios") or []
                                           if isinstance(s, dict) and s is not scenario],
                       "basis": doc.get("generation_basis", ""), "share": session_share(rows, evidence),
                       "sessions": sorted({s for r in rows for s in r["sessions"]}, key=lambda s: int(s[1:]))}
    result["only_a"] = unmatched_steps(result["a"]["rows"], result["b"]["rows"])
    result["only_b"] = unmatched_steps(result["b"]["rows"], result["a"]["rows"])
    return result


def render(r: dict, title: str, baselines: list[dict] | None = None) -> str:
    a_name, b_name = r["files"]
    a, b = r["a"], r["b"]
    who = "確認過的受訪者" if r["attribution"] else "場次（來源檔，S1..Sn；這份 run 沒有確認講者歸屬，所以數場次不數人）"
    lines = [f"# {title}", "",
             "purpose-aware-research-calibration 任務 4.3。唯讀審計，沒有呼叫模型，沒有改動任何旅程檔。", "",
             f"run：`{r['run']}`。比較主要情境：`{a_name}`「{a['scenario']}」與 `{b_name}`「{b['scenario']}」"
             f"（{b_name} 的生成依據：`{b['basis'] or '未記'}`）。", "",
             f"每一步的證據包含步驟本身與顧客行動引用的證據。涵蓋單位是{who}。", "",
             "## 場次對照", ""]
    lines += [f"- {code}：{source}" for source, code in r["sources"].items()]
    lines += ["", "## 總覽", "", "| | 步數 | 涵蓋場次 | 證據數（每步最少–最多） | 場次數（每步最少–最多） | 只由一個場次撐起的步驟 | 引用落在各場次的比例 |",
              "|---|---:|---|---|---|---:|---|"]
    for name, side in ((a_name, a), (b_name, b)):
        rows = side["rows"]
        if rows:
            lines.append(f"| `{name}` | {len(rows)} | {'、'.join(side['sessions'])}（{len(side['sessions'])}/{len(r['sources'])}） | "
                         f"{min(x['refs'] for x in rows)}–{max(x['refs'] for x in rows)} | "
                         f"{min(len(x['sessions']) for x in rows)}–{max(len(x['sessions']) for x in rows)} | "
                         f"{sum(1 for x in rows if len(x['sessions']) == 1)} | {share_text(side['share'])} |")
    if r["persona_sessions"] is not None:
        lines.append(f"| 參考：目標 Persona 成員的來源 | — | {'、'.join(r['persona_sessions'])}（{len(r['persona_sessions'])}/{len(r['sources'])}） | — | — | — | — |")
    gained = sorted(set(b["sessions"]) - set(a["sessions"]), key=lambda s: int(s[1:]))
    lost = sorted(set(a["sessions"]) - set(b["sessions"]), key=lambda s: int(s[1:]))
    lines += ["", f"- `{b_name}` 比 `{a_name}` 多涵蓋的場次：{'、'.join(gained) or '沒有'}；少涵蓋的場次：{'、'.join(lost) or '沒有'}。"]
    if r["persona_sessions"] is not None:
        for name, side in ((a_name, a), (b_name, b)):
            beyond = [x for x in side["sessions"] if x not in r["persona_sessions"]]
            lines.append(f"- `{name}` 涵蓋的 {len(side['sessions'])} 個場次裡，有 {len(beyond)} 個不在目標 Persona 成員的來源裡"
                         f"{'：這份旅程不只用 Persona 成員的證據。' if beyond else '：這份旅程只用到 Persona 成員的證據。'}")
    for name, side in ((a_name, a), (b_name, b)):
        if side["other_scenarios"]:
            lines.append(f"- `{name}` 另有情境（不在本表比較）：" + "、".join(f"「{label}」{n} 步" for label, n in side["other_scenarios"]))
    lines += ["", "## 逐步對照（依步驟順序並排）", "",
              f"| # | `{a_name}` 步驟 | 證據 | 場次 | `{b_name}` 步驟 | 證據 | 場次 |", "|---:|---|---:|---|---|---:|---|"]
    for i in range(max(len(a["rows"]), len(b["rows"]))):
        left = a["rows"][i] if i < len(a["rows"]) else None
        right = b["rows"][i] if i < len(b["rows"]) else None
        cell = lambda x: (x["label"], str(x["refs"]), "、".join(x["sessions"]) or "—") if x else ("—", "—", "—")
        lines.append(f"| {i + 1} | " + " | ".join(cell(left) + cell(right)) + " |")
    lines += ["", "## 只由一個場次撐起的步驟", ""]
    for name, side in ((a_name, a), (b_name, b)):
        single = [x for x in side["rows"] if len(x["sessions"]) == 1]
        lines.append(f"- `{name}`：" + ("、".join(f"「{x['label']}」（{x['sessions'][0]}）" for x in single) or "沒有"))
    lines += ["", "## 只出現在其中一份的步驟", "",
              f"同一步常常換了說法，所以不只比標題：另一份有步驟和它共用至少 {EVIDENCE_MATCH:.0%} 的證據（交集÷聯集），"
              f"或標題雙字組相似度達 {TITLE_MATCH}，就算有對應。括號是共用證據最多的那一步與兩個分數，供研究者判斷。", ""]
    for name, items in ((a_name, r["only_a"]), (b_name, r["only_b"])):
        lines.append(f"**只在 `{name}`**")
        lines.append("")
        lines += [f"- {label}（共用證據最多：「{closest}」，證據 {overlap:.2f}、標題 {title:.2f}）"
                  for label, closest, overlap, title in items] or ["- 沒有"]
        lines.append("")
    missing = sum(x["missing"] for side in (a, b) for x in side["rows"])
    if missing:
        lines += [f"另有 {missing} 個引用找不到對應證據，沒有算進場次。", ""]
    for base in baselines or []:
        lines += [f"## 對照：較早的 run `{base['run']}`", "",
                  f"`{base['file']}`「{base['scenario']}」{base['steps']} 步。兩份 run 的證據編號不同，只比場次，不逐步配對。", "",
                  f"- 引用落在各場次的比例：{share_text(base['share'])}（涵蓋 {len(base['share'])}/{len(base['sources'])} 場次）"]
        if base["persona_sessions"] is not None:
            lines.append(f"- 目標 Persona 成員的來源：{'、'.join(base['persona_sessions']) or '—'}")
        lines.append("")
    lines += ["## 後續建議", "",
              "- 2026-10-22 走查 W1：步驟標題只寫「2／15」，看不出這一步有幾位或哪些受訪者經歷，研究者無法判斷「是不是每個人都走過這一步」或「這條路是不是拼出來的」。"
              "本審計的逐步場次就是這個問題的資料；建議下一支 CJM change 把每步涉及的場次（有歸屬時是受訪者）帶到畫面，層級先當 L1，若要在生成時記下每步涉及的受訪者則升 L2。"
              "本 change 只評估，不改旅程。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cjm_coverage_audit")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--a", default="journey.yaml")
    p.add_argument("--b", default="journey_proposal.yaml")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--title", default="CJM 涵蓋審計")
    p.add_argument("--baseline", action="append", type=Path, default=[], help="an earlier run whose journey.yaml is summarised by session share")
    args = p.parse_args(argv)
    result = audit(args.run, args.a, args.b)
    write_report(args.out, render(result, args.title, [baseline(run) for run in args.baseline]))
    for key in ("a", "b"):
        side = result[key]
        print(f"{result['files'][0 if key == 'a' else 1]}：{len(side['rows'])} 步、涵蓋 {len(side['sessions'])}/{len(result['sources'])} 場次")
    print(f"報告：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
