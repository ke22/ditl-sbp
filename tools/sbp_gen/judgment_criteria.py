"""Load the judgment criteria that both the model and the output check read.

`schema_contract.py` renders a stage's mechanical contract — which fields
exist, what values they accept — from the schema that validates the stage's
output. Its docstring names what stays hand-written in the `.md`: "when to use
a field, and why."

That second half had no source. Every judgment rule was written twice, once as
prose in a prompt and once as a check in the validator, in independently
authored words with nothing forcing them to agree. LEARNINGS §8 records that
gap producing a live defect three times, most recently on 2026-09-02.

This module reads the one file where each such rule is now authored, so the
sentence sent to the model and the check applied to its output come from the
same entry.

Everything here raises rather than degrading. A criterion silently dropped is
a rule that has stopped applying while still appearing to be written down —
the exact failure the single source exists to close off.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Iterable, Mapping

import yaml

import validate_sbp as V

CRITERIA_FILE = V.SCHEMA_DIR / "service-design-judgment.criteria.yaml"

ARBITERS = ("machine", "researcher")
SOURCE_STATUSES = ("corpus_verified", "literature_general")

_KNOWLEDGE_FIELDS = ("label", "rule", "arbiter", "source_status", "citation")
_BINDING_FIELDS = ("binds_to", "check", "asked_when")


class JudgmentCriteriaError(RuntimeError):
    """A criteria source could not be loaded in full.

    Raised rather than returning what parsed: a partially loaded criteria set
    is a set of rules that quietly stopped being applied, which reads exactly
    like a set of rules that are being applied.
    """


class Criterion:
    """One judgment rule, with the two fields that govern what may be done with it.

    `arbiter` decides whether the system is allowed to state a conclusion:
    `machine` criteria are settled by inspecting the artifact, `researcher`
    criteria are questions the artifact cannot answer and are never rendered
    as a verdict. `source_status` decides how much force the criterion carries:
    a rule carried in from general literature can instruct and can advise, but
    cannot be the sole basis of a finding the researcher cannot dismiss.
    """

    __slots__ = ("id", "label", "rule", "arbiter", "source_status", "citation",
                 "policy", "compatibility_only", "binds_to", "check", "asked_when")

    def __init__(self, cid: str, knowledge: Mapping, binding: Mapping):
        self.id = cid
        self.label = knowledge["label"]
        self.rule = knowledge["rule"]
        self.arbiter = knowledge["arbiter"]
        self.source_status = knowledge["source_status"]
        self.citation = knowledge["citation"]
        # evidence-aware-persona-provenance: a criterion whose rule is a table
        # rather than a sentence carries that table here, so the sentence sent
        # to the model and the check applied to its output read the same rows.
        # Optional -- every existing criterion is prose only and stays so.
        self.policy = knowledge.get("policy")
        # model-facing-current-fields-only: policy rows kept so older artifacts
        # still validate; the validator reads them, the model is not shown them.
        self.compatibility_only = frozenset(knowledge.get("compatibility_only") or ())
        self.binds_to = tuple(binding.get("binds_to") or ())
        # A machine criterion names the check that settles it; a researcher
        # criterion names the condition under which the question is worth
        # asking. Exactly one, decided by the arbiter -- a criterion carrying
        # both would be claiming to settle a question it is also asking.
        self.check = binding.get("check")
        self.asked_when = binding.get("asked_when")

    @property
    def settles_itself(self) -> bool:
        """Whether the system is permitted to state this criterion's answer."""
        return self.arbiter == "machine"

    @property
    def dismissible(self) -> bool:
        """Whether a finding from this criterion must be dismissible.

        True for general-literature criteria: the researcher never has to argue
        with a rule this project has not verified against its own corpus.
        """
        return self.source_status == "literature_general"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Criterion {self.id} {self.arbiter}/{self.source_status}>"


class Criteria:
    """The loaded criteria set, queried by stage or by check name."""

    def __init__(self, criteria: dict[str, Criterion]):
        self._by_id = criteria

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self):
        return iter(self._by_id.values())

    def __getitem__(self, cid: str) -> Criterion:
        return self._by_id[cid]

    def for_stage(self, stage: str) -> list[Criterion]:
        """Criteria bound to a generation stage, in file order."""
        return [c for c in self._by_id.values() if stage in c.binds_to]

    def by_check(self, check: str) -> Criterion:
        for c in self._by_id.values():
            if c.check == check:
                return c
        raise JudgmentCriteriaError(f"沒有任何判準對應到檢查 {check!r}")

    def settled_by_machine(self) -> list[Criterion]:
        return [c for c in self._by_id.values() if c.settles_itself]

    def asked_of_researcher(self) -> list[Criterion]:
        return [c for c in self._by_id.values() if not c.settles_itself]


