"""Authorized live runner for the real muji-cafe-meal-001 evaluation lane.

See docs/research/integrated-desktop-workbench-real-case-protocol.md for the
full protocol this implements (validate-integrated-desktop-workbench-experience,
task 4.1). This script owns only the pre-review-server phase of a fresh real-case
run: it gate-checks authorization/de-identification, refuses reused or frozen
input, invokes the production `sbp_gen` entrypoint exactly as an operator would
by hand (never a reimplementation of stage generation), and seals a provenance
manifest for the align+journey stages it ran. Journey confirmation, As-is
review, To-be review, and validation-plan creation are researcher decisions
made through the normal review server (task 4.2) — this script does not touch
them, and its sealed manifest is intentionally a subset of the full
`real_case_evidence` schema shape (see the module docstring on
`seal_generation_manifest` for exactly which fields are deferred).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import yaml

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

from sbp_gen import ingest, lifecycle, llm  # noqa: E402

CANONICAL_CASE_ID = "muji-cafe-meal-001"
FROZEN_FIXTURE_ROOT = TOOLS_DIR / "tests" / "fixtures" / "integrated-desktop-workbench"
DEFAULT_DENYLIST_NAMES = ("gt_muji_from_final_report.md",)
# align+journey only: this script covers the pre-confirmation phase of a fresh
# run. compare/structure/reference/reconcile/brief run later through the
# review server once a researcher has confirmed the journey (task 4.2).
REQUIRED_ROUTE_STAGES = ("align", "journey")
STAGE_OUTPUT_FILES = {"align": "evidence.yaml", "journey": "journey_proposal.yaml"}
# Names that mark a directory as an existing run's output rather than raw
# interview material — used to refuse a downstream artifact passed off as
# fresh raw-source input.
DOWNSTREAM_ARTIFACT_MARKERS = ("run.json", "journey_proposal.yaml", "journey_confirmed.yaml", "evidence.yaml")


class RealCaseGateError(RuntimeError):
    """One refused precondition. `code` matches a design.md failure mode."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Gates — each one is a pure, independently testable refusal. main() runs all
# of them before any network-capable call.
# --------------------------------------------------------------------------
def require_attestations(*, authorization_attested: bool, deidentification_attested: bool) -> None:
    if not authorization_attested:
        raise RealCaseGateError("authorization_missing", "研究者授權未確認，拒絕呼叫外部 API")
    if not deidentification_attested:
        raise RealCaseGateError("deidentification_missing", "去識別化確認未完成，拒絕呼叫外部 API")


def require_canonical_case(case_id: str) -> None:
    if case_id != CANONICAL_CASE_ID:
        raise RealCaseGateError(
            "frozen_worker_substitution",
            f"real-case runner 只接受 {CANONICAL_CASE_ID!r} 這個案例，收到 {case_id!r}",
        )


def require_not_frozen_path(*paths: Path) -> None:
    frozen = FROZEN_FIXTURE_ROOT.resolve()
    for path in paths:
        resolved = path.resolve()
        if resolved == frozen or frozen in resolved.parents:
            raise RealCaseGateError(
                "frozen_worker_substitution",
                f"{path} 落在凍結測試語料目錄下，real-case run 不得使用凍結 worker 的路徑",
            )


def require_fresh_output(out_dir: Path) -> None:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RealCaseGateError(
            "replay",
            f"輸出目錄 {out_dir} 已存在且非空；real-case run 必須寫入全新目錄，不得重放既有輸出",
        )


def require_raw_source_not_downstream(raw_source: Path) -> None:
    for marker in DOWNSTREAM_ARTIFACT_MARKERS:
        hit = next(raw_source.rglob(marker), None)
        if hit is not None:
            raise RealCaseGateError(
                "reused_downstream_artifact",
                f"{raw_source} 內含既有 run 的產物 {hit}，不是原始逐字稿；拒絕當作 raw-source",
            )


