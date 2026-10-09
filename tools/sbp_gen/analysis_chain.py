"""Loads `schema/analysis-chain.yaml` and reconciles it against the prompt files.

The declaration states the analysis chain as six fixed operations (切/聚/排/層/
標/問), each holding named instances, plus eight cross-cutting principles and a
list of prompt files that are support stages rather than chain instances.

Reconciliation runs in both directions against `tools/sbp_gen/prompts/`: every
prompt file on disk must resolve to exactly one entry (an instance or a support
stage), and every instance whose status is `implemented` or `partial` must name
a prompt file that actually exists. A stage added, removed, or rerouted without
updating this declaration fails to load — see `openspec/LANGUAGE.md` for why
this reconciliation exists rather than a hand-kept stage list.
"""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "analysis-chain.yaml"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

OPERATIONS = ("切", "聚", "排", "層", "標", "問")
CATEGORIES = ("derivation", "projection", "researcher_input")
STATUSES = ("implemented", "partial", "absent")
EXECUTORS = ("prompt", "validator", "researcher", "doc")
ARBITERS = ("machine", "researcher")
PRINCIPLE_COUNT = 8


class AnalysisChainError(ValueError):
    """The declaration is malformed, or disagrees with the stages that exist."""


class Instance:
    __slots__ = (
        "id", "label", "operation", "category", "status", "prompt_file",
        "consumes", "produces", "missing", "deviation", "note",
    )

    def __init__(self, *, id, label, operation, category, status, prompt_file=None,
                 consumes=None, produces=None, missing=None, deviation=None, note=None):
        self.id = id
        self.label = label
        self.operation = operation
        self.category = category
        self.status = status
        self.prompt_file = prompt_file
        self.consumes = consumes
        self.produces = produces
        self.missing = missing
        self.deviation = deviation
        self.note = note


class Principle:
    __slots__ = (
        "id", "label", "statement", "checked_at", "look_upstream_at",
        "executor", "arbiter", "source_status",
    )

    def __init__(self, *, id, label, statement, checked_at, look_upstream_at,
                 executor, arbiter, source_status):
        self.id = id
        self.label = label
        self.statement = statement
        self.checked_at = checked_at
        self.look_upstream_at = look_upstream_at
        self.executor = executor
        self.arbiter = arbiter
        self.source_status = source_status


class AnalysisChain:
    """The loaded, reconciled declaration. Owns no knowledge of what any stage
    does beyond what the declaration states."""

    def __init__(self, instances: list[Instance], principles: list[Principle], support_stages: list[dict],
                 prompt_budgets: dict[str, int]):
        self._instances = {inst.id: inst for inst in instances}
        self._principles = {p.id: p for p in principles}
        self.support_stages = support_stages
        self.prompt_budgets = prompt_budgets

    def instances(self) -> list[Instance]:
        return list(self._instances.values())

    def instance(self, instance_id: str) -> Instance:
        try:
            return self._instances[instance_id]
        except KeyError:
            raise AnalysisChainError(f"沒有這個 instance：{instance_id!r}") from None

    def principles(self) -> list[Principle]:
        return list(self._principles.values())

    def principle(self, principle_id: str) -> Principle:
        try:
            return self._principles[principle_id]
        except KeyError:
            raise AnalysisChainError(f"沒有這個 principle：{principle_id!r}") from None


def _load_instances(raw_operations: dict) -> list[Instance]:
    instances: list[Instance] = []
    seen_ids: dict[str, str] = {}
    for op_id, op_body in raw_operations.items():
        if op_id not in OPERATIONS:
            raise AnalysisChainError(f"未知的操作：{op_id!r}（只能是 {OPERATIONS}）")
        for rec in (op_body or {}).get("instances") or []:
            iid = rec.get("id")
            if iid in seen_ids:
                raise AnalysisChainError(
                    f"instance {iid!r} 同時被宣告在 {seen_ids[iid]!r} 與 {op_id!r} 底下"
                    "——一個 instance 只能屬於一個操作")
            seen_ids[iid] = op_id
            category = rec.get("category")
            status = rec.get("status")
            if category not in CATEGORIES:
                raise AnalysisChainError(f"instance {iid!r} 的 category 不合法：{category!r}")
            if status not in STATUSES:
                raise AnalysisChainError(f"instance {iid!r} 的 status 不合法：{status!r}")
            prompt_file = rec.get("prompt_file")
            if status == "absent" and prompt_file:
                raise AnalysisChainError(
                    f"instance {iid!r} 是 absent，不該宣告 prompt_file（{prompt_file!r}）")
            if status == "partial" and not rec.get("missing"):
                raise AnalysisChainError(f"instance {iid!r} 是 partial，必須用 missing 說明缺什麼")
            instances.append(Instance(
                id=iid, label=rec.get("label"), operation=op_id, category=category, status=status,
                prompt_file=prompt_file, consumes=rec.get("consumes"), produces=rec.get("produces"),
                missing=rec.get("missing"), deviation=rec.get("deviation"), note=rec.get("note"),
            ))
    return instances


