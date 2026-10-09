"""sbp-gen — evidence → compare → action graph → reference → reconcile → projection."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sbp_gen"

from . import as_is_research, decisions, gates, hmw_formation, improvement_goal_review, ingest, journey_operations, lifecycle, llm, stages, to_be  # noqa: E402
from . import run_state as run_state_file  # noqa: E402
import role_coverage  # noqa: E402
import validate_sbp as V  # noqa: E402
import evidence_locator  # noqa: E402
import narrative_framing  # noqa: E402
import hmw_scope as hmw_scope_module  # noqa: E402
import storage_locations  # noqa: E402

# streamline-sbp-generation-pipeline 決定一：關鍵路徑收斂成五階段（align/journey/compare/
# structure/projection）。reference/reconcile 不在這裡——它們是 `sbp-gen reference --run
# <dir>` 事後補跑的選配分析，草稿完成狀態不等它們（見 design.md 決定一）。
PIPELINE = ("align", "journey", "compare", "structure", "projection")
MODEL_STAGES = PIPELINE[:-1]
REFERENCE_PIPELINE = ("reference", "reconcile")
RECHECK_STAGE = "journey_recheck"


def _read_raw(path: Path) -> tuple[str, dict]:
    text, manifest = ingest.ingest(path)
    if not text.strip():
        details = "; ".join(error for source in manifest["sources"] for error in source["errors"])
        raise SystemExit(f"{path} 沒有可讀內容。{details}")
    return text, manifest


def _prompt_sha256(stage_names: tuple[str, ...] = MODEL_STAGES) -> str:
    h = hashlib.sha256()
    for name in stage_names:
        p = stages.PROMPT_DIR / f"{name}.md"
        if p.exists():
            # Hash the prompt as sent to the model, including shared
            # reasoning fragments such as channel_reasoning.md.
            h.update(stages._prompt(name).encode("utf-8"))
    return h.hexdigest()


def _prompt_hash(stage_names: tuple[str, ...] = MODEL_STAGES) -> str:
    return _prompt_sha256(stage_names)[:12]


# 每個階段都已經落檔了（evidence/conflicts/service_graph/sbp_draft），但除了 align
# 之外沒有一個會重用——改一個小東西，最貴的 structure 也跟著重跑。缺的只是「這個
# 階段上次的輸入是什麼」。這裡補上那個指紋。
#
# 指紋涵蓋三件事，少一件就會拿到不該拿的舊結果：
#   1. 這個階段的語意輸入（證據、衝突、案例、歷程⋯）
#   2. 它實際送出去的系統提示詞——用 `stages._system()` 而不是 `_prompt_hash()`，
#      因為 journey／structure 的提示詞是 .md 加上從 schema 產生的欄位契約，
#      只雜湊 .md 會讓「改了 schema」這件事悄悄不算數
#   3. provider／model／effort——換模型當然要重跑
def _stage_fingerprint(stage: str, inputs: dict, opts: dict) -> str:
    material = {
        "stage": stage,
        "system_prompt": stages._system(stage),
        "provider": opts.get(f"{stage}_provider"),
        "model": opts.get(f"{stage}_model"),
        "effort": opts.get(f"{stage}_effort"),
        "inputs": {k: lifecycle.canonical_content_digest(v) for k, v in sorted(inputs.items())},
    }
    return lifecycle.canonical_content_digest(material)


def _reuse_stage(out_dir: Path, previous: dict, stage: str, fingerprint: str, filename: str):
    """上次同樣的輸入產出的結果，如果檔案還在就直接拿來用。

    任何一點不確定都回 None（重跑）——重跑只是慢，拿到錯的舊結果是靜默的錯誤。
    """
    if (previous.get("stage_inputs") or {}).get(stage) != fingerprint:
        return None
    path = out_dir / filename
    if not path.is_file():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 讀不起來就重跑，不要讓快取變成失敗原因
        return None


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _persist(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_artifact_registry(out: Path, case_id: str) -> Path:
    return storage_locations.derive_registry_path(
        case_id, storage_locations.runs_root_for_run(out)
    )


def _publish_generation_artifacts(
    registry_path: Path,
    case: dict,
    evidence: list,
    journey: dict,
    graph: dict,
    now: str,
    ledger_path: Path | None = None,
) -> dict:
    evidence_ids = {item["id"] for item in evidence if isinstance(item, dict) and "id" in item}
    evidence_texts = {
        item["id"]: item.get("text", "")
        for item in evidence
        if isinstance(item, dict) and "id" in item
    }
    scenario_id = graph.get("meta", {}).get("scenario_id") or journey.get("primary_scenario")
    return lifecycle.publish_batch(
        registry_path,
        [
            {
                "key": "evidence",
                "artifact_type": "evidence",
                "case_id": case["case_id"],
                "content": evidence,
                "validate": lambda data: V.structural_errors(data, V.load_schema("evidence")),
                "dependencies": {},
            },
            {
                "key": "confirmed_journey",
                "artifact_type": "confirmed_journey",
                "case_id": case["case_id"],
                "content": journey,
                "validate": lambda data: (
                    V.structural_errors(data, V.load_schema("journey"))
                    + stages._journey_errors(data, evidence_ids, require_confirmed=True)
                ),
                "dependencies": {"evidence": "@evidence"},
            },
            {
                "key": "as_is_graph",
                "artifact_type": "as_is_graph",
                "case_id": case["case_id"],
                "scenario_id": scenario_id,
                "content": graph,
                "validate": lambda data: stages._validate_graph(
                    data, evidence_ids, evidence_texts, journey
                ),
                "dependencies": {
                    "evidence": "@evidence",
                    "confirmed_journey": "@confirmed_journey",
                },
            },
        ],
        now,
        ledger_path=ledger_path,
    )


def _load_yaml(path: Path, label: str):
    """Load one required YAML input with a stable researcher-facing error."""
    try:
        if not path.is_file():
            raise ValueError("檔案不存在")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"{label} 無法讀取：{path}（{exc}）") from exc
    if data is None:
        raise ValueError(f"{label} 是空檔案：{path}")
    return data


def _resolve_core_category(run_dir: Path, categories_doc: dict) -> dict | None:
    """The researcher-selected core category, if any -- as a pointer to
    surface as an emphasis signal, never as a scope boundary.

    core-category-as-ranking-signal-not-filter: this used to be
    `_scope_categories_to_core()`, which narrowed `categories_doc` itself to
    just the matching category -- silently discarding the other categories
    and their evidence before persona_matrix/jtbd ever saw them (92% of
    evidence on the real MUJI case, selecting the AI's third candidate).
    `openspec/TARGET-ARCHITECTURE.md`'s 決定十 states the opposite policy: a
    framework-point selection is a ranking/framing signal, never a filter.
    This function no longer touches `categories_doc` at all -- callers pass
    the full document to generation unconditionally and separately thread
    this return value through as an emphasis signal (see
    `stages.persona_matrix()`/`stages.jtbd()`'s `core_category` parameter).

    Reads `<run_dir>/selective_coding.yaml`'s `core_category_id`. Returns
    `None` whenever the file is absent, unreadable, not a dict, or its
    `core_category_id` doesn't match any category currently in
    `categories_doc` — a stale or missing selection degrades to no signal,
    it never blocks the caller. Otherwise returns the matching category
    record itself (drawn from `categories_doc`'s own current contents, so it
    can never name a category the document doesn't actually have).
    """
    selection_path = run_dir / "selective_coding.yaml"
    if not selection_path.exists():
        return None
    try:
        selection = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(selection, dict):
        return None
    core_category_id = selection.get("core_category_id")
    categories = [c for c in (categories_doc.get("categories") or []) if isinstance(c, dict)]
    matching = [c for c in categories if c.get("id") == core_category_id]
    return matching[0] if matching else None


def _read_dimension_selection(run_dir: Path, category_ids: set[str]) -> list | None:
    """connect-dimension-and-job-selections-to-downstream: read a researcher's
    `persona_dimensions_selection.yaml`, if one exists, and return its `axes`
    list — or `None` when the file is absent, unreadable, not a mapping, has
    no non-empty `axes` list, or names a grounding category the run's axial
    coding no longer contains.

    Shaped like `_scope_categories_to_core`: a missing or stale selection
    degrades to no selection, it never blocks the caller. `category_ids`
    SHOULD be the run's full axial-coding category set, not one already
    narrowed by `_scope_categories_to_core` — the dimension selection was
    made against the full set, and checking it against a narrowed subset
    would misreport a valid selection as stale (the same reasoning
    `_main_persona_matrix`'s `--show` mode already applies when checking
    axis grounding).
    """
    selection_path = run_dir / "persona_dimensions_selection.yaml"
    if not selection_path.exists():
        return None
    try:
        selection = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(selection, dict):
        return None
    axes = selection.get("axes")
    if not isinstance(axes, list) or not axes:
        return None
    for axis in axes:
        if not isinstance(axis, dict):
            return None
        for cat_id in axis.get("grounded_in") or []:
            if cat_id not in category_ids:
                return None
    return axes


def _read_one_job_selection(path: Path, category_ids: set[str]) -> dict | None:
    """One selection file, or `None` under the four degradation conditions
    `_read_dimension_selection` uses: absent, unreadable, not a mapping, or
    naming a grounding category the run's axial coding no longer contains.

    merge-primary-job-into-stage-two: `grounded_in` is an array in both
    schemas, and the previous version of this check tested it for membership
    in a set of ids — which raises `TypeError: unhashable type: 'list'`
    rather than degrading, for any schema-valid selection. It never fired
    because no run on disk had ever recorded one. A selection is refused
    here when any of its groundings does not resolve.
    """
    if not path.exists():
        return None
    try:
        selection = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(selection, dict):
        return None
    grounded_in = selection.get("grounded_in")
    if not isinstance(grounded_in, list) or not grounded_in:
        return None
    if any(cat_id not in category_ids for cat_id in grounded_in):
        return None
    return selection


def _record_narrative_framing(data: dict, selection: dict | None) -> None:
    """frame-hmw-from-narrative: stamp an HMW output with the pair it read,
    so the workbench can tell when the researcher has since chosen another."""
    digest = narrative_framing.selection_digest(selection)
    if digest:
        data["framed_by"] = {"narrative_job_selection": digest}
    else:
        data.pop("framed_by", None)


def _read_primary_job_selection(run_dir: Path, category_ids: set[str]) -> dict | None:
    """The primary Job How Might We should read, or `None`.

    merge-primary-job-into-stage-two: stage ② now records a narrative-and-job
    pair, and the researcher's stated rule is that the chosen pair travels
    downstream. So the pair is tried first and the older
    `primary_job_selection.yaml` is the fallback, which keeps a run recorded
    before stage ② existed working exactly as it did.

    Read rather than copied: the pair is not written into the older file.
    Two copies of one statement on disk is the drift this project has had to
    clean up before, and it would leave the researcher unable to tell which
    one they had edited.
    """
    # multi-jtbd-single-job-source: a list selection (schema_version 2) is
    # returned whole -- its digest covers every JTBD, and readers take the
    # list from it with narrative_framing.selections().
    listed = _load_yaml_if_exists(run_dir / "narrative_job_selection.yaml")
    if isinstance(listed, dict) and listed.get("schema_version") == 2 and narrative_framing.selections(listed):
        return listed
    for name in ("narrative_job_selection.yaml", "primary_job_selection.yaml"):
        selection = _read_one_job_selection(run_dir / name, category_ids)
        if selection is not None:
            return selection
    return None


def _load_yaml_if_exists(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return None


def _existing_recheck_errors(
    result: dict,
    evidence: list,
    state: dict,
    *,
    request_digest: str,
    source_manifest: dict,
    raw_material: str,
    prior_evidence_ids: set[str],
) -> list[str]:
    """Validate every persisted file before treating a replay as complete."""
    errors = V.structural_errors(result, V.load_schema("journey-recheck"))
    errors += V.journey_recheck_errors(
        result,
        source_manifest=source_manifest,
        source_units=stages._recheck_source_units(raw_material, source_manifest),
        prior_evidence_ids=prior_evidence_ids,
    )
    if result.get("request_digest") != request_digest:
        errors.append("[journey-recheck] 已存結果與本次 request digest 不同")
    if state.get("request_digest") != request_digest:
        errors.append("[journey-recheck] run state 與本次 request digest 不同")
    if state.get("state") != "awaiting_journey_confirmation":
        errors.append("[journey-recheck] run state 尚未待確認")
    if lifecycle.canonical_content_digest(evidence) != lifecycle.canonical_content_digest(
        result.get("reconciled_evidence")
    ):
        errors.append("[journey-recheck] evidence.yaml 與完整結果不一致")
    return errors


def _main_journey_recheck(argv: list[str]) -> int:
    """Run only evidence reconciliation and stop before As-is generation."""
    p = argparse.ArgumentParser(
        prog="sbp-gen journey-recheck",
        description="依 raw data 重查研究者調整後的完整歷程，完成後停在待確認",
    )
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--input", required=True, type=Path, help="所選案例的 raw data 根目錄或檔案")
    p.add_argument("--evidence", required=True, type=Path, help="目前 evidence YAML")
    p.add_argument("--baseline-journey", required=True, type=Path, help="伺服器保存的 AI 初版歷程")
    p.add_argument("--edited-journey", required=True, type=Path, help="研究者最新調整的歷程")
    p.add_argument("--out", required=True, type=Path, help="不覆寫其他 request 的安全輸出目錄")
    p.add_argument("--provider", choices=llm.PROVIDERS, default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    p.add_argument("--fallback-model", default=None)
    p.add_argument("--fallback-effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    p.add_argument("--max-tokens", type=int, default=32000)
    args = p.parse_args(argv)

    try:
        case = _load_yaml(args.case, "案例邊界")
        evidence = _load_yaml(args.evidence, "既有 evidence")
        baseline = _load_yaml(args.baseline_journey, "AI 初版歷程")
        edited = _load_yaml(args.edited_journey, "研究者調整歷程")
        raw_material, source_manifest = _read_raw(args.input)

        input_errors = V.structural_errors(case, V.load_schema("case")) + V.case_errors(case)
        input_errors += V.structural_errors(evidence, V.load_schema("evidence"))
        input_errors += [f"AI 初版：{item}" for item in V.editable_journey_errors(baseline)]
        input_errors += [f"研究者調整：{item}" for item in V.editable_journey_errors(edited)]
        case_id = case.get("case_id") if isinstance(case, dict) else None
        if case_id and baseline.get("case_id") != case_id:
            input_errors.append("AI 初版歷程不屬於所選案例")
        if case_id and edited.get("case_id") != case_id:
            input_errors.append("研究者調整歷程不屬於所選案例")
        evidence_ids = [item.get("id") for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
        for duplicate in sorted({item for item in evidence_ids if item and evidence_ids.count(item) > 1}):
            input_errors.append(f"既有 evidence id 重複：{duplicate}")
        if input_errors:
            raise ValueError("輸入契約無效（尚未呼叫模型）：\n" + "\n".join(input_errors))

        digests = stages.journey_recheck_digests(
            raw_material, evidence, baseline, edited, case_id
        )
        request_digest = digests["request_digest"]
        result_path = args.out / "journey_recheck.yaml"
        evidence_path = args.out / "evidence.yaml"
        state_path = args.out / "run.json"
        existing_entries = list(args.out.iterdir()) if args.out.exists() else []
        existing_state = {}
        if state_path.exists():
            try:
                existing_state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"輸出識別已存在，但 run.json 無法驗證：{exc}") from exc
        existing_digest = existing_state.get("request_digest")
        if existing_digest and existing_digest != request_digest:
            raise FileExistsError("輸出識別已屬於另一份歷程調整；請使用不同輸出目錄")
        if existing_entries and not existing_digest:
            raise FileExistsError("輸出目錄已有無法辨識的內容；請使用新的輸出目錄")
        if result_path.exists():
            if not evidence_path.exists():
                raise ValueError("已存 recheck 結果缺少 evidence.yaml，不能重用或覆寫")
            existing_result = _load_yaml(result_path, "已存 recheck 結果")
            existing_evidence = _load_yaml(evidence_path, "已存 reconciled evidence")
            replay_errors = _existing_recheck_errors(
                existing_result,
                existing_evidence,
                existing_state,
                request_digest=request_digest,
                source_manifest=source_manifest,
                raw_material=raw_material,
                prior_evidence_ids=set(evidence_ids),
            )
            if replay_errors:
                raise ValueError("已存結果未通過重用驗證，為避免覆寫已停止：\n" + "\n".join(replay_errors))
            print(f"重用既有歷程重查結果：{result_path}")
            return 0
    except FileExistsError as exc:
        print(f"歷程重查未開始：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"歷程重查輸入無效：{exc}", file=sys.stderr)
        return 2

    primary_provider = llm.resolve_provider(args.provider, stage=RECHECK_STAGE)
    opts = {
        "max_tokens": args.max_tokens,
        "journey_recheck_provider": primary_provider,
        "journey_recheck_model": args.model,
        "journey_recheck_effort": args.effort,
        "journey_recheck_fallback_model": args.fallback_model,
        "journey_recheck_fallback_effort": args.fallback_effort,
    }
    state = {
        "kind": "journey_recheck",
        "case_id": case_id,
        **digests,
        "state": "rechecking",
        "stages": {"journey_recheck": "running"},
        "provider": primary_provider,
        "model": args.model or llm.default_model(RECHECK_STAGE, primary_provider),
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    _persist(state_path, state)
    try:
        result, usage = stages.journey_recheck(
            raw_material, source_manifest, evidence, baseline, edited, case, opts
        )
        _write_yaml(result_path, result)
        _write_yaml(evidence_path, result["reconciled_evidence"])
        state.update({
            "state": "awaiting_journey_confirmation",
            "stages": {"journey_recheck": "complete"},
            "usage": {"journey_recheck": usage},
            "result_file": "journey_recheck.yaml",
            "evidence_file": "evidence.yaml",
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        _persist(state_path, state)
        print(f"歷程重查完成，待研究者確認：{result_path}")
        return 0
    except Exception as exc:
        state.update({
            "state": "failed",
            "stages": {"journey_recheck": "failed"},
            "failure": {"stage": "journey_recheck", "message": str(exc)},
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        if isinstance(exc, llm.ValidatedJSONError):
            state["attempts"] = exc.attempts
        _persist(state_path, state)
        print(f"歷程重查失敗；未產生可確認結果：{exc}", file=sys.stderr)
        return 1


# add-standalone-compare-structure-and-journey-commands：run-state-isolation。
# _main_generate 自己會寫的 run.json 鍵——組完新 state dict 之後，只有不在這個集合
# 裡的 previous_state 既有欄位（獨立指令各自擁有的 <stage>_state／<stage>_failure，
# 以及 primary_hmw_decision、hmw_wording_edit 這類研究者裁定的 provenance 記錄）
# 才會被帶到這一輪。清單本身要跟著 _main_generate 函式體維護，不是自動推導——
# 新增一個正交欄位時記得回來加，design.md 的 Decisions 有逐一列出原始欄位的來源。
_GENERATE_OWNED_KEYS = frozenset({
    # 組出新 state dict 時的初始鍵
    "case_id", "prompt_hash", "prompt_sha256", "from_evidence", "providers", "models", "efforts",
    "stages", "stage_inputs", "usage", "state", "started_at", "active_stage",
    # 執行過程中陸續指派的鍵
    "artifact_registry", "artifact_versions",
    "briefing_state", "briefing_failure",
    "completed_at", "failure", "finished_at",
    "generation_state", "origin", "quality_state", "reference_state", "review_state",
})


def _main_generate(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="sbp-gen", description="Action-graph-first SBP 生成")
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--input", type=Path)
    p.add_argument("--from-evidence", type=Path)
    p.add_argument("--discover-journey", action="store_true", help="從 evidence 提出 journey，寫檔後停在 awaiting_journey")
    p.add_argument("--journey-file", type=Path, help="研究者確認過的 journey v2 YAML")
    p.add_argument("--fresh", action="store_true",
                   help="每個階段都重跑，不重用上次的結果（預設會在輸入沒變時重用）")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument(
        "--artifact-registry",
        type=Path,
        default=None,
        help="canonical artifact lifecycle registry (default: hashed case key under out parent)",
    )
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--researcher-note", default=None)
    for stage in MODEL_STAGES:
        p.add_argument(
            f"--{stage}-provider",
            choices=llm.PROVIDERS,
            default=None,
            help=f"{stage} 階段使用的 provider（預設沿用 Anthropic）",
        )
        p.add_argument(f"--{stage}-model", default=None)
        p.add_argument(f"--{stage}-effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    # Compatibility alias: existing automation can still specify build model/effort.
    # reconcile 不在這條指令的關鍵路徑了（streamline-sbp-generation-pipeline 決定一），
    # 這個別名只還餵給 structure——它是現在關鍵路徑裡最重的推理階段，跟舊版「build」
    # 這個名字指的其實是同一件事。
    p.add_argument("--build-model", default=None, help="相容別名：供 structure 使用")
    p.add_argument("--build-effort", choices=["low", "medium", "high", "xhigh", "max"], default=None, help="相容別名")
    args = p.parse_args(argv)
    if not args.input and not args.from_evidence and not (args.journey_file and (args.out / "evidence.yaml").exists()):
        p.error("--input 與 --from-evidence 至少要給一個")

    case = yaml.safe_load(args.case.read_text(encoding="utf-8"))
    case_errors = V.structural_errors(case, V.load_schema("case")) + V.case_errors(case)
    if case_errors:
        raise SystemExit("案例邊界契約無效（尚未呼叫模型）：\n" + "\n".join(case_errors))

    opts = {"max_tokens": args.max_tokens}
    for stage in MODEL_STAGES:
        opts[f"{stage}_provider"] = getattr(args, f"{stage}_provider")
        opts[f"{stage}_model"] = getattr(args, f"{stage}_model")
        opts[f"{stage}_effort"] = getattr(args, f"{stage}_effort")
    opts["structure_model"] = opts["structure_model"] or args.build_model
    opts["structure_effort"] = opts["structure_effort"] or args.build_effort

    if args.dry_run:
        print("[dry-run] 案例邊界已通過；不呼叫 API\n")
        for stage in MODEL_STAGES:
            provider = llm.resolve_provider(opts[f"{stage}_provider"], stage=stage)
            model = opts[f"{stage}_model"] or llm.default_model(stage, provider)
            print(f"  {stage:10s} provider={provider:9s} model={model:18s} effort={llm.effort_for_record(stage, provider, opts[f'{stage}_effort'])}")
        print(f"\n  prompt_hash = {_prompt_hash()}")
        print(f"  步驟軸 {len(case['steps'])} 步 × {len(stages.LAYERS)} 層 = {len(case['steps']) * len(stages.LAYERS)} 格")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    # 上一次的紀錄要在覆寫 run.json 之前讀出來——階段能不能重用就是靠比對它。
    run_json_path = args.out / "run.json"
    previous_state = {}
    if run_json_path.is_file():
        try:
            previous_state = json.loads(run_json_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 讀不起來就當作沒有上一次，全部重跑
            previous_state = {}
    providers = {s: llm.resolve_provider(opts[f"{s}_provider"], stage=s) for s in MODEL_STAGES}
    efforts = {s: llm.effort_for_record(s, providers[s], opts[f"{s}_effort"]) for s in MODEL_STAGES}
    models = {s: {
        "provider": providers[s],
        "model": opts[f"{s}_model"] or llm.default_model(s, providers[s]),
        "effort": efforts[s],
    } for s in MODEL_STAGES}
    prompt_digest = _prompt_sha256()
    state = {"case_id": case["case_id"], "prompt_hash": prompt_digest[:12], "prompt_sha256": prompt_digest, "from_evidence": str(args.from_evidence) if args.from_evidence else None, "providers": providers, "models": models, "efforts": efforts, "stages": {}, "stage_inputs": {}, "usage": {}, "state": "running", "active_stage": "align", "started_at": dt.datetime.now().isoformat(timespec="seconds")}
    # run-state-isolation：只帶走 previous_state 裡不屬於這條指令的既有欄位——
    # 獨立指令（how-might-we／persona-content／primary-hmw⋯）各自擁有的
    # <stage>_state／<stage>_failure，以及研究者裁定寫進去的 provenance 記錄，
    # 都不在 _GENERATE_OWNED_KEYS 裡，會原樣存活。這條指令自己擁有的鍵（包含
    # 只在成功路徑才會被設定的 generation_state／review_state／quality_state／
    # reference_state）一律不帶，讓它們照這一輪的實際結果重新決定要不要存在，
    # 不會讓失敗重跑保留上一輪成功時的舊值。
    carried = {k: v for k, v in previous_state.items() if k not in _GENERATE_OWNED_KEYS}
    state = {**carried, **state}
    _persist(args.out / "run.json", state)

    def record_active_stage(name: str) -> None:
        state["active_stage"] = name
        _persist(args.out / "run.json", state)

    try:
        if args.from_evidence:
            evidence = yaml.safe_load(args.from_evidence.read_text(encoding="utf-8"))
            # 證據也要落在 run 目錄裡，跟 align 那條路一致。下游（as_is_research
            # .resolve_baseline）讀的是 `<run>/evidence.yaml`，所以少寫這一份，
            # 「開始提案」就對每一個用現成證據檔跑的案例失效——而真案例正好都走
            # 這條路，因為只有它們有整理好的證據檔可以指定。
            _write_yaml(args.out / "evidence.yaml", evidence)
            state["stages"]["align"] = "skipped"
        elif not args.input and (args.out / "evidence.yaml").exists():
            evidence = yaml.safe_load((args.out / "evidence.yaml").read_text(encoding="utf-8"))
            state["stages"]["align"] = "resumed"
        else:
            # align 一定要跟著做指紋，否則整條快取是白做的：給了 --input 就會重跑
            # align，而模型輸出本來就有隨機性，證據一變下游指紋全變。實測過——
            # 第二次同樣的輸入照樣跑滿 37 秒、節點數還從 14 變 16。
            raw_text, manifest = _read_raw(args.input)
            _write_yaml(args.out / "source_manifest.yaml", manifest)
            align_print = _stage_fingerprint("align", {"raw_material": raw_text}, opts)
            evidence = None if args.fresh else _reuse_stage(
                args.out, previous_state, "align", align_print, "evidence.yaml")
            if evidence is None:
                evidence, usage = stages.align(raw_text, opts)
                _write_yaml(args.out / "evidence.yaml", evidence)
                state["stages"]["align"], state["usage"]["align"] = "complete", usage
            else:
                state["stages"]["align"] = "reused"
                print("重用既有的證據（原始資料沒變）")
            state["stage_inputs"]["align"] = align_print
        _persist(args.out / "run.json", state)

        record_active_stage("journey")
        if args.journey_file:
            journey_data = yaml.safe_load(args.journey_file.read_text(encoding="utf-8"))
            errors = stages._journey_errors(journey_data, {e["id"] for e in evidence}, require_confirmed=True)
            if errors:
                raise ValueError("確認歷程無效：\n" + "\n".join(errors))
            _write_yaml(args.out / "journey.yaml", journey_data)
            state["stages"]["journey"] = "confirmed"
        elif args.discover_journey:
            journey_data, usage = stages.journey(evidence, case, opts)
            _write_yaml(args.out / "journey_proposal.yaml", journey_data)
            state["stages"]["journey"], state["usage"]["journey"] = "complete", usage
            state["state"] = "awaiting_journey"
            state.pop("active_stage", None)
            state["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            _persist(args.out / "run.json", state)
            print(f"歷程待確認：{args.out / 'journey_proposal.yaml'}")
            return 0
        else:
            journey_data = stages.journey_from_case(case)
            _write_yaml(args.out / "journey.yaml", journey_data)
            state["stages"]["journey"] = "case_confirmed"
        runtime_case = stages.case_with_journey(case, journey_data)
        # 讓 run 目錄自己帶完整案例邊界，`sbp-gen reference --run <dir>` 事後補跑時
        # 不用依賴原始 --case 檔案的路徑還存在（design.md 決定一）。
        _write_yaml(args.out / "case.yaml", runtime_case)
        _persist(args.out / "run.json", state)

        record_active_stage("compare")
        compare_print = _stage_fingerprint("compare", {"evidence": evidence}, opts)
        conflicts = None if args.fresh else _reuse_stage(
            args.out, previous_state, "compare", compare_print, "conflicts.yaml")
        if conflicts is None:
            conflicts, usage = stages.compare(evidence, opts)
            _write_yaml(args.out / "conflicts.yaml", conflicts)
            state["stages"]["compare"], state["usage"]["compare"] = "complete", usage
        else:
            state["stages"]["compare"] = "reused"
            print("重用既有的說法比對結果（輸入沒變）")
        state["stage_inputs"]["compare"] = compare_print
        _persist(args.out / "run.json", state)

        record_active_stage("structure")
        structure_print = _stage_fingerprint("structure", {
            "evidence": evidence, "conflicts": conflicts, "case": runtime_case,
            "journey": journey_data, "researcher_note": args.researcher_note,
        }, opts)
        graph = None if args.fresh else _reuse_stage(
            args.out, previous_state, "structure", structure_print, "service_graph.yaml")
        if graph is None:
            graph, usage = stages.structure(evidence, conflicts, runtime_case, opts, args.researcher_note, journey_data)
            _write_yaml(args.out / "service_graph.yaml", graph)
            state["stages"]["structure"], state["usage"]["structure"] = "complete", usage
        else:
            state["stages"]["structure"] = "reused"
            print("重用既有的現況藍圖（輸入沒變）")
        state["stage_inputs"]["structure"] = structure_print
        _persist(args.out / "run.json", state)
        graph_warnings = V.graph_warnings(
            graph, {e["id"] for e in evidence}, {e["id"]: e.get("text", "") for e in evidence}, journey=journey_data,
            evidence_tiers={e["id"]: e.get("tier") for e in evidence},
        )
        for warning in graph_warnings:
            print(f"  [structure] ! {warning}", file=sys.stderr)

        record_active_stage("projection")
        draft = stages.project(graph, None, conflicts, runtime_case, journey_data=journey_data, annotations=graph.get("annotations"), evidence=evidence)
        draft["meta"].update({"prompt_hash": _prompt_hash(), "timestamp": dt.datetime.now().strftime("%Y-%m-%d")})
        errors = V.structural_errors(draft, V.load_schema("sbp-draft")) + V.projection_errors(draft, {e["id"] for e in evidence}, runtime_case, journey=journey_data)
        if errors:
            raise ValueError("投影契約無效：\n" + "\n".join(errors))
        _write_yaml(args.out / "sbp_draft.yaml", draft)
        # Run artifact manifest gate（design.md 決定四）：標記完成前檢查這個 run 宣稱完成的
        # 必要產物是不是真的都在磁碟上。防的是「前面階段有寫但漏寫某一個檔案」的邏輯錯誤，
        # 不是防 API 呼叫失敗（那條路徑已經被這個 try/except 擋住，走不到這裡）。
        required_artifacts = ("sbp_draft.yaml", "service_graph.yaml", "case.yaml", "run.json")
        missing = [name for name in required_artifacts if not (args.out / name).exists()]
        if missing:
            raise ValueError(f"標記 draft_complete 前發現缺少必要產物：{', '.join(missing)}")
        state["stages"]["projection"] = "complete"
        record_active_stage("publication")
        registry_path = args.artifact_registry or _default_artifact_registry(args.out, case["case_id"])
        try:
            publication = _publish_generation_artifacts(
                registry_path,
                case,
                evidence,
                journey_data,
                graph,
                dt.datetime.now().isoformat(timespec="seconds"),
                args.out / ".gates" / "ledger.yaml",
            )
        except Exception as publication_exc:
            state["stages"]["publication"] = "failed"
            raise RuntimeError(f"artifact lifecycle persistence failed: {publication_exc}") from publication_exc
        state["stages"]["publication"] = "complete"
        state["artifact_registry"] = storage_locations.normalized_registry_pointer(
            registry_path, args.out, storage_locations.runs_root_for_run(args.out)
        )
        state["artifact_versions"] = publication["version_ids"]
        # Persist committed identities while generation is still "running";
        # draft_complete is not observable until the registry commit is durable.
        _persist(args.out / "run.json", state)
        state["state"] = "draft_complete"
        # run.json 四個正交欄位（design.md 決定三）：generation_state 是 state 的正式化版本
        # （值域完全一樣），origin／review_state／quality_state 分開表達「是不是模擬產出」
        # 「研究者審過了沒」「品質檢查過了沒」，不再擠在同一個 state 欄位裡。
        state["generation_state"] = state["state"]
        state["origin"] = "api_run"
        # 任何一次成功寫出 draft_complete 都無條件回到 unreviewed，不管這個 out 目錄
        # 之前的 run.json 是什麼狀態——compare／structure 一定會重新呼叫模型，內容不保證
        # 跟上一輪相同，繼續沿用舊的 reviewed 只會製造「畫面講的跟系統做的對不上」
        # （allow-unmarking-reviewed-runs 決定三，取代 stabilize-sbp-task-flow-and-run-trust
        # 原本「保留不覆蓋」的判斷）。
        state["review_state"] = "unreviewed"
        state["quality_state"] = "checks_passed"
        state["reference_state"] = "not_run"

        # 簡報跑在關鍵路徑上，但非致命（add-research-briefing-layer design.md 決定一）：
        # 這個 try/except 跟外層的失敗處理完全分開——簡報失敗絕不能讓一份已經完成、
        # 可用的草稿被標記成 failed。任何失敗都要清掉舊 run 目錄可能留著的簡報檔，
        # 否則會變成一份描述舊草稿的簡報，悄悄留在新草稿旁邊。
        record_active_stage("brief")
        briefing_path = args.out / "briefing.yaml"
        try:
            role_coverage_facts = {
                "covered": role_coverage.covered_roles(evidence),
                "boundary": role_coverage.boundary_role_check(runtime_case, evidence),
                "citation": role_coverage.citation_coverage(evidence, draft),
            }
            briefing, brief_usage = stages.brief(draft, evidence, conflicts, role_coverage_facts, runtime_case, opts)
            _write_yaml(briefing_path, briefing)
            state["usage"]["brief"] = brief_usage
            state["briefing_state"] = "complete"
        except Exception as brief_exc:
            state["briefing_state"] = "failed"
            state["briefing_failure"] = str(brief_exc)
            if briefing_path.exists():
                briefing_path.unlink()

        state["completed_at"] = dt.datetime.now().isoformat(timespec="seconds")
        state.pop("active_stage", None)
        _persist(args.out / "run.json", state)
        print(f"完成：{args.out / 'sbp_draft.yaml'}（{len(draft['cells'])} cells、{len(graph['nodes'])} nodes）。"
              f"reference 分析尚未執行，需要時跑：sbp-gen reference --run {args.out}")
        return 0
    except Exception as exc:
        current = (
            "publication"
            if state["stages"].get("publication") == "failed"
            else next((s for s in PIPELINE if s not in state["stages"]), "projection")
        )
        state["stages"][current] = "failed"
        state["state"], state["failure"] = "failed", {"stage": current, "message": str(exc)}
        state["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        _persist(args.out / "run.json", state)
        print(f"生成失敗（{current}）；已保留完成的中間產物，沒有寫入完整 draft：{exc}", file=sys.stderr)
        return 1


def _main_reference(argv: list[str]) -> int:
    """`sbp-gen reference --run <dir>`——事後、選填的 reference/reconcile 補跑
    （streamline-sbp-generation-pipeline 決定一）。讀既有 run 目錄的 service_graph.yaml／
    case.yaml，把結果 in-place 寫回同一個 run 的 sbp_draft.yaml，不新建輸出目錄，不動
    run.json 的 `state`（草稿完成狀態），只更新獨立的 `reference_state` 欄位。
    """
    p = argparse.ArgumentParser(prog="sbp-gen reference", description="事後補跑 reference/reconcile，更新既有 run 的草稿")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    for stage in REFERENCE_PIPELINE:
        p.add_argument(f"--{stage}-model", default=None)
        p.add_argument(f"--{stage}-effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    graph_path = run_dir / "service_graph.yaml"
    case_path = run_dir / "case.yaml"
    draft_path = run_dir / "sbp_draft.yaml"
    for required_path in (graph_path, case_path, draft_path):
        if not required_path.exists():
            print(f"{required_path} 不存在，無法補跑 reference——先跑過主要生成指令，"
                  f"拿到完整的 run 目錄再回來補跑。", file=sys.stderr)
            return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    conflicts_path = run_dir / "conflicts.yaml"
    conflicts = yaml.safe_load(conflicts_path.read_text(encoding="utf-8")) if conflicts_path.exists() else []

    opts = {"max_tokens": args.max_tokens}
    for stage in REFERENCE_PIPELINE:
        opts[f"{stage}_model"] = getattr(args, f"{stage}_model")
        opts[f"{stage}_effort"] = getattr(args, f"{stage}_effort")

    run_state.setdefault("usage", {})
    try:
        hypotheses, usage = stages.reference(case, opts)
        run_state["usage"]["reference"] = usage
        reconciled, usage = stages.reconcile(graph, hypotheses, conflicts, case, opts)
        run_state["usage"]["reconcile"] = usage
        _write_yaml(run_dir / "reference_hypotheses.yaml", {**reconciled, "raw_expectations": hypotheses.get("expectations", [])})

        draft["expectations"] = reconciled.get("expectations") or []
        errors = V.structural_errors(draft, V.load_schema("sbp-draft"))
        if errors:
            raise ValueError("補跑後的投影契約無效：\n" + "\n".join(errors))
        _write_yaml(draft_path, draft)

        run_state["reference_state"] = "complete"
        run_state["reference_prompt_hash"] = _prompt_hash(REFERENCE_PIPELINE)
        _persist(run_json_path, run_state)
        print(f"完成：{draft_path} 已更新 {len(draft['expectations'])} 則 expectations")
        return 0
    except Exception as exc:
        run_state["reference_state"] = "failed"
        run_state["reference_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"reference 補跑失敗，草稿完成狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_implementation_research(argv: list[str]) -> int:
    """`sbp-gen implementation-research --run <dir>`——pre-blueprint-implementation-research：
    研究者在服務藍圖站勾選落地研究問題、按「開始落地研究」時執行的那一個工作。
    只跑目前這組已採用解方與勾選問題的作業（伺服器已先寫好 running 的作業紀錄），
    結果是 complete／no_admissible／failed 之一，寫在作業目錄裡；不建立 SBP。"""
    from . import implementation_research
    p = argparse.ArgumentParser(prog="sbp-gen implementation-research", description="建立 SBP 前的落地研究")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    args = p.parse_args(argv)
    try:
        operation = implementation_research.run(args.run_dir)
    except implementation_research.ImplementationResearchError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    state = operation.get("state")
    print(f"落地研究：{state}")
    return 1 if state == implementation_research.FAILED else 0


def _main_as_is_research(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="sbp-gen as-is-research",
        description="從已確認且 current 的現況藍圖建立問題包與受限外部調研",
    )
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--registry", required=True, type=Path)
    p.add_argument("--as-is-version", required=True)
    args = p.parse_args(argv)
    try:
        operation, reused = as_is_research.execute_operation(
            args.run_dir,
            registry_path=args.registry,
            expected_as_is_version=args.as_is_version,
        )
    except as_is_research.ResearchOperationError as exc:
        payload = {
            "kind": as_is_research.OPERATION_KIND,
            "state": "failed",
            "phase": "deriving_issues",
            "retryable": exc.retryable,
            "request_digest": None,
            "counts": {"issues": 0, "sources": 0, "claims": 0},
            "result": {},
            "error": {"code": exc.code, "user_message": exc.user_message},
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    payload = as_is_research.project_operation(operation)
    payload["reused"] = reused
    stream = sys.stdout if operation.get("state") == "done" else sys.stderr
    print(json.dumps(payload, ensure_ascii=False), file=stream)
    return 0 if operation.get("state") == "done" and operation.get("phase") in as_is_research.TERMINAL_PHASES else 1


def _main_to_be(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sbp-gen to-be",
        description="從已提交的內部 handoff 建立一份 evidence-grounded To-be 方案",
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--handoff", required=True)
    args = parser.parse_args(argv)
    try:
        handoff = json.loads(args.handoff)
        if not isinstance(handoff, dict):
            raise ValueError("handoff must be an object")
        operation, reused = to_be.execute_operation(
            args.run_dir,
            registry_path=args.registry,
            handoff=handoff,
        )
    except (ValueError, json.JSONDecodeError, to_be.ToBeOperationError) as exc:
        if isinstance(exc, to_be.ToBeOperationError):
            code, message, retryable = exc.code, exc.user_message, exc.retryable
        else:
            code = "missing_to_be_baseline"
            message, retryable = to_be.ERROR_MESSAGES[code]
        print(json.dumps({
            "kind": to_be.OPERATION_KIND,
            "state": "failed",
            "phase": "synthesizing",
            "retryable": retryable,
            "error": {"code": code, "user_message": message},
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    payload = to_be.project_operation(operation)
    payload["reused"] = reused
    print(
        json.dumps(payload, ensure_ascii=False),
        file=sys.stdout if operation.get("state") == "done" else sys.stderr,
    )
    return 0 if operation.get("state") == "done" else 1


def _main_to_be_revision(argv: list[str]) -> int:
    """`sbp-gen to-be-revision`——ground-to-be-revision-in-prior-draft：讓一次
    以現行草稿為基準的修訂能完全走 CLI 完成。修訂原本只有 HTTP 端點可以觸發，
    而手動傳輸靠的是暫停後結束行程、再跑同一道指令續跑，HTTP 執行緒裡跑不出
    這個流程。`--decisions` 是選填的逐提案裁定 JSON 陣列；不給就是現行的純自
    由文字修訂。
    """
    parser = argparse.ArgumentParser(
        prog="sbp-gen to-be-revision",
        description="以目前的 To-be 草稿為基準，依研究者的調整說明產生替代方案",
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--decisions", default=None)
    args = parser.parse_args(argv)
    def _fail(code: str) -> int:
        message, retryable = to_be.ERROR_MESSAGES[code]
        print(json.dumps({
            "kind": to_be.OPERATION_KIND,
            "state": "failed",
            "phase": "synthesizing",
            "retryable": retryable,
            "error": {"code": code, "user_message": message},
        }, ensure_ascii=False), file=sys.stderr)
        return 1

    # Only the decisions argument is parsed here; a ValueError from deeper in
    # synthesis is not an invalid decision set and must not be labelled one.
    decisions = None
    if args.decisions is not None:
        try:
            decisions = json.loads(args.decisions)
        except json.JSONDecodeError:
            return _fail("invalid_revision_decisions")
        if not isinstance(decisions, list):
            return _fail("invalid_revision_decisions")
    try:
        operation, reused, classification = to_be.request_revision(
            args.run_dir,
            registry_path=args.registry,
            expected_fingerprint=args.fingerprint,
            instruction=args.instruction,
            decisions=decisions,
        )
    except to_be.ToBeOperationError as exc:
        message, retryable = exc.user_message, exc.retryable
        print(json.dumps({
            "kind": to_be.OPERATION_KIND,
            "state": "failed",
            "phase": "synthesizing",
            "retryable": retryable,
            "error": {"code": exc.code, "user_message": message},
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    payload = to_be.project_operation(operation)
    payload["reused"] = reused
    payload["revision_classification"] = classification
    print(
        json.dumps(payload, ensure_ascii=False),
        file=sys.stdout if operation.get("state") == "done" else sys.stderr,
    )
    return 0 if operation.get("state") == "done" else 1


def _main_coding_candidates(argv: list[str]) -> int:
    """Run the first research action without entering Journey or Blueprint work."""
    p = argparse.ArgumentParser(prog="sbp-gen coding-candidates")
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--max-tokens", type=int, default=32000)
    args = p.parse_args(argv)

    case = yaml.safe_load(args.case.read_text(encoding="utf-8"))
    if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"].strip():
        print("專案缺少有效的 case_id，編碼未開始。", file=sys.stderr)
        return 1
    raw_text, manifest = _read_raw(args.input)
    source_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    state_path = args.out / "run.json"
    if args.out.exists() and any(args.out.iterdir()) and not state_path.is_file():
        print("輸出位置已存在但不是可續作的編碼 run。", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if state and (state.get("case_id") != case["case_id"] or state.get("source_digest") != source_digest):
        print("案例或原始資料已變更；請建立新的編碼 run，不覆寫舊研究產物。", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)

    def update_state(**fields: object) -> dict:
        current = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        current.update(fields)
        _persist(state_path, current)
        return current

    if not state:
        _write_yaml(args.out / "case.yaml", case)
        _write_yaml(args.out / "source_manifest.yaml", manifest)
        update_state(
            case_id=case["case_id"], selected_case=str(args.case),
            source_digest=source_digest, coding_state="running", stages={},
            started_at=dt.datetime.now().isoformat(timespec="seconds"),
        )

    stages_to_run = (
        ("align", "evidence.yaml", None, None),
        # open-coding-on-semantic-segments: open coding reads semantic segments.
        ("semantic_segments", "semantic_segments.yaml", "semantic_segments_state", _main_semantic_segments),
        ("open_coding", "open_coding.yaml", "open_coding_state", _main_open_coding),
        ("axial_coding", "axial_coding.yaml", "axial_coding_state", _main_axial_coding),
        # add-selective-coding-layer: formed only when the run has a research
        # purpose; otherwise recorded as needs_purpose and coding carries on.
        ("selective_themes", "selective_themes.yaml", "selective_themes_state", _main_selective_themes),
        ("narrative_jtbd", "narrative_job_candidates.yaml", "narrative_job_candidates_state", _main_narrative_job_candidates),
    )
    from . import selective_themes as selective_module
    selective_module.copy_purpose_into_run(Path(args.case).parent, args.out)
    for stage_name, artifact_name, stage_key, runner in stages_to_run:
        current = json.loads(state_path.read_text(encoding="utf-8"))
        if stage_name == "selective_themes" and not selective_module.read_purpose(args.out):
            update_state(selective_themes_state="needs_purpose")
            continue
        # open-coding-on-semantic-segments: segments only feed open coding, so
        # a run whose open coding is already complete (e.g. an older run being
        # retried) is not segmented after the fact.
        if stage_name == "semantic_segments" and current.get("open_coding_state") == "complete" \
                and (args.out / "open_coding.yaml").is_file():
            continue
        # multi-jtbd-single-job-source: candidates come from Selective Coding
        # only; without it the run waits for the researcher, it does not fail.
        if stage_name == "narrative_jtbd" and not (args.out / selective_module.THEMES_FILE).is_file():
            update_state(narrative_job_candidates_state="needs_selective")
            continue
        complete = (current.get("stages") or {}).get("align") == "complete" if stage_name == "align" else current.get(stage_key) == "complete"
        if complete and (args.out / artifact_name).is_file():
            continue
        update_state(coding_state="running", active_stage=stage_name, coding_failure=None)
        try:
            if stage_name == "align":
                # Rejected attempts are kept for diagnosis beside the runs, never
                # inside this run's research artifacts.
                diagnostics_dir = (args.out.parent / "_diagnostics" / args.out.name
                                   / f"align-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}")
                evidence, usage = stages.align(raw_text, {"case_id": case["case_id"], "max_tokens": args.max_tokens,
                                                          "diagnostics_dir": str(diagnostics_dir)})
                _write_yaml(args.out / artifact_name, evidence)
                current = json.loads(state_path.read_text(encoding="utf-8"))
                current.setdefault("stages", {})["align"] = "complete"
                current.setdefault("usage", {})["align"] = usage
                _persist(state_path, current)
                status = 0
            else:
                status = runner(["--run", str(args.out)])
            if status != 0:
                current = json.loads(state_path.read_text(encoding="utf-8"))
                failure_key = stage_key.removesuffix("_state") + "_failure"
                cause = current.get(failure_key) or f"{stage_name} 沒有完成（exit {status}）。"
                update_state(coding_state="failed", coding_failure=str(cause), active_stage=None)
                return status
        except Exception as exc:
            update_state(coding_state="failed", coding_failure=str(exc), active_stage=None)
            print(f"{stage_name} 失敗：{exc}", file=sys.stderr)
            return 1

    update_state(
        coding_state="awaiting_narrative_jtbd", active_stage=None,
        coding_failure=None, finished_at=dt.datetime.now().isoformat(timespec="seconds"),
    )
    print("編碼與敘事／顧客任務候選已完成，等待研究者選擇。")
    return 0


def _main_semantic_segments(argv: list[str]) -> int:
    """`sbp-gen semantic-segments --run <dir> [--input <raw dir>] [--review <xlsx>]`
    （open-coding-on-semantic-segments，2026-10-03）：每份逐字稿依時間序切成
    語意段落，寫 `semantic_segments.yaml`；段落原文由程式依行號從原始逐字稿
    切出，完整性由程式檢查。原始逐字稿預設讀這份 run 的案例資料夾的 raw/，
    內容必須跟這份 run 編碼時讀的是同一版。`--review` 只寫驗收用的唯讀
    檢視檔，不重新分段。只更新 run.json 自己的 `semantic_segments_state`。"""
    from . import semantic_segments
    p = argparse.ArgumentParser(prog="sbp-gen semantic-segments", description="把每份逐字稿切成語意段落")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--input", type=Path, default=None)
    p.add_argument("--review", type=Path, default=None)
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    if args.review is not None:
        doc = semantic_segments.load(run_dir)
        if doc is None:
            print(f"{run_dir} 還沒有語意段落，無法寫檢視檔。", file=sys.stderr)
            return 1
        open_doc = yaml.safe_load((run_dir / "open_coding.yaml").read_text(encoding="utf-8")) \
            if (run_dir / "open_coding.yaml").is_file() else None
        target = semantic_segments.render_review(doc, open_doc, args.review)
        print(f"完成：{target}")
        return 0
    evidence, evidence_status = evidence_locator.locate_evidence(run_dir)
    if evidence is None:
        print(f"{run_dir} 找不到可用的證據檔（{evidence_status}），無法分段。", file=sys.stderr)
        return 1
    raw_dir = args.input
    if raw_dir is None and run_state.get("selected_case"):
        raw_dir = Path(run_state["selected_case"]).parent / "raw"
    if raw_dir is None or not raw_dir.is_dir():
        print(f"{run_dir} 找不到原始逐字稿資料夾，請用 --input 指定。", file=sys.stderr)
        return 1
    raw_text, _manifest = _read_raw(raw_dir)
    recorded = run_state.get("source_digest")
    if recorded and recorded != hashlib.sha256(raw_text.encode("utf-8")).hexdigest():
        print("原始逐字稿跟這份 run 編碼時讀的不是同一版，不分段。", file=sys.stderr)
        return 1
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case_id = (yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}).get("case_id") or case_id
    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "semantic_segments_model": args.model, "semantic_segments_effort": args.effort}
    try:
        data, usage = semantic_segments.generate(raw_text, evidence, opts)
        _write_yaml(run_dir / semantic_segments.SEGMENTS_FILE, data)
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        run_state.setdefault("usage", {})["semantic_segments"] = usage
        run_state["semantic_segments_state"] = "complete"
        run_state.pop("semantic_segments_failure", None)
        _persist(run_json_path, run_state)
        count = sum(len(t["segments"]) for t in data["transcripts"])
        print(f"完成：{run_dir / semantic_segments.SEGMENTS_FILE} 共 {count} 個語意段落")
        return 0
    except Exception as exc:
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        run_state["semantic_segments_state"] = "failed"
        run_state["semantic_segments_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"語意分段失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_open_coding(argv: list[str]) -> int:
    """`sbp-gen open-coding --run <dir>`：獨立命令也供 fresh coding 協調器使用。
    分析單位是語意段落（open-coding-on-semantic-segments），一份逐字稿一組，每組各
    呼叫一次模型，合併成一份 `open_coding.yaml`。任一組耗盡重試次數，整支
    指令回傳非零、不寫任何檔案（`stages.open_coding()` 本身即拋出例外，這裡
    只需要不要在例外路徑上寫檔）。只更新 run.json 自己的 `open_coding_state`
    欄位，不動主管線的 `state` 或其他任何欄位——跟 axial-coding 是同一支
    CLI 的下游延伸，不是取代它。
    """
    p = argparse.ArgumentParser(prog="sbp-gen open-coding", description="把證據單元逐句轉成行為概念")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    evidence, evidence_status = evidence_locator.locate_evidence(run_dir)
    if evidence is None:
        print(f"{run_dir} 找不到可用的證據檔（{evidence_status}），無法跑開放編碼。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    # open-coding-on-semantic-segments: the analysis unit is the semantic
    # segment; without segments a new open coding is refused.
    from . import semantic_segments
    segments_doc = semantic_segments.load(run_dir)
    if segments_doc is None:
        print(f"{run_dir} 還沒有語意段落，先跑 `sbp-gen semantic-segments --run {run_dir}`。", file=sys.stderr)
        return 1
    # Rejected attempts are kept for diagnosis beside the runs, never inside
    # this run's research artifacts (same place as align's).
    diagnostics_dir = (run_dir.parent / "_diagnostics" / run_dir.name
                       / f"open-coding-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}")
    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "open_coding_model": args.model, "open_coding_effort": args.effort,
            "diagnostics_dir": str(diagnostics_dir)}
    try:
        data, usage = stages.open_coding(evidence, opts, segments_doc)
        _write_yaml(run_dir / "open_coding.yaml", data)
        run_state.setdefault("usage", {})["open_coding"] = usage
        run_state["open_coding_state"] = "complete"
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'open_coding.yaml'} 產生 {len(data.get('concepts') or [])} 個概念")
        return 0
    except Exception as exc:
        run_state["open_coding_state"] = "failed"
        run_state["open_coding_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"開放編碼失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_category_properties(argv: list[str]) -> int:
    """`sbp-gen category-properties --run <dir>`——derive-axis-candidates-
    from-properties：獨立、選填的屬性與面向補跑，接在主軸編碼之後、選維度
    之前。讀 axial_coding.yaml 與（透過 evidence_locator 定位的）證據，寫
    category_properties.yaml。只更新 run.json 自己的
    `category_properties_state` 欄位，不動主管線的 `state` 或其他任何欄位。
    """
    p = argparse.ArgumentParser(prog="sbp-gen category-properties", description="從主軸編碼類別讀出屬性與面向")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    axial_path = run_dir / "axial_coding.yaml"
    if not axial_path.exists():
        print(f"{axial_path} 不存在，無法跑屬性與面向——先跑過 "
              f"`sbp-gen axial-coding --run {run_dir}`。", file=sys.stderr)
        return 1
    evidence, evidence_status = evidence_locator.locate_evidence(run_dir)
    if evidence is None:
        print(f"{run_dir} 找不到可用的證據檔（{evidence_status}），無法跑屬性與面向。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    axial_data = yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "category_properties_model": args.model, "category_properties_effort": args.effort}
    try:
        data, usage = stages.category_properties(axial_data, evidence, opts)
        _write_yaml(run_dir / "category_properties.yaml", data)
        # Lens steps can run concurrently in HMW formation: write only this
        # step's own keys, never the whole state read at the start.
        run_state_file.record_stage(run_json_path, "category_properties", state_key="category_properties_state",
                                    failure_key="category_properties_failure", usage=usage)
        print(f"完成：{run_dir / 'category_properties.yaml'} 產生 {len(data.get('properties') or [])} 個屬性")
        return 0
    except Exception as exc:
        run_state_file.record_stage(run_json_path, "category_properties", state_key="category_properties_state",
                                    failure_key="category_properties_failure", failure=str(exc))
        print(f"屬性與面向失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_axial_coding(argv: list[str]) -> int:
    """`sbp-gen axial-coding --run <dir>`：可獨立補跑，也由 fresh coding 協調器呼叫。
    讀 evidence.yaml
    （驗證用）跟 open_coding.yaml（分析輸入），只寫 axial_coding.yaml，只更新
    run.json 自己的 `axial_coding_state` 欄位，不動主管線的 `state` 或其他
    任何欄位。

    `open_coding.yaml` 不存在或裡面沒有任何概念都直接擋下來，不落回舊的
    「直接吃證據原話」路徑——研究者無法從產出的檔案本身分辨兩種來源，見
    design.md「The command refuses rather than falling back when open
    coding is absent」。
    """
    p = argparse.ArgumentParser(prog="sbp-gen axial-coding", description="把開放編碼概念聚成有解釋力的類別")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法跑主軸編碼——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1

    open_coding_path = run_dir / "open_coding.yaml"
    if not open_coding_path.exists():
        print(f"{open_coding_path} 不存在，無法跑主軸編碼——先跑 "
              f"`sbp-gen open-coding --run {run_dir}` 產生開放編碼概念，再回來跑。", file=sys.stderr)
        return 1
    open_coding_doc = yaml.safe_load(open_coding_path.read_text(encoding="utf-8")) or {}
    concepts = open_coding_doc.get("concepts") or []
    if not concepts:
        print(f"{open_coding_path} 沒有任何概念，無法跑主軸編碼——先跑 "
              f"`sbp-gen open-coding --run {run_dir}` 產生開放編碼概念，再回來跑。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "axial_coding_model": args.model, "axial_coding_effort": args.effort}
    try:
        data, usage = stages.axial_coding(concepts, evidence, opts)
        _write_yaml(run_dir / "axial_coding.yaml", data)
        run_state.setdefault("usage", {})["axial_coding"] = usage
        run_state["axial_coding_state"] = "complete"
        # axial-label-quality-contract: label quality signals are recorded for
        # review, never a failure and never a rerun.
        signals = stages.axial_label_quality_signals(data)
        run_state["axial_coding_quality_signals"] = signals
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'axial_coding.yaml'} 產生 {len(data.get('categories') or [])} 個類別")
        for signal in signals:
            print(f"label 品質提醒（不影響完成）：{signal['signal']} {'、'.join(str(c) for c in signal['categories'])}")
        return 0
    except Exception as exc:
        run_state["axial_coding_state"] = "failed"
        run_state["axial_coding_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"主軸編碼失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_persona_matrix(argv: list[str]) -> int:
    """`sbp-gen persona-matrix --run <dir>`——add-persona-matrix：獨立、選填
    的 Persona Matrix 補跑，不進主管線（design.md「`persona-matrix` is a
    standalone CLI command consuming two existing files, not a pipeline
    stage」）。讀 evidence.yaml 跟 axial_coding.yaml 兩個檔案，只寫
    persona_matrix.yaml，只更新 run.json 自己的 `persona_matrix_state`
    欄位，不動主管線的 `state` 或 `axial_coding_state` 或任何其他欄位。若
    run 目錄有 selective_coding.yaml，讀進來的核心類別只當排序／加權訊號
    （core-category-as-ranking-signal-not-filter，見 `_resolve_core_
    category`）——類別與證據完整不縮減送給模型，這個訊號只是多一段提示，
    要模型對核心類別與其他類別的關聯特別留意，不會因此略過任何類別。帶
    `--show` 是唯讀模式（enforce-axis-quality-
    criteria-in-persona-matrix）：讀既有的 persona_matrix.yaml 跟完整的
    axial_coding.yaml（不縮減），把 `persona_matrix_warnings()` 的判準發現
    逐軸、逐受訪者攤開印出來，全程不呼叫模型——`persona_matrix_warnings()`
    產生的發現在這之前沒有任何生產端呼叫者，只有測試看得到，等於判準做了但
    研究者看不到。跟 `sbp-gen selective-coding` 的 show 模式同一種形狀。**不
    縮減是刻意的**：`persona_matrix.yaml` 裡已經存在的軸，是在縮減政策生效
    之前或之後產生的都可能發生（例如 2026-08-21-manual-transport-chongzheng
    是縮減功能上線前的產物，軸的 grounded_in 橫跨全部 6 個類別），對這份
    「已經寫在磁碟上的文件」做完整性檢查，答案只能是「這個 id 在這個 run 的
    類別清單裡存不存在」，縮減後的子集不是這個問題的正確分母——用縮減後的
    子集查會把橫跨多個類別的舊軸全部誤判成「沒有對應到類別」。

    若 run 目錄有 `persona_dimensions_selection.yaml`（connect-dimension-
    and-job-selections-to-downstream），讀進來的軸會經 `_read_dimension_
    selection` 對照**完整**的類別清單（不是縮減後的子集，理由同上一段）
    確認沒有過期，再交給 `stages.persona_matrix` 逐字沿用——這個階段不再
    自己造軸，只做落點判斷。缺檔、壞檔、或選到的類別已經不存在，都會靜默
    退回成沒有選擇時的行為，不擋這次生成。
    """
    p = argparse.ArgumentParser(prog="sbp-gen persona-matrix", description="把主軸編碼類別讀成行為變項軸與受訪者落點")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--show", action="store_true")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    axial_coding_path = run_dir / "axial_coding.yaml"
    if not axial_coding_path.exists():
        print(f"{axial_coding_path} 不存在，無法跑 Persona Matrix——先跑過 "
              f"`sbp-gen axial-coding --run {run_dir}`。", file=sys.stderr)
        return 1

    if args.show:
        persona_matrix_path = run_dir / "persona_matrix.yaml"
        if not persona_matrix_path.exists():
            print(f"{persona_matrix_path} 不存在，無法顯示 Persona Matrix 判準發現——先跑過 "
                  f"`sbp-gen persona-matrix --run {run_dir}`。", file=sys.stderr)
            return 1
        categories_doc = yaml.safe_load(axial_coding_path.read_text(encoding="utf-8")) or {}
        persona_doc = yaml.safe_load(persona_matrix_path.read_text(encoding="utf-8")) or {}
        # flag-persona-matrix-subject-identity-conflicts: subject identity
        # is resolved from evidence.yaml's source strings, not from
        # anything already in persona_doc/categories_doc -- evidence.yaml
        # is guaranteed present here (Persona Matrix's own prerequisite is
        # axial_coding.yaml, which is itself derived from evidence.yaml).
        evidence_path = run_dir / "evidence.yaml"
        evidence_doc = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or [] if evidence_path.exists() else []
        findings = V.persona_matrix_warnings(persona_doc, categories_doc, evidence_doc)
        for axis in persona_doc.get("axes") or []:
            if not isinstance(axis, dict):
                continue
            axis_id = axis.get("id")
            print(f"{axis_id}\t{axis.get('label')}")
            print(f"  區辨依據：{axis.get('discriminating_basis')}")
            matches = [f for f in findings if f"軸「{axis_id}」" in f or f"軸「{axis_id} " in f]
            if matches:
                for m in matches:
                    print(f"  {m}")
            else:
                print("  （沒有相關的判準發現）")
        for subject in persona_doc.get("subjects") or []:
            if not isinstance(subject, dict):
                continue
            subject_id = subject.get("id")
            print(f"受訪者 {subject_id}")
            matches = [f for f in findings if f"受訪者「{subject_id}」" in f]
            if matches:
                for m in matches:
                    print(f"  {m}")
            else:
                print("  （沒有相關的判準發現）")
        return 0

    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法跑 Persona Matrix——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    categories_doc_full = yaml.safe_load(axial_coding_path.read_text(encoding="utf-8")) or {}
    full_category_ids = {c.get("id") for c in (categories_doc_full.get("categories") or [])
                         if isinstance(c, dict) and c.get("id")}
    core_category = _resolve_core_category(run_dir, categories_doc_full)
    selected_axes = _read_dimension_selection(run_dir, full_category_ids)
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "persona_matrix_model": args.model, "persona_matrix_effort": args.effort}
    try:
        scope = hmw_scope_module.resolve(run_dir)
        data, usage = stages.persona_matrix(
            evidence, categories_doc_full, opts, selected_axes=selected_axes, core_category=core_category,
            hmw_scope=scope,
        )
        if scope:
            data["framed_by"] = {"primary_hmw_selection": hmw_scope_module.digest(scope)}
        _write_yaml(run_dir / "persona_matrix.yaml", data)
        run_state.setdefault("usage", {})["persona_matrix"] = usage
        run_state["persona_matrix_state"] = "complete"
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'persona_matrix.yaml'} 產生 {len(data.get('axes') or [])} 條軸、"
              f"{len(data.get('subjects') or [])} 位受訪者")
        return 0
    except Exception as exc:
        run_state["persona_matrix_state"] = "failed"
        run_state["persona_matrix_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"Persona Matrix 失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_persona_content(argv: list[str]) -> int:
    """`sbp-gen persona-content --run <dir>`——generate-persona-for-target-
    quadrant：獨立、選填的 Persona 內容補跑，不進主管線。讀 evidence.yaml、
    persona_matrix.yaml、segmentation_axis_selection.yaml、
    target_segmentation_selection.yaml（格子的兩端由 matrix 與選定兩軸算出，
    不讀四格畫像檔），
    只寫 persona_content.yaml，只更新 run.json 自己的 `persona_content_state`
    欄位，不動主管線的 `state` 或其他任何欄位。

    只為研究者指定的主要目標象限產生一份 persona，不是四象限各一份。沒有
    `primary_quadrant_id`、或那一格沒有任何確認過的成員時拒絕並指出要先
    完成哪個決定，不退回用候選檔的建議名單（空格子本來就不能被指定為主要
    目標，見 target-segmentation-designation，這裡的檢查是防禦性的，涵蓋
    這條規則生效之前寫下的選擇檔）。

    axial_coding.yaml 盡力讀取，只用來把兩條選定軸的 grounded_in 類別 id
    解析成 label；讀不到就留空 label，不因為它缺席而拒絕執行——這份檔案是
    persona_matrix.yaml 產生鏈更早一步的必要輸入，存在 persona_matrix.yaml
    的地方幾乎必然存在它。
    """
    p = argparse.ArgumentParser(prog="sbp-gen persona-content", description="把研究者指定的主要目標象限寫成一份有名字有理由的 persona")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法跑 Persona 內容——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1
    persona_matrix_path = run_dir / "persona_matrix.yaml"
    if not persona_matrix_path.exists():
        print(f"{persona_matrix_path} 不存在，無法跑 Persona 內容——先跑過 "
              f"`sbp-gen persona-matrix --run {run_dir}`。", file=sys.stderr)
        return 1
    axis_selection_path = run_dir / "segmentation_axis_selection.yaml"
    if not axis_selection_path.exists():
        print(f"{axis_selection_path} 不存在，無法跑 Persona 內容——先跑過 "
              f"`sbp-gen segmentation-axes --run {run_dir} --selection-file <file>` 選定兩條軸。", file=sys.stderr)
        return 1
    target_selection_path = run_dir / "target_segmentation_selection.yaml"
    if not target_selection_path.exists():
        print(f"{target_selection_path} 不存在，無法跑 Persona 內容——研究者要先在工作台完成象限成員的指派"
              f"與主要目標的指定，不會退回用候選檔的建議名單。", file=sys.stderr)
        return 1
    if not gates.is_derivation_runnable(run_dir, "persona_content", now=dt.datetime.now().isoformat(timespec="seconds")):
        print("目標族群（主要/次要目標）還沒確認過目前這一版，Persona 內容暫時不能生成——"
              f"先跑 `sbp-gen target-segmentation --run {run_dir} --selection-file <path>` 重新確認。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    persona_matrix_doc = yaml.safe_load(persona_matrix_path.read_text(encoding="utf-8")) or {}
    axis_selection_doc = yaml.safe_load(axis_selection_path.read_text(encoding="utf-8")) or {}
    target_selection_doc = yaml.safe_load(target_selection_path.read_text(encoding="utf-8")) or {}

    primary_quadrant_id = target_selection_doc.get("primary_quadrant_id")
    if not primary_quadrant_id:
        print(f"{target_selection_path} 沒有記錄主要目標象限——研究者要先在工作台指定主要目標，"
              f"不會退回用候選檔的建議名單。", file=sys.stderr)
        return 1
    confirmed_by_quadrant = {
        m.get("quadrant_id"): list(m.get("subject_ids") or [])
        for m in (target_selection_doc.get("quadrant_members") or [])
        if isinstance(m, dict) and m.get("quadrant_id")
    }
    if not confirmed_by_quadrant:
        print(f"{target_selection_path} 沒有任何一格記錄過確認的成員——先在工作台完成象限成員的指派，"
              f"不會退回用候選檔的建議名單。", file=sys.stderr)
        return 1
    primary_members = confirmed_by_quadrant.get(primary_quadrant_id) or []
    if not primary_members:
        print(f"主要目標象限 {primary_quadrant_id} 沒有任何確認過的成員——一個沒有人的格子不能被指定為"
              f"主要目標，先回工作台重新指定或補上成員指派。", file=sys.stderr)
        return 1
    axis_ids = list(axis_selection_doc.get("axis_ids") or [])
    axes_by_id = {a.get("id"): a for a in (persona_matrix_doc.get("axes") or []) if isinstance(a, dict) and a.get("id")}
    axes = [axes_by_id.get(aid) or {"id": aid} for aid in axis_ids]
    if len(axes) != 2:
        print(f"{axis_selection_path} 選定的軸數量是 {len(axes)}，不是 2——"
              f"Persona 內容只能對應恰好兩條選定軸。", file=sys.stderr)
        return 1
    # persona-generation-confirmation-gate：格子的兩端由 matrix 與選定兩軸機械
    # 算出，不讀 AI 寫的四格畫像檔——新分群不產那份檔，Persona 也不該拿別的
    # 模型摘要當輸入。
    matrix_quadrants, _unclassified = stages.assign_target_segmentation_quadrants(persona_matrix_doc, axis_ids)
    matrix_quadrant = next((q for q in matrix_quadrants if q["id"] == primary_quadrant_id), None)
    if matrix_quadrant is None:
        print(f"主要目標象限 {primary_quadrant_id} 不是選定兩軸算得出的四格之一——先回工作台重新確認目標。",
              file=sys.stderr)
        return 1
    quadrant = {
        "id": primary_quadrant_id,
        "subject_ids": primary_members,
        "axis_1_pole": matrix_quadrant["axis_1_pole"],
        "axis_2_pole": matrix_quadrant["axis_2_pole"],
    }

    axial_coding_path = run_dir / "axial_coding.yaml"
    categories_by_id: dict = {}
    if axial_coding_path.exists():
        axial_coding_doc = yaml.safe_load(axial_coding_path.read_text(encoding="utf-8")) or {}
        categories_by_id = {
            c.get("id"): c for c in (axial_coding_doc.get("categories") or [])
            if isinstance(c, dict) and c.get("id")
        }

    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "persona_content_model": args.model, "persona_content_effort": args.effort}
    try:
        data, usage = stages.persona_content(evidence, quadrant, axes, persona_matrix_doc, categories_by_id, opts)
        _write_yaml(run_dir / "persona_content.yaml", data)
        run_state.setdefault("usage", {})["persona_content"] = usage
        run_state["persona_content_state"] = "complete"
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'persona_content.yaml'} 為 {primary_quadrant_id} 產生 1 個 persona")
        return 0
    except Exception as exc:
        run_state["persona_content_state"] = "failed"
        run_state["persona_content_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"Persona 內容失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_jtbd(argv: list[str]) -> int:
    """`sbp-gen jtbd --run <dir>`——add-jtbd：獨立、選填的 JTBD 補跑，不進
    主管線，跟 `_main_persona_matrix` 完全同一個形狀。讀 evidence.yaml 跟
    axial_coding.yaml 兩個檔案，只寫 jtbd.yaml，只更新 run.json 自己的
    `jtbd_state` 欄位，不動主管線的 `state`、`axial_coding_state`、
    `persona_matrix_state` 或任何其他欄位——jtbd 不讀 persona_matrix.yaml，
    兩者是平行讀法，不是依序推導（design.md）。若 run 目錄有
    selective_coding.yaml，讀進來的類別會先被縮到核心類別（見
    `_scope_categories_to_core`）。
    """
    p = argparse.ArgumentParser(prog="sbp-gen jtbd", description="把主軸編碼類別與證據讀成情境／進展／結果三格的 Job 陳述")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法跑 JTBD——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1
    axial_coding_path = run_dir / "axial_coding.yaml"
    if not axial_coding_path.exists():
        print(f"{axial_coding_path} 不存在，無法跑 JTBD——先跑過 "
              f"`sbp-gen axial-coding --run {run_dir}`。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    categories_doc = yaml.safe_load(axial_coding_path.read_text(encoding="utf-8")) or {}
    core_category = _resolve_core_category(run_dir, categories_doc)
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "jtbd_model": args.model, "jtbd_effort": args.effort}
    try:
        data, usage = stages.jtbd(evidence, categories_doc, opts, core_category=core_category)
        _write_yaml(run_dir / "jtbd.yaml", data)
        # Lens steps can run concurrently in HMW formation: write only this
        # step's own keys, never the whole state read at the start.
        run_state_file.record_stage(run_json_path, "jtbd", state_key="jtbd_state",
                                    failure_key="jtbd_failure", usage=usage)
        print(f"完成：{run_dir / 'jtbd.yaml'} 產生 {len(data.get('jobs') or [])} 則 Job")
        return 0
    except Exception as exc:
        run_state_file.record_stage(run_json_path, "jtbd", state_key="jtbd_state",
                                    failure_key="jtbd_failure", failure=str(exc))
        print(f"JTBD 失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_journey_annotations(argv: list[str]) -> int:
    """`sbp-gen journey-annotations --run <dir> --input <steps.json>`: the AI
    drafts emotion, satisfaction and solutions for each confirmed step and
    merges them into journey_annotations.yaml with `source: ai-draft`. A field
    the researcher wrote is never replaced. The caller (the workbench) passes
    the confirmed steps and their pain points, because resolving the current
    journey belongs to the workbench's lifecycle view, not to this command."""
    p = argparse.ArgumentParser(prog="sbp-gen journey-annotations", description="AI 起草顧客旅程註記")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)
    evidence_path = args.run_dir / "evidence.yaml"
    if not evidence_path.is_file():
        print(f"{evidence_path} 不存在，無法起草註記。", file=sys.stderr)
        return 1
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    request = json.loads(args.input.read_text(encoding="utf-8"))
    steps = [step for step in request.get("steps") or [] if isinstance(step, dict) and step.get("id")]
    if not steps:
        print("沒有可以起草的步驟。", file=sys.stderr)
        return 1
    opts = {"max_tokens": args.max_tokens, "journey_annotations_model": args.model,
            "journey_annotations_effort": args.effort}
    draft, _usage = stages.journey_annotation_draft(steps, evidence, request.get("pain_points") or {}, opts)
    path = args.run_dir / "journey_annotations.yaml"
    existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    merged = stages.merge_annotation_draft(existing or {"schema_version": 1, "steps": {}}, draft,
                                           dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    errors = V.structural_errors(merged, V.load_schema("journey-annotations"))
    if errors:
        print("起草結果不符合註記格式：\n" + "\n".join(errors), file=sys.stderr)
        return 1
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    print(f"已起草：{path}")
    return 0


def _main_selective_themes(argv: list[str]) -> int:
    """`sbp-gen selective-themes --run <dir>`: form core and secondary selective
    themes from the run's axial themes and research purpose
    (add-selective-coding-layer). A new document replaces the current one,
    which is archived with its researcher edits; nothing is carried over.
    Only selective_themes.yaml (and its history) and run.json's own
    selective_themes_* fields are written."""
    from . import selective_themes as ST
    p = argparse.ArgumentParser(prog="sbp-gen selective-themes", description="依研究目的形成選擇編碼")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)
    run_dir = args.run_dir
    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    try:
        view = ST.structure_input(run_dir)
    except ST.SelectiveError as exc:
        print(str(exc), file=sys.stderr)
        run_state["selective_themes_state"] = "needs_purpose" if "研究目的" in str(exc) else "failed"
        run_state["selective_themes_failure"] = str(exc)
        _persist(run_json_path, run_state)
        return 1
    opts = {"max_tokens": args.max_tokens, "selective_themes_model": args.model, "selective_themes_effort": args.effort}
    try:
        draft, usage = stages.selective_themes(view, opts)
    except llm.LLMError as exc:
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        run_state["selective_themes_state"] = "failed"
        run_state["selective_themes_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"選擇編碼沒有形成：{exc}", file=sys.stderr)
        return 1
    case = yaml.safe_load((run_dir / "case.yaml").read_text(encoding="utf-8")) if (run_dir / "case.yaml").is_file() else {}
    case_id = str(run_state.get("case_id") or (case or {}).get("case_id") or "unknown")
    doc = ST.compose_document(draft, case_id, view["research_purpose"], ST.framing(run_dir))
    errors = V.structural_errors(doc, V.load_schema("selective-themes"))
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    if not errors:
        doc = ST.new_document(run_dir, doc)
    if errors:
        run_state["selective_themes_state"] = "failed"
        run_state["selective_themes_failure"] = "; ".join(errors[:3])
        _persist(run_json_path, run_state)
        print("選擇編碼不符合格式：\n" + "\n".join(errors), file=sys.stderr)
        return 1
    run_state["selective_themes_state"] = "complete"
    run_state.pop("selective_themes_failure", None)
    run_state.setdefault("usage", {})["selective_themes"] = usage
    _persist(run_json_path, run_state)
    print(f"完成：{run_dir / ST.THEMES_FILE}（{len(doc['themes'])} 個選擇性編碼主題）")
    return 0


def _main_chart_guides(argv: list[str]) -> int:
    """`sbp-gen chart-guides --run <dir> --kind persona|journey|blueprint`: the
    AI drafts that station's chart guide (導讀) and merges it into
    chart_guides.yaml; a block the researcher wrote keeps the researcher's
    text. Only chart_guides.yaml is written."""
    from . import chart_guides as G
    p = argparse.ArgumentParser(prog="sbp-gen chart-guides", description="AI 起草圖表導讀")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--kind", required=True, choices=list(G.KINDS))
    p.add_argument("--max-tokens", type=int, default=16000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)
    opts = {"max_tokens": args.max_tokens, "chart_guides_model": args.model, "chart_guides_effort": args.effort}
    try:
        draft, usage, digest = stages.draft_chart_guide(args.run_dir, args.kind, opts)
    except llm.LLMError as exc:
        print(f"起草導讀失敗：{exc}", file=sys.stderr)
        return 1
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    merged = G.merge_draft(G.load_document(args.run_dir), args.kind, draft, digest, now)
    errors = V.structural_errors(merged, V.load_schema("chart-guides"))
    if errors:
        print("起草結果不符合導讀格式：\n" + "\n".join(errors), file=sys.stderr)
        return 1
    path = G.write_document(args.run_dir, merged)
    print(f"已起草導讀（{args.kind}）：{path}；{usage.get('attempts')} 次、{usage.get('wall_seconds')} 秒、"
          f"{usage.get('tokens')} tokens")
    return 0


def _main_journey(argv: list[str]) -> int:
    """`sbp-gen journey --run <dir>`——add-standalone-compare-structure-and-
    journey-commands：獨立、選填的歷程發現與確認指令，不進多階段管線（手動傳輸
    明確拒絕那個入口，見 test_manual_transport.py 的
    `test_the_multi_stage_pipeline_command_refuses_the_transport`）。兩種模式：

    `--propose` 讀 evidence.yaml／case.yaml，呼叫 `stages.journey()`（真的
    模型呼叫，證據導向的歷程發現，不是機械照抄 case.yaml 的步驟標籤——
    `stages.journey_from_case()` 那條零成本的機械路徑刻意不用），寫
    journey_proposal.yaml，記 journey_state: awaiting_confirmation。

    `--confirm-file <path>` 純驗證＋寫檔，不呼叫模型。`<path>` 預期是研究者
    把 journey_proposal.yaml 複製一份、把每個 scenario 的 `status` 改成
    `confirmed`（必要時同時修改 steps／evidence_refs 本身）之後存出來的結果。
    用該 run 目錄真實的 evidence.yaml 建出的 evidence id 集合驗證——不是從
    這份歷程檔自己身上收集 evidence id 再驗證自己，那樣等於拿答案對答案；
    `case_with_journey()` 內部既有的檢查就是這麼做的，這裡刻意更嚴
    （design.md 的「歷程確認的真實機制」記錄了這個差異）。通過才寫
    journey.yaml，記 journey_state: complete。

    兩種模式互斥。
    """
    p = argparse.ArgumentParser(prog="sbp-gen journey", description="證據導向的歷程發現與確認")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--confirm-file", type=Path, default=None, dest="confirm_file")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    if args.propose and args.confirm_file is not None:
        print("--propose 跟 --confirm-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1
    if not args.propose and args.confirm_file is None:
        print("需要 --propose 或 --confirm-file 其中一個。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法跑歷程——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    evidence_ids = {e.get("id") for e in evidence if isinstance(e, dict) and e.get("id")}

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}

    if args.propose:
        case_path = run_dir / "case.yaml"
        if not case_path.exists():
            print(f"{case_path} 不存在，無法提案歷程——需要這個 run 的案例邊界檔。", file=sys.stderr)
            return 1
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        opts = {"max_tokens": args.max_tokens, "journey_model": args.model, "journey_effort": args.effort}
        axial_path = run_dir / "axial_coding.yaml"
        axial_doc = (yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}) if axial_path.exists() else {}
        persona_path = run_dir / "persona_confirmed.yaml"
        persona_snapshot = (yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}) if persona_path.exists() else {}
        persona_doc = persona_snapshot.get("persona_content") if isinstance(persona_snapshot, dict) else None
        confirmed_persona = {
            "target": (persona_doc.get("personas") or [None])[0],
        } if isinstance(persona_doc, dict) and persona_doc.get("personas") else None
        hmw_scope = hmw_scope_module.resolve(run_dir)
        try:
            data, usage = stages.journey(evidence, case, opts, coding_categories=axial_doc.get("categories"),
                                         confirmed_persona=confirmed_persona, hmw_scope=hmw_scope)
            _write_yaml(run_dir / "journey_proposal.yaml", data)
            run_state.setdefault("usage", {})["journey"] = usage
            run_state["journey_state"] = "awaiting_confirmation"
            # Persona is the CJM's analytical lens: record which confirmed Persona
            # this CJM was built through, so a later Persona change makes it
            # non-current instead of silently still counting as current.
            if isinstance(persona_snapshot, dict) and persona_snapshot.get("persona_digest"):
                run_state["journey_persona_lens"] = {"persona_digest": persona_snapshot["persona_digest"]}
                # The selected HMW is the CJM's design focus: a later HMW change
                # makes this CJM non-current the same way a Persona change does.
                if hmw_scope:
                    run_state["journey_persona_lens"]["hmw_digest"] = hmw_scope_module.digest(hmw_scope)
            else:
                run_state.pop("journey_persona_lens", None)
            run_state.pop("journey_failure", None)
            _persist(run_json_path, run_state)
            print(f"完成：{run_dir / 'journey_proposal.yaml'} 待研究者複核確認")
            return 0
        except Exception as exc:
            run_state["journey_state"] = "failed"
            run_state["journey_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"歷程提案失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    # --confirm-file 模式：純驗證＋寫檔，不呼叫模型。
    try:
        data = yaml.safe_load(args.confirm_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        print(f"讀取或解析 {args.confirm_file} 失敗：{exc}", file=sys.stderr)
        return 1

    errors = stages._journey_errors(data, evidence_ids, require_confirmed=True)
    if errors:
        run_state["journey_state"] = "failed"
        run_state["journey_failure"] = "; ".join(errors)
        _persist(run_json_path, run_state)
        print("確認歷程無效：\n" + "\n".join(errors), file=sys.stderr)
        return 1

    _write_yaml(run_dir / "journey.yaml", data)
    run_state["journey_state"] = "complete"
    run_state.pop("journey_failure", None)
    _persist(run_json_path, run_state)
    print(f"完成：{run_dir / 'journey.yaml'} 記錄確認過的歷程")
    return 0


def _main_journey_solutions(argv: list[str]) -> int:
    """`sbp-gen journey-solutions --run <dir>` (cjm-experience-and-moment-solutions):
    develop Solutions only for the Journey moments the researcher selected
    (`develop_solution: true` on journey_proposal.yaml), then write them back
    onto those steps. A failure leaves journey_proposal.yaml untouched."""
    p = argparse.ArgumentParser(prog="sbp-gen journey-solutions", description="為研究者選定的旅程時刻發展解方")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)
    run_dir = args.run_dir
    proposal_path = run_dir / "journey_proposal.yaml"
    evidence_path = run_dir / "evidence.yaml"
    if not proposal_path.exists() or not evidence_path.exists():
        print("這份產出還沒有顧客旅程或證據，無法發展解方。", file=sys.stderr)
        return 1
    journey_data = yaml.safe_load(proposal_path.read_text(encoding="utf-8")) or {}
    if not stages.selected_solution_moments(journey_data):
        print("研究者還沒有在旅程上選任何要發展解方的時刻。", file=sys.stderr)
        return 1
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    persona_path = run_dir / "persona_confirmed.yaml"
    persona_snapshot = (yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}) if persona_path.exists() else {}
    persona_doc = persona_snapshot.get("persona_content") if isinstance(persona_snapshot, dict) else None
    confirmed_persona = {"target": (persona_doc.get("personas") or [None])[0]} \
        if isinstance(persona_doc, dict) and persona_doc.get("personas") else None
    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    opts = {"max_tokens": args.max_tokens, "journey_solutions_model": args.model,
            "journey_solutions_effort": args.effort}
    try:
        output, usage = stages.journey_solutions(journey_data, evidence, opts, confirmed_persona=confirmed_persona,
                                                 hmw_scope=hmw_scope_module.resolve(run_dir))
        updated = stages.apply_journey_solutions(journey_data, output)
        evidence_ids = {e.get("id") for e in evidence if isinstance(e, dict) and e.get("id")}
        errors = stages._journey_errors(updated, evidence_ids, allow_split_stages=True)
        if errors:
            raise ValueError("寫回旅程後驗證沒有通過：" + "；".join(errors[:3]))
        _write_yaml(proposal_path, updated)
        run_state.setdefault("usage", {})["journey_solutions"] = usage
        run_state["journey_solutions_state"] = "complete"
        run_state.pop("journey_solutions_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{proposal_path} 已為 {len(output.get('moments') or [])} 個時刻發展解方")
        return 0
    except Exception as exc:
        run_state["journey_solutions_state"] = "failed"
        run_state["journey_solutions_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"發展解方沒有完成，旅程沒有變動：{exc}", file=sys.stderr)
        return 1


def _main_journey_branch(argv: list[str]) -> int:
    """Apply one explicit, non-model Journey Branch write operation.

    The payload is a researcher-authored YAML/JSON object.  This command is a
    canonical write entry point for Phase 3; it intentionally does not expose
    UI state or perform delivery/read projection.
    """
    p = argparse.ArgumentParser(prog="sbp-gen journey-branch", description="套用一個 Journey Branch 研究者寫入操作")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--operation", required=True, choices=sorted(journey_operations.OPERATIONS))
    p.add_argument("--payload-file", required=True, type=Path)
    p.add_argument("--rationale", default=None)
    args = p.parse_args(argv)
    try:
        payload = yaml.safe_load(args.payload_file.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise journey_operations.JourneyOperationError("payload 必須是 object")
        operation = getattr(journey_operations, args.operation)
        rationale = args.rationale if args.rationale is not None else payload.pop("rationale", None)

        def apply(current):
            return operation(current, **payload, **({"rationale": rationale} if args.operation in journey_operations.CONVERSION_OPERATIONS else {}))

        result = journey_operations.commit_journey_operation(
            args.run_dir,
            apply,
            operation_name=args.operation,
            rationale=rationale,
            presentation_only_step_ids=(
                {str(payload["step_id"])}
                if args.operation == "rename_branch_step" and isinstance(payload.get("step_id"), str)
                else None
            ),
        )
    except (OSError, yaml.YAMLError, journey_operations.JourneyOperationError, TypeError) as exc:
        print(f"Journey Branch 操作失敗：{exc}", file=sys.stderr)
        return 1
    print(f"完成：{args.operation}；confirmed_journey={result['state']['artifact_versions']['confirmed_journey']}")
    return 0


def _main_compare(argv: list[str]) -> int:
    """`sbp-gen compare --run <dir>`——add-standalone-compare-structure-and-
    journey-commands：獨立、選填的說法衝突比對指令，跟 `_main_job_map` 同一個
    形狀。讀 evidence.yaml，呼叫 `stages.compare()`，只寫 conflicts.yaml，只
    更新 run.json 自己的 `compare_state` 欄位。
    """
    p = argparse.ArgumentParser(prog="sbp-gen compare", description="找出證據裡彼此矛盾的說法")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法比對說法衝突——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []

    opts = {"max_tokens": args.max_tokens, "compare_model": args.model, "compare_effort": args.effort}
    try:
        conflicts, usage = stages.compare(evidence, opts)
        _write_yaml(run_dir / "conflicts.yaml", conflicts)
        run_state.setdefault("usage", {})["compare"] = usage
        run_state["compare_state"] = "complete"
        run_state.pop("compare_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'conflicts.yaml'} 產生 {len(conflicts)} 則衝突候選")
        return 0
    except Exception as exc:
        run_state["compare_state"] = "failed"
        run_state["compare_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"說法衝突比對失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_structure(argv: list[str]) -> int:
    """`sbp-gen structure --run <dir>`——add-standalone-compare-structure-and-
    journey-commands：獨立、選填的現況藍圖產生指令，跟 `_main_job_map` 同一個
    形狀，但一路做到投影，不是只有中間產物：

    讀 evidence.yaml／conflicts.yaml／case.yaml／確認過的 journey.yaml，用
    `stages.case_with_journey(case, journey_data)` 算出確認歷程覆蓋步驟軸
    之後的 `runtime_case`（**不是**傳讀進來的原始 case——`case_with_journey`
    內部會驗證歷程真的標記為 confirmed，不合格時 raise），呼叫
    `stages.structure()` 寫 service_graph.yaml、記 `structure_state`；成功後
    接著跑既有的純函式投影 `stages.project()`（不呼叫模型）並驗證，通過才寫
    sbp_draft.yaml。全程不寫入 case.yaml——`runtime_case` 只在記憶體裡傳遞，
    design.md 的「`case.yaml` 保持不動」記錄了這個跟 `_main_generate` 既有
    行為不一致、但刻意不處理的差異。
    """
    p = argparse.ArgumentParser(prog="sbp-gen structure", description="從證據、衝突、確認過的歷程建立現況藍圖")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    p.add_argument("--researcher-note", default=None, dest="researcher_note")
    args = p.parse_args(argv)

    run_dir = args.run_dir
    required = [
        ("evidence.yaml", None),
        ("conflicts.yaml", "`sbp-gen compare --run {run_dir}`"),
        ("case.yaml", None),
        ("journey.yaml", "`sbp-gen journey --run {run_dir} --confirm-file <研究者確認過的檔案>`"),
    ]
    for filename, hint in required:
        path = run_dir / filename
        if not path.exists():
            if hint:
                print(f"{path} 不存在，無法建立現況藍圖——先跑過 {hint.format(run_dir=run_dir)}。", file=sys.stderr)
            else:
                print(f"{path} 不存在，無法建立現況藍圖——先跑過主要生成指令，"
                      f"拿到帶 {filename} 的 run 目錄再回來跑。", file=sys.stderr)
            return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    evidence = yaml.safe_load((run_dir / "evidence.yaml").read_text(encoding="utf-8")) or []
    conflicts = yaml.safe_load((run_dir / "conflicts.yaml").read_text(encoding="utf-8")) or []
    case = yaml.safe_load((run_dir / "case.yaml").read_text(encoding="utf-8")) or {}
    journey_data = yaml.safe_load((run_dir / "journey.yaml").read_text(encoding="utf-8")) or {}

    opts = {"max_tokens": args.max_tokens, "structure_model": args.model, "structure_effort": args.effort}
    try:
        runtime_case = stages.case_with_journey(case, journey_data)
        axial_path = run_dir / "axial_coding.yaml"
        axial_doc = (yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}) if axial_path.exists() else {}
        # pre-blueprint-implementation-research: only claims the researcher
        # selected in the current, reviewed research; empty otherwise.
        from . import implementation_research
        external_references = (implementation_research.accepted_references(run_dir)
                               if stages.journey_has_solutions(journey_data) else [])
        graph, usage = stages.structure(evidence, conflicts, runtime_case, opts, args.researcher_note, journey_data,
                                        coding_categories=axial_doc.get("categories"),
                                        external_references=external_references)
        _write_yaml(run_dir / "service_graph.yaml", graph)
        run_state.setdefault("usage", {})["structure"] = usage
        run_state["structure_state"] = "complete"
        run_state.pop("structure_failure", None)
        _persist(run_json_path, run_state)
    except Exception as exc:
        run_state["structure_state"] = "failed"
        run_state["structure_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"現況藍圖建立失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1

    evidence_ids = {e["id"] for e in evidence}
    draft = stages.project(
        graph, None, conflicts, runtime_case, journey_data=journey_data,
        annotations=graph.get("annotations"), evidence=evidence,
    )
    errors = V.structural_errors(draft, V.load_schema("sbp-draft")) + V.projection_errors(
        draft, evidence_ids, runtime_case, journey=journey_data)
    if errors:
        run_state["structure_state"] = "failed"
        run_state["structure_failure"] = "投影契約無效：" + "; ".join(errors)
        _persist(run_json_path, run_state)
        print("投影契約無效：\n" + "\n".join(errors), file=sys.stderr)
        return 1

    _write_yaml(run_dir / "sbp_draft.yaml", draft)
    # track-sbp-upstream-freshness：記下這份藍圖實際依據的上游內容；之後不改寫。
    from . import sbp_basis
    run_state["sbp_basis"] = sbp_basis.basis_after_build(stages.structure_input_view(
        evidence, runtime_case, journey_data, axial_doc.get("categories"), external_references))
    _persist(run_json_path, run_state)
    print(f"完成：{run_dir / 'service_graph.yaml'}（{len(graph.get('nodes') or [])} nodes）、"
          f"{run_dir / 'sbp_draft.yaml'}（{len(draft.get('cells') or [])} cells）")
    return 0


def _main_job_map(argv: list[str]) -> int:
    """`sbp-gen job-map --run <dir>`——add-job-map：獨立、選填的 Job Map 補跑，
    不進主管線，跟 `_main_jtbd` 完全同一個形狀。讀 jtbd.yaml（要拆的 Job）跟
    evidence.yaml（把 Job 自己的 evidence_refs id 解回原文），只寫
    job_map.yaml，只更新 run.json 自己的 `job_map_state` 欄位，不動主管線的
    `state`、`jtbd_state`、`axial_coding_state`、`persona_matrix_state` 或
    任何其他欄位——job_map 不讀 axial_coding.yaml 或 persona_matrix.yaml
    （design.md）。
    """
    p = argparse.ArgumentParser(prog="sbp-gen job-map", description="把 JTBD 的 Job 拆成有序、solution-independent 的階段")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    jtbd_path = run_dir / "jtbd.yaml"
    if not jtbd_path.exists():
        print(f"{jtbd_path} 不存在，無法跑 Job Map——先跑過 "
              f"`sbp-gen jtbd --run {run_dir}`。", file=sys.stderr)
        return 1
    evidence_path = run_dir / "evidence.yaml"
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法跑 Job Map——先跑過主要生成指令，"
              f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    jobs_doc = yaml.safe_load(jtbd_path.read_text(encoding="utf-8")) or {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "job_map_model": args.model, "job_map_effort": args.effort}
    try:
        data, usage = stages.job_map(jobs_doc, evidence, opts)
        _write_yaml(run_dir / "job_map.yaml", data)
        # Lens steps can run concurrently in HMW formation: write only this
        # step's own keys, never the whole state read at the start.
        run_state_file.record_stage(run_json_path, "job_map", state_key="job_map_state",
                                    failure_key="job_map_failure", usage=usage)
        print(f"完成：{run_dir / 'job_map.yaml'} 產生 {len(data.get('job_maps') or [])} 則 Job 的階段序列")
        return 0
    except Exception as exc:
        run_state_file.record_stage(run_json_path, "job_map", state_key="job_map_state",
                                    failure_key="job_map_failure", failure=str(exc))
        print(f"Job Map 失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_how_might_we(argv: list[str]) -> int:
    """`sbp-gen how-might-we --run <dir>`（HMW method v9，multi-jtbd-single-
    job-source，2026-10-03）：研究者選定的一則或多則顧客任務合起來是一個設計
    範圍，讀主軸關係、屬性與面向（行為模式）、選擇編碼與證據，在背後找出張力，
    寫成 HMW。不再讀 jtbd.yaml／job_map.yaml。沒有選擇編碼或沒有選定顧客任務
    就拒絕。只寫 hmw.yaml，只更新 run.json 自己的 `hmw_state`。"""
    from . import selective_themes as ST
    p = argparse.ArgumentParser(prog="sbp-gen how-might-we", description="依選定的顧客任務範圍找出張力寫成 HMW")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    for filename, hint in (("axial_coding.yaml", "`sbp-gen axial-coding --run {run_dir}`"),
                           ("category_properties.yaml", "`sbp-gen category-properties --run {run_dir}`"),
                           ("evidence.yaml", "主要生成指令")):
        path = run_dir / filename
        if not path.exists():
            print(f"{path} 不存在，無法跑 How Might We——先跑過 {hint.format(run_dir=run_dir)}。", file=sys.stderr)
            return 1
    selective_doc = ST.load_document(run_dir)
    if selective_doc is None:
        print("這份 run 沒有選擇編碼；新方法下要先重新編碼、形成選擇編碼，才能形成 HMW。", file=sys.stderr)
        return 1
    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    categories_doc = yaml.safe_load((run_dir / "axial_coding.yaml").read_text(encoding="utf-8")) or {}
    category_ids = {c.get("id") for c in (categories_doc.get("categories") or [])
                    if isinstance(c, dict) and c.get("id")}
    category_properties_doc = yaml.safe_load((run_dir / "category_properties.yaml").read_text(encoding="utf-8")) or {}
    evidence = yaml.safe_load((run_dir / "evidence.yaml").read_text(encoding="utf-8")) or []
    selection_doc = _read_primary_job_selection(run_dir, category_ids)
    selections = narrative_framing.selections(selection_doc)
    if not selections:
        print("還沒有選定顧客任務，無法形成 HMW。", file=sys.stderr)
        return 1
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    opts = {"case_id": case_id, "max_tokens": args.max_tokens,
            "how_might_we_model": args.model, "how_might_we_effort": args.effort}
    try:
        data, usage = stages.how_might_we(categories_doc, category_properties_doc, evidence, opts,
                                           selections, selective_doc)
        _record_narrative_framing(data, selection_doc)
        _write_yaml(run_dir / "hmw.yaml", data)
        run_state.setdefault("usage", {})["how_might_we"] = usage
        run_state["hmw_state"] = "complete"
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'hmw.yaml'} 產生 {len(data.get('entries') or [])} 則 HMW")
        return 0
    except Exception as exc:
        run_state["hmw_state"] = "failed"
        run_state["hmw_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"How Might We 失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_form_hmw(argv: list[str]) -> int:
    """`sbp-gen form-hmw --run <dir>`——form-hmw-from-confirmed-narrative：
    研究者在③按「形成 HMW」時執行的那一個工作。依序跑屬性與面向、顧客任務、
    Job Map、HMW 張力、主要 HMW 候選五段現有指令，停在研究者挑 HMW；不寫任何
    選擇檔，也不啟動 Persona。哪一段要重跑由 `hmw_formation` 依每段實際讀的
    依賴判斷，見該模組說明。五段各自的指令照舊可以單獨跑。
    """
    p = argparse.ArgumentParser(prog="sbp-gen form-hmw", description="從確認過的敘事形成 HMW 候選，停在研究者挑選")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    args = p.parse_args(argv)

    refusal = hmw_formation.readiness(args.run_dir)
    if refusal:
        print(refusal["error"], file=sys.stderr)
        return 1
    passthrough: list[str] = []
    for flag, value in (("--max-tokens", args.max_tokens), ("--model", args.model), ("--effort", args.effort)):
        if value is not None:
            passthrough += [flag, str(value)]
    runners = {
        "category_properties": _main_category_properties,
        "how_might_we": _main_how_might_we,
        "primary_hmw_candidates": _main_primary_hmw,
    }
    status = hmw_formation.run(args.run_dir, runners, passthrough)
    if status == 0:
        print("HMW 候選已形成，等待研究者挑選。")
    else:
        print(f"形成 HMW 沒有完成：{hmw_formation.formation_state(args.run_dir).get('failure')}", file=sys.stderr)
    return status


def _main_selective_coding(argv: list[str]) -> int:
    """`sbp-gen selective-coding --run <dir>`——add-selective-coding：獨立、
    選填的選擇編碼指令，不進主管線。三種模式：不帶任何 flag 是 show 模式
    （唯讀，攤出既有的判準發現，不推薦答案）；帶 `--selection-file <path>`
    是 selection 模式（驗證並寫檔）；帶 `--propose` 是 propose 模式
    （propose-core-category-candidates：呼叫模型提出 3 個核心類別候選並
    各附理由，寫入 core_category_candidates.yaml，不動 selective_coding.yaml
    或 selective_coding_state）。show／selection 兩種模式全程沒有模型呼叫
    ——挑核心類別是研究者的判斷，不是這支程式的判斷（CLAUDE.md「助理的
    界線」）；propose 模式是這個指令第一次引入模型呼叫，但只提候選、不
    決定，決定權仍在研究者（見 propose-core-category-candidates design.md
    決定五）。`--propose` 跟 `--selection-file` 互斥，兩個一起給是使用錯誤。
    """
    p = argparse.ArgumentParser(prog="sbp-gen selective-coding", description="記錄研究者挑選的核心類別")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--selection-file", type=Path, default=None, dest="selection_file")
    p.add_argument("--propose", action="store_true")
    args = p.parse_args(argv)

    if args.propose and args.selection_file is not None:
        print("--propose 跟 --selection-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    axial_path = run_dir / "axial_coding.yaml"
    evidence_path = run_dir / "evidence.yaml"
    if not axial_path.exists():
        print(f"{axial_path} 不存在，無法做選擇編碼——先跑過 `sbp-gen axial-coding --run {run_dir}`。",
              file=sys.stderr)
        return 1
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法做選擇編碼。", file=sys.stderr)
        return 1

    axial_data = yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    categories = [c for c in (axial_data.get("categories") or []) if isinstance(c, dict)]
    category_ids = {c.get("id") for c in categories if c.get("id")}

    if args.propose:
        run_json_path = run_dir / "run.json"
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        case_path = run_dir / "case.yaml"
        case_id = run_dir.name
        if case_path.exists():
            case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
            case_id = case.get("case_id") or case_id
        opts = {"case_id": case_id}
        try:
            data, usage = stages.propose_core_category_candidates(axial_data, evidence, opts)
            _write_yaml(run_dir / "core_category_candidates.yaml", data)
            run_state.setdefault("usage", {})["core_category_candidates"] = usage
            run_state["core_category_candidates_state"] = "complete"
            _persist(run_json_path, run_state)
            print(f"完成：{run_dir / 'core_category_candidates.yaml'} 產生 "
                  f"{len(data.get('candidates') or [])} 個核心類別候選")
            return 0
        except Exception as exc:
            run_state["core_category_candidates_state"] = "failed"
            run_state["core_category_candidates_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"核心類別候選失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    if args.selection_file is None:
        findings = V.axial_coding_warnings(axial_data, evidence)
        for category in categories:
            label = category.get("label")
            print(f"{category.get('id')}\t{label}")
            print(f"  區辨依據：{category.get('discriminating_basis')}")
            matches = [f for f in findings if f"「{label}」" in f]
            if matches:
                for m in matches:
                    print(f"  {m}")
            else:
                print("  （沒有相關的判準發現）")
        return 0

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    try:
        raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        run_state["selective_coding_state"] = "failed"
        run_state["selective_coding_failure"] = f"讀取或解析 {args.selection_file} 失敗：{exc}"
        _persist(run_json_path, run_state)
        print(run_state["selective_coding_failure"], file=sys.stderr)
        return 1

    if not isinstance(raw, dict):
        run_state["selective_coding_state"] = "failed"
        run_state["selective_coding_failure"] = f"{args.selection_file} 的內容必須是一個物件，包含 core_category_id 與 rationale"
        _persist(run_json_path, run_state)
        print(run_state["selective_coding_failure"], file=sys.stderr)
        return 1

    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    doc = {
        "schema_version": 1,
        "case_id": case_id,
        "core_category_id": raw.get("core_category_id"),
        "rationale": raw.get("rationale"),
    }
    errors = stages._selective_coding_errors(doc, category_ids)
    if errors:
        run_state["selective_coding_state"] = "failed"
        run_state["selective_coding_failure"] = "; ".join(errors)
        _persist(run_json_path, run_state)
        print(run_state["selective_coding_failure"], file=sys.stderr)
        return 1

    _write_yaml(run_dir / "selective_coding.yaml", doc)
    run_state["selective_coding_state"] = "complete"
    run_state.pop("selective_coding_failure", None)
    _persist(run_json_path, run_state)
    chosen = next((c for c in categories if c.get("id") == doc["core_category_id"]), None)
    chosen_label = chosen.get("label") if chosen else doc["core_category_id"]
    print(f"完成：{run_dir / 'selective_coding.yaml'} 記錄核心類別 {doc['core_category_id']}（{chosen_label}）")
    return 0


def _main_narrative_job_selection(argv: list[str]) -> int:
    """`sbp-gen narrative-job-selection --run <dir> --selection-file <path>`
    （multi-jtbd-single-job-source，2026-10-03）：記錄研究者選定的一則或多則
    顧客任務。檔案內容是 `{selections: [...]}`；每則存完整內容，候選與自寫
    同形，不記來自哪則候選。新方法下要有選擇編碼；全程不呼叫模型。

    只寫 `narrative_job_selection.yaml`（schema_version 2），只更新 run.json
    自己的 `narrative_job_selection_state`。"""
    from . import selective_themes as ST
    p = argparse.ArgumentParser(prog="sbp-gen narrative-job-selection", description="記錄研究者選定的顧客任務")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--selection-file", required=True, type=Path, dest="selection_file")
    args = p.parse_args(argv)

    run_dir = args.run_dir
    themes = ST.load_document(run_dir)
    if themes is None:
        print("這份 run 還沒有選擇編碼；新方法下要先形成選擇編碼，才能選顧客任務。", file=sys.stderr)
        return 1
    if not args.selection_file.exists():
        print(f"{args.selection_file} 不存在。", file=sys.stderr)
        return 1
    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("selections"), list):
        print(f"{args.selection_file} 的內容要是 {{selections: [...]}}：一則或多則顧客任務，"
              f"每則有情境、任務、動機、結果、selective_refs 與理由。", file=sys.stderr)
        return 1
    axial = yaml.safe_load((run_dir / "axial_coding.yaml").read_text(encoding="utf-8")) if (run_dir / "axial_coding.yaml").is_file() else {}
    category_ids = {c["id"] for c in ((axial or {}).get("categories") or []) if isinstance(c, dict) and c.get("id")}
    selective_ids = {t.get("id") for t in themes.get("themes") or [] if isinstance(t, dict)}
    case_path = run_dir / "case.yaml"
    case_id = run_state.get("case_id") or run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id
    doc = {"schema_version": 2, "case_id": case_id, "selections": raw["selections"],
           "framed_by": {"selective_themes": ST.themes_digest(run_dir)}}
    errors = stages._jtbd_selection_list_errors(doc, category_ids, selective_ids)
    if errors:
        run_state["narrative_job_selection_state"] = "failed"
        run_state["narrative_job_selection_failure"] = errors[0]
        _persist(run_json_path, run_state)
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    _write_yaml(run_dir / "narrative_job_selection.yaml", doc)
    run_state["narrative_job_selection_state"] = "complete"
    run_state.pop("narrative_job_selection_failure", None)
    _persist(run_json_path, run_state)
    print(f"完成：{run_dir / 'narrative_job_selection.yaml'} 記錄選定的 {len(doc['selections'])} 則顧客任務")
    return 0


def _main_narrative_job_candidates(argv: list[str]) -> int:
    """`sbp-gen narrative-job-candidates --run <dir>`（multi-jtbd-single-job-
    source，2026-10-03）：依選擇編碼端出 1–8 則顧客任務候選（沒有目標數量、
    不帶隱藏敘事），只寫 `narrative_job_candidates.yaml`，只更新 run.json 自己的
    `narrative_job_candidates_state`。沒有 `selective_themes.yaml` 就拒絕——
    不退回只靠主軸編碼的舊做法。**只提候選，不記任何決定**。"""
    from . import selective_themes as ST
    p = argparse.ArgumentParser(prog="sbp-gen narrative-job-candidates", description="依選擇編碼端出顧客任務候選")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    p.add_argument("--model", default=None)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    try:
        view = ST.candidates_input(run_dir)
    except ST.SelectiveError as exc:
        print(str(exc), file=sys.stderr)
        run_state["narrative_job_candidates_state"] = "needs_selective"
        _persist(run_json_path, run_state)
        return 1
    case_path = run_dir / "case.yaml"
    case_id = run_state.get("case_id") or run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id
    opts = {"case_id": case_id, "max_tokens": args.max_tokens, "narrative_job_candidates_model": args.model}
    try:
        data, usage = stages.narrative_job_candidates(view, opts)
        _write_yaml(run_dir / "narrative_job_candidates.yaml", data)
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        run_state.setdefault("usage", {})["narrative_job_candidates"] = usage
        run_state["narrative_job_candidates_state"] = "complete"
        run_state.pop("narrative_job_candidates_failure", None)
        _persist(run_json_path, run_state)
        count = len(data.get("candidates") or [])
        print(f"完成：{run_dir / 'narrative_job_candidates.yaml'} "
              + (f"產生 {count} 則顧客任務候選" if count else "證據撐不起任何一則顧客任務"))
        return 0
    except Exception as exc:
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        run_state["narrative_job_candidates_state"] = "failed"
        run_state["narrative_job_candidates_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"候選產生失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
        return 1


def _main_selective_coding_narrative(argv: list[str]) -> int:
    """`sbp-gen selective-coding-narrative --run <dir>`——selective-coding-
    produces-a-narrative：選擇編碼原本只記一個核心類別 id 加一句理由，這支
    補上方法論第六步要求的後半段（次要主題、150–200 字洞察敘事、3–5 條
    關鍵洞察），合併進同一份 `selective_coding.yaml`，`core_category_id`／
    `rationale`（研究者已經做的判斷）完全不動，一個位元組都不改。前置：
    研究者必須已經跑過 `selective-coding --selection-file` 選定核心類別
    ——這支不是選擇機制，是選擇之後的補述。
    """
    p = argparse.ArgumentParser(prog="sbp-gen selective-coding-narrative", description="把選擇編碼的核心類別串成完整的洞察敘事")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--max-tokens", type=int, default=32000)
    args = p.parse_args(argv)

    run_dir = args.run_dir
    selective_path = run_dir / "selective_coding.yaml"
    axial_path = run_dir / "axial_coding.yaml"
    if not selective_path.exists():
        print(f"{selective_path} 不存在——先跑過 `sbp-gen selective-coding --run {run_dir} "
              f"--selection-file <path>` 選定核心類別。", file=sys.stderr)
        return 1
    if not axial_path.exists():
        print(f"{axial_path} 不存在。", file=sys.stderr)
        return 1

    selective_doc = yaml.safe_load(selective_path.read_text(encoding="utf-8")) or {}
    categories_doc = yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}
    conflicts = yaml.safe_load((run_dir / "conflicts.yaml").read_text(encoding="utf-8")) if (run_dir / "conflicts.yaml").exists() else []
    evidence, evidence_status = evidence_locator.locate_evidence(run_dir)
    if evidence is None:
        print(f"{run_dir} 找不到可用的證據檔（{evidence_status}），無法產生洞察敘事。", file=sys.stderr)
        return 1

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    opts = {"case_id": selective_doc.get("case_id") or run_dir.name, "max_tokens": args.max_tokens}
    try:
        data, usage = stages.selective_coding_narrative(categories_doc, selective_doc, conflicts, evidence, opts)
        _write_yaml(selective_path, data)
        run_state.setdefault("usage", {})["selective_coding_narrative"] = usage
        run_state["selective_coding_narrative_state"] = "complete"
        _persist(run_json_path, run_state)
        print(f"完成：{selective_path} 補上 {len(data.get('secondary_themes') or [])} 個次要主題、"
              f"{len(data.get('key_insights') or [])} 條關鍵洞察")
        return 0
    except Exception as exc:
        run_state["selective_coding_narrative_state"] = "failed"
        run_state["selective_coding_narrative_failure"] = str(exc)
        _persist(run_json_path, run_state)
        print(f"洞察敘事失敗，核心類別選擇不受影響：{exc}", file=sys.stderr)
        return 1


def _main_persona_dimensions(argv: list[str]) -> int:
    """`sbp-gen persona-dimensions --run <dir>`——propose-persona-dimension-
    candidates：全新的獨立指令，不是延伸既有的 `_main_persona_matrix`——
    Persona Matrix 維度目前完全沒有選擇步驟，是從零建立。兩種模式：帶
    `--propose` 是提案模式（呼叫模型，內部走完候選池篩選方法論，端出 3 組
    候選維度框架，寫 persona_dimension_candidates.yaml）；帶
    `--selection-file <path>` 是選擇模式（驗證並寫檔 persona_dimensions_
    selection.yaml，不管是照抄某個候選還是研究者自己寫的新維度）。兩者
    互斥。這次不做 show 模式——維度本身不是預先存在的資料，沒有模型呼叫就
    沒有東西可以攤開看。`persona_matrix()` 本身不讀這裡寫出的選擇檔，接線
    留給之後一個獨立的 change。
    """
    p = argparse.ArgumentParser(prog="sbp-gen persona-dimensions", description="Persona Matrix 維度候選與選擇")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--selection-file", type=Path, default=None, dest="selection_file")
    args = p.parse_args(argv)

    if args.propose and args.selection_file is not None:
        print("--propose 跟 --selection-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    axial_path = run_dir / "axial_coding.yaml"
    evidence_path = run_dir / "evidence.yaml"
    if not axial_path.exists():
        print(f"{axial_path} 不存在，無法做 Persona Matrix 維度候選——先跑過 "
              f"`sbp-gen axial-coding --run {run_dir}`。", file=sys.stderr)
        return 1
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法做 Persona Matrix 維度候選。", file=sys.stderr)
        return 1

    axial_data = yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    categories = [c for c in (axial_data.get("categories") or []) if isinstance(c, dict)]
    category_ids = {c.get("id") for c in categories if c.get("id")}

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    if args.propose:
        opts = {"case_id": case_id}
        properties_path = run_dir / "category_properties.yaml"
        properties_doc = (
            yaml.safe_load(properties_path.read_text(encoding="utf-8")) if properties_path.exists() else None
        )
        try:
            data, usage = stages.propose_persona_dimension_candidates(
                axial_data, evidence, opts, properties_doc=properties_doc)
            _write_yaml(run_dir / "persona_dimension_candidates.yaml", data)
            run_state.setdefault("usage", {})["persona_dimension_candidates"] = usage
            run_state["persona_dimension_candidates_state"] = "complete"
            _persist(run_json_path, run_state)
            source_note = (
                "屬性與面向候選池" if data.get("dimension_source") == "category_properties"
                else "類別＋證據舊路徑（這個 run 還沒有 category_properties.yaml）"
            )
            print(f"完成：{run_dir / 'persona_dimension_candidates.yaml'} 產生 "
                  f"{len(data.get('candidates') or [])} 組候選維度框架（來源：{source_note}）")
            for finding in V.persona_dimension_candidate_warnings(data, axial_data, evidence):
                print(f"  {finding}")
            return 0
        except Exception as exc:
            run_state["persona_dimension_candidates_state"] = "failed"
            run_state["persona_dimension_candidates_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"Persona Matrix 維度候選失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    if args.selection_file is not None:
        try:
            raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            run_state["persona_dimensions_selection_state"] = "failed"
            run_state["persona_dimensions_selection_failure"] = f"讀取或解析 {args.selection_file} 失敗：{exc}"
            _persist(run_json_path, run_state)
            print(run_state["persona_dimensions_selection_failure"], file=sys.stderr)
            return 1

        if not isinstance(raw, dict):
            run_state["persona_dimensions_selection_state"] = "failed"
            run_state["persona_dimensions_selection_failure"] = (
                f"{args.selection_file} 的內容必須是一個物件，包含 rationale 與 axes")
            _persist(run_json_path, run_state)
            print(run_state["persona_dimensions_selection_failure"], file=sys.stderr)
            return 1

        doc = {
            "schema_version": 1,
            "case_id": case_id,
            "rationale": raw.get("rationale"),
            "axes": raw.get("axes"),
        }
        errors = stages._persona_dimensions_selection_errors(doc, category_ids)
        if errors:
            run_state["persona_dimensions_selection_state"] = "failed"
            run_state["persona_dimensions_selection_failure"] = "; ".join(errors)
            _persist(run_json_path, run_state)
            print(run_state["persona_dimensions_selection_failure"], file=sys.stderr)
            return 1

        _write_yaml(run_dir / "persona_dimensions_selection.yaml", doc)
        run_state["persona_dimensions_selection_state"] = "complete"
        run_state.pop("persona_dimensions_selection_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'persona_dimensions_selection.yaml'} 記錄選定維度")
        return 0

    print("需要 --propose 或 --selection-file 其中一個。", file=sys.stderr)
    return 1


def _main_primary_job(argv: list[str]) -> int:
    """`sbp-gen primary-job --run <dir>`——propose-primary-job-candidates：
    全新的獨立指令，不是延伸既有的 `_main_jtbd`——JTBD 目前完全沒有選擇
    步驟，是從零建立，跟 `_main_persona_dimensions` 同一個形狀。兩種模式：
    帶 `--propose` 是提案模式（呼叫模型，端出 3 組候選主要 Job 框架，寫
    primary_job_candidates.yaml）；帶 `--selection-file <path>` 是選擇模式
    （驗證並寫檔 primary_job_selection.yaml，不管是照抄某個候選還是研究者
    自己寫的新 Job）。兩者互斥。這次不做 show 模式——主要 Job 不是預先存在
    的資料，沒有模型呼叫就沒有東西可以攤開看。`jtbd()` 本身不讀這裡寫出的
    選擇檔，接線留給之後一個獨立的 change。
    """
    p = argparse.ArgumentParser(prog="sbp-gen primary-job", description="JTBD 主要 Job 候選與選擇")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--selection-file", type=Path, default=None, dest="selection_file")
    args = p.parse_args(argv)

    if args.propose and args.selection_file is not None:
        print("--propose 跟 --selection-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    axial_path = run_dir / "axial_coding.yaml"
    evidence_path = run_dir / "evidence.yaml"
    if not axial_path.exists():
        print(f"{axial_path} 不存在，無法做主要 Job 候選——先跑過 "
              f"`sbp-gen axial-coding --run {run_dir}`。", file=sys.stderr)
        return 1
    if not evidence_path.exists():
        print(f"{evidence_path} 不存在，無法做主要 Job 候選。", file=sys.stderr)
        return 1

    axial_data = yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
    categories = [c for c in (axial_data.get("categories") or []) if isinstance(c, dict)]
    category_ids = {c.get("id") for c in categories if c.get("id")}

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    if args.propose:
        opts = {"case_id": case_id}
        try:
            data, usage = stages.propose_primary_job_candidates(axial_data, evidence, opts)
            _write_yaml(run_dir / "primary_job_candidates.yaml", data)
            run_state.setdefault("usage", {})["primary_job_candidates"] = usage
            run_state["primary_job_candidates_state"] = "complete"
            _persist(run_json_path, run_state)
            print(f"完成：{run_dir / 'primary_job_candidates.yaml'} 產生 "
                  f"{len(data.get('candidates') or [])} 組候選主要 Job")
            for finding in V.primary_job_candidate_warnings(data, axial_data, evidence):
                print(f"  {finding}")
            return 0
        except Exception as exc:
            run_state["primary_job_candidates_state"] = "failed"
            run_state["primary_job_candidates_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"主要 Job 候選失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    if args.selection_file is not None:
        try:
            raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            run_state["primary_job_selection_state"] = "failed"
            run_state["primary_job_selection_failure"] = f"讀取或解析 {args.selection_file} 失敗：{exc}"
            _persist(run_json_path, run_state)
            print(run_state["primary_job_selection_failure"], file=sys.stderr)
            return 1

        if not isinstance(raw, dict):
            run_state["primary_job_selection_state"] = "failed"
            run_state["primary_job_selection_failure"] = (
                f"{args.selection_file} 的內容必須是一個物件，包含 rationale、situation、progress、"
                f"outcome 與 grounded_in")
            _persist(run_json_path, run_state)
            print(run_state["primary_job_selection_failure"], file=sys.stderr)
            return 1

        doc = {
            "schema_version": 1,
            "case_id": case_id,
            "rationale": raw.get("rationale"),
            "situation": raw.get("situation"),
            "progress": raw.get("progress"),
            "outcome": raw.get("outcome"),
            "grounded_in": raw.get("grounded_in"),
        }
        errors = stages._primary_job_selection_errors(doc, category_ids)
        if errors:
            run_state["primary_job_selection_state"] = "failed"
            run_state["primary_job_selection_failure"] = "; ".join(errors)
            _persist(run_json_path, run_state)
            print(run_state["primary_job_selection_failure"], file=sys.stderr)
            return 1

        _write_yaml(run_dir / "primary_job_selection.yaml", doc)
        run_state["primary_job_selection_state"] = "complete"
        run_state.pop("primary_job_selection_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'primary_job_selection.yaml'} 記錄選定主要 Job")
        return 0

    print("需要 --propose 或 --selection-file 其中一個。", file=sys.stderr)
    return 1


def _main_primary_hmw(argv: list[str]) -> int:
    """`sbp-gen primary-hmw --run <dir>`——propose-primary-hmw-candidates：
    全新的獨立指令,不是延伸既有的 `_main_how_might_we`。跟前三個候選機制
    不同的地方:`how_might_we()` 已經在做真的篩選(只有真的有張力才產出),
    這裡不是「無中生有再篩選」,是「從已經產出的 hmw.yaml 裡挑出最多 3 則
    最值得追的」。兩種模式:帶 `--propose` 是提案模式(讀既有 hmw.yaml,
    呼叫模型排出最多 3 則候選,只用 hmw_id 參照不重寫內容,寫
    primary_hmw_candidates.yaml);帶 `--selection-file <path>` 是選擇模式
    (驗證並寫檔 primary_hmw_selection.yaml,可以是照抄某個候選的 hmw_id
    參照,也可以是完全自己寫的新張力)。兩者互斥。如果 hmw.yaml 存在但
    entries 是空陣列,代表沒有找到真的張力——這是誠實的空結果,不是失敗,
    印出訊息、exit 0、不寫檔、不動 `run.json` 的候選狀態欄位。`how_might_we()`
    本身不讀這裡寫出的選擇檔,接線留給之後一個獨立的 change。
    """
    p = argparse.ArgumentParser(prog="sbp-gen primary-hmw", description="HMW 主要候選與選擇")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--selection-file", type=Path, default=None, dest="selection_file")
    args = p.parse_args(argv)

    if args.propose and args.selection_file is not None:
        print("--propose 跟 --selection-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    axial_path = run_dir / "axial_coding.yaml"
    hmw_path = run_dir / "hmw.yaml"
    if not axial_path.exists():
        print(f"{axial_path} 不存在，無法做主要 HMW 候選——先跑過 "
              f"`sbp-gen axial-coding --run {run_dir}`。", file=sys.stderr)
        return 1
    if not hmw_path.exists():
        print(f"{hmw_path} 不存在，無法做主要 HMW 候選——先跑過 "
              f"`sbp-gen how-might-we --run {run_dir}`。", file=sys.stderr)
        return 1

    axial_data = yaml.safe_load(axial_path.read_text(encoding="utf-8")) or {}
    hmw_data = yaml.safe_load(hmw_path.read_text(encoding="utf-8")) or {}
    categories = [c for c in (axial_data.get("categories") or []) if isinstance(c, dict)]
    category_ids = {c.get("id") for c in categories if c.get("id")}
    hmw_ids = {e.get("id") for e in (hmw_data.get("entries") or []) if isinstance(e, dict) and e.get("id")}

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    if args.propose:
        opts = {"case_id": case_id}
        narrative_job_selection = _read_primary_job_selection(run_dir, category_ids)
        try:
            data, usage = stages.propose_primary_hmw_candidates(hmw_data, axial_data, opts, narrative_job_selection)
            if data is None:
                print("hmw.yaml 沒有任何張力，無法選出主要 HMW 候選。")
                return 0
            _record_narrative_framing(data, narrative_job_selection)
            _write_yaml(run_dir / "primary_hmw_candidates.yaml", data)
            run_state.setdefault("usage", {})["primary_hmw_candidates"] = usage
            run_state["primary_hmw_candidates_state"] = "complete"
            _persist(run_json_path, run_state)
            print(f"完成：{run_dir / 'primary_hmw_candidates.yaml'} 產生 "
                  f"{len(data.get('candidates') or [])} 則候選主要 HMW")
            return 0
        except Exception as exc:
            run_state["primary_hmw_candidates_state"] = "failed"
            run_state["primary_hmw_candidates_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"主要 HMW 候選失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    if args.selection_file is not None:
        try:
            raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            # Selection-file parsing is an input/operation rejection.  It must
            # not turn a previously completed selection into a lifecycle
            # failure or create run metadata for a run that had none.
            message = f"讀取或解析 {args.selection_file} 失敗：{exc}"
            print(message, file=sys.stderr)
            return 1

        if not isinstance(raw, dict):
            message = (
                f"{args.selection_file} 的內容必須是一個物件，"
                f"包含 hmw_id（參照既有候選）或 statement（自填新的）")
            print(message, file=sys.stderr)
            return 1

        # 理由可留白；自寫那一支只有問題句必填，沒給的選填欄位不寫進檔案
        # （2026-09-27 研究者依版面稿裁定）。
        rationale = raw.get("rationale")
        rationale = "" if rationale is None else rationale
        if "hmw_id" in raw:
            doc = {
                "schema_version": 1,
                "case_id": case_id,
                "rationale": rationale,
                "hmw_id": raw.get("hmw_id"),
            }
        else:
            doc = {
                "schema_version": 1,
                "case_id": case_id,
                "rationale": rationale,
                "statement": raw.get("statement"),
            }
            for key in ("tension", "grounded_in", "evidence_refs"):
                if raw.get(key) not in (None, "", []):
                    doc[key] = raw.get(key)
        errors = stages._primary_hmw_selection_errors(doc, hmw_ids, category_ids)
        if errors:
            # Validation rejection is not a failure of the existing selection
            # lifecycle.  Leave both the selection and run metadata untouched.
            print("; ".join(errors), file=sys.stderr)
            return 1

        _write_yaml(run_dir / "primary_hmw_selection.yaml", doc)
        run_state["primary_hmw_selection_state"] = "complete"
        run_state.pop("primary_hmw_selection_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'primary_hmw_selection.yaml'} 記錄選定主要 HMW")
        return 0

    print("需要 --propose 或 --selection-file 其中一個。", file=sys.stderr)
    return 1


def _main_segmentation_axes(argv: list[str]) -> int:
    """`sbp-gen segmentation-axes --run <dir>`——add-segmentation-axis-designation：
    全新的獨立指令。跟核心目標對象一樣的地方：這個框架點的判斷依據（業務
    邏輯與商業價值）不在證據裡，AI 沒有根據可以排序或推薦（CLAUDE.md「助理
    的界線」）。兩種模式：帶 `--propose` 是簡報模式（讀既有
    persona_matrix.yaml，呼叫模型對每一條軸寫一份不排序的簡報，寫
    segmentation_axis_candidates.yaml）；帶 `--selection-file <path>` 是選擇
    模式（驗證並寫檔 segmentation_axis_selection.yaml——只用 axis_ids 參照
    既有的軸，沒有自填新軸的分支，因為軸已經由 Persona Matrix 階段固定
    下來）。兩者互斥。恰好 2 條軸是這個框架點的規格（交叉出 2×2），schema
    本身已經強制 `minItems`/`maxItems`/`uniqueItems`，這裡的參照完整性檢查
    只補 schema 管不到的「軸是不是真的存在於這次 run」。`persona_matrix()`
    本身不讀這裡寫出的選擇檔，接線留給之後一個獨立的 change（目標族群切分
    ⑤c，見 TARGET-ARCHITECTURE.md 決定十一）。
    """
    p = argparse.ArgumentParser(prog="sbp-gen segmentation-axes", description="目標族群軸簡報與指定")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--selection-file", type=Path, default=None, dest="selection_file")
    p.add_argument("--preview-id", type=str, default=None, dest="preview_id")
    args = p.parse_args(argv)

    if args.propose and args.selection_file is not None:
        print("--propose 跟 --selection-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    persona_matrix_path = run_dir / "persona_matrix.yaml"
    if not persona_matrix_path.exists():
        print(f"{persona_matrix_path} 不存在，無法做目標族群軸簡報——先跑過 "
              f"`sbp-gen persona-matrix --run {run_dir}`。", file=sys.stderr)
        return 1

    persona_matrix_data = yaml.safe_load(persona_matrix_path.read_text(encoding="utf-8")) or {}
    axis_ids = {a.get("id") for a in (persona_matrix_data.get("axes") or [])
                if isinstance(a, dict) and a.get("id")}

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    if args.propose:
        opts = {"case_id": case_id}
        try:
            scope = hmw_scope_module.resolve(run_dir)
            data, usage = stages.propose_segmentation_axis_candidates(persona_matrix_data, opts, hmw_scope=scope)
            if scope:
                data["framed_by"] = {"primary_hmw_selection": hmw_scope_module.digest(scope)}
            _write_yaml(run_dir / "segmentation_axis_candidates.yaml", data)
            run_state.setdefault("usage", {})["segmentation_axis_candidates"] = usage
            run_state["segmentation_axis_candidates_state"] = "complete"
            _persist(run_json_path, run_state)
            print(f"完成：{run_dir / 'segmentation_axis_candidates.yaml'} 產生 "
                  f"{len(data.get('briefs') or [])} 份軸簡報")
            return 0
        except Exception as exc:
            run_state["segmentation_axis_candidates_state"] = "failed"
            run_state["segmentation_axis_candidates_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"目標族群軸簡報失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    if args.selection_file is not None:
        try:
            raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            run_state["segmentation_axis_selection_state"] = "failed"
            run_state["segmentation_axis_selection_failure"] = f"讀取或解析 {args.selection_file} 失敗：{exc}"
            _persist(run_json_path, run_state)
            print(run_state["segmentation_axis_selection_failure"], file=sys.stderr)
            return 1

        if not isinstance(raw, dict):
            run_state["segmentation_axis_selection_state"] = "failed"
            run_state["segmentation_axis_selection_failure"] = (
                f"{args.selection_file} 的內容必須是一個物件，包含 axis_ids 與 business_rationale")
            _persist(run_json_path, run_state)
            print(run_state["segmentation_axis_selection_failure"], file=sys.stderr)
            return 1

        doc = {
            "schema_version": 1,
            "case_id": case_id,
            "axis_ids": raw.get("axis_ids"),
            # 選擇原因可留白（2026-09-15）——schema 只要求這個欄位是字串、不要求非空。
            "business_rationale": raw.get("business_rationale") or "",
        }
        now = dt.datetime.now().isoformat(timespec="seconds")
        preview = gates.preview_mutation(run_dir, "segmentation_axes", doc, now=now)
        if preview.is_mutation and args.preview_id != preview.preview_id:
            run_state["segmentation_axis_selection_state"] = "failed"
            run_state["segmentation_axis_selection_failure"] = (
                f"這會異動已經確認過的目標族群軸，送出前要先預覽影響範圍：{preview.plain_summary}"
                f"（帶 --preview-id {preview.preview_id} 重新送出）"
            )
            _persist(run_json_path, run_state)
            print(run_state["segmentation_axis_selection_failure"], file=sys.stderr)
            return 1

        try:
            gates.apply_mutation(
                run_dir, "segmentation_axes", doc, args.preview_id or "", now=now,
                validate=lambda d: stages._segmentation_axis_selection_errors(d, axis_ids),
            )
        except gates.ValidationFailed as exc:
            run_state["segmentation_axis_selection_state"] = "failed"
            run_state["segmentation_axis_selection_failure"] = "; ".join(exc.errors)
            _persist(run_json_path, run_state)
            print(run_state["segmentation_axis_selection_failure"], file=sys.stderr)
            return 1
        except gates.StalePreview as exc:
            run_state["segmentation_axis_selection_state"] = "failed"
            run_state["segmentation_axis_selection_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(str(exc), file=sys.stderr)
            return 1

        run_state["segmentation_axis_selection_state"] = "complete"
        run_state.pop("segmentation_axis_selection_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'segmentation_axis_selection.yaml'} 記錄指定的目標族群軸")
        return 0

    print("需要 --propose 或 --selection-file 其中一個。", file=sys.stderr)
    return 1


def _main_target_segmentation(argv: list[str]) -> int:
    """`sbp-gen target-segmentation --run <dir>`——add-target-segmentation-
    designation：全新的獨立指令,取代（規格層級,不是程式碼層級——見
    proposal.md Non-Goals）primary-persona 的核心目標對象判斷。兩種模式：
    帶 `--propose` 是畫像模式（讀既有 persona_matrix.yaml 與
    segmentation_axis_selection.yaml，把受訪者機械分派到 2×2 的四格，只對
    有成員的格子呼叫模型寫畫像，寫 target_segmentation_candidates.yaml）；
    帶 `--selection-file <path>` 是選擇模式（驗證並寫檔
    target_segmentation_selection.yaml——記錄主要目標(必填理由)與選填的
    次要目標(附了格子就一定要附理由)）。兩者互斥。次要目標只記錄,不接進
    任何下游產出（決定十一第 4 點）。
    """
    p = argparse.ArgumentParser(prog="sbp-gen target-segmentation", description="目標族群 2x2 切分與主要/次要目標指定")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--propose", action="store_true")
    p.add_argument("--selection-file", type=Path, default=None, dest="selection_file")
    p.add_argument("--preview-id", type=str, default=None, dest="preview_id")
    args = p.parse_args(argv)

    if args.propose and args.selection_file is not None:
        print("--propose 跟 --selection-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    persona_matrix_path = run_dir / "persona_matrix.yaml"
    if not persona_matrix_path.exists():
        print(f"{persona_matrix_path} 不存在，無法做目標族群切分——先跑過 "
              f"`sbp-gen persona-matrix --run {run_dir}`。", file=sys.stderr)
        return 1
    segmentation_selection_path = run_dir / "segmentation_axis_selection.yaml"
    if not segmentation_selection_path.exists():
        print(f"{segmentation_selection_path} 不存在，無法做目標族群切分——先跑過 "
              f"`sbp-gen segmentation-axes --run {run_dir} --selection-file <path>`。", file=sys.stderr)
        return 1

    persona_matrix_data = yaml.safe_load(persona_matrix_path.read_text(encoding="utf-8")) or {}
    segmentation_selection_data = yaml.safe_load(segmentation_selection_path.read_text(encoding="utf-8")) or {}

    run_json_path = run_dir / "run.json"
    run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
    case_path = run_dir / "case.yaml"
    case_id = run_dir.name
    if case_path.exists():
        case = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_id = case.get("case_id") or case_id

    if args.propose:
        evidence_path = run_dir / "evidence.yaml"
        if not evidence_path.exists():
            print(f"{evidence_path} 不存在，無法做目標族群切分——先跑過主要生成指令，"
                  f"拿到帶 evidence.yaml 的 run 目錄再回來跑。", file=sys.stderr)
            return 1
        if not gates.is_derivation_runnable(
            run_dir, "target_segmentation_candidates", now=dt.datetime.now().isoformat(timespec="seconds"),
        ):
            print("目標族群軸還沒確認過目前這一版，需要先重新確認才能重新產生候選——"
                  f"跑 `sbp-gen segmentation-axes --run {run_dir} --selection-file <path>`。", file=sys.stderr)
            return 1
        evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8")) or []
        opts = {"case_id": case_id}
        try:
            data, usage = stages.target_segmentation(evidence, persona_matrix_data, segmentation_selection_data, opts)
            _write_yaml(run_dir / "target_segmentation_candidates.yaml", data)
            run_state.setdefault("usage", {})["target_segmentation"] = usage
            run_state["target_segmentation_candidates_state"] = "complete"
            _persist(run_json_path, run_state)
            non_empty = sum(1 for q in data.get("quadrants") or [] if stages._quadrant_suggested_ids(q))
            print(f"完成：{run_dir / 'target_segmentation_candidates.yaml'} 產生 4 格，"
                  f"{non_empty} 格有建議成員")
            return 0
        except Exception as exc:
            run_state["target_segmentation_candidates_state"] = "failed"
            run_state["target_segmentation_candidates_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(f"目標族群切分失敗，run 其他狀態不受影響：{exc}", file=sys.stderr)
            return 1

    if args.selection_file is not None:
        candidates_path = run_dir / "target_segmentation_candidates.yaml"
        if not candidates_path.exists():
            print(f"{candidates_path} 不存在，無法指定主要/次要目標——先跑過 "
                  f"`sbp-gen target-segmentation --run {run_dir} --propose`。", file=sys.stderr)
            return 1
        candidates_data = yaml.safe_load(candidates_path.read_text(encoding="utf-8")) or {}
        quadrant_ids = {q.get("id") for q in (candidates_data.get("quadrants") or [])
                        if isinstance(q, dict) and q.get("id")}
        non_empty_quadrant_ids = {
            q.get("id") for q in (candidates_data.get("quadrants") or [])
            if isinstance(q, dict) and q.get("id") and stages._quadrant_suggested_ids(q)
        }
        unclassified_subject_ids = set(candidates_data.get("unclassified_subject_ids") or [])
        subject_ids = {
            s.get("id") for s in (persona_matrix_data.get("subjects") or [])
            if isinstance(s, dict) and s.get("id")
        }

        try:
            raw = yaml.safe_load(args.selection_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            run_state["target_segmentation_selection_state"] = "failed"
            run_state["target_segmentation_selection_failure"] = f"讀取或解析 {args.selection_file} 失敗：{exc}"
            _persist(run_json_path, run_state)
            print(run_state["target_segmentation_selection_failure"], file=sys.stderr)
            return 1

        if not isinstance(raw, dict):
            run_state["target_segmentation_selection_state"] = "failed"
            run_state["target_segmentation_selection_failure"] = (
                f"{args.selection_file} 的內容必須是一個物件，包含 primary_quadrant_id 與 primary_rationale")
            _persist(run_json_path, run_state)
            print(run_state["target_segmentation_selection_failure"], file=sys.stderr)
            return 1

        doc = {
            "schema_version": 1,
            "case_id": case_id,
            "primary_quadrant_id": raw.get("primary_quadrant_id"),
            # 選擇原因可留白（2026-09-15）——schema 只要求這個欄位是字串、不要求非空，
            # 省略時當空字串，不當成缺欄位。
            "primary_rationale": raw.get("primary_rationale") or "",
        }
        if raw.get("secondary_quadrant_id") is not None:
            doc["secondary_quadrant_id"] = raw.get("secondary_quadrant_id")
        if raw.get("secondary_rationale") is not None:
            doc["secondary_rationale"] = raw.get("secondary_rationale")
        if raw.get("quadrant_members") is not None:
            doc["quadrant_members"] = raw.get("quadrant_members")
        if raw.get("unplaced_dispositions") is not None:
            doc["unplaced_dispositions"] = raw.get("unplaced_dispositions")

        now = dt.datetime.now().isoformat(timespec="seconds")
        preview = gates.preview_mutation(run_dir, "target_segmentation", doc, now=now)
        if preview.is_mutation and args.preview_id != preview.preview_id:
            run_state["target_segmentation_selection_state"] = "failed"
            run_state["target_segmentation_selection_failure"] = (
                f"這會異動已經確認過的主要/次要目標，送出前要先預覽影響範圍：{preview.plain_summary}"
                f"（帶 --preview-id {preview.preview_id} 重新送出）"
            )
            _persist(run_json_path, run_state)
            print(run_state["target_segmentation_selection_failure"], file=sys.stderr)
            return 1

        try:
            gates.apply_mutation(
                run_dir, "target_segmentation", doc, args.preview_id or "", now=now,
                validate=lambda d: stages._target_segmentation_selection_errors(
                    d, quadrant_ids, subject_ids, unclassified_subject_ids, non_empty_quadrant_ids),
            )
        except gates.ValidationFailed as exc:
            run_state["target_segmentation_selection_state"] = "failed"
            run_state["target_segmentation_selection_failure"] = "; ".join(exc.errors)
            _persist(run_json_path, run_state)
            print(run_state["target_segmentation_selection_failure"], file=sys.stderr)
            return 1
        except gates.StalePreview as exc:
            run_state["target_segmentation_selection_state"] = "failed"
            run_state["target_segmentation_selection_failure"] = str(exc)
            _persist(run_json_path, run_state)
            print(str(exc), file=sys.stderr)
            return 1

        run_state["target_segmentation_selection_state"] = "complete"
        run_state.pop("target_segmentation_selection_failure", None)
        _persist(run_json_path, run_state)
        print(f"完成：{run_dir / 'target_segmentation_selection.yaml'} 記錄指定的主要/次要目標")
        return 0

    print("需要 --propose 或 --selection-file 其中一個。", file=sys.stderr)
    return 1


def _main_improvement_goal_review(argv: list[str]) -> int:
    """`sbp-gen improvement-goal-review --run <dir>`——add-improvement-goal-
    review-stage：站⑩的改善目標確認/改寫。跟另外四個框架點機制不同的地方：
    這裡沒有候選機制，`--confirm-file` 直接收研究者自己的文字，不管是原封
    不動確認、還是改寫過的版本（決定一：閘門要留著，只是它擋的值從站①換成
    站⑩；決定六：站①的原始目標要能繼續當比對用的證偽基準）。`--show` 跟
    `--confirm-file` 互斥，兩者都沒給時視為使用方式錯誤。
    """
    p = argparse.ArgumentParser(prog="sbp-gen improvement-goal-review", description="站⑩改善目標確認/改寫")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--show", action="store_true")
    p.add_argument("--confirm-file", type=Path, default=None, dest="confirm_file")
    args = p.parse_args(argv)

    if args.show and args.confirm_file is not None:
        print("--show 跟 --confirm-file 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir

    if args.show:
        original_goal = improvement_goal_review.read_original_goal(run_dir)
        hmw_statements = improvement_goal_review.read_hmw_statements(run_dir)
        print("研究前的改善目標：")
        print(original_goal if original_goal else "（沒有記錄——研究前沒有填寫改善目標）")
        print()
        print("目前確認的 HMW 陳述：")
        if hmw_statements:
            for statement in hmw_statements:
                print(f"- {statement}")
        else:
            print("（目前沒有 HMW 陳述）")
        return 0

    if args.confirm_file is not None:
        run_json_path = run_dir / "run.json"
        run_state = json.loads(run_json_path.read_text(encoding="utf-8")) if run_json_path.exists() else {}
        try:
            raw = yaml.safe_load(args.confirm_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as exc:
            run_state["improvement_goal_review_state"] = "failed"
            run_state["improvement_goal_review_failure"] = f"讀取或解析 {args.confirm_file} 失敗：{exc}"
            _persist(run_json_path, run_state)
            print(run_state["improvement_goal_review_failure"], file=sys.stderr)
            return 1

        if not isinstance(raw, dict):
            run_state["improvement_goal_review_state"] = "failed"
            run_state["improvement_goal_review_failure"] = (
                f"{args.confirm_file} 的內容必須是一個物件，包含 operative_goal（必填）"
                f"與 framing_changed_note（可選）")
            _persist(run_json_path, run_state)
            print(run_state["improvement_goal_review_failure"], file=sys.stderr)
            return 1

        # confirm-improvement-goal-save-only: the one shared write.
        _doc, errors = improvement_goal_review.confirm(
            run_dir, raw.get("operative_goal"), raw.get("framing_changed_note") if "framing_changed_note" in raw else None)
        if errors:
            print("; ".join(errors), file=sys.stderr)
            return 1
        print(f"完成：{run_dir / improvement_goal_review.REVIEW_FILENAME} 記錄確認的改善目標")
        return 0

    print("需要 --show 或 --confirm-file 其中一個。", file=sys.stderr)
    return 1


def _main_decisions(argv: list[str]) -> int:
    """`sbp-gen decisions --case <dir>`——add-case-level-decisions-index：
    掃描既有輸出、不呼叫模型、不寫 run.json，把六個候選機制(核心類別／
    Persona維度／主要Job／主要HMW／目標族群軸／主要目標)分散在各個
    runs/ 目錄裡的選擇結果整理成一份案子層級的 decisions.yaml
    （add-segmentation-axis-designation：第五個框架點；
    add-target-segmentation-designation：第六個框架點，取代原本核心目標
    對象的位置，見 decisions.py 的說明）。`--case` 是必要的字面路徑參數，不做
    案子目錄自動探索。runs_root 不是寫死的相對路徑——`python3 -m sbp_gen`
    的實際慣例是從 `tools/` 目錄執行（見 tools/README.md，`cases/`／`runs/`
    都用 `../` 前綴），CWD 不保證是專案根目錄，所以改成從 `--case` 給的路徑
    往上解析：`cases/<name>` 的上一層就是專案根目錄，`runs/` 是它的手足目錄
    ——這樣不管從專案根目錄還是從 `tools/` 執行都能找到同一個 `runs/`
    （design.md 原本假設 CWD 固定是專案根目錄，這裡是實作時發現並修正的地方）。
    """
    p = argparse.ArgumentParser(prog="sbp-gen decisions", description="案子層級判斷軌跡索引")
    p.add_argument("--case", required=True, type=Path, dest="case_dir")
    args = p.parse_args(argv)

    case_path = args.case_dir / "case.yaml"
    if not case_path.exists():
        print(f"{case_path} 不存在，無法建立判斷軌跡索引。", file=sys.stderr)
        return 1

    runs_root = args.case_dir.resolve().parent.parent / "runs"
    try:
        data = decisions.build_decisions(args.case_dir, runs_root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    errors = V.structural_errors(data, V.load_schema("decisions"))
    if errors:
        print("; ".join(errors), file=sys.stderr)
        return 1

    _write_yaml(args.case_dir / "decisions.yaml", data)

    labels = {
        "core_category": "核心類別",
        "narrative_job": "顧客任務",
        "persona_dimensions": "Persona 維度",
        "primary_job": "主要 Job",
        "primary_hmw": "主要 HMW",
        "segmentation_axes": "目標族群軸",
        "primary_target": "主要目標",
    }
    for key, label in labels.items():
        point = data["decisions"][key]
        if point["status"] == "decided":
            print(f"{label}：已決定（來自 {point['run']}）")
        else:
            print(f"{label}：尚未決定")

    return 0


# project-existing-graph-without-model-call: --show translates the
# projection's own coverage_state enum into researcher-facing language.
# Only used when a cell carries no blank_note of its own (has_evidence never
# does) -- every blank_note the projection writes is already researcher-
# facing prose (see stages.project()'s cell-state block) and is surfaced
# verbatim instead of being re-translated here.
_COVERAGE_STATE_TEXT = {
    "has_evidence": "有訪談證據",
}


def _project_show_line(cell: dict) -> str:
    note = cell.get("blank_note")
    if note:
        return note
    return _COVERAGE_STATE_TEXT.get(cell.get("coverage_state"), "未知的狀態")


def _main_project(argv: list[str]) -> int:
    """`sbp-gen project --run <dir>`——project-existing-graph-without-model-
    call：把一個已經有服務結構的 run 投影成服務藍圖草稿，完全不呼叫模型
    （`stages.project()` 本身是純函式）。三種模式：不帶旗標是寫入模式；
    `--show` 是預覽模式（唯讀，把覆蓋狀況與缺口用研究者的語言講出來，不
    印 schema 列舉值原文或內部 id）；`--overwrite` 允許覆寫已存在的
    `sbp_draft.yaml`（預設拒絕，因為那份檔案研究者可能已經在介面上修正
    過，純函式重跑會把修正沖掉）。`--show` 跟 `--overwrite` 互斥。
    """
    p = argparse.ArgumentParser(prog="sbp-gen project", description="把已有的服務結構投影成服務藍圖草稿，不呼叫模型")
    p.add_argument("--run", required=True, type=Path, dest="run_dir")
    p.add_argument("--show", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)

    if args.show and args.overwrite:
        print("--show 跟 --overwrite 互斥，一次只能用一種模式。", file=sys.stderr)
        return 1

    run_dir = args.run_dir
    required = {
        "service_graph.yaml": run_dir / "service_graph.yaml",
        "case.yaml": run_dir / "case.yaml",
        "journey.yaml": run_dir / "journey.yaml",
        "conflicts.yaml": run_dir / "conflicts.yaml",
    }
    present = {name: path.is_file() for name, path in required.items()}
    if not all(present.values()):
        missing = [name for name, ok in present.items() if not ok]
        have = [name for name, ok in present.items() if ok]
        lines = [f"{run_dir} 缺少投影需要的檔案：{'、'.join(missing)}"]
        if have:
            lines.append(f"已經有的：{'、'.join(have)}")
        run_json_path = run_dir / "run.json"
        if run_json_path.is_file():
            try:
                run_state = json.loads(run_json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                run_state = {}
            failure = run_state.get("failure") if isinstance(run_state, dict) else None
            if isinstance(failure, dict) and failure.get("message"):
                lines.append(f"這個 run 記錄的失敗原因（{failure.get('stage', '未知階段')}）：{failure['message']}")
        print("\n".join(lines), file=sys.stderr)
        return 1

    draft_path = run_dir / "sbp_draft.yaml"
    if not args.show and not args.overwrite and draft_path.is_file():
        print(
            f"{draft_path} 已經存在。重新投影會蓋掉研究者已經在介面上做過的檢視與修正——"
            f"確定要重畫請加 --overwrite；只想看重畫後會長什麼樣、不動現有的圖，用 --show。",
            file=sys.stderr,
        )
        return 1

    evidence, evidence_status = evidence_locator.locate_evidence(run_dir)
    if evidence is None:
        if evidence_status == "ambiguous":
            print(
                f"{run_dir} 的案例目錄裡有不只一份證據檔，沒有紀錄這次分析用的是哪一份，不猜。"
                f"請先確認研究者所指的證據檔並補上這個 run 的 evidence.yaml。",
                file=sys.stderr,
            )
        else:
            print(f"{run_dir} 找不到可用的證據檔（沒有 run 內副本，run.json 也沒有指到任何一份）。", file=sys.stderr)
        return 1

    graph = yaml.safe_load((run_dir / "service_graph.yaml").read_text(encoding="utf-8")) or {}
    conflicts = yaml.safe_load((run_dir / "conflicts.yaml").read_text(encoding="utf-8")) or []
    case = yaml.safe_load((run_dir / "case.yaml").read_text(encoding="utf-8")) or {}
    journey_data = yaml.safe_load((run_dir / "journey.yaml").read_text(encoding="utf-8")) or {}

    draft = stages.project(
        graph, None, conflicts, case,
        journey_data=journey_data, annotations=graph.get("annotations"), evidence=evidence,
    )
    draft["meta"].update({"prompt_hash": _prompt_hash(), "timestamp": dt.datetime.now().strftime("%Y-%m-%d")})
    evidence_ids = {e["id"] for e in evidence if isinstance(e, dict) and e.get("id")}
    errors = V.structural_errors(draft, V.load_schema("sbp-draft")) + V.projection_errors(draft, evidence_ids, case, journey=journey_data)
    if errors:
        print("投影契約無效：\n" + "\n".join(errors), file=sys.stderr)
        return 1

    if args.show:
        cells = draft.get("cells") or []
        print(f"服務藍圖投影預覽　{case.get('service_type') or case.get('case_id', '')}")
        by_layer: dict[str, list] = {}
        for cell in cells:
            by_layer.setdefault(cell.get("layer"), []).append(cell)
        for layer, layer_cells in by_layer.items():
            print(f"\n{layer}")
            for cell in sorted(layer_cells, key=lambda c: c.get("step") or 0):
                print(f"  第 {cell.get('step')} 步：{_project_show_line(cell)}")
        gaps = draft.get("research_gaps") or []
        if gaps:
            print("\n沒有服務動作在處理的痛點：")
            for gap in gaps:
                question = gap.get("question")
                if question:
                    print(f"  {question}")
        print("\n投影檢查通過，沒有錯誤。要寫入請拿掉 --show。")
        return 0

    _write_yaml(draft_path, draft)
    cells = draft.get("cells") or []
    steps = len({c.get("step") for c in cells})
    layers = len({c.get("layer") for c in cells})
    print(f"完成：{draft_path}（{len(cells)} 格：{steps} 步驟 × {layers} 泳道）")
    return 0


# 子指令 → 進入點的名字。原本是一串 `if sys.argv[1] == "..."` 的鏈；改成表格是因為
# 手動傳輸要能回答「這道指令是不是多階段管線」，而那個答案在鏈式寫法裡只能靠
# 「有沒有落到最後的 fallback」間接推得（make-manual-transport-repeatable）。
SUBCOMMANDS = {
    "coding-candidates": "_main_coding_candidates",
    "decisions": "_main_decisions",
    "journey": "_main_journey",
    "journey-solutions": "_main_journey_solutions",
    "journey-branch": "_main_journey_branch",
    "journey-annotations": "_main_journey_annotations",
    "chart-guides": "_main_chart_guides",
    "selective-themes": "_main_selective_themes",
    "compare": "_main_compare",
    "structure": "_main_structure",
    "primary-hmw": "_main_primary_hmw",
    "segmentation-axes": "_main_segmentation_axes",
    "target-segmentation": "_main_target_segmentation",
    "improvement-goal-review": "_main_improvement_goal_review",
    "primary-job": "_main_primary_job",
    "persona-dimensions": "_main_persona_dimensions",
    "selective-coding": "_main_selective_coding",
    "selective-coding-narrative": "_main_selective_coding_narrative",
    "narrative-job-candidates": "_main_narrative_job_candidates",
    "narrative-job-selection": "_main_narrative_job_selection",
    "semantic-segments": "_main_semantic_segments",
    "open-coding": "_main_open_coding",
    "category-properties": "_main_category_properties",
    "axial-coding": "_main_axial_coding",
    "persona-matrix": "_main_persona_matrix",
    "persona-content": "_main_persona_content",
    "jtbd": "_main_jtbd",
    "job-map": "_main_job_map",
    "how-might-we": "_main_how_might_we",
    "form-hmw": "_main_form_hmw",
    "reference": "_main_reference",
    "journey-recheck": "_main_journey_recheck",
    "as-is-research": "_main_as_is_research",
    "implementation-research": "_main_implementation_research",
    "to-be": "_main_to_be",
    "to-be-revision": "_main_to_be_revision",
    "project": "_main_project",
}

# 暫停不是失敗，退出碼要分得開，否則腳本無法區分「等人貼回覆」跟「這一步爛掉了」。
TRANSPORT_SUSPENDED_STATUS = 3


def _transport_selected() -> bool:
    """任何一個 provider 環境變數指向手動傳輸就算數——單階段指令沒有
    `--provider` 旗標，provider 只能從環境變數來。"""
    if os.environ.get("SBP_LLM_PROVIDER") == "transport":
        return True
    return any(
        key.startswith("SBP_") and key.endswith("_PROVIDER") and value == "transport"
        for key, value in os.environ.items()
    )


# relay-external-research-through-manual-transport: `--run-dir` is here
# because the research command spells it that way while every other
# single-stage command spells it `--run`. Recognising only `--run` left the
# transport workspace unset for exactly the one command this change is for,
# so every relay refused to start with a message about a missing run
# directory.
_RUN_DIR_FLAGS = ("--run", "--run-dir")


def _run_dir_from_argv(argv: list[str]) -> Path | None:
    for index, token in enumerate(argv):
        if token in _RUN_DIR_FLAGS and index + 1 < len(argv):
            return Path(argv[index + 1])
        for flag in _RUN_DIR_FLAGS:
            if token.startswith(f"{flag}="):
                return Path(token.split("=", 1)[1])
    return None


def _dispatch(argv: list[str]) -> int:
    # 進入點是在呼叫當下才從模組取出來的，不是 import 時就綁進表格：測試靠
    # monkeypatch 換掉模組屬性來確認路由走對了哪一支，綁死函式物件會讓那些
    # 測試換不到（本次重構實際打破了七條，這是修回來的做法）。
    if len(argv) > 1 and argv[1] in SUBCOMMANDS:
        return globals()[SUBCOMMANDS[argv[1]]](argv[2:])
    return _main_generate(argv[1:])


def main() -> int:
    argv = sys.argv
    if not _transport_selected():
        return _dispatch(argv)

    from . import transport_llm

    if len(argv) <= 1 or argv[1] not in SUBCOMMANDS:
        # 多階段管線在中途停下來會留下殘缺狀態，續跑又會把已完成的階段重跑一次。
        print(
            "手動傳輸只支援單階段指令，不支援整條管線。可用的有："
            + "、".join(sorted(SUBCOMMANDS)),
            file=sys.stderr,
        )
        return 1

    run_dir = _run_dir_from_argv(argv)
    transport_llm.configure_workspace(run_dir)
    try:
        status = _dispatch(argv)
    except transport_llm.TransportSuspended as suspension:
        # argv[0] 在 `python3 -m sbp_gen` 底下是 __main__.py 的實體路徑，照抄
        # 出來的指令貼回去跑不動。要給操作者能直接複製的那一行。
        invocation = (
            ["python3", "-m", "sbp_gen"] + argv[1:]
            if argv and argv[0].endswith("__main__.py")
            else argv
        )
        print(
            suspension.operator_instructions(command=" ".join(invocation)),
            file=sys.stderr,
        )
        return TRANSPORT_SUSPENDED_STATUS
    done = transport_llm.consumed_stages()
    if status == 0 and done and run_dir is not None:
        transport_llm.record_provenance(run_dir / "run.json", done)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