def resolve_denylist(case_dir: Path, extra: Iterable[Path] = ()) -> list[Path]:
    candidates = [case_dir / name for name in DEFAULT_DENYLIST_NAMES]
    candidates.extend(Path(p) for p in extra)
    return [p for p in candidates if p.exists()]


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def require_denylist_clear(raw_source: Path, denylist: Iterable[Path]) -> None:
    denylist = list(denylist)
    if not denylist:
        return
    denylist_resolved = {p.resolve() for p in denylist}
    # Match by content digest too, not just path identity — a copy of the
    # held-out analysis under a different filename inside raw-source would
    # otherwise slip past a path-only check.
    denylist_digests = {_file_digest(p) for p in denylist}
    for candidate in raw_source.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.resolve() in denylist_resolved or _file_digest(candidate) in denylist_digests:
            raise RealCaseGateError(
                "denylist_access",
                f"{candidate} 在保留給封存後比對用的分析清單上，generation-time 不得存取，拒絕執行",
            )


def compute_source_manifest_digest(raw_source: Path) -> tuple[dict, str]:
    _, manifest = ingest.ingest(raw_source)
    return manifest, lifecycle.canonical_content_digest(manifest)


def require_source_digest_match(computed: str, expected: str | None) -> None:
    if expected and computed != expected:
        raise RealCaseGateError(
            "source_digest_mismatch",
            f"raw-source manifest digest 與預期不符（預期 {expected}，實際 {computed}）；"
            "原始資料可能在授權之後被更動過，拒絕執行",
        )