def load(declared_checks: Iterable[str], declared_conditions: Iterable[str] = (),
         path: Path | None = None) -> Criteria:
    """Load and fully validate the criteria source.

    `declared_checks` is the set of judgment checks the validator declares. It
    is passed in rather than imported so the two directions can be verified
    against each other: a criterion naming a check nobody implements, and an
    implemented check no criterion authorises, are both errors. Neither can be
    detected from one side alone.
    """
    path = path or CRITERIA_FILE
    if not path.exists():
        raise JudgmentCriteriaError(f"判準來源檔不存在：{path}")

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    knowledge = document.get("knowledge")
    bindings = document.get("bindings")
    if not isinstance(knowledge, dict) or not isinstance(bindings, dict):
        raise JudgmentCriteriaError(
            f"{path.name} 必須有 `knowledge` 與 `bindings` 兩個區塊，"
            f"實際讀到 {sorted(document)}"
        )

    missing_binding = sorted(set(knowledge) - set(bindings))
    if missing_binding:
        raise JudgmentCriteriaError(f"這些判準沒有對應的 bindings：{missing_binding}")
    orphan_binding = sorted(set(bindings) - set(knowledge))
    if orphan_binding:
        raise JudgmentCriteriaError(f"這些 bindings 沒有對應的判準：{orphan_binding}")

    criteria: dict[str, Criterion] = {}
    for cid in knowledge:
        entry, binding = knowledge[cid], bindings[cid]
        if not isinstance(entry, dict) or not isinstance(binding, dict):
            raise JudgmentCriteriaError(f"判準 {cid!r} 的內容不是一組欄位")
        for field in _KNOWLEDGE_FIELDS:
            if not str(entry.get(field) or "").strip():
                raise JudgmentCriteriaError(f"判準 {cid!r} 缺少 `{field}`")
        arbiter = entry.get("arbiter")
        if arbiter == "machine":
            for field in ("binds_to", "check"):
                if not binding.get(field):
                    raise JudgmentCriteriaError(f"判準 {cid!r} 由機器判定，缺少 `{field}`")
            if binding.get("asked_when"):
                raise JudgmentCriteriaError(
                    f"判準 {cid!r} 由機器判定，不該有 `asked_when`；那是研究者判準才需要的"
                )
        elif arbiter == "researcher":
            if not binding.get("asked_when"):
                raise JudgmentCriteriaError(
                    f"判準 {cid!r} 由研究者判定，缺少 `asked_when`——"
                    f"沒有條件的問題會在每一份產出上重複出現，那是雜訊不是判斷"
                )
            if binding.get("check"):
                raise JudgmentCriteriaError(
                    f"判準 {cid!r} 由研究者判定，不該有 `check`"
                )
            if binding.get("binds_to"):
                raise JudgmentCriteriaError(
                    f"判準 {cid!r} 由研究者判定，不該綁到生成階段——"
                    f"它是問研究者的問題，不是給模型的指令"
                )
        # 邊界要能被機器守住，不能只寫在檔案開頭的註解裡：bindings 的欄位混進
        # knowledge 就等於把專案接線寫進了「與 repo 無關」的那一半，之後要抽出
        # 去時才會發現搬不動。
        for field in _BINDING_FIELDS:
            if field in entry:
                raise JudgmentCriteriaError(
                    f"判準 {cid!r} 把 `{field}` 寫進 knowledge 區塊；"
                    f"那一半必須與這個 repo 無關"
                )
        for field in _KNOWLEDGE_FIELDS:
            if field in binding:
                raise JudgmentCriteriaError(
                    f"判準 {cid!r} 把 `{field}` 寫進 bindings 區塊"
                )
        if entry["arbiter"] not in ARBITERS:
            raise JudgmentCriteriaError(
                f"判準 {cid!r} 的 `arbiter` 是 {entry['arbiter']!r}，只能是 {list(ARBITERS)}"
            )
        if entry["source_status"] not in SOURCE_STATUSES:
            raise JudgmentCriteriaError(
                f"判準 {cid!r} 的 `source_status` 是 {entry['source_status']!r}，"
                f"只能是 {list(SOURCE_STATUSES)}"
            )
        criteria[cid] = Criterion(cid, entry, binding)

    declared = set(declared_checks)
    authorised = {c.check for c in criteria.values() if c.check}
    unimplemented = sorted(
        (c.id, c.check) for c in criteria.values() if c.check and c.check not in declared
    )
    if unimplemented:
        named = "、".join(f"{cid}→{check}" for cid, check in unimplemented)
        raise JudgmentCriteriaError(
            f"這些判準指定的檢查不存在：{named}"
            f"（validator 宣告的是 {sorted(declared)}）"
        )
    unauthorised = sorted(declared - authorised)
    if unauthorised:
        raise JudgmentCriteriaError(
            f"這些檢查沒有任何判準授權：{unauthorised}——"
            f"validator 正在檢查一條沒人告訴模型的規則"
        )

    conditions = set(declared_conditions)
    asked = {c.asked_when for c in criteria.values() if c.asked_when}
    missing_condition = sorted(
        (c.id, c.asked_when) for c in criteria.values()
        if c.asked_when and c.asked_when not in conditions
    )
    if missing_condition:
        named = "、".join(f"{cid}→{cond}" for cid, cond in missing_condition)
        raise JudgmentCriteriaError(f"這些判準指定的提問條件不存在：{named}")
    unasked = sorted(conditions - asked)
    if unasked:
        raise JudgmentCriteriaError(f"這些提問條件沒有任何判準使用：{unasked}")
    return Criteria(criteria)


