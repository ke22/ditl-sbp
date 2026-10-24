"""Controlled rerun wrapper (purpose-aware-research-calibration, task 5.1).

A rerun calls a model, so it only happens when the researcher has written a
count in the proposal's 「模型重跑授權」 section. With 0 (the default) the
wrapper writes 「待授權，未執行」 and exits 0 without copying anything or calling
the executor. With a count, it copies the run into an isolated folder under
the data root, changes exactly one variable (hypothesis a: the new axial input
and prompt, nothing to set; hypothesis b: the project context), runs the
declared stages once and records executor, model, effort, input digest,
seconds and tokens. Every execution is written to a ledger before the stage
starts, so a crash still counts and the authorised count is never exceeded.

The original run is never written.

Usage:
  python3 -m sbp_gen.audits.controlled_rerun --hypothesis axial --run DIR --case henglong \
      [--report docs/research/axial-paradigm-audit-2026-10-23.md]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from sbp_gen.audits import replace_after_section

ROOT = Path(__file__).resolve().parents[3]
CHANGE_DIR = ROOT / "openspec" / "changes" / "purpose-aware-research-calibration"
PROPOSAL = CHANGE_DIR / "proposal.md"
LEDGER = CHANGE_DIR / "evidence" / "rerun-ledger.yaml"
DATA_ROOT = ROOT.parent / "ditl_sbp-ui-v3-data"
SECTION = "## 模型重跑授權"

HYPOTHESES = {
    "axial": {
        "authorization": "恆隆行 主軸編碼 重跑",
        "label": "假設 a：主軸編碼用新輸入與新 prompt，其餘凍結",
        "stages": ["axial-coding"],
        "usage_keys": ["axial_coding"],
        "targets": "docs/research/2026-10-21-henglong-system-vs-researcher-comparison.md 第 3 節「研究者有、系統沒有或弱化」6 條（出處 cases_calibration/henglonghang/reference.md）",
        "pass": ["6 條漏掉或弱化的判斷至少找回 3 條，且每條翻得回證據",
                 "推論格全部有 reasoning", "沒有新的 E1（引文與原文不符）"],
    },
    "hmw": {
        # 2026-10-23 the researcher dropped Persona stage 2 from this rerun: no
        # Henglong run has the stage 1 comparison, and every one has chosen axes.
        "authorization": "恆隆行 HMW 形成 重跑",
        "label": "假設 b：既有主軸不變，只填入專案目的與議題，重跑 HMW 形成",
        "stages": ["form-hmw"],
        "usage_keys": ["how_might_we", "primary_hmw_candidates"],
        # 2026-10-23: the concern is written in the purpose, not as a separate issue.
        "extra_args": " --project-purpose \"改善顧客參與濾網回收服務的體驗，提高持續參與的意願；品牌重視永續\"",
        "pass": ["「永續」出現在至少一則框法，或被明寫證據不足，兩者只有一種",
                 "沒有 ISS 或 BI 進 evidence_refs", "沒有新的 U1（框法寫了證據沒有說的事）"],
    },
}


class RerunError(Exception):
    pass


def read_authorization(proposal: Path) -> dict[str, int]:
    """Counts written under 「模型重跑授權」 as {label: n}; lines read `- <label>：N 次`."""
    text = Path(proposal).read_text(encoding="utf-8")
    if SECTION not in text:
        return {}
    section = text[text.index(SECTION):].split("\n## ", 1)[0]
    counts = {}
    for line in section.splitlines():
        match = re.match(r"^\s*-\s*(.+?)[：:]\s*(\d+)\s*次", line)
        if match:
            counts[match.group(1).strip()] = int(match.group(2))
    return counts


def _ledger(path: Path) -> list[dict]:
    if not Path(path).is_file():
        return []
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _append_ledger(path: Path, entry: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_ledger(path) + [entry], allow_unicode=True, sort_keys=False), encoding="utf-8")


def input_digest(folder: Path) -> str:
    """sha256 over every YAML file of the copy (names and bytes), before the stage runs."""
    h = hashlib.sha256()
    for path in sorted(Path(folder).glob("*.yaml")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return "sha256:" + h.hexdigest()


def run_usage(run_dir: Path) -> dict:
    path = Path(run_dir) / "run.json"
    if not path.is_file():
        return {}
    return (json.loads(path.read_text(encoding="utf-8")) or {}).get("usage") or {}


def executor_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Always the signed-in local agent (決定十五): CLI mode, no API keys -- the
    legacy default provider is an API and must never be reached. The runner
    command comes from the environment the launcher sets up; the research
    pipeline never names a runner itself."""
    env = dict(os.environ if base is None else base)
    for name in [k for k in env if k.endswith("_API_KEY")]:
        env.pop(name)
    env["SBP_EXECUTION_MODE"] = "cli"
    env["SBP_LLM_PROVIDER"] = "cli"
    env.pop("SBP_API_PROVIDER", None)
    env.setdefault("SBP_CLI_EXECUTOR", "codex")
    return env