def resolve_routing_policy(stage_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    policy = {}
    for stage in stage_ids:
        provider = llm.resolve_provider(stage=stage)
        model = llm.default_model(stage, provider)
        effort = llm.DEFAULT_EFFORT.get(stage, "medium")
        policy[stage] = {"provider": provider, "model": model, "effort": effort}
    return policy


def routing_policy_digest(policy: Mapping[str, Mapping[str, str]]) -> str:
    return lifecycle.canonical_content_digest(policy)


def require_route_match(stage: str, *, provider: str, model: str, policy: Mapping[str, Mapping[str, str]]) -> None:
    expected = policy.get(stage)
    if expected is None or expected["provider"] != provider or expected["model"] != model:
        raise RealCaseGateError(
            "route_mismatch",
            f"{stage} 實際使用 {provider}/{model}，跟執行前鎖定的 routing policy {expected} 不一致",
        )


def require_research_not_current_state(
    research_sources: Iterable[Mapping], current_state_claim_ids: Iterable[str]
) -> None:
    blocked = set(current_state_claim_ids)
    for source in research_sources:
        overlap = set(source.get("supported_claim_ids") or []) & blocked
        if overlap:
            raise RealCaseGateError(
                "external_source_as_current_evidence",
                f"研究來源 {source.get('source_id')} 被拿來支持現況（As-is）主張 {sorted(overlap)}；"
                "外部研究只能佐證詮釋／To-be，不能變成 MUJI 現況的證據",
            )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def build_stage_runs(out_dir: Path, run_json: dict, stage_ids: Iterable[str], policy: Mapping) -> list[dict]:
    stage_runs = []
    for stage in stage_ids:
        if run_json.get("stages", {}).get(stage) != "complete":
            continue
        provider = run_json["providers"][stage]
        model = llm.model_name(run_json["models"][stage])
        effort = run_json["efforts"][stage]
        require_route_match(stage, provider=provider, model=model, policy=policy)
        usage = run_json.get("usage", {}).get(stage, {})
        output_path = out_dir / STAGE_OUTPUT_FILES[stage]
        output_digest = lifecycle.canonical_content_digest(
            yaml.safe_load(output_path.read_text(encoding="utf-8"))
        )
        prompt_path = TOOLS_DIR / "sbp_gen" / "prompts" / f"{stage}.md"
        stage_runs.append({
            "stage": stage,
            "provider": provider,
            "model": model,
            "prompt_version": lifecycle.canonical_content_digest(prompt_path.read_text(encoding="utf-8")),
            "schema_version": "1",
            "effort": effort,
            "started_at": run_json["started_at"],
            "completed_at": run_json["finished_at"],
            "attempts": [{
                "attempt": 1,
                "provider": provider,
                "model": model,
                "outcome": "accepted",
                "usage_tokens": usage.get("tokens", 0),
            }],
            "published_output_digest": output_digest,
        })
    return stage_runs


def seal_generation_manifest(
    *,
    out_dir: Path,
    case_id: str,
    run_id: str,
    source_manifest_digest: str,
    denylist_digest: str,
    routing_policy_digest_value: str,
    required_stage_ids: Iterable[str],
    stage_runs: list[dict],
    research_sources: list[dict],
    sealed_at: str,
) -> dict:
    """Write the generation-phase provenance seal.

    This is deliberately a SUBSET of the schema's `real_case_evidence` shape —
    it omits `lifecycle_valid`, `traceability`, `human_rubric`,
    `validation_state`, `fabricated_pilot_data`, `limitations`, `status`, and
    `held_out.comparison_completed_after_seal` / `comparison_digest`. Those
    depend on the researcher-driven journey/As-is/To-be/validation decisions
    and the post-seal held-out comparison (task 4.2's responsibility) and
    cannot be known at generation time.
    """
    artifact_chain_digest = lifecycle.canonical_content_digest({
        "case_id": case_id,
        "run_id": run_id,
        "source_manifest_digest": source_manifest_digest,
        "stage_runs": stage_runs,
    })
    manifest = {
        "case_id": case_id,
        "run_id": run_id,
        "execution_mode": "live",
        "source_manifest_digest": source_manifest_digest,
        "authorization_attested": True,
        "deidentification_attested": True,
        "frozen_worker_results_used": False,
        "reused_downstream_artifacts": False,
        "held_out": {
            "denylist_digest": denylist_digest,
            "access_before_seal_count": 0,
        },
        "routing_policy_digest": routing_policy_digest_value,
        "required_stage_ids": list(required_stage_ids),
        "stage_runs": stage_runs,
        "artifact_chain_digest": artifact_chain_digest,
        "research_sources": research_sources,
        "sealed_at": sealed_at,
    }
    (out_dir / "real_case_generation_seal.yaml").write_text(
        yaml.dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--raw-source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--attest-authorization", action="store_true")
    parser.add_argument("--attest-deidentification", action="store_true")
    parser.add_argument("--expected-source-manifest-digest", default=None)
    parser.add_argument("--denylist", action="append", default=[], type=Path)
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    case = yaml.safe_load((args.case_dir / "case.yaml").read_text(encoding="utf-8"))

    try:
        require_attestations(
            authorization_attested=args.attest_authorization,
            deidentification_attested=args.attest_deidentification,
        )
        require_canonical_case(case.get("case_id"))
        require_not_frozen_path(args.case_dir, args.raw_source, args.out)
        require_fresh_output(args.out)
        require_raw_source_not_downstream(args.raw_source)
        denylist = resolve_denylist(args.case_dir, args.denylist)
        require_denylist_clear(args.raw_source, denylist)
        _, source_digest = compute_source_manifest_digest(args.raw_source)
        require_source_digest_match(source_digest, args.expected_source_manifest_digest)
    except RealCaseGateError as exc:
        print(f"[real-case:{exc.code}] {exc}", file=sys.stderr)
        return 2

    policy = resolve_routing_policy(REQUIRED_ROUTE_STAGES)

    completed = subprocess.run(
        [
            sys.executable, "-m", "sbp_gen",
            "--case", str(args.case_dir / "case.yaml"),
            "--input", str(args.raw_source),
            "--out", str(args.out),
            "--discover-journey",
        ],
        cwd=TOOLS_DIR,
    )
    if completed.returncode != 0:
        print("[real-case] sbp_gen 執行失敗，未封存", file=sys.stderr)
        return 1

    run_json = json.loads((args.out / "run.json").read_text(encoding="utf-8"))
    try:
        stage_runs = build_stage_runs(args.out, run_json, REQUIRED_ROUTE_STAGES, policy)
    except RealCaseGateError as exc:
        print(f"[real-case:{exc.code}] {exc}", file=sys.stderr)
        return 2

    denylist_digest = lifecycle.canonical_content_digest(sorted(str(p) for p in denylist))
    seal_generation_manifest(
        out_dir=args.out,
        case_id=case["case_id"],
        run_id=args.run_id or args.out.name,
        source_manifest_digest=source_digest,
        denylist_digest=denylist_digest,
        routing_policy_digest_value=routing_policy_digest(policy),
        required_stage_ids=REQUIRED_ROUTE_STAGES,
        stage_runs=stage_runs,
        research_sources=[],
        sealed_at=datetime.now(timezone.utc).isoformat(),
    )
    print(f"real-case run 已封存：{args.out / 'real_case_generation_seal.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