@functools.lru_cache(maxsize=1)
def load_default() -> Criteria:
    """Load the project's criteria against the checks the validator declares.

    Cached: a validator run asks for a criterion once per finding, and the
    source cannot change mid-process. Tests that need a modified source call
    `load()` with their own path instead.
    """
    return load(V.JUDGMENT_CHECKS, V.JUDGMENT_ASK_CONDITIONS)


class JudgmentFinding(str):
    """A warning that carries the criterion authorising it.

    A `str` subclass rather than a record, so every existing consumer keeps
    printing it unchanged, while the judgment report can group findings by the
    rule that produced them without parsing the sentence back apart. The label
    comes from the criterion, so the name the researcher reads and the name in
    the source are the same string by construction.
    """

    __slots__ = ("criterion",)

    def __new__(cls, criterion: Criterion, detail: str):
        # A researcher-settled criterion reaching this constructor is not a
        # formatting problem to be corrected downstream -- a finding IS a
        # statement of what was found, and this system is not entitled to
        # state the answer to a question the artifact cannot contain. Raising
        # here is the only place the distinction cannot be lost by accident.
        if not criterion.settles_itself:
            raise JudgmentCriteriaError(
                f"判準 {criterion.id!r} 由研究者裁定，不能寫成一則已判定的發現；"
                f"它只能以問題的形式出現"
            )
        finding = super().__new__(cls, f"[{criterion.label}？] {detail}")
        finding.criterion = criterion
        return finding


def finding(check: str, detail: str) -> JudgmentFinding:
    """Build a finding for a declared check, via the criterion that authorises it."""
    return JudgmentFinding(load_default().by_check(check), detail)


_HEADER = (
    "## 判斷判準（由 service-design-judgment.criteria.yaml 自動產生，不要手動修改）\n\n"
    "以下是這個階段的輸出會被檢查的判斷規則。每一條在系統裡只寫一次，"
    "送給你的這段文字和事後檢查你輸出的那條檢查，讀的是同一筆資料。\n"
)


def _render_policy(policy: Mapping, hidden: frozenset = frozenset()) -> str:
    """Render a criterion's table as the rows the model must satisfy.

    Rendered from the same mapping the validator enforces, so a row cannot be
    changed for one reader without changing it for the other -- the failure
    this criteria file exists to close, applied to a table instead of a
    sentence.
    """
    rows = ["| 欄位 | 可以標成 |", "| --- | --- |"]
    for field, allowed in policy.items():
        if field in hidden:
            continue
        rows.append(f"| `{field}` | " + "、".join(f"`{a}`" for a in allowed) + " |")
    return "\n".join(rows)


def render(stage: str, criteria: Criteria | None = None) -> str:
    """Render the judgment criteria bound to a stage, for its system prompt.

    Returns an empty string when nothing binds to the stage, so a caller can
    append unconditionally without a stage growing an empty heading.
    """
    criteria = criteria if criteria is not None else load_default()
    # Researcher criteria never reach the model. A question the artifact
    # cannot answer is not an instruction, and sending it as one would ask the
    # model to decide exactly what it is not entitled to decide.
    bound = [c for c in criteria.for_stage(stage) if c.settles_itself]
    if not bound:
        return ""
    lines = [_HEADER]
    for criterion in bound:
        lines.append(f"### {criterion.label}\n")
        lines.append(criterion.rule + "\n")
        if criterion.policy:
            lines.append(_render_policy(criterion.policy, criterion.compatibility_only) + "\n")
    return "\n".join(lines).rstrip() + "\n"