def _load_principles(raw_principles: list) -> list[Principle]:
    required = ("id", "statement", "checked_at", "look_upstream_at", "executor", "arbiter", "source_status")
    principles: list[Principle] = []
    for rec in raw_principles or []:
        pid = rec.get("id")
        for field in required:
            if not rec.get(field):
                raise AnalysisChainError(f"principle {pid!r} 缺少必填欄位：{field}")
        if rec["executor"] not in EXECUTORS:
            raise AnalysisChainError(f"principle {pid!r} 的 executor 不合法：{rec['executor']!r}")
        if rec["arbiter"] not in ARBITERS:
            raise AnalysisChainError(f"principle {pid!r} 的 arbiter 不合法：{rec['arbiter']!r}")
        principles.append(Principle(
            id=pid, label=rec.get("label"), statement=rec["statement"], checked_at=rec["checked_at"],
            look_upstream_at=rec["look_upstream_at"], executor=rec["executor"], arbiter=rec["arbiter"],
            source_status=rec["source_status"],
        ))
    if len(principles) != PRINCIPLE_COUNT:
        raise AnalysisChainError(
            f"通則必須恰好 {PRINCIPLE_COUNT} 條，目前 {len(principles)} 條——"
            f"新規則要嘛綁定既有通則，要嘛說明為什麼需要第 {len(principles)} 條")
    return principles


def _reconcile_against_prompts(instances: list[Instance], support_stages: list, prompt_dir: Path) -> None:
    claimed: dict[str, str] = {}

    for inst in instances:
        if not inst.prompt_file:
            continue
        if inst.status not in ("implemented", "partial"):
            raise AnalysisChainError(
                f"instance {inst.id!r} 宣告了 prompt_file（{inst.prompt_file}），"
                f"但 status 是 {inst.status!r}——只有 implemented 或 partial 可以指到檔案")
        if inst.prompt_file in claimed:
            raise AnalysisChainError(
                f"prompt file {inst.prompt_file!r} 被兩筆宣告各自認領："
                f"{claimed[inst.prompt_file]!r} 與 {inst.id!r}")
        if not (prompt_dir / inst.prompt_file).exists():
            raise AnalysisChainError(f"instance {inst.id!r} 指到的 prompt file 不存在：{inst.prompt_file}")
        claimed[inst.prompt_file] = inst.id

    for rec in support_stages or []:
        fname = rec.get("file") if isinstance(rec, dict) else rec
        if fname in claimed:
            raise AnalysisChainError(
                f"prompt file {fname!r} 同時被 instance {claimed[fname]!r} 與 support stage 認領")
        if not (prompt_dir / fname).exists():
            raise AnalysisChainError(f"support stage 指到的 prompt file 不存在：{fname}")
        claimed[fname] = f"support:{fname}"

    on_disk = {p.name for p in prompt_dir.glob("*.md")}
    unclaimed = on_disk - set(claimed)
    if unclaimed:
        raise AnalysisChainError(
            f"以下 prompt file 沒有被任何 instance 或 support stage 認領：{sorted(unclaimed)}")


def load(path: Path | None = None, prompt_dir: Path | None = None) -> AnalysisChain:
    """Load and reconcile the declaration. Raises `AnalysisChainError` on any
    disagreement between what is declared and what exists on disk."""
    path = path or SCHEMA_PATH
    prompt_dir = prompt_dir or PROMPT_DIR
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    for section in ("operations", "principles", "support_stages"):
        if section not in raw:
            raise AnalysisChainError(f"宣告缺少 {section} 區塊")

    instances = _load_instances(raw["operations"])
    principles = _load_principles(raw["principles"])
    support_stages = raw["support_stages"] or []
    prompt_budgets = raw.get("prompt_budgets") or {}
    _reconcile_against_prompts(instances, support_stages, prompt_dir)

    return AnalysisChain(instances, principles, support_stages, prompt_budgets)


@functools.lru_cache(maxsize=1)
def load_default() -> AnalysisChain:
    return load()