def default_executor(stage: str, run_dir: Path, model: str | None, effort: str | None) -> int:
    command = [sys.executable, "-m", "sbp_gen", stage, "--run", str(run_dir)]
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    return subprocess.run(command, cwd=ROOT / "tools", env=executor_environment()).returncode


def preconditions(hypothesis: str, run_dir: Path) -> list[str]:
    """What the source run must already have; the wrapper never edits a copy to make it fit."""
    problems = []
    if not (run_dir / "run.json").is_file():
        problems.append("找不到 run.json")
    if hypothesis == "axial" and not (run_dir / "open_coding.yaml").is_file():
        problems.append("沒有開放編碼，主軸無從重跑")
    if hypothesis == "hmw" and not (run_dir / "narrative_job_selection.yaml").is_file():
        problems.append("沒有 narrative_job_selection.yaml：HMW 從研究者選定的 JTBD 形成")
    return problems


def _usage_line(entry: dict) -> str:
    return (f"{entry.get('tokens', '未回報')} tokens、{entry.get('wall_seconds', entry.get('seconds', '未回報'))} 秒"
            f"（{entry.get('executor', '?')}／{entry.get('model_requested', '?')}／effort {entry.get('effort_requested', '?')}）")


def pending_body(hypothesis: str, authorized: int, used: int, run_dir: Path) -> str:
    spec = HYPOTHESES[hypothesis]
    state = "待授權，未執行" if authorized == 0 else f"授權 {authorized} 次已用完（已執行 {used} 次），未再執行"
    before = run_usage(run_dir)
    lines = [f"**{state}**", "", f"- 假設：{spec['label']}",
             f"- proposal「模型重跑授權」：{spec['authorization']} {authorized} 次", f"- 原 run：`{run_dir.name}`（沒有複製、沒有呼叫模型）"]
    for key in spec["usage_keys"]:
        if key in before:
            lines.append(f"- before 用量（{key}）：{_usage_line(before[key])}")
    if spec.get("targets"):
        lines.append(f"- 對照清單：{spec['targets']}")
    problems = preconditions(hypothesis, run_dir)
    if problems:
        lines += ["", "這份 run 授權後也還不能直接重跑（wrapper 不會自己改複本去配合）：", ""] + [f"- {item}" for item in problems]
    lines += ["", "授權後的通過條件（事先寫定）：", ""] + [f"- {item}" for item in spec["pass"]]
    lines += ["", "要執行：研究者把 proposal 的次數改成 1，再跑 "
              f"`python3 -m sbp_gen.audits.controlled_rerun --hypothesis {hypothesis} "
              f"--run <run> --case <case> --report <本報告>{spec.get('extra_args', '')}`。"]
    return "\n".join(lines)


def execute(hypothesis: str, run_dir: Path, case: str, *, proposal: Path = PROPOSAL, ledger: Path = LEDGER,
            data_root: Path = DATA_ROOT, report: Path | None = None, executor=default_executor,
            model: str | None = None, effort: str | None = None, project_purpose: str = "",
            today: str | None = None) -> dict:
    spec = HYPOTHESES[hypothesis]
    run_dir = Path(run_dir)
    authorized = read_authorization(proposal).get(spec["authorization"], 0)
    used = sum(1 for e in _ledger(ledger) if e.get("hypothesis") == hypothesis)
    if used >= authorized:
        body = pending_body(hypothesis, authorized, used, run_dir)
        if report:
            replace_after_section(report, body)
        return {"status": "pending" if authorized == 0 else "exhausted", "authorized": authorized, "used": used, "called": False}
    problems = preconditions(hypothesis, run_dir)
    if problems:
        raise RerunError("前置條件不符，沒有複製也沒有呼叫模型：" + "；".join(problems))
    date = today or dt.date.today().isoformat()
    copy_root = Path(data_root) / f"{case}-calibration-{hypothesis}-{date}"
    copy = copy_root / "runs" / run_dir.name
    if copy.exists():
        raise RerunError(f"複本已存在，不覆蓋：{copy}")
    copy.parent.mkdir(parents=True)
    shutil.copytree(run_dir, copy, copy_function=shutil.copy2)
    if hypothesis == "hmw":
        from sbp_gen import project_context
        project_context.update(copy, project_purpose=project_purpose)
    record = {"hypothesis": hypothesis, "label": spec["label"], "source_run": str(run_dir), "copy": str(copy),
              "executor": executor_environment()["SBP_CLI_EXECUTOR"], "model": model or "（執行器預設）",
              "effort": effort or "（執行器預設）", "input_digest": input_digest(copy),
              "started_at": dt.datetime.now().isoformat(timespec="seconds"), "stages": []}
    _append_ledger(ledger, {"hypothesis": hypothesis, "at": record["started_at"], "copy": str(copy)})
    before = run_usage(copy)
    for stage in spec["stages"]:
        start = time.monotonic()
        code = executor(stage, copy, model, effort)
        record["stages"].append({"stage": stage, "returncode": code, "wall_seconds": round(time.monotonic() - start, 1)})
        if code != 0:
            break
    after = run_usage(copy)
    record["usage"] = {k: after[k] for k in spec["usage_keys"] if k in after and after.get(k) != before.get(k)}
    (copy_root / "calibration_run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if report:
        lines = [f"**已執行**（{record['started_at']}）", "", f"- 假設：{spec['label']}", f"- 複本：`{copy}`",
                 f"- 執行器／模型／effort：{record['executor']}／{record['model']}／{record['effort']}",
                 f"- 輸入 digest：`{record['input_digest']}`"]
        lines += [f"- {s['stage']}：exit {s['returncode']}，{s['wall_seconds']} 秒" for s in record["stages"]]
        lines += [f"- 用量（{k}）：{_usage_line(v)}" for k, v in record["usage"].items()]
        lines += ["", "逐條 finding（where、against-what、why）由研究者或下一個工作階段對照通過條件填寫：", ""]
        lines += [f"- {item}" for item in spec["pass"]]
        replace_after_section(report, "\n".join(lines))
    return {"status": "executed", "authorized": authorized, "used": used + 1, "called": True, "record": record}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="controlled_rerun")
    p.add_argument("--hypothesis", choices=sorted(HYPOTHESES), required=True)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--case", required=True, help="slug for the isolated copy folder, e.g. henglong")
    p.add_argument("--report", type=Path)
    p.add_argument("--proposal", type=Path, default=PROPOSAL)
    p.add_argument("--model")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--project-purpose", default="")
    args = p.parse_args(argv)
    try:
        result = execute(args.hypothesis, args.run, args.case, proposal=args.proposal, report=args.report,
                         model=args.model, effort=args.effort, project_purpose=args.project_purpose)
    except RerunError as exc:
        print(exc, file=sys.stderr)
        return 2
    if result["status"] == "pending":
        print(f"{HYPOTHESES[args.hypothesis]['authorization']}：待授權，未執行")
    elif result["status"] == "exhausted":
        print(f"{HYPOTHESES[args.hypothesis]['authorization']}：授權 {result['authorized']} 次已用完，未執行")
    else:
        record = result["record"]
        print(f"複本：{record['copy']}")
        print("授權：已用 {used}/{authorized}".format(**result))
        for stage in record["stages"]:
            print(f"{stage['stage']}：exit {stage['returncode']}，{stage['wall_seconds']} 秒")
        for key, entry in record["usage"].items():
            print(f"{key}：{_usage_line(entry)}")
        if any(s["returncode"] != 0 for s in record["stages"]):
            return 1
    if args.report:
        print(f"報告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
