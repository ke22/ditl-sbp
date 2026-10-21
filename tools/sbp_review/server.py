"""Research Workbench HTTP server: project intake, research gates, jobs, and outputs.

The current Golden Path is routed by the active Spectra contracts. Earlier
two-screen and Journey-first behavior remains relevant only to legacy runs.
"""
from __future__ import annotations

import argparse
import contextlib
import base64
import copy
import contextvars
import datetime as dt
import time
import email
import email.policy
import errno
import hashlib
import functools
import io
import json
import os
import signal
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
UI = Path(__file__).resolve().parent / "ui.html"
# workbench-v3-frontend: prototype-v3 wired to the real system; served at / (and /v3), the previous interface at /classic.
UI_V3 = Path(__file__).resolve().parent / "ui-v3.html"
RELATION_LAYOUT = Path(__file__).resolve().parent / "relation_layout.js"
WORKBENCH_CONTRACT = REPO / "design-system" / "desktop-workbench.yaml"
DEFAULT_DATA_ROOT = REPO
DEFAULT_PREVIEW_RUN_NAME = os.environ.get(
    "SBP_PREVIEW_RUN", "2026-08-22-manual-transport-muji"
)
DATA_ROOT = DEFAULT_DATA_ROOT
CASES_DIR = DATA_ROOT / "cases"
RUNS_DIR = DATA_ROOT / "runs"
PREVIEW_RUN_NAME = DEFAULT_PREVIEW_RUN_NAME
UPLOADS_DIR = CASES_DIR / "_uploads"
FROZEN_CORPUS = None


def _active_data_root() -> Path:
    """Return the process-local canonical data root.

    Production startup sets ``DATA_ROOT`` explicitly.  A small compatibility
    bridge remains for the repository's older isolated unit fixtures, which
    patch the module's ``REPO`` constant without calling the runtime
    configurator; it is unreachable in normal startup and does not override
    an explicit ``--data-root``.
    """
    if DATA_ROOT == DEFAULT_DATA_ROOT and REPO != DEFAULT_DATA_ROOT:
        return REPO
    return DATA_ROOT


def _active_cases_dir() -> Path:
    return _active_data_root() / "cases"

# 預覽中的確認頁與藍圖共用這一份已由研究者整理的歷程名稱。這不是從節點
# 文字猜出來的縮寫，因此不會隨生成內容漂移成冗長敘述。
PREVIEW_JOURNEY = [
    # 表頭是可掃讀的顧客行為短語；完整條件與證據仍留在卡片內容／來源。
    "取得使用資格",
    "預約用車",
    "接收預約結果",
    "等待接送通知",
    "確認需求後上車",
    "搭車前往目的地",
    "抵達目的地下車",
    "取消預約或失約",
    "提出服務申訴",
]
# 跟 sbp_gen/__main__.py 一樣的做法：tools/ 目錄本身就是 validate_sbp.py 所在的
# sys.path 根，不新增依賴、不重複實作驗證規則（add-run-watch-mode design.md
# 「Decision: Validity gate reuses validate_sbp」）。
sys.path.insert(0, str(REPO / "tools"))
from sbp_review import artifact_identity  # noqa: E402
from sbp_review import auth as sbp_auth  # noqa: E402
from sbp_review import client_deliverables  # noqa: E402
from sbp_review import child_processes  # noqa: E402
from sbp_review import diagnostics as sbp_diagnostics  # noqa: E402
from sbp_review import instance_lock as sbp_instance_lock  # noqa: E402
from sbp_review import provider_setup as sbp_provider_setup  # noqa: E402
import evidence_locator  # noqa: E402
from yaml_loader import safe_load as load_safe_yaml  # noqa: E402
import narrative_framing  # noqa: E402
import hmw_scope  # noqa: E402
import formation_basis  # noqa: E402
from sbp_gen import solution_applicability as sbp_solution_applicability  # noqa: E402
import pathnames  # noqa: E402
import framework_points  # noqa: E402
import storage_locations  # noqa: E402
from internal_id_patterns import ID_PATTERN, readable_without_ids  # noqa: E402
import validate_sbp as V  # noqa: E402
from sbp_gen import ingest as sbp_ingest  # noqa: E402
from sbp_gen import source_attribution as sbp_source_attribution  # noqa: E402
from sbp_gen import coding_deliverable as sbp_coding_deliverable  # noqa: E402
from sbp_gen import as_is_research as sbp_as_is_research  # noqa: E402
from sbp_gen import decisions as sbp_decisions  # noqa: E402
from sbp_gen import axial_coding_edits as sbp_axial_coding_edits  # noqa: E402
from sbp_gen import inference_reviews as sbp_inference_reviews  # noqa: E402
from sbp_gen import gates as sbp_gates  # noqa: E402
from sbp_gen import hmw_formation as sbp_hmw_formation  # noqa: E402
from sbp_gen import run_files as sbp_run_files  # noqa: E402
from sbp_gen import run_lock as sbp_run_lock  # noqa: E402
from sbp_gen import improvement_goal_review as sbp_improvement_goal_review  # noqa: E402
from sbp_gen import research_catalog_review as sbp_research_catalog_review  # noqa: E402
from sbp_gen import research_review_record as sbp_research_review_record  # noqa: E402
from sbp_gen import lifecycle as sbp_lifecycle  # noqa: E402
from sbp_gen import llm as sbp_llm  # noqa: E402
from sbp_gen import openai_llm as sbp_openai_llm  # noqa: E402
from sbp_gen import researcher_edits as sbp_researcher_edits  # noqa: E402
from sbp_gen import chart_guides as sbp_chart_guides  # noqa: E402
from sbp_gen import sbp_basis  # noqa: E402
from sbp_gen import implementation_research as sbp_implementation_research  # noqa: E402
from sbp_gen import selective_themes as sbp_selective  # noqa: E402
from sbp_gen import stages as sbp_stages  # noqa: E402
from sbp_gen import to_be as sbp_to_be  # noqa: E402
from sbp_gen import validation as sbp_validation  # noqa: E402
from sbp_gen import delivery as sbp_delivery  # noqa: E402
from sbp_gen import execution as sbp_execution  # noqa: E402
from sbp_gen import cli_runner as sbp_cli_runner  # noqa: E402
from sbp_gen import journey_operations as sbp_journey_operations  # noqa: E402
from sbp_gen.delivery_as_is_blueprint import build_as_is_blueprint_view  # noqa: E402
from sbp_gen.delivery_as_is_blueprint_markdown import render_as_is_blueprint_markdown  # noqa: E402
from sbp_gen.delivery_as_is_blueprint_pdf import render_as_is_blueprint_pdf  # noqa: E402
from sbp_gen.delivery_customer_journey import build_customer_journey_view  # noqa: E402
from sbp_gen.delivery_customer_journey_markdown import render_customer_journey_markdown  # noqa: E402
from sbp_gen.delivery_customer_journey_pdf import render_customer_journey_pdf  # noqa: E402
from sbp_gen.delivery_evidence_appendix import build_evidence_appendix_view  # noqa: E402
from sbp_gen.delivery_evidence_appendix_xlsx import render_evidence_appendix_xlsx  # noqa: E402
from sbp_gen.delivery_jtbd_persona import build_jtbd_persona_view  # noqa: E402
from sbp_gen.delivery_jtbd_persona_markdown import render_jtbd_persona_markdown  # noqa: E402
from sbp_gen.delivery_jtbd_persona_pdf import render_jtbd_persona_pdf  # noqa: E402
from sbp_gen.delivery_markdown import render_coding_synthesis_markdown  # noqa: E402
from sbp_gen.delivery_pdf import render_coding_synthesis_pdf  # noqa: E402
from sbp_gen.delivery_to_be_report import build_to_be_report_view  # noqa: E402
from sbp_gen.delivery_to_be_report_markdown import render_to_be_report_markdown  # noqa: E402
from sbp_gen.delivery_to_be_report_pdf import render_to_be_report_pdf  # noqa: E402
from sbp_gen.delivery_sbp_evaluation_report_pdf import (  # noqa: E402
    build_research_report_view, render_research_report_markdown, render_research_report_pdf,
)
from sbp_gen.delivery_xlsx import render_coding_synthesis_xlsx  # noqa: E402
# 目前這條管線的指紋。用產生端自己那個函式，不在這裡重算一份雜湊——重算就會有
# 兩份「什麼算同一代」的定義，改了 prompt 之後兩邊會悄悄不同意。
from sbp_gen.__main__ import _prompt_hash as _pipeline_prompt_hash  # noqa: E402
# restore-browser-generation-workflow design.md 「Decision: The browser reuses
# task 4.1's gate functions directly, not a reimplementation」——授權案例的閘門
# 邏輯只有一份，CLI 跟瀏覽器共用同一支函式，不各自重新推導一次規則。
import evaluate_integrated_desktop_workbench_real_case as real_case  # noqa: E402


class FrozenCorpusConfigurationError(ValueError):
    """Raised when an explicit test-only corpus configuration is unsafe."""


class FrozenCorpusPublicationError(RuntimeError):
    """Raised when a frozen checkpoint cannot pass the production validators."""


class FrozenCorpusConflict(FrozenCorpusPublicationError):
    """Raised for an out-of-order or stale frozen checkpoint request."""


def _stable_tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    elif path.is_dir():
        for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
            digest.update(item.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.read_bytes())
            digest.update(b"\0")
    else:
        raise FileNotFoundError(path)
    return "sha256:" + digest.hexdigest()


def _is_ordered_subsequence(candidate: list, reference: tuple) -> bool:
    """True if every item of candidate appears in reference, in the same order.

    Used to check a frozen corpus's checkpoint_order still makes sense against
    WORKBENCH_STAGE_IDS once that tuple grew past the corpus's own
    stages: the corpus only ever exercises create-case/confirm-journey/
    review-as-is/review-to-be, never the framework-point stages, so exact
    equality is the wrong invariant — order-preservation is.
    """
    remaining = iter(reference)
    return all(item in remaining for item in candidate)


def _write_json_atomic(path: Path, value: dict) -> None:
    # run-state-write-integrity: one implementation. For run.json the caller
    # must own the project lock, and a malformed existing file is never replaced.
    sbp_run_files.write_json_atomic(path, value)


def _write_yaml_atomic(path: Path, value) -> None:
    sbp_run_files.write_yaml_atomic(path, value)


def _owns_project_lock(resolve):
    """run-state-write-integrity: a function that reads a project's state and
    writes it back owns that project's lock for its whole body, taken at entry
    before anything is read (re-entered when the calling route already holds
    it). Never waits: a held lock raises RunLockBusy."""
    def decorate(function):
        @functools.wraps(function)
        def locked(*args, **kwargs):
            run_dir = resolve(*args, **kwargs)
            if run_dir is None:
                return function(*args, **kwargs)
            with sbp_run_lock.hold(run_dir):
                return function(*args, **kwargs)
        return locked
    return decorate


def _run_dir_argument(run_dir, *args, **kwargs):
    return run_dir


def _named_run_dir(run_name, *args, **kwargs):
    safe = _safe_component(str(run_name or ""), "")
    return RUNS_DIR / safe if safe and (RUNS_DIR / safe).is_dir() else None


def _payload_run_dir(payload, *args, **kwargs):
    return _named_run_dir(payload.get("run")) if isinstance(payload, dict) else None


_FEEDBACK_CONTEXT_KEYS = {
    "app_version",
    "current_station",
    "timestamp",
    "browser",
    "os",
    "execution_mode",
    "executor",
    "requested_model",
    "requested_effort",
    "failure_type",
    "error_code",
    "stage",
    "operation",
}


def _persist_feedback(payload: dict) -> dict:
    """Persist the small, researcher-authored feedback record locally.

    This is deliberately a file-backed local path rather than a pretend support
    service.  Only the free-text the researcher entered and an explicit,
    allow-listed technical context are retained; no case/run or research
    artifact is read while saving the report.
    """
    kind = payload.get("kind")
    if kind not in {"error_report", "feedback"}:
        raise ValueError("feedback kind 不正確。")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("請先寫下想回報的內容。")
    goal = payload.get("goal", "")
    if not isinstance(goal, str):
        raise ValueError("goal 必須是文字。")
    technical = payload.get("technical_context") or {}
    if not isinstance(technical, dict):
        raise ValueError("technical_context 必須是物件。")
    safe_technical = {
        key: str(value)[:500]
        for key, value in technical.items()
        if key in _FEEDBACK_CONTEXT_KEYS and value not in (None, "")
    }
    record_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    record = {
        "id": record_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "kind": kind,
        "message": message[:20000],
        "goal": goal[:10000],
        "technical_context": safe_technical,
        "screenshot_requested": bool(payload.get("screenshot_requested")),
        "screen_context_requested": bool(payload.get("screen_context_requested")),
    }
    path = _active_data_root() / "feedback" / f"{record_id}.json"
    _write_json_atomic(path, record)
    return {"ok": True, "id": record_id, "stored_at": record["created_at"]}


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=path.name + ".",
            suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


class FrozenCorpusController:
    """Server-side frozen worker boundary for deterministic browser acceptance.

    It publishes canonical fixture content through ``lifecycle.publish_batch``;
    it never injects presentation state or browser objects.  The controller is
    constructed only by an explicit non-default data-root configuration.
    """

    ARTIFACT_FILES = {
        "evidence": "evidence.yaml",
        "confirmed_journey": "journey.yaml",
        "as_is_graph": "service_graph.yaml",
        "issue_package": "issue_package.yaml",
        "research_catalog": "research_catalog.yaml",
        "to_be_bundle": "to_be_bundle.yaml",
        "pilot_record": "pilot_record.yaml",
        "validation_outcome": "validation_outcome.yaml",
    }

    def __init__(self, manifest_path: Path, data_root: Path):
        self.manifest_path = manifest_path.resolve()
        self.corpus_root = self.manifest_path.parent
        self.data_root = data_root.resolve()
        self.manifest = self._read_manifest()
        self.run_dir = self.data_root / "runs" / self.manifest["run_id"]
        self.registry_path = storage_locations.derive_registry_path(
            self.manifest["case_id"], self.data_root / "runs"
        )
        self.state_path = self.run_dir / ".frozen-checkpoint.json"
        self._lock = threading.Lock()
        self._materialize_case()
        self.publish_checkpoint("create-case", expected_previous_fingerprint=None)

    def _read_manifest(self) -> dict:
        try:
            manifest = load_safe_yaml(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise FrozenCorpusConfigurationError(
                f"frozen corpus manifest cannot be read: {exc}"
            ) from exc
        required = {
            "schema_version", "corpus_id", "case_id", "scenario_id", "run_id",
            "mode", "production_discovery", "notice", "run_digest",
            "stable_identities", "artifact_files", "checkpoint_order", "checkpoints",
        }
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise FrozenCorpusConfigurationError("frozen corpus manifest fields are not canonical")
        if manifest.get("mode") != "deterministic-offline" or manifest.get("production_discovery") is not False:
            raise FrozenCorpusConfigurationError("frozen corpus must be deterministic and excluded from production discovery")
        order = manifest.get("checkpoint_order")
        if not isinstance(order, list) or not _is_ordered_subsequence(order, WORKBENCH_STAGE_IDS):
            raise FrozenCorpusConfigurationError("frozen corpus checkpoint order does not match the workbench")
        self._assert_source_path("run/case.yaml")
        self._verify_run_digest(manifest)
        return manifest

    def _assert_source_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative:
            raise FrozenCorpusConfigurationError("frozen corpus contains an empty source path")
        candidate = (self.corpus_root / relative).resolve()
        try:
            candidate.relative_to(self.corpus_root)
        except ValueError as exc:
            raise FrozenCorpusConfigurationError("frozen corpus path escapes its root") from exc
        if not candidate.is_file():
            raise FrozenCorpusConfigurationError(f"frozen corpus source is missing: {relative}")
        return candidate

    def _verify_run_digest(self, manifest: dict | None = None) -> None:
        declared = (manifest or self.manifest).get("run_digest")
        actual = _stable_tree_digest(self.corpus_root / "run")
        if declared != actual:
            raise FrozenCorpusPublicationError(
                f"frozen corpus run_digest mismatch: expected {declared}, got {actual}"
            )

    def _materialize_case(self) -> None:
        source = self._assert_source_path("run/case.yaml")
        case = load_safe_yaml(source.read_text(encoding="utf-8"))
        errors = V.structural_errors(case, V.load_schema("case")) + V.case_errors(case)
        if errors or case.get("case_id") != self.manifest["case_id"]:
            raise FrozenCorpusConfigurationError(
                "frozen case validation failed: " + "; ".join(errors or ["case_id mismatch"])
            )
        target = self.data_root / "cases" / self.manifest["case_id"] / "case.yaml"
        _write_yaml_atomic(target, case)

    def _load_checkpoint(self, checkpoint_id: str) -> dict:
        relative = self.manifest["checkpoints"].get(checkpoint_id)
        path = self._assert_source_path(relative)
        checkpoint = load_safe_yaml(path.read_text(encoding="utf-8"))
        required = {
            "schema_version", "case_id", "scenario_id", "stable_identities",
            "checkpoint_id", "stage", "current_artifacts", "review_decisions",
            "checkpoint_fingerprint",
        }
        if not isinstance(checkpoint, dict) or set(checkpoint) != required:
            raise FrozenCorpusPublicationError(f"checkpoint {checkpoint_id} is malformed")
        fingerprint = checkpoint.pop("checkpoint_fingerprint")
        actual = sbp_lifecycle.canonical_content_digest(checkpoint)
        checkpoint["checkpoint_fingerprint"] = fingerprint
        if (
            checkpoint.get("checkpoint_id") != checkpoint_id
            or checkpoint.get("stage") != checkpoint_id
            or checkpoint.get("case_id") != self.manifest["case_id"]
            or checkpoint.get("scenario_id") != self.manifest["scenario_id"]
            or checkpoint.get("stable_identities") != self.manifest["stable_identities"]
            or fingerprint != actual
        ):
            raise FrozenCorpusPublicationError(f"checkpoint {checkpoint_id} identity or fingerprint mismatch")
        return checkpoint

    def _load_artifacts(self) -> dict[str, object]:
        self._verify_run_digest()
        artifacts = {}
        for artifact_type, relative in self.manifest["artifact_files"].items():
            if artifact_type not in self.ARTIFACT_FILES:
                raise FrozenCorpusPublicationError(f"unsupported frozen artifact: {artifact_type}")
            path = self._assert_source_path(relative)
            artifacts[artifact_type] = load_safe_yaml(path.read_text(encoding="utf-8"))
        return artifacts

    def _content_errors(self, artifact_type: str, content, artifacts: dict) -> list[str]:
        schema_kind = {
            "evidence": "evidence",
            "confirmed_journey": "journey",
            "as_is_graph": "service-graph",
            "issue_package": "issue-package",
            "research_catalog": "research-catalog",
            "to_be_bundle": "to-be-bundle",
            "pilot_record": "pilot-record",
            "validation_outcome": "validation-outcome",
        }[artifact_type]
        errors = V.structural_errors(content, V.load_schema(schema_kind))
        evidence = artifacts.get("evidence") or []
        journey = artifacts.get("confirmed_journey") or {}
        graph = artifacts.get("as_is_graph") or {}
        if artifact_type == "confirmed_journey":
            evidence_ids = {item.get("id") for item in evidence if isinstance(item, dict)}
            errors += sbp_stages._journey_errors(content, evidence_ids, require_confirmed=True)
        elif artifact_type == "as_is_graph":
            evidence_ids = {item.get("id") for item in evidence if isinstance(item, dict)}
            evidence_texts = {
                item.get("id"): item.get("text", "")
                for item in evidence if isinstance(item, dict) and item.get("id")
            }
            errors += sbp_stages._validate_graph(content, evidence_ids, evidence_texts, journey)
        elif artifact_type == "issue_package":
            errors += V.issue_package_errors(content, evidence, graph)
        elif artifact_type == "research_catalog":
            errors += V.research_catalog_errors(content)
        elif artifact_type == "to_be_bundle":
            errors += V.to_be_bundle_errors(
                content, evidence, graph, artifacts["issue_package"],
                artifacts["research_catalog"],
            )
        elif artifact_type == "pilot_record":
            errors += V.pilot_record_errors(content, artifacts["to_be_bundle"])
        elif artifact_type == "validation_outcome":
            errors += V.validation_outcome_errors(
                content, artifacts["pilot_record"], artifacts["to_be_bundle"]
            )
        return errors

    def _current_versions(self) -> dict[str, str]:
        if not self.registry_path.is_file():
            return {}
        registry = load_safe_yaml(self.registry_path.read_text(encoding="utf-8"))
        records = {
            item["version_id"]: item for item in registry.get("versions", [])
            if isinstance(item, dict) and item.get("version_id")
        }
        versions = {}
        for pointer in registry.get("current", []):
            record = records.get(pointer.get("version_ref"))
            if record:
                versions[record["artifact_type"]] = record["version_id"]
        return versions

    def _publication_batch(self, checkpoint: dict, artifacts: dict) -> list[dict]:
        current = self._current_versions()
        batch = []
        for artifact_type in checkpoint["current_artifacts"]:
            if artifact_type in current:
                continue
            rule = sbp_lifecycle.ARTIFACT_RULES[artifact_type]
            dependencies = {}
            batch_keys = {item["key"] for item in batch}
            for role in rule["dependencies"]:
                dependencies[role] = f"@{role}" if role in batch_keys else current.get(role)
            batch.append({
                "key": artifact_type,
                "artifact_type": artifact_type,
                "case_id": self.manifest["case_id"],
                **({"scenario_id": self.manifest["scenario_id"]} if rule["scope"] == "scenario" else {}),
                "content": artifacts[artifact_type],
                "validate": lambda content, artifact_type=artifact_type: self._content_errors(
                    artifact_type, content, artifacts
                ),
                "dependencies": dependencies,
            })
        return batch

    def _draft_projection(self, checkpoint: dict, artifacts: dict) -> dict:
        available = set(checkpoint["current_artifacts"])
        journey = artifacts["confirmed_journey"] if "confirmed_journey" in available else {
            "schema_version": 2,
            "case_id": self.manifest["case_id"],
            "primary_scenario": self.manifest["scenario_id"],
            "scenarios": [{
                "id": self.manifest["scenario_id"],
                "label": "主要服務情境",
                "customer_goal": "待確認",
                "object_types": [],
                "status": "proposed",
                "steps": [],
            }],
        }
        if "as_is_graph" not in available:
            return {
                "schema_version": 2,
                "meta": {
                    "case_id": self.manifest["case_id"],
                    "scenario_id": self.manifest["scenario_id"],
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "origin": "integrated-test-only",
                },
                "graph": {"nodes": []},
                "relations": [],
                "markers": [],
                "journey": journey,
                "cells": [],
                "annotations": [],
                "research_gaps": [],
                "expectations": [],
                "other_scenarios": [],
            }
        case = load_safe_yaml((
            self.data_root / "cases" / self.manifest["case_id"] / "case.yaml"
        ).read_text(encoding="utf-8"))
        graph = artifacts["as_is_graph"]
        draft = sbp_stages.project(
            graph, None, [], case, journey_data=journey,
            annotations=graph.get("annotations"), evidence=artifacts["evidence"],
        )
        draft["meta"].update({
            "checkpoint_id": checkpoint["checkpoint_id"],
            "origin": "integrated-test-only",
        })
        return draft

    def _persist_projection(self, checkpoint: dict, artifacts: dict, publication: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # Research workers consume the immutable run-local case handoff.  The
        # canonical case schema intentionally has no improvement goal, so the
        # frozen fixture carries that goal in its canonical To-be bundle and we
        # materialize it only in this production-shaped worker input.
        run_case = load_safe_yaml((
            self.data_root / "cases" / self.manifest["case_id"] / "case.yaml"
        ).read_text(encoding="utf-8"))
        run_case["improvement_goal"] = artifacts["to_be_bundle"]["improvement_goal"]
        _write_yaml_atomic(self.run_dir / "case.yaml", run_case)
        # add-improvement-goal-review-stage: as_is_research.py's precondition
        # now reads improvement_goal_review.yaml's operative_goal, not
        # case.yaml's improvement_goal directly. Materialize a confirmed
        # (unchanged) operative goal alongside the case.yaml handoff above so
        # this frozen-corpus replay reaches the same gate a real confirmed
        # run would have already passed — this fixture is not exercising the
        # confirm/revise UX itself, only the downstream research replay.
        _write_yaml_atomic(self.run_dir / sbp_improvement_goal_review.REVIEW_FILENAME, {
            "schema_version": 1,
            "case_id": self.manifest["case_id"],
            "original_goal": run_case["improvement_goal"],
            "hmw_statements": [],
            "operative_goal": run_case["improvement_goal"],
        })
        for artifact_type in checkpoint["current_artifacts"]:
            _write_yaml_atomic(
                self.run_dir / self.ARTIFACT_FILES[artifact_type], artifacts[artifact_type]
            )
        draft = self._draft_projection(checkpoint, artifacts)
        _write_yaml_atomic(self.run_dir / "sbp_draft.yaml", draft)
        versions = self._current_versions()
        state = {
            "case_id": self.manifest["case_id"],
            "scenario_id": self.manifest["scenario_id"],
            "state": "complete",
            "generation_state": "draft_complete",
            "review_state": "reviewed" if checkpoint["checkpoint_id"] in {
                "review-as-is", "review-to-be"
            } else "unreviewed",
            "quality_state": "checks_passed",
            "reference_state": "complete",
            "origin": "integrated-test-only",
            "production_discovery": False,
            "provenance_notice": self.manifest["notice"],
            "artifact_registry": str(self.registry_path),
            "artifact_versions": versions,
            "checkpoint_id": checkpoint["checkpoint_id"],
            "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
        }
        if state["review_state"] == "reviewed":
            state["reviewed_artifact_fingerprint"] = sbp_as_is_research.run_artifact_fingerprint(
                self.run_dir
            )
        _write_json_atomic(self.run_dir / "run.json", state)
        self._persist_frozen_worker_sidecars(checkpoint, artifacts, state)

    def _persist_frozen_worker_sidecars(
        self, checkpoint: dict, artifacts: dict, state: dict
    ) -> None:
        """Materialize worker outputs through the production operation stores."""
        available = set(checkpoint["current_artifacts"])
        decided_at = checkpoint["review_decisions"][-1]["decided_at"]
        versions = state["artifact_versions"]
        if {"issue_package", "research_catalog"} <= available:
            baseline = sbp_as_is_research.resolve_baseline(
                self.run_dir,
                registry_path=self.registry_path,
                expected_as_is_version=versions["as_is_graph"],
                require_reviewed=True,
            )
            operation, reused = sbp_as_is_research.prepare_operation(
                baseline,
                model_policy=sbp_as_is_research.DEFAULT_MODEL_POLICY,
                now=decided_at,
            )
            if not reused:
                stage_dir = sbp_as_is_research.operation_path(
                    self.run_dir, operation["request_digest"]
                ).parent / "staged"
                _write_yaml_atomic(stage_dir / "issue_package.yaml", artifacts["issue_package"])
                _write_yaml_atomic(stage_dir / "research_catalog.yaml", artifacts["research_catalog"])
                operation = sbp_as_is_research.transition_operation(
                    baseline,
                    operation,
                    "research_ready",
                    state="done",
                    now=decided_at,
                    retryable=False,
                    staged_digests={
                        "issue_package": sbp_lifecycle.canonical_content_digest(
                            artifacts["issue_package"]
                        ),
                        "research_catalog": sbp_lifecycle.canonical_content_digest(
                            artifacts["research_catalog"]
                        ),
                    },
                    counts={
                        "issues": len(artifacts["issue_package"].get("issues") or []),
                        "sources": len(artifacts["research_catalog"].get("sources") or []),
                        "claims": len(artifacts["research_catalog"].get("claims") or []),
                    },
                    result={
                        "issue_version_id": versions["issue_package"],
                        "research_version_id": versions["research_catalog"],
                    },
                    error=None,
                )
        if "to_be_bundle" in available:
            baseline = sbp_to_be.resolve_baseline(
                self.run_dir, registry_path=self.registry_path
            )
            operation, reused = sbp_to_be.prepare_operation(
                baseline,
                model_policy=sbp_to_be.DEFAULT_MODEL_POLICY,
                now=decided_at,
            )
            if not reused:
                stage_dir = sbp_to_be.operation_path(
                    self.run_dir, operation["request_digest"]
                ).parent / "staged"
                _write_yaml_atomic(stage_dir / "to_be_bundle.yaml", artifacts["to_be_bundle"])
                operation = sbp_to_be.transition_operation(
                    baseline,
                    operation,
                    "to_be_draft_ready",
                    state="done",
                    now=decided_at,
                    retryable=False,
                    summary=artifacts["to_be_bundle"].get("direction", {}).get("summary"),
                    counts={
                        "proposals": len(artifacts["to_be_bundle"].get("proposals") or []),
                        "blueprint_changes": len(
                            artifacts["to_be_bundle"].get("blueprint_changes") or []
                        ),
                    },
                    result={"to_be_version_id": versions["to_be_bundle"]},
                    error=None,
                )
            registry = load_safe_yaml(self.registry_path.read_text(encoding="utf-8"))
            record = next(
                item for item in registry["versions"]
                if item["version_id"] == versions["to_be_bundle"]
            )
            review = sbp_to_be.ensure_draft_review(
                self.run_dir,
                version_id=record["version_id"],
                content_digest=record["content_digest"],
                now=decided_at,
            )

    def status(self) -> dict:
        if not self.state_path.is_file():
            return {
                "run": self.manifest["run_id"],
                "checkpoint_id": None,
                "checkpoint_fingerprint": None,
            }
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def publish_checkpoint(
        self, checkpoint_id: str, *, expected_previous_fingerprint: str | None
    ) -> dict:
        # run-state-write-integrity: the corpus run's run.json is a state file;
        # the publication (and its rollback) owns the project lock throughout.
        with self._lock, sbp_run_lock.hold(self.run_dir):
            return self._publish_checkpoint(
                checkpoint_id,
                expected_previous_fingerprint=expected_previous_fingerprint,
            )

    def _publish_checkpoint(
        self, checkpoint_id: str, *, expected_previous_fingerprint: str | None
    ) -> dict:
        if checkpoint_id not in self.manifest["checkpoint_order"]:
            raise FrozenCorpusConflict(f"unknown frozen checkpoint: {checkpoint_id}")
        current = self.status()
        current_id = current.get("checkpoint_id")
        if current_id == checkpoint_id:
            if expected_previous_fingerprint != current.get("previous_checkpoint_fingerprint"):
                raise FrozenCorpusConflict("frozen checkpoint replay fingerprint mismatch")
            return {**current, "replayed": True}
        current_index = (
            self.manifest["checkpoint_order"].index(current_id)
            if current_id in self.manifest["checkpoint_order"] else -1
        )
        target_index = self.manifest["checkpoint_order"].index(checkpoint_id)
        if target_index != current_index + 1:
            raise FrozenCorpusConflict(
                f"frozen checkpoint must advance exactly one stage from {current_id!r}"
            )
        if expected_previous_fingerprint != current.get("checkpoint_fingerprint"):
            raise FrozenCorpusConflict("frozen checkpoint previous fingerprint is stale")
        tracked_paths = {
            self.registry_path,
            self.state_path,
            self.run_dir / "run.json",
            self.run_dir / "sbp_draft.yaml",
            *(self.run_dir / filename for filename in self.ARTIFACT_FILES.values()),
            *(path for path in self.run_dir.rglob("*") if path.is_file()),
        }
        before = {
            path: path.read_bytes() if path.is_file() else None
            for path in tracked_paths
        }
        try:
            checkpoint = self._load_checkpoint(checkpoint_id)
            artifacts = self._load_artifacts()
            batch = self._publication_batch(checkpoint, artifacts)
            if not batch:
                raise FrozenCorpusPublicationError("checkpoint adds no canonical artifact")
            publication = sbp_lifecycle.publish_batch(
                self.registry_path,
                batch,
                checkpoint["review_decisions"][-1]["decided_at"],
            )
            self._persist_projection(checkpoint, artifacts, publication)
            state = {
                "run": self.manifest["run_id"],
                "case_id": self.manifest["case_id"],
                "scenario_id": self.manifest["scenario_id"],
                "checkpoint_id": checkpoint_id,
                "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
                "previous_checkpoint_fingerprint": current.get("checkpoint_fingerprint"),
                "artifact_versions": publication["version_ids"],
                "replayed": False,
            }
            _write_json_atomic(self.state_path, state)
        except Exception as exc:
            for path in sorted(
                (item for item in self.run_dir.rglob("*") if item.is_file()),
                reverse=True,
            ):
                if path not in before:
                    if sbp_run_files.is_state_file(path):
                        sbp_run_files.restore(path, None)
                    else:
                        path.unlink()
            for path, prior in before.items():
                if sbp_run_files.is_state_file(path):
                    sbp_run_files.restore(path, prior)
                elif prior is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    _write_bytes_atomic(path, prior)
            for directory in sorted(
                (item for item in self.run_dir.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts), reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if isinstance(exc, FrozenCorpusPublicationError):
                raise
            raise FrozenCorpusPublicationError(
                f"frozen checkpoint publication failed: {exc}"
            ) from exc
        return state


def configure_data_root(
    data_root: str | Path | None = None,
    *,
    frozen_corpus_manifest: str | Path | None = None,
) -> FrozenCorpusController | None:
    """Configure one process-local data root; the default remains production data.

    A frozen corpus is accepted only with an explicit non-default root.  This
    keeps synthetic identities out of normal discovery without filtering names.
    """
    global DATA_ROOT, CASES_DIR, RUNS_DIR, UPLOADS_DIR, PREVIEW_RUN_NAME, FROZEN_CORPUS
    root = DEFAULT_DATA_ROOT if data_root is None else Path(data_root).expanduser().resolve()
    if frozen_corpus_manifest is not None and root == DEFAULT_DATA_ROOT.resolve():
        raise FrozenCorpusConfigurationError(
            "frozen corpus requires an explicit non-default data root"
        )
    DATA_ROOT = root
    CASES_DIR = root / "cases"
    RUNS_DIR = root / "runs"
    UPLOADS_DIR = CASES_DIR / "_uploads"
    evidence_locator.configure_data_root(root)
    framework_points.configure_data_root(root)
    PREVIEW_RUN_NAME = DEFAULT_PREVIEW_RUN_NAME
    FROZEN_CORPUS = None


    CASES_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if frozen_corpus_manifest is not None:
        FROZEN_CORPUS = FrozenCorpusController(
            Path(frozen_corpus_manifest), root
        )
        PREVIEW_RUN_NAME = FROZEN_CORPUS.manifest["run_id"]
    return FROZEN_CORPUS


# Research Delivery is a horizontal capability.  The UI reads this registry;
# it does not reproduce adapter or renderer logic.  Keeping the mapping here
# also makes the supported Local V1 package explicit without adding a station.
WORKBENCH_DELIVERIES = (
    {
        "id": "coding_synthesis", "label": "Coding & Synthesis", "stage": "selective-coding",
        "formats": ("pdf", "xlsx"), "builder": sbp_delivery.build_coding_synthesis_view,
        "markdown": render_coding_synthesis_markdown,
        "renderers": {"pdf": render_coding_synthesis_pdf, "xlsx": render_coding_synthesis_xlsx},
    },
    {
        "id": "jtbd_persona", "label": "JTBD + Persona", "stage": "persona-target-selection",
        "formats": ("pdf",), "builder": build_jtbd_persona_view,
        "markdown": render_jtbd_persona_markdown, "renderers": {"pdf": render_jtbd_persona_pdf},
    },
    {
        "id": "customer_journey", "label": "Customer Journey", "stage": "journey-map",
        "formats": ("pdf",), "builder": build_customer_journey_view,
        "markdown": render_customer_journey_markdown, "renderers": {"pdf": render_customer_journey_pdf},
    },
    {
        "id": "as_is_blueprint", "label": "As-is Service Blueprint", "stage": "review-as-is",
        "formats": ("pdf",), "builder": build_as_is_blueprint_view,
        "markdown": render_as_is_blueprint_markdown, "renderers": {"pdf": render_as_is_blueprint_pdf},
    },
    {
        "id": "to_be_report", "label": "To-be Service Report", "stage": "review-to-be",
        # New runs: SBP Evaluation Report; legacy runs: the unchanged To-be Service Report.
        "formats": ("pdf",), "builder": build_research_report_view,
        "markdown": render_research_report_markdown, "renderers": {"pdf": render_research_report_pdf},
    },
    {
        "id": "evidence_appendix", "label": "Evidence & Decision Traceability", "stage": "project-home",
        "formats": ("xlsx",), "builder": build_evidence_appendix_view,
        "markdown": None, "renderers": {"xlsx": render_evidence_appendix_xlsx},
    },
)
WORKBENCH_DELIVERY_BY_ID = {item["id"]: item for item in WORKBENCH_DELIVERIES}
# New workflow (CJM + selected Solutions -> Service Blueprint -> SBP Evaluation):
# the same export ids carry the new names. Legacy runs keep the registry labels.
WORKBENCH_SBP_WORKFLOW_DELIVERY_LABELS = {
    "as_is_blueprint": "Service Blueprint",
    "to_be_report": "SBP Evaluation Report",
}
WORKBENCH_DELIVERY_FORMAT_LABELS = {"pdf": "PDF", "xlsx": "XLSX"}

# 生成是分鐘級的工作，跑在背景執行緒，前台輪詢進度。
AS_IS_GENERATION_OPERATION_KIND = "generation"
FRESH_CODING_OPERATION_KIND = "fresh_coding"
PERSONA_CONTENT_GENERATION_OPERATION_KIND = "persona_content_generation"
HMW_FORMATION_OPERATION_KIND = "hmw_formation"
JOURNEY_GENERATION_OPERATION_KIND = "journey_generation"
JOURNEY_SOLUTIONS_OPERATION_KIND = "journey_solutions"
AS_IS_BLUEPRINT_OPERATION_KIND = "as_is_blueprint_generation"
CHART_GUIDE_OPERATION_KIND = "chart_guide_drafting"
SELECTIVE_THEMES_OPERATION_KIND = "selective_themes_generation"
JTBD_CANDIDATES_OPERATION_KIND = "jtbd_candidates_generation"
IMPLEMENTATION_RESEARCH_OPERATION_KIND = "implementation_research"
_ACTIVE_JOB_STATES = frozenset({"running", "waiting_for_model"})
_FINISHED_JOB_STATES = frozenset({"done", "failed", "cancelled"})
_RETRY_MARKER = sbp_diagnostics.retry_marker(sbp_stages.RETRY_NOTICE)


class _JobState(dict):
    """The process-wide job record. A move from a running state to done, failed or
    cancelled leaves one allowlisted line in the data root's diagnostics folder
    (desktop-runtime-foundation). Callers keep using it as a plain dict."""

    def __setitem__(self, key, value):
        before = self.get("state") if key == "state" else None
        super().__setitem__(key, value)
        if key == "state":
            self._finished_since(before)

    def update(self, *args, **kwargs):
        before = self.get("state")
        super().update(*args, **kwargs)
        self._finished_since(before)

    def _finished_since(self, before) -> None:
        if before not in _ACTIVE_JOB_STATES or self.get("state") not in _FINISHED_JOB_STATES:
            return
        # A job that never went through _replace_job has no start time; leave no record.
        if "started_monotonic" not in self:
            return
        try:
            event = sbp_diagnostics.build_job_event(
                dict(self), now_monotonic=time.monotonic(), retry=_RETRY_MARKER,
                cause_of=_generation_failure_cause,
            )
            sbp_diagnostics.record(event, _active_data_root())
        except Exception:  # noqa: BLE001 - diagnostics must never change a job's outcome
            pass


_job: dict = _JobState({
    "kind": None,
    "state": "idle",
    "phase": None,
    "log": [],
    "out": None,
    "output_identity": None,
    "result_endpoint": None,
})
_job_lock = threading.Lock()

# merge-persona-matrix-selection-into-one-screen: a third job kind joins the
# existing two -- the "which job is currently running" phrase used in a 409
# refusal needs to name any of the three, not just pick between two with a
# ternary (the previous shape, now widened to a mapping).
JOB_KIND_ACTIVE_PHRASES = {
    FRESH_CODING_OPERATION_KIND: "編碼與顧客任務候選產生",
    "journey_recheck": "歷程資料檢查",
    AS_IS_GENERATION_OPERATION_KIND: "服務藍圖產生",
    PERSONA_CONTENT_GENERATION_OPERATION_KIND: "persona 產生",
    HMW_FORMATION_OPERATION_KIND: "HMW 形成",
    JOURNEY_GENERATION_OPERATION_KIND: "顧客旅程建立",
    JOURNEY_SOLUTIONS_OPERATION_KIND: "解方發展",
    AS_IS_BLUEPRINT_OPERATION_KIND: "服務藍圖產生",
    CHART_GUIDE_OPERATION_KIND: "導讀起草",
    SELECTIVE_THEMES_OPERATION_KIND: "選擇編碼形成",
    JTBD_CANDIDATES_OPERATION_KIND: "顧客任務候選形成",
    "to_be_generation": "改善方案形成",
    "as_is_research": "外部調研",
    IMPLEMENTATION_RESEARCH_OPERATION_KIND: "落地研究",
}


def _job_kind_active_phrase(kind: str | None) -> str:
    return JOB_KIND_ACTIVE_PHRASES.get(kind, JOB_KIND_ACTIVE_PHRASES[AS_IS_GENERATION_OPERATION_KIND])


WORKBENCH_CONTRACT_VERSION = 1
WORKBENCH_REGION_IDS = (
    "case-context",
    "stage-navigation",
    "stage-work-area",
    "inspector",
    "review-gate",
    # rebuild-stage-two-as-cards-modal-and-pair-picker: a panel that covers
    # the page is a region, and this tuple is what makes the contract's
    # enumeration true rather than aspirational -- an overlay not named here
    # is one the contract does not know the application has.
    "focus-overlay",
)
WORKBENCH_STAGE_IDS = (
    "project-home",
    "selective-coding",
    "primary-hmw",
    "persona-target-selection",
    "journey-map",
    "review-as-is",
    "review-to-be",
)
# defer-research-evaluation-station (researcher ruling 2026-10-08,
# docs/release/public-preview-plan.md §4): the Public Preview leaves these
# stations out of the researcher-facing workflow. The contract, Station 7's
# code, API and every stored artifact stay as they are; removing an id here
# reopens the station.
PUBLIC_PREVIEW_DEFERRED_STAGES = ("review-to-be",)
DEFERRED_DELIVERY_MESSAGE = "這份產出不在目前版本的範圍內。"
# The framework-point stages, in WORKBENCH_STAGE_IDS order. Ids reuse
# tools/workbench/server.py's MECHANISMS keys verbatim (not imported — the
# two servers deliberately don't depend on each other) so a researcher who
# crosses over recognises the same name. Each entry names the decisions.py
# decision-point key, the field in that mechanism's selection schema that
# carries the researcher's own free-text reasoning (every one of these
# selection schemas requires a non-empty rationale field — this is the one
# thing they all share, so it doubles as the human-readable "answer" shown in
# navigation without inventing a per-mechanism label resolver), and a short
# visible label. merge-primary-job-into-stage-two folded 主要 Job into
# selective-coding, so the JTBD entry carries both.
# merge-persona-matrix-selection-into-one-screen folded segmentation-axes and
# target-segmentation into one nav stage the same way. The merged entry
# reports decided/undecided from primary_target alone (not segmentation_axes)
# because target_segmentation_selection.yaml can only exist once the axes are
# already decided (decisions.py's own data dependency) — so primary_target's
# decided state already implies the whole merged decision is complete.
# reorder-hmw-before-persona (openspec/TARGET-ARCHITECTURE.md 決定十九 A1):
# primary-hmw now sits before persona-target-selection, matching
# design-system/desktop-workbench.yaml's stages: order. The routing tuple in
# route-current-stage-through-framework-points below must stay in the same
# order as this one.
FRAMEWORK_POINT_STAGES = (
    ("selective-coding", "narrative_job", "rationale", "JTBD"),
    ("primary-hmw", "primary_hmw", "rationale", "HMW"),
    ("persona-target-selection", "primary_target", "primary_rationale", "Persona"),
)
FRAMEWORK_POINT_STAGE_IDS = frozenset(item[0] for item in FRAMEWORK_POINT_STAGES)
WORKBENCH_COMPONENT_IDS = {
    "AppShell", "CaseContext", "StageNav", "PageHeader", "DecisionStatement",
    "StatusSummary", "EvidenceRef", "ClaimBadge", "IssueCard", "ProposalCard",
    "ReviewGate", "Inspector", "CanvasToolbar", "VersionPicker", "BlueprintCell",
    "EmptyState", "ProcessingState", "BlockedState", "ErrorState",
}
# The adapter intentionally exposes only presentation tokens understood by the
# version-1 browser. Adding a YAML key cannot create a CSS variable or markup
# path until this allowlist and its contract tests are updated together.
WORKBENCH_TOKEN_ALLOWLIST = {
    "surface": ("app", "work", "raised", "overlay", "selected", "hover"),
    "text": ("primary", "secondary", "muted", "inverse", "link"),
    "border": ("subtle", "strong", "selected", "warning"),
    "action": ("primary-bg", "primary-text", "secondary-bg", "secondary-text", "destructive", "dark-bg", "dark-hover"),
    "accent": ("bg", "hover", "tint", "text"),
    "status": ("neutral", "processing", "pending", "confirmed", "failed", "pending-surface", "failed-surface"),
    "as_is": ("accent", "surface", "border", "pattern"),
    "to_be": ("accent", "surface", "border", "pattern"),
    "claim_source": ("evidence", "synthesis", "inference", "researcher", "proposal", "hypothesis", "inference-opacity"),
    "freshness": ("current", "stale", "unconfirmed"),
    "blueprint_lane": ("evidence", "customer-actions", "frontstage-actions", "backstage-actions", "supporting-processes"),
    "density": ("compact-row", "control-height", "tag-height", "blueprint-card-min", "readable-line-height"),
    "typography": (
        "ui-family", "source-family", "mono-family",
        "title-size", "title-weight", "title-line-height",
        "section-size", "section-weight", "section-line-height",
        "card-size", "card-weight", "card-line-height",
        "body-size", "body-weight",
        "quote-size", "quote-weight", "quote-line-height",
        "small-size", "small-weight", "small-line-height",
        "tag-size", "tag-weight", "tag-line-height", "label-tracking",
    ),
    "radius": ("component", "floating", "card", "pill"),
    "spacing": ("base", "s2", "s3", "s4", "s5", "s6", "s8", "s10", "s12", "s14", "s16"),
    "shadow": ("none", "overlay"),
    "focus": ("ring-color", "ring-width", "ring-offset"),
    "motion": ("duration-fast-ms", "duration-standard-ms", "reduced-duration-ms", "easing"),
}
# converge-workbench-shell-and-visual-grammar: every semantic token maps to one
# CSS custom property. The values are written into the served document as a
# `<style id="workbench-tokens">` block, so the first paint already carries the
# contract and the browser keeps no literal copy of any token.
WORKBENCH_TOKEN_CSS = (
    ("surface.app", "--wb-surface-app"),
    ("surface.work", "--wb-surface-work"),
    ("surface.raised", "--wb-surface-raised"),
    ("surface.overlay", "--wb-surface-overlay"),
    ("surface.selected", "--wb-surface-selected"),
    ("surface.hover", "--wb-surface-hover"),
    ("text.primary", "--wb-text-primary"),
    ("text.secondary", "--wb-text-secondary"),
    ("text.muted", "--wb-text-muted"),
    ("text.inverse", "--wb-text-inverse"),
    ("text.link", "--wb-text-link"),
    ("border.subtle", "--wb-border-subtle"),
    ("border.strong", "--wb-border-strong"),
    ("border.selected", "--wb-border-selected"),
    ("border.warning", "--wb-border-warning"),
    ("action.primary-bg", "--wb-action-primary"),
    ("action.primary-text", "--wb-action-primary-text"),
    ("action.secondary-bg", "--wb-action-secondary"),
    ("action.secondary-text", "--wb-action-secondary-text"),
    ("action.destructive", "--wb-action-destructive"),
    ("action.dark-bg", "--wb-action-dark"),
    ("action.dark-hover", "--wb-action-dark-hover"),
    ("accent.bg", "--wb-accent"),
    ("accent.hover", "--wb-accent-hover"),
    ("accent.tint", "--wb-accent-tint"),
    ("accent.text", "--wb-accent-text"),
    ("status.neutral", "--wb-status-neutral"),
    ("status.processing", "--wb-status-processing"),
    ("status.pending", "--wb-status-pending"),
    ("status.confirmed", "--wb-status-confirmed"),
    ("status.failed", "--wb-status-failed"),
    ("status.pending-surface", "--wb-status-pending-surface"),
    ("status.failed-surface", "--wb-status-failed-surface"),
    ("as_is.accent", "--wb-as-is"),
    ("as_is.surface", "--wb-as-is-surface"),
    ("as_is.border", "--wb-as-is-border"),
    ("to_be.accent", "--wb-to-be"),
    ("to_be.surface", "--wb-to-be-surface"),
    ("to_be.border", "--wb-to-be-border"),
    ("claim_source.evidence", "--wb-claim-evidence"),
    ("claim_source.synthesis", "--wb-claim-synthesis"),
    ("claim_source.inference", "--wb-claim-inference"),
    ("claim_source.researcher", "--wb-claim-researcher"),
    ("claim_source.proposal", "--wb-claim-proposal"),
    ("claim_source.hypothesis", "--wb-claim-hypothesis"),
    ("claim_source.inference-opacity", "--wb-inference-opacity"),
    ("freshness.current", "--wb-fresh-current"),
    ("freshness.stale", "--wb-fresh-stale"),
    ("freshness.unconfirmed", "--wb-fresh-unconfirmed"),
    ("blueprint_lane.evidence", "--wb-lane-evidence"),
    ("blueprint_lane.customer-actions", "--wb-lane-customer"),
    ("blueprint_lane.frontstage-actions", "--wb-lane-front"),
    ("blueprint_lane.backstage-actions", "--wb-lane-back"),
    ("blueprint_lane.supporting-processes", "--wb-lane-support"),
    ("density.control-height", "--wb-control-height"),
    ("density.tag-height", "--wb-tag-height"),
    ("density.readable-line-height", "--wb-line-body"),
    ("typography.title-size", "--wb-type-title-size"),
    ("typography.title-weight", "--wb-type-title-weight"),
    ("typography.title-line-height", "--wb-type-title-line"),
    ("typography.section-size", "--wb-type-section-size"),
    ("typography.section-weight", "--wb-type-section-weight"),
    ("typography.section-line-height", "--wb-type-section-line"),
    ("typography.card-size", "--wb-type-card-size"),
    ("typography.card-weight", "--wb-type-card-weight"),
    ("typography.card-line-height", "--wb-type-card-line"),
    ("typography.body-size", "--wb-type-body-size"),
    ("typography.body-weight", "--wb-type-body-weight"),
    ("typography.quote-size", "--wb-type-quote-size"),
    ("typography.quote-weight", "--wb-type-quote-weight"),
    ("typography.quote-line-height", "--wb-type-quote-line"),
    ("typography.small-size", "--wb-type-small-size"),
    ("typography.small-weight", "--wb-type-small-weight"),
    ("typography.small-line-height", "--wb-type-small-line"),
    ("typography.tag-size", "--wb-type-tag-size"),
    ("typography.tag-weight", "--wb-type-tag-weight"),
    ("typography.tag-line-height", "--wb-type-tag-line"),
    ("typography.label-tracking", "--wb-label-tracking"),
    ("spacing.s2", "--wb-space-2"),
    ("spacing.s3", "--wb-space-3"),
    ("spacing.s4", "--wb-space-4"),
    ("spacing.s5", "--wb-space-5"),
    ("spacing.s6", "--wb-space-6"),
    ("spacing.s8", "--wb-space-8"),
    ("spacing.s10", "--wb-space-10"),
    ("spacing.s12", "--wb-space-12"),
    ("spacing.s14", "--wb-space-14"),
    ("spacing.s16", "--wb-space-16"),
    ("radius.component", "--wb-radius"),
    ("radius.card", "--wb-radius-card"),
    ("radius.floating", "--wb-floating-radius"),
    ("radius.pill", "--wb-radius-pill"),
    ("focus.ring-color", "--wb-focus"),
    ("focus.ring-width", "--wb-focus-width"),
    ("focus.ring-offset", "--wb-focus-offset"),
    ("motion.duration-fast-ms", "--wb-motion-fast"),
    ("motion.duration-standard-ms", "--wb-motion-standard"),
)
WORKBENCH_TOKEN_PLACEHOLDER = b"<!--workbench-tokens-->"


def _css_token_value(path: str, value: object) -> str:
    px = (
        path.startswith(("spacing.", "radius.", "focus.ring-width", "focus.ring-offset"))
        or path in ("density.control-height", "density.tag-height")
        or re.fullmatch(r"typography\.[a-z]+-size", path) is not None
    )
    if px:
        return f"{value}px"
    if path.startswith("motion.duration-"):
        return f"{value}ms"
    return str(value)


def _workbench_token_style(contract: dict | None = None) -> str:
    """Render the projected tokens as CSS custom properties, or nothing when unsupported."""
    contract = _project_workbench_contract() if contract is None else contract
    tokens = contract.get("tokens") if contract.get("contract_status") == "supported" else None
    if not tokens:
        return ""
    declarations = []
    for path, name in WORKBENCH_TOKEN_CSS:
        value = tokens
        for key in path.split("."):
            value = value[key]
        declarations.append(f"{name}:{_css_token_value(path, value)}")
    return '<style id="workbench-tokens">:root{' + ";".join(declarations) + "}</style>"


WORKBENCH_ATTENTION_LIMITS = {
    "page_titles": 1,
    "decision_statements": 1,
    "primary_actions": 1,
    "visible_statuses": 3,
    "visible_utilities": 3,
    "warning_summaries": 1,
    "overflow_policy": "replace-merge-or-disclose",
}


class WorkbenchContractError(ValueError):
    """Raised when the production UI cannot safely consume its interface contract."""


def _unsupported_workbench() -> dict:
    """Return one non-technical, fail-closed bootstrap state."""
    return {
        "contract_version": None,
        "contract_status": "unsupported",
        "error": {
            "what_happened": "工作台介面規則無法載入。",
            "why_it_matters": "為避免用錯誤的畫面確認研究成果，目前操作已暫停。",
            "next_action": "請恢復支援的工作台介面規則版本 1，然後重新載入。",
        },
    }


def _project_tokens(tokens: object) -> dict:
    if not isinstance(tokens, dict) or set(tokens) != set(WORKBENCH_TOKEN_ALLOWLIST):
        raise WorkbenchContractError("token families do not match version 1")
    projected = {}
    for family, keys in WORKBENCH_TOKEN_ALLOWLIST.items():
        values = tokens.get(family)
        if not isinstance(values, dict) or any(key not in values for key in keys):
            raise WorkbenchContractError(f"token family {family!r} is incomplete")
        projected[family] = {key: values[key] for key in keys}
    return projected


def _project_workbench_contract(path: Path = WORKBENCH_CONTRACT) -> dict:
    """Load and narrow the canonical YAML to the browser's version-1 API shape."""
    try:
        contract = _load(path)
        if not isinstance(contract, dict):
            raise WorkbenchContractError("contract root must be an object")
        if contract.get("contract_version") != WORKBENCH_CONTRACT_VERSION:
            raise WorkbenchContractError("unsupported contract version")

        shell = contract.get("shell")
        regions = shell.get("regions") if isinstance(shell, dict) else None
        if not isinstance(regions, list) or tuple(item.get("id") for item in regions if isinstance(item, dict)) != WORKBENCH_REGION_IDS:
            raise WorkbenchContractError("shell regions do not match version 1")

        stages = contract.get("stages")
        if not isinstance(stages, list) or tuple(item.get("id") for item in stages if isinstance(item, dict)) != WORKBENCH_STAGE_IDS:
            raise WorkbenchContractError("stage order does not match version 1")
        if any(not isinstance(stage.get("primary_actions"), list) or len(stage["primary_actions"]) != 1 for stage in stages):
            raise WorkbenchContractError("every stage must have one primary action")

        journey_confirmation = contract.get("journey_confirmation")
        journey_states = journey_confirmation.get("states") if isinstance(journey_confirmation, dict) else None
        if not isinstance(journey_states, list) or [item.get("id") for item in journey_states if isinstance(item, dict)] != [
            "unchanged", "edited", "rechecking", "recheck-failed", "rechecked", "edited-again"
        ]:
            raise WorkbenchContractError("journey confirmation states do not match version 1")
        if any(not isinstance(item.get("primary_action"), dict) for item in journey_states):
            raise WorkbenchContractError("journey confirmation state action is incomplete")
        candidate_confirmation = contract.get("journey_candidate_confirmation")
        if not isinstance(candidate_confirmation, dict) or not all(
            isinstance(candidate_confirmation.get(key), dict) and candidate_confirmation[key].get("label")
            for key in ("confirm_action", "continue_action", "build_action")
        ):
            raise WorkbenchContractError("journey candidate confirmation is incomplete")

        as_is_research = contract.get("as_is_research")
        research_states = as_is_research.get("states") if isinstance(as_is_research, dict) else None
        if not isinstance(research_states, list) or [
            item.get("id") for item in research_states if isinstance(item, dict)
        ] != [
            "research_processing", "research_failed", "no_grounded_issues",
            "research_ready", "research_stale",
        ]:
            raise WorkbenchContractError("As-is research states do not match version 1")
        if as_is_research.get("proposal_eligibility") is not False:
            raise WorkbenchContractError("research-only To-be stage cannot enable proposals")

        to_be = contract.get("to_be")
        to_be_states = to_be.get("states") if isinstance(to_be, dict) else None
        expected_to_be_states = [
            "research_processing", "research_failed", "no_grounded_issues",
            "to_be_processing", "to_be_failed", "to_be_draft_ready",
            "to_be_revision_processing", "to_be_accepted", "to_be_declined",
            "to_be_stale",
        ]
        if not isinstance(to_be_states, list) or [
            item.get("id") for item in to_be_states if isinstance(item, dict)
        ] != expected_to_be_states:
            raise WorkbenchContractError("To-be states do not match version 1")
        views = to_be.get("views")
        if not isinstance(views, list) or [item.get("id") for item in views] != [
            "to-be-blueprint", "proposal-summary", "basis-validation"
        ] or sum(bool(item.get("default")) for item in views) != 1 or not views[0].get("default"):
            raise WorkbenchContractError("To-be views do not match version 1")


        attention = contract.get("attention_budget")
        if attention != WORKBENCH_ATTENTION_LIMITS:
            raise WorkbenchContractError("attention budget does not match version 1")

        breakpoints = contract.get("breakpoints")
        if not isinstance(breakpoints, list) or [item.get("id") for item in breakpoints if isinstance(item, dict)] != [
            "constrained-desktop", "standard-desktop", "wide-desktop"
        ]:
            raise WorkbenchContractError("breakpoints do not match version 1")

        components = contract.get("components")
        component_ids = {
            item.get("id") for item in components or [] if isinstance(item, dict)
        }
        if component_ids != WORKBENCH_COMPONENT_IDS:
            raise WorkbenchContractError("component responsibilities do not match version 1")

        language = contract.get("content_language")
        statuses = language.get("statuses") if isinstance(language, dict) else None
        if not isinstance(statuses, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("label"), str)
            for item in statuses
        ):
            raise WorkbenchContractError("content language is incomplete")

        return {
            "contract_version": WORKBENCH_CONTRACT_VERSION,
            "contract_status": "supported",
            "shell": {
                "document_horizontal_overflow": shell.get("document_horizontal_overflow"),
                "regions": regions,
            },
            "stages": stages,
            "journey_confirmation": journey_confirmation,
            "journey_candidate_confirmation": candidate_confirmation,
            "as_is_candidate_confirmation": contract.get("as_is_candidate_confirmation"),
            "improvement_goal_confirmation": contract.get("improvement_goal_confirmation"),
            "as_is_research": as_is_research,
            "to_be": to_be,
            "attention_budget": attention,
            "breakpoints": breakpoints,
            "tokens": _project_tokens(contract.get("tokens")),
            "content_language": language,
        }
    except (OSError, UnicodeError, yaml.YAMLError, WorkbenchContractError, AttributeError, TypeError):
        return _unsupported_workbench()


def _blocked_runtime_stage(stage: dict, status: str = "draft") -> dict:
    blocked = stage["blocked_state"]
    return {
        "id": stage["id"],
        "availability": "blocked",
        "status": status,
        "primary_action_available": False,
        "blocked_reason": blocked["missing_prerequisite_message"],
        "next_action": blocked["next_action"],
    }


def _framework_point_answer_label(runs_root: Path, decision: dict, rationale_field: str) -> str | None:
    """Read-only: the researcher's own free-text reasoning for a decided point.

    Every one of the six selection schemas requires a non-empty rationale
    field (see FRAMEWORK_POINT_STAGES) — reusing it avoids building six
    separate per-mechanism label resolvers just to answer "what did the
    researcher decide", and it is prose the researcher wrote, never a bare
    schema id.
    """
    selection_file = decision.get("selection_file")
    if not isinstance(selection_file, str) or not selection_file:
        return None
    try:
        data = _load(runs_root / selection_file)
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    text = data.get(rationale_field)
    if not isinstance(text, str) or not text.strip():
        return None
    # A researcher's free-text rationale can still name an internal id
    # in passing (e.g. "AXIS-3 有 9 位") -- CLAUDE.md bans that from being
    # the only thing on screen, and this label has no evidence-locator
    # context to pair it with, so it is scrubbed rather than kept.
    text = ID_PATTERN.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ，,、")
    if not text:
        return None
    limit = 60
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _framework_point_runtime_entries(
    case_id: str | None, case_dir: Path | None, runs_root: Path
) -> list[dict]:
    """Build the six framework-point runtime entries.

    Read-only end to end: `sbp_decisions.build_decisions()`'s own docstring
    guarantees it never writes `decisions.yaml` to disk, and the answer-label
    lookup above only ever reads a selection file. Any read failure (no
    case, no decisions yet, an unreadable file) degrades to "undecided"
    rather than raising — expose-remaining-framework-points-in-workbench's
    established failure mode for this exact situation.
    """
    decisions_by_point: dict = {}
    if case_dir is not None:
        try:
            built = sbp_decisions.build_decisions(case_dir, runs_root)
            decisions_by_point = built.get("decisions") or {}
        except (ValueError, OSError, yaml.YAMLError):
            decisions_by_point = {}
    entries = []
    for stage_id, point, rationale_field, _visible_label in FRAMEWORK_POINT_STAGES:
        decision = decisions_by_point.get(point) or {"status": "undecided"}
        decided = decision.get("status") == "decided"
        if decided and stage_id == "persona-target-selection":
            # persona-generation-confirmation-gate: the Persona station is
            # decided only once its Persona is confirmed, not when a target
            # exists -- Journey eligibility waits on the Persona Human Gate.
            selection_file = decision.get("selection_file")
            decided = isinstance(selection_file, str) and bool(selection_file) and _persona_confirmation(
                (runs_root / selection_file).parent)["state"] == "confirmed"
        label = _framework_point_answer_label(runs_root, decision, rationale_field) if decided else None
        if decided and not label:
            # The rationale became optional (2026-09-27 wireframe ruling); a
            # decided point still says so in words.
            label = "已選定"
        entries.append({
            "id": stage_id,
            "availability": "available",
            "status": "decided" if decided else "undecided",
            "label": label,
        })
    return entries


def _derive_workbench_runtime(
    contract: dict,
    *,
    case_ready: bool,
    journey_draft_ready: bool,
    journey_confirmed: bool,
    as_is_present: bool,
    as_is_valid: bool,
    as_is_validity_findings: list[str] | None = None,
    as_is_advisory_findings: list[str] | None = None,
    proposal_eligible: bool = True,
    proposal_ineligible_reason: str | None = None,
    job_state: str = "idle",
    job_kind: str | None = None,
    job_run: str | None = None,
    run_name: str | None = None,
    review_state: str | None = None,
    reviewed_fingerprint: str | None = None,
    current_fingerprint: str | None = None,
    lifecycle_state: str | None = None,
    to_be_available: bool = False,
    as_is_research: dict | None = None,
    to_be_operation: dict | None = None,
    to_be_review: dict | None = None,
    to_be_stale: bool = False,
    validation_detail: dict | None = None,
    operative_goal: str | None = None,
    sbp_solutions_state: str | None = None,
    case_context: dict | None = None,
    journey_state: str | None = None,
    framework_point_stages: list[dict] | None = None,
    fresh_as_is_path: bool = False,
) -> dict:
    """Derive presentation state without persisting a second workflow state."""
    stages = contract["stages"]
    # Named lookup, not positional: WORKBENCH_STAGE_IDS grew past five
    # entries once the framework-point stages were inserted between
    # create-case and confirm-journey, so `stages[1]`..`stages[4]` no longer
    # name what they used to.
    stages_by_id = {stage["id"]: stage for stage in stages}
    runtime = []

    reviewed_current = bool(
        review_state == "reviewed"
        and reviewed_fingerprint
        and current_fingerprint
        and reviewed_fingerprint == current_fingerprint
        # confirm-as-is-save-only: a fresh-path run has no artifact registry
        # until research needs a baseline, so its As-is freshness is the
        # review record matching the current draft alone.
        and (lifecycle_state == "current" or fresh_as_is_path)
    )
    # route-current-stage-through-framework-points: this used to jump
    # straight from case_ready to journey-map, which was correct back when
    # confirm-journey was the very next thing after create-case. The seven-
    # station nav inserted three framework-point stations in between
    # (selective-coding / primary-hmw / persona-target-selection) — a run
    # that hasn't decided those yet still landed on journey-map or even
    # review-as-is, because this decision tree never looked at them.
    # reorder-hmw-before-persona: order here must match FRAMEWORK_POINT_STAGES
    # above — primary-hmw before persona-target-selection.
    fp_by_id = {entry["id"]: entry for entry in (framework_point_stages or [])}
    first_undecided_fp = next(
        (sid for sid in ("selective-coding", "primary-hmw", "persona-target-selection")
         if (fp_by_id.get(sid) or {}).get("status") != "decided"),
        None,
    )
    if not case_ready:
        current_stage = "project-home"
    elif first_undecided_fp:
        current_stage = first_undecided_fp
    elif not journey_confirmed:
        current_stage = "journey-map"
    elif reviewed_current and "review-to-be" not in PUBLIC_PREVIEW_DEFERRED_STAGES:
        current_stage = "review-to-be"
    else:
        current_stage = "review-as-is"

    runtime.append({
        "id": "project-home",
        "availability": "current" if current_stage == "project-home" else "complete",
        "status": "draft" if current_stage == "project-home" else "confirmed",
        "primary_action_available": current_stage == "project-home" and case_ready,
    })

    # The framework-point stages, positioned between create-case and
    # confirm-journey per WORKBENCH_STAGE_IDS order. They sit outside the
    # current_stage state machine above — a researcher can decide them in
    # any order, unlike the five sequential stages — so every entry is
    # always "available", never "current" or "blocked".
    runtime.extend(framework_point_stages or [])

    if not case_ready:
        runtime.append(_blocked_runtime_stage(stages_by_id["journey-map"]))
    else:
        journey_states = {
            item["id"]: item for item in contract["journey_confirmation"]["states"]
        }
        selected_journey_state = journey_states.get(journey_state or "unchanged")
        if selected_journey_state is None:
            selected_journey_state = journey_states["unchanged"]
        runtime.append({
            "id": "journey-map",
            "availability": "current" if current_stage == "journey-map" else "complete",
            "status": "confirmed" if journey_confirmed else (
                selected_journey_state["status"] if journey_draft_ready else "draft"
            ),
            "journey_state": None if journey_confirmed else selected_journey_state["id"],
            "primary_action": None if journey_confirmed else selected_journey_state["primary_action"],
            "primary_action_available": bool(
                current_stage == "journey-map"
                and journey_draft_ready
                and selected_journey_state["action_available"]
            ),
            "as_is_blocked": not journey_confirmed,
        })

    if not journey_confirmed and review_state == "reviewed" and as_is_present:
        # solution-reconfirmation: a blueprint confirmed earlier stays readable while
        # the journey is not reconfirmed -- read-only, never shown as current.
        runtime.append({
            "id": "review-as-is",
            "availability": "current" if current_stage == "review-as-is" else "available",
            "status": "upstream-unresolved",
            "primary_action_available": False,
            "lifecycle_certainty": lifecycle_state or "legacy",
            "operative_goal": operative_goal,
            "advisory_findings": [],
        })
        runtime.append(_blocked_runtime_stage(stages_by_id["review-to-be"]))
    elif not journey_confirmed:
        runtime.append(_blocked_runtime_stage(stages_by_id["review-as-is"]))
        runtime.append(_blocked_runtime_stage(stages_by_id["review-to-be"]))
    else:
        as_is_job_state = (
            job_state
            if job_kind == AS_IS_GENERATION_OPERATION_KIND and job_run == run_name
            else "idle"
        )
        if as_is_job_state == "running":
            as_is_status = "processing"
        elif as_is_job_state == "failed" or (as_is_present and not as_is_valid):
            as_is_status = "generation-failed"
        elif review_state == "reviewed" and not reviewed_current:
            as_is_status = "update-required"
        elif reviewed_current and (lifecycle_state == "current" or fresh_as_is_path):
            as_is_status = "confirmed"
        else:
            as_is_status = "pending-confirmation" if as_is_present else "processing"
        # solution-reconfirmation: unresolved Solutions make the blueprint read-only;
        # Solutions reconfirmed after its confirmation keep it identifiable as earlier.
        # Only a blueprint confirmed before becomes "upstream-unresolved"; a running job
        # or a failed generation keeps its own status.
        if sbp_solutions_state == "unresolved" and review_state == "reviewed" \
                and as_is_status in {"confirmed", "update-required", "pending-confirmation"}:
            as_is_status = "upstream-unresolved"
        elif sbp_solutions_state == "reconsidered" and as_is_status == "confirmed":
            as_is_status = "upstream-reconsidered"
        # 「產出失敗」有兩個完全不同的來源：背景作業失敗，以及存下來的藍圖沒通過
        # 驗證。以前兩者收斂成同一個狀態詞、而且驗證這一路完全沒有說明，於是研究者
        # 看到的是一張完整的圖被標成失敗、確認鍵是灰的、沒有任何理由。兩種來源各自
        # 帶自己的說明，用的是背景作業那一路本來就有的三段式（發生什麼／為什麼卡住
        # ／可以做什麼）。
        as_is_entry = {
            "id": "review-as-is",
            "availability": "current" if current_stage == "review-as-is" else "complete",
            "status": as_is_status,
            # 檢查沒過不再鎖住確認鍵。這個系統的分工是：助理讀完、排序、講出重點；
            # 裁定事實是研究者的事。把檢查結果變成鎖，等於介面代替研究者做了
            # 裁定。檢查結果照樣攤在畫面上，按不按由他決定——按下去會連同當下
            # 還沒解決的問題一起記進 review 紀錄，不是當作沒發生。
            # 這個動作只有在 run 帶著 artifact_versions 紀錄時，`POST /api/review`
            # 才會真的啟動調研（見該 endpoint 的 lifecycle_enabled）。以前介面照樣
            # 把它開放出來，於是研究者按下「開始提案」只會被標成已確認、什麼都沒
            # 開始。同一個條件在這裡評一次，介面就不會跟伺服器講不一樣的話。
            "primary_action_available": bool(
                proposal_eligible
                and as_is_present
                and sbp_solutions_state != "unresolved"
                and as_is_status in {"pending-confirmation", "update-required", "generation-failed",
                                     "upstream-reconsidered"}
            ),
            "lifecycle_certainty": lifecycle_state or "legacy",
            "operative_goal": operative_goal,
            # declare-analysis-chain-and-journey-coverage：跟上面的檢查結果不同，
            # 這裡是非阻斷的判斷式發現（例如已確認旅程有步驟完全沒有卡片）。永遠
            # 附在這一格，不看 as_is_status，因為它跟確認鍵可不可按無關——研究者
            # 裁定要不要理會，助理只負責講出來。
            "advisory_findings": list(as_is_advisory_findings or []),
        }
        if not proposal_eligible and proposal_ineligible_reason:
            as_is_entry["proposal_ineligible_reason"] = proposal_ineligible_reason
        if as_is_status == "generation-failed":
            if as_is_job_state == "failed":
                as_is_entry["blocked_explanation"] = {
                    "cause": "generation_failed",
                    "what_happened": "上一次產生服務藍圖的工作中途失敗了。",
                    "why_it_matters": "沒有一份完整的服務藍圖，後續的調研與提案就沒有可靠的基準。",
                    "next_action": "回到上一步重新產生；如果一直失敗，把這個案例的資料交給維護的人看。",
                    "findings": [],
                }
            else:
                findings = list(as_is_validity_findings or [])
                as_is_entry["blocked_explanation"] = {
                    "cause": "draft_failed_validation",
                    "what_happened": (
                        f"這份藍圖畫出來了，但有 {len(findings)} 個地方沒通過檢查。"
                        if findings else "這份藍圖沒有通過檢查。"
                    ),
                    "why_it_matters": "確認之後，後續的調研與提案都會以這個版本為基準，這些問題會跟著帶下去。",
                    "next_action": "你可以照樣往下走——按下確認時，這些還沒解決的問題會一起記進這個版本的紀錄。也可以先重新產生這份藍圖，或把問題交給維護的人處理。",
                    "findings": findings,
                }
        runtime.append(as_is_entry)

        to_be_view = _derive_to_be_runtime(
            contract,
            as_is_research,
            to_be_operation,
            to_be_review,
            stale=to_be_stale or (
                as_is_status == "update-required" and lifecycle_state is not None
            ),
        )
        # `reviewed_current`（研究者確認過、指紋仍相符、版本仍是 current），不是
        # `as_is_status`。狀態字串會因為藍圖有未解決的檢查結果而顯示成
        # 「產出失敗」，即使研究者已經知情地確認過它當基線——那是
        # explain-blocked-as-is-confirmation 立下的規則：檢查結果不能代替研究者
        # 做裁定。用狀態字串當條件，等於讓那些檢查結果反過來把已經確認的基線
        # 又變回未確認，畫面因此叫他「回去按開始提案」，而那一頁只給得出
        # 「繼續」——按了又回到這裡，繞不出去。
        if not reviewed_current and to_be_view["state"] != "to_be_stale":
            # A researcher reported this directly: not-yet-confirmed is a fact
            # about the current stage, not a reason to lock the next one behind
            # a wall the nav rail refuses to open. The stage is reachable and
            # says plainly that there is nothing to look at yet — the same
            # pattern `validation_locked` already uses one stage further on.
            # `_derive_to_be_runtime()`, called above, would otherwise report
            # this as "research_failed" (no as_is_research operation exists
            # yet), which is not true — nothing has failed, nothing has been
            # asked to start. This view is built directly rather than through
            # `contract["to_be"]["states"]`: that list is a locked "version 1"
            # shape (`_project_workbench_contract` rejects the whole contract
            # if it doesn't match exactly), and this state has no bundle, no
            # counts, and nothing for a researcher to act on — a landing page,
            # not a workflow state worth adding to that contract's surface.
            not_started_view = {
                "state": "to_be_not_started",
                "status": "draft",
                "heading": "服務藍圖尚未確認",
                "phase_label": None,
                "primary_action": None,
                "primary_action_available": False,
                "proposal_eligible": False,
                "what_happened": "這裡還沒有內容，因為服務藍圖還沒有被確認。",
                "why_it_matters": "研究要評估一份不會再變動的服務藍圖；沒有確認，就沒有穩定的起點可以參照。",
                "next_action": "回到服務藍圖，確認目前的版本。",
            }
            runtime.append({
                "id": "review-to-be",
                "availability": "current" if current_stage == "review-to-be" else "available",
                "status": not_started_view["status"],
                "primary_action": None,
                "primary_action_available": False,
                "capability_available": bool(to_be_available),
                "proposal_eligible": False,
                "research": not_started_view,
                "to_be": not_started_view,
            })
        else:
            runtime.append({
                "id": "review-to-be",
                "availability": "current" if current_stage == "review-to-be" else "available",
                "status": to_be_view["status"],
                "primary_action": to_be_view.get("primary_action"),
                "primary_action_available": bool(to_be_view.get("primary_action_available")),
                "capability_available": bool(to_be_available),
                "proposal_eligible": bool(to_be_view.get("proposal_eligible")),
                "research": to_be_view,
                "to_be": to_be_view,
            })

    context = case_context or {
        "case_label": "尚未建立案例",
        "scenario_label": "尚未選定服務情境",
        "version_label": "尚無可審閱版本",
        "freshness": "unconfirmed",
    }
    current_journey = next((item for item in runtime if item["id"] == "journey-map"), None)
    return {
        "current_stage": current_stage,
        "stages": runtime,
        "case_context": context,
        "journey_confirmation": {
            "state": (current_journey or {}).get("journey_state"),
            "primary_action": (current_journey or {}).get("primary_action"),
            "primary_action_available": bool((current_journey or {}).get("primary_action_available")),
            "as_is_blocked": bool((current_journey or {}).get("as_is_blocked")),
        },
    }


def _derive_to_be_research_runtime(contract: dict, operation: dict | None) -> dict:
    """Translate a persisted operation projection into one researcher-facing state."""
    research_contract = contract["as_is_research"]
    definitions = {item["id"]: item for item in research_contract["states"]}
    phase_labels = research_contract["phase_labels"]
    operation = operation if isinstance(operation, dict) else None
    raw_state = str((operation or {}).get("state") or "")
    phase = str((operation or {}).get("phase") or "")
    if raw_state == "stale":
        state_id = "research_stale"
    elif raw_state == "failed":
        state_id = "research_failed"
    elif raw_state in {"queued", "running"}:
        state_id = "research_processing"
    elif raw_state == "done" and phase == "no_grounded_issues":
        state_id = "no_grounded_issues"
    elif raw_state == "done" and phase == "research_ready":
        state_id = "research_ready"
    else:
        state_id = "research_failed"

    definition = definitions[state_id]
    def nonnegative_count(key: str) -> int:
        try:
            value = int(((operation or {}).get("counts") or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    counts = {key: nonnegative_count(key) for key in ("issues", "sources", "claims")}
    result = {
        "state": state_id,
        "status": definition["status"],
        "heading": definition["heading"],
        "counts": counts,
        "phase_label": phase_labels.get(phase),
        "primary_action_available": False,
        "primary_action": None,
    }
    if state_id == "research_processing":
        result.update({
            "what_happened": "系統正在整理現況問題並查找可採用的外部資料。",
            "why_it_matters": "完成來源與主張檢查前，不能把外部資料當成提案設計依據。",
            "next_action": "可以離開此頁；重新載入不會重複啟動調研。",
        })
    elif state_id == "research_failed":
        message = (((operation or {}).get("error") or {}).get("user_message"))
        retryable = bool((operation or {}).get("retryable"))
        result.update({
            "what_happened": message or "調研狀態未能建立或本次調研未完成。",
            "why_it_matters": "目前沒有可被信任的外部調研結果，不能據此宣稱已形成提案。",
            "next_action": "重新進行調研。" if retryable else "回到現況檢查目前基線或請系統管理者處理。",
            "primary_action_available": retryable,
            "primary_action": research_contract["retry_action"] if retryable else None,
        })
    elif state_id == "no_grounded_issues":
        result.update({
            "what_happened": "已確認資料未支持可供外部調研的現況問題。",
            "why_it_matters": "系統沒有啟動外部調研，也沒有產生提案。",
            "next_action": "保留此結論，或回到現況補充資料後重新確認。",
        })
    elif state_id == "research_ready":
        result.update({
            "what_happened": "現況問題與符合來源標準的外部資料已完成整理。",
            "why_it_matters": "外部主張仍是假設，不是案例中已觀察到的事實。",
            "next_action": "檢視問題、來源與外部主張；提案產生不在此版本範圍。",
        })
    else:
        result.update({
            "what_happened": "目前調研結果對應的是較早的現況。",
            "why_it_matters": "現況已經改過，舊的調研不能拿來當這次提案的依據。",
            "next_action": "回到現況，確認目前版本後再開始新的調研。",
        })
    return result


def _derive_to_be_runtime(
    contract: dict,
    research_operation: dict | None,
    to_be_operation: dict | None,
    review: dict | None,
    *,
    stale: bool = False,
) -> dict:
    """Derive exactly one truthful To-be state from persisted records."""
    definitions = {item["id"]: item for item in contract["to_be"]["states"]}
    # 沒有任何調研紀錄 ≠ 調研失敗。`_derive_to_be_research_runtime()` 的 else
    # 分支把「還沒開始」也算成 research_failed，於是一個剛確認完基線、根本還沒
    # 跑過調研的 run，畫面會說「調研未完成」而且不給任何動作——研究者按不到
    # 開始，也回不去，整條路就斷在這裡。這個狀態是可以行動的，要給控制項。
    # 直接組出來而不走 contract["to_be"]["states"]：那份清單是鎖死的 version 1
    # 形狀（`_project_workbench_contract` 對不上就整份拒絕）。
    if research_operation is None and not stale:
        start_action = {
            "id": "retry-as-is-research",
            "label": "開始外部調研",
            "consequence": "以目前已確認的現況執行問題分析與受限外部調研",
        }
        return {
            "state": "research_not_started",
            "status": "draft",
            "heading": "尚未開始外部調研",
            "phase_label": None,
            "primary_action": start_action,
            "primary_action_available": True,
            "proposal_eligible": False,
            "counts": {"issues": 0, "sources": 0, "claims": 0},
            "what_happened": "這個服務藍圖版本已經確認，但還沒有跑過外部調研。",
            "why_it_matters": "沒有調研結果就沒有可追溯的外部依據，提案無從談起。",
            "next_action": "按下「開始外部調研」。",
        }
    research = _derive_to_be_research_runtime(contract, research_operation)
    to_be_operation = to_be_operation if isinstance(to_be_operation, dict) else None
    raw_state = str((to_be_operation or {}).get("state") or "")
    phase = str((to_be_operation or {}).get("phase") or "")
    revision_kind = str(((to_be_operation or {}).get("revision_context") or {}).get("kind") or "")
    review_state = str((review or {}).get("state") or "")
    if stale or research["state"] == "research_stale":
        state_id = "to_be_stale"
    elif research["state"] in {"research_processing", "research_failed", "no_grounded_issues"}:
        state_id = research["state"]
    elif raw_state in {"queued", "running"}:
        state_id = "to_be_revision_processing" if revision_kind == "revision" else "to_be_processing"
    elif raw_state == "failed":
        state_id = "to_be_failed"
    elif review_state in {"accepted", "accepted_pending_validation"}:
        state_id = "to_be_accepted"
    elif review_state == "declined":
        state_id = "to_be_declined"
    elif review_state == "draft" and raw_state == "done" and phase == "to_be_draft_ready":
        state_id = "to_be_draft_ready"
    elif research["state"] == "research_ready":
        state_id = "to_be_processing"
    else:
        state_id = "to_be_failed"
    definition = definitions[state_id]
    result = {
        "state": state_id,
        "status": definition["status"],
        "heading": definition["heading"],
        "phase_label": contract["to_be"]["phase_labels"].get(
            phase or str((research_operation or {}).get("phase") or "")
        ),
        "primary_action": None,
        "primary_action_available": False,
        "proposal_eligible": state_id in {
            "to_be_draft_ready", "to_be_revision_processing", "to_be_accepted", "to_be_declined"
        },
    }
    if state_id in {"research_processing", "research_failed", "no_grounded_issues"}:
        result.update({key: research.get(key) for key in (
            "what_happened", "why_it_matters", "next_action", "counts"
        )})
        if state_id == "research_failed":
            result["primary_action"] = research.get("primary_action")
            result["primary_action_available"] = bool(research.get("primary_action_available"))
    elif state_id == "to_be_processing":
        result.update({
            "what_happened": "系統正在把現況問題、外部研究與改善目標整合成一份完整方案。",
            "why_it_matters": "完成驗證與保存前，不顯示部分提案，也不能進行採納判斷。",
            "next_action": "可以離開此頁；完成後會顯示完整的待驗證方案。",
        })
    elif state_id == "to_be_revision_processing":
        result.update({
            "what_happened": "系統正在依調整說明建立一份完整替代方案。",
            "why_it_matters": "目前方案仍可閱讀，但完成前不能接受或不採用舊版本。",
            "next_action": "等待新方案完成；失敗時仍保留目前版本。",
        })
    elif state_id == "to_be_failed":
        retryable = bool((to_be_operation or {}).get("retryable"))
        message = (((to_be_operation or {}).get("error") or {}).get("user_message"))
        result.update({
            "what_happened": message or "本次方案產生未完成。",
            "why_it_matters": "沒有完整通過依據與投影檢查的方案可供判斷。",
            "next_action": "重新產生方案。" if retryable else "先修正畫面指出的前置條件。",
            "primary_action": {
                "id": "retry-to-be-generation",
                "label": "重新產生方案",
                "consequence": "沿用目前已確認資料重試完整方案產生",
            } if retryable else None,
            "primary_action_available": retryable,
        })
    elif state_id == "to_be_draft_ready":
        result.update({
            "what_happened": "一份完整提案已準備好，所有改變仍待驗證。",
            "why_it_matters": "接受表示進入驗證，不代表方案已核准或實施。",
            "next_action": "審閱藍圖、方案摘要與依據後，判斷是否進入驗證。",
            "primary_action": contract["to_be"]["actions"]["accept"],
            "primary_action_available": True,
        })
    elif state_id == "to_be_accepted":
        result.update({
            "what_happened": "這份方案已採納，待驗證。",
            "why_it_matters": "驗證計畫與停止條件仍然有效，尚未形成實施核准。",
            "next_action": "依方案中的試行範圍與門檻進行驗證。",
        })
    elif state_id == "to_be_declined":
        result.update({
            "what_happened": "這份提案記錄為不採用。",
            "why_it_matters": "已確認的現況與所有版本歷史都還在。",
            "next_action": "可返回現況或查看版本歷史。",
        })
    else:
        result.update({
            "what_happened": "目前提案對應的是較早的改善目標或上游版本。",
            "why_it_matters": "舊方案不能代表目前確認的服務狀態。",
            "next_action": "回到畫面指出的上游階段，確認目前版本後重新產生。",
        })
    return result


def _run_has_confirmed_journey(run: dict | None) -> bool:
    if not run:
        return False
    name = _safe_component(run.get("name", ""), "")
    if name and _journey_is_candidate_path(RUNS_DIR / name):
        # confirm-journey-candidate-save-only: a fresh-path Journey is
        # confirmed only while the current candidate matches what was confirmed.
        return _journey_confirmation(RUNS_DIR / name)["state"] == "confirmed"
    meta = run.get("meta") or {}
    if meta.get("confirmed_journey_digest") or meta.get("confirmed_journey_file"):
        return True
    draft_path = RUNS_DIR / name / "sbp_draft.yaml" if name else None
    if not draft_path or not draft_path.is_file():
        return False
    try:
        journey = (_load(draft_path) or {}).get("journey") or {}
        primary = next(
            (item for item in journey.get("scenarios") or [] if item.get("id") == journey.get("primary_scenario")),
            None,
        )
        return bool(primary and primary.get("status") == "confirmed" and primary.get("steps"))
    except Exception:
        return False


def _runtime_for_preview(contract: dict, preview_run: dict | None, preview_case: dict | None) -> dict:
    if contract.get("contract_status") != "supported":
        return {}
    meta = (preview_run or {}).get("meta") or {}
    run_name = _safe_component((preview_run or {}).get("name", ""), "")
    run_dir = RUNS_DIR / run_name if run_name else None
    as_is_present = bool(run_dir and (run_dir / "sbp_draft.yaml").is_file())
    as_is_validity_findings = _run_validity_findings(run_dir) if as_is_present else []
    as_is_advisory_findings = _run_advisory_findings(run_dir) if as_is_present else []
    proposal_eligible, proposal_ineligible_reason = _proposal_eligibility(run_dir)
    current_fingerprint = _run_fingerprint(run_dir) if run_dir else None
    lifecycle_state = None
    to_be_available = False
    research_projection = None
    to_be_operation = None
    to_be_review = None
    to_be_is_stale = False
    validation_detail = None
    operative_goal = None
    registry_path = meta.get("artifact_registry")
    versions = meta.get("artifact_versions") if isinstance(meta.get("artifact_versions"), dict) else {}
    if isinstance(registry_path, str) and registry_path and versions:
        try:
            from sbp_gen import lifecycle as artifact_lifecycle

            registry = _load(
                storage_locations.resolve_registry_pointer(
                    registry_path, run_dir, RUNS_DIR
                )
            )
            states = artifact_lifecycle.resolve_lifecycle(registry)
            lifecycle_state = states.get(versions.get("as_is_graph"))
            to_be_available = True
        except Exception:
            lifecycle_state = None
            to_be_available = False
    if run_name:
        try:
            research_projection = _as_is_research_projection(run_name)
        except AsIsResearchPrerequisiteRejected:
            research_projection = None
        if run_dir:
            operative_goal = sbp_improvement_goal_review.read_operative_goal(run_dir)
            raw_to_be_operation = sbp_to_be.load_current_operation(run_dir)
            to_be_operation = raw_to_be_operation
            if raw_to_be_operation is not None:
                try:
                    baseline = sbp_to_be.resolve_baseline(run_dir)
                    _current_to_be, review = sbp_to_be.load_current_review(baseline)
                    to_be_review = review
                except sbp_to_be.ToBeOperationError:
                    to_be_is_stale = True
            try:
                validation_detail = _validation_detail(run_name)
            except (
                sbp_validation.ValidationOperationError,
                AsIsResearchRequestRejected,
                AsIsResearchPrerequisiteRejected,
            ):
                validation_detail = None
    case_label = _preview_label(preview_case) if preview_case else "尚未建立案例"
    # One lock acquisition, not three scattered .get() calls: under
    # ThreadingHTTPServer a concurrent request can now race a background
    # generation thread's `with _job_lock:` write between those calls,
    # pairing a new state with a stale kind. A single snapshot can't tear.
    with _job_lock:
        job_snapshot = dict(_job)
    if job_snapshot.get("kind") == "journey_recheck":
        journey_state = {
            "running": "rechecking",
            "done": "rechecked",
            "failed": "recheck-failed",
        }.get(str(job_snapshot.get("state")))
    else:
        journey_state = None
    return _derive_workbench_runtime(
        contract,
        case_ready=bool(preview_case),
        journey_draft_ready=bool((preview_case or {}).get("steps")),
        journey_confirmed=_run_has_confirmed_journey(preview_run),
        as_is_present=as_is_present,
        as_is_validity_findings=as_is_validity_findings,
        as_is_advisory_findings=as_is_advisory_findings,
        proposal_eligible=proposal_eligible,
        proposal_ineligible_reason=proposal_ineligible_reason,
        as_is_valid=not as_is_validity_findings if as_is_present else False,
        job_state=str(job_snapshot.get("state") or "idle"),
        job_kind=job_snapshot.get("kind"),
        job_run=job_snapshot.get("out"),
        run_name=run_name,
        review_state=meta.get("review_state"),
        reviewed_fingerprint=meta.get("reviewed_artifact_fingerprint"),
        current_fingerprint=current_fingerprint,
        lifecycle_state=lifecycle_state,
        to_be_available=to_be_available,
        as_is_research=research_projection,
        to_be_operation=to_be_operation,
        to_be_review=to_be_review,
        to_be_stale=to_be_is_stale,
        validation_detail=validation_detail,
        operative_goal=operative_goal,
        sbp_solutions_state=_sbp_solutions_state(run_dir) if run_dir else None,
        journey_state=journey_state,
        fresh_as_is_path=bool(run_dir and _journey_is_candidate_path(run_dir)),
        case_context={
            "case_label": case_label,
            "scenario_label": "主要服務情境",
            "version_label": "目前服務藍圖版本" if as_is_present else "尚無可審閱版本",
            "freshness": "stale" if lifecycle_state == "stale" else ("current" if lifecycle_state == "current" else "unconfirmed"),
        },
        framework_point_stages=_framework_point_runtime_entries(
            (preview_case or {}).get("case_id"),
            (_active_data_root() / preview_case["path"]).parent if preview_case and preview_case.get("path") else None,
            RUNS_DIR,
        ),
    )


def _bootstrap_payload(run_name: str | None = None, user: sbp_auth.User | None = None) -> dict | None:
    """Build the additive bootstrap response used by the handler and tests.

    An explicit run_name computes every projection against that run instead
    of the established default-run selection. Omitting it preserves current
    first-load behavior. A run_name that resolves to no existing run returns
    None so the caller can respond with the not-found shape already used by
    run-scoped endpoints, instead of silently falling back to another run.
    """
    cases = _discover_cases()
    runs = _discover_runs()
    if user is not None:
        _ensure_legacy_projects(user)
        owned_case_ids = _auth_service().owned_case_ids(user.user_id)
        cases = [case for case in cases if case.get("case_id") in owned_case_ids]
        runs = [
            run for run in runs
            if (run.get("meta") or {}).get("case_id") in owned_case_ids
        ]
    if run_name is not None:
        preview_run = next((run for run in runs if run["name"] == run_name), None)
        if preview_run is None:
            return None
    else:
        visible_runs = [run for run in runs if run["name"] not in _project_prefs()["archived_versions"]]
        preview_run = next((run for run in visible_runs if run["name"] == PREVIEW_RUN_NAME), None)
        if preview_run is None:
            preview_run = next((run for run in visible_runs if run.get("format") == "action-graph"), None)
    preview_case = None
    case_id = ((preview_run or {}).get("meta") or {}).get("case_id")
    if case_id:
        preview_case = next((case for case in cases if case.get("case_id") == case_id), None)
    workbench = _project_workbench_contract()
    if workbench.get("contract_status") == "supported":
        workbench["runtime"] = _runtime_for_preview(workbench, preview_run, preview_case)
        # Navigation is drawn from these stages; the runtime above still sees
        # the full contract, which its derivation needs.
        workbench["stages"] = [
            stage for stage in workbench.get("stages") or []
            if stage.get("id") not in PUBLIC_PREVIEW_DEFERRED_STAGES
        ]
    return {
        "cases": cases,
        "runs": runs,
        "preview_run": preview_run,
        "preview_case": preview_case,
        "preview_journey": _preview_journey(preview_run, preview_case),
        "preview_label": _preview_label(preview_case),
        "ui": _ui_status(),
        "workbench": workbench,
    }


def _ui_status() -> dict:
    """Expose the exact UI asset being served.

    The review server is commonly kept open while the HTML changes.  A visible
    asset version plus no-cache headers makes an old tab or another server
    instance diagnosable instead of looking like a product regression.

    Hashed by content, not mtime: a fresh process serving byte-identical
    ui.html (e.g. a restarted test server) must report the same version, or
    every restart would invalidate reviewed visual baselines that display it.
    """
    data = UI.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    return {
        "version": f"ui-{digest}-{len(data):x}",
        "path": str(UI.relative_to(REPO)),
        "input_mode": "single_case_bundle",
    }


def _static_asset(path: str) -> tuple[bytes, str, dict[str, str]] | None:
    """Return review assets with the cache policy used by the HTTP handler."""
    # v3-becomes-root: the prototype-v3 workbench is the default page; the
    # previous interface stays whole at /classic (PDF export prints from it).
    if path == "/classic":
        document = UI.read_bytes().replace(
            WORKBENCH_TOKEN_PLACEHOLDER, _workbench_token_style().encode("utf-8"), 1,
        )
        return document, "text/html; charset=utf-8", {
            "Cache-Control": "no-store, max-age=0",
            "X-SBP-UI-Version": _ui_status()["version"],
        }
    if path in ("/", "/v3"):
        document = UI_V3.read_bytes().replace(
            WORKBENCH_TOKEN_PLACEHOLDER, _workbench_token_style().encode("utf-8"), 1,
        )
        return document, "text/html; charset=utf-8", {"Cache-Control": "no-store, max-age=0"}
    if path == "/relation_layout.js":
        return RELATION_LAYOUT.read_bytes(), "text/javascript; charset=utf-8", {
            "Cache-Control": "no-store",
        }
    return None


# Moved to tools/pathnames.py (2026-09-08) so tools/workbench/server.py can
# use the same guard without importing from this module \u2014 see that module's
# docstring for why. Re-bound under the old name here so this file's ~20
# existing call sites don't need to change.
_safe_component = pathnames.safe_component


def _recheck_root() -> Path:
    """Keep noncanonical recheck runs outside researcher-facing As-is runs."""
    return RUNS_DIR / "_journey-rechecks"


_PROGRESS_LINE_RE = re.compile(r"\[進度\] (\S+) (\d+)/(\d+) (\S+)")


def _job_progress(log: object) -> dict[str, tuple[int, int, str]]:
    """Latest `[進度] <label> <done>/<total> <unit>` per label in a job log."""
    progress: dict[str, tuple[int, int, str]] = {}
    for line in log if isinstance(log, list) else []:
        match = _PROGRESS_LINE_RE.search(str(line))
        if match:
            progress[match.group(1)] = (int(match.group(2)), int(match.group(3)), match.group(4))
    return progress


def _with_progress(text: str, label: str, progress: dict) -> str:
    """「正在整理開放編碼」 + 6/15 份 -> 「正在整理開放編碼（6／15 份）」."""
    if label not in progress:
        return text
    done, total, unit = progress[label]
    return f"{text}（{done}／{total} {unit}）"


def _project_job_status() -> dict:
    """The researcher-facing view of `_job` — never the raw dict.

    `_job["proc"]` is a live `subprocess.Popen`, added so cancellation has
    something to terminate; it is not JSON-serialisable and it is not
    something a researcher reads, so it must never reach this projection.
    """
    with _job_lock:
        projected_job = dict(_job)
        projected_job.pop("proc", None)
        projected_job.pop("run_lock", None)  # the job's project lease (run-state-write-integrity)
        projected_job.pop("owner_actor_id", None)
        projected_job.pop("case_ref", None)
        started = projected_job.pop("started_monotonic", None)
        if projected_job.get("state") in {"running", "waiting_for_model"} and isinstance(started, float):
            projected_job["elapsed_seconds"] = int(time.monotonic() - started)
        if (projected_job.get("kind") in {AS_IS_GENERATION_OPERATION_KIND, FRESH_CODING_OPERATION_KIND}
                and projected_job.get("state") == "running"
                and isinstance(projected_job.get("out"), str)):
            projected_job["phase"] = _generation_phase_label(projected_job["out"]) or "處理中"
            # Honest progress for the long coding steps: the latest count the
            # running step reported, shown only beside its own phase.
            progress = _job_progress(projected_job.get("log"))
            label = next((name for name in progress if name in projected_job["phase"]), None)
            if label:
                projected_job["phase"] = _with_progress(projected_job["phase"], label, progress)
        if projected_job.get("state") == "failed":
            if projected_job.get("kind") == PERSONA_CONTENT_GENERATION_OPERATION_KIND:
                projected_job["failure"] = _persona_content_failure_projection(projected_job.get("log"))
            elif projected_job.get("kind") != FRESH_CODING_OPERATION_KIND:
                projected_job["failure"] = _generation_failure_cause(projected_job.get("log"))
        if projected_job.get("kind") in {"journey_recheck", FRESH_CODING_OPERATION_KIND}:
            projected_job.pop("log", None)
        if projected_job.get("kind") == STAGE_RUN_OPERATION_KIND:
            projected_job["log"] = _researcher_readable_log(projected_job.get("log"))
    if projected_job.get("kind") == HMW_FORMATION_OPERATION_KIND:
        projected_job = _hmw_formation_job_view(projected_job)
    return projected_job


def _job_belongs_to(user: object | None) -> bool:
    """Keep the process-wide queue observable only to its authenticated owner."""
    service = _auth_service()
    if not service.enabled:
        return True
    with _job_lock:
        owner_id = _job.get("owner_actor_id")
    return user is not None and (not owner_id or owner_id == getattr(user, "user_id", None))


def _cancel_running_job() -> dict:
    """Terminate the job's subprocess, if any is actually running.

    子行程一旦開始跑，研究者除了關頁籤沒有別的辦法喊停，留下一個孤兒行程繼續
    佔用資源、繼續打外部 API。這是唯一終止它的路徑。抽成獨立函式，不用真的開
    HTTP 連線就能測（跟 `_build_generate_cmd` 抽出來的理由一樣）。
    """
    with _job_lock:
        proc = _job.get("proc")
        if _job.get("state") != "running" or proc is None:
            return {"ok": True, "state": _job.get("state")}
        child_processes.terminate_group(proc)
        _job["state"] = "cancelled"
        _job["phase"] = "已取消"
        _job["log"].append("[已取消] 研究者手動取消")
    # run-state-write-integrity: HMW formation's own "cancelled" record is
    # written by the job thread once the worker has exited (it owns the
    # project lock until then), not here while the worker may still write.
    return {"ok": True, "state": "cancelled"}


# run-state-write-integrity: in-process jobs that never write a project's
# run.json / journey_proposal.yaml run without the project lock.
_UNLOCKED_JOB_KINDS = frozenset({
    sbp_to_be.OPERATION_KIND, sbp_as_is_research.OPERATION_KIND,
    CHART_GUIDE_OPERATION_KIND, IMPLEMENTATION_RESEARCH_OPERATION_KIND,
})
JOB_START_BUSY_MESSAGE = "目前無法開始。此專案仍有其他工作正在執行，可能是先前未結束的背景分析。請稍後再試。"


def _replace_job(**values) -> None:
    """Replace, rather than merge, typed operation state under the shared lock.

    Registering a running project job takes that project's lock first, without
    waiting (`RunLockBusy` leaves the job record untouched). The lease is kept
    in `_job["run_lock"]`; `_start_job_thread` hands it to the job thread, which
    releases it after its last write."""
    lease = None
    previous = _job.get("run_lock")
    lock_dir = values.pop("lock_dir", None)
    if values.get("state") == "running" and values.get("kind") not in _UNLOCKED_JOB_KINDS and values.get("out"):
        target = Path(lock_dir) if lock_dir is not None else RUNS_DIR / str(values["out"])
        if (previous is not None and previous.held_by_current_thread()
                and previous.run_dir == os.path.realpath(target)):
            # A job thread continuing with a follow-up job on the same project
            # (Selective Coding -> JTBD candidates) keeps the lease it holds;
            # its thread releases it once, at the very end.
            lease = previous
        else:
            try:
                lease = sbp_run_lock.acquire(target)
            except sbp_run_lock.RunLockBusy:
                raise sbp_run_lock.RunLockBusy(JOB_START_BUSY_MESSAGE) from None
    if previous is not None and previous is not lease and not previous.adopted:
        previous.release()  # registered but never handed to a job thread
    owner_actor_id = values.pop("owner_actor_id", None)
    if owner_actor_id is None:
        owner_actor_id = _current_validation_actor().get("actor_id")
    _job.clear()
    _job.update({
        "kind": None,
        "state": "idle",
        "phase": None,
        "log": [],
        "out": None,
        "output_identity": None,
        "result_endpoint": None,
        "owner_actor_id": owner_actor_id,
    })
    _job.update(values)
    if lease is not None:
        _job["run_lock"] = lease
    if values.get("state") == "running":
        _job["started_monotonic"] = time.monotonic()
        summary = _REQUEST_EXECUTION_SUMMARY.get()
        if summary and "execution" not in values:
            _job["execution"] = summary


def _start_job_thread(target, args: tuple = ()) -> None:
    """Start a registered job's thread. The job's project lease (if any) moves
    to that thread and is released only after `target` has returned, i.e.
    after the job's last post-run write."""
    with _job_lock:
        lease = _job.get("run_lock")

    def run() -> None:
        if lease is not None:
            lease.adopt_current_thread()
        try:
            target(*args)
        finally:
            if lease is not None:
                with _job_lock:
                    if _job.get("run_lock") is lease:
                        _job.pop("run_lock", None)
                lease.release()

    threading.Thread(target=run, daemon=True).start()


def _current_job_lease():
    """The running job's project lease, for handing to its `sbp_gen` child."""
    with _job_lock:
        return _job.get("run_lock")


def _recheck_failure_projection() -> dict:
    return {
        "what_happened": "歷程資料檢查未能完成。",
        "why_it_matters": "尚未取得可再次確認的完整歷程，因此服務藍圖不會開始產生。",
        "next_action": "保留目前修改後重試，或先修正歷程內容再重新檢查。",
    }


class JourneyRecheckRequestRejected(ValueError):
    pass


class JourneyRecheckSourceUnavailable(JourneyRecheckRequestRejected):
    """The raw data holds a file this machine has no reader for (HTTP 422)."""


def _unreadable_source_message(manifest: object) -> str | None:
    """desktop-runtime-foundation: a PDF the machine cannot read is refused up
    front, naming the files, rather than silently coded without them."""
    names = sbp_ingest.unavailable_sources(manifest if isinstance(manifest, dict) else {})
    if not names:
        return None
    return ("PDF 讀取目前不可用：系統找不到 Poppler。讀不到的檔案：" + "、".join(names)
            + "。你可以安裝 Poppler，或移除這些 PDF、改用 TXT／MD 資料。")


class JourneyRecheckConflict(ValueError):
    pass


class JourneyRecheckNotFound(ValueError):
    pass


def _declared_raw_root(case_path: Path, declared: object, out: str) -> Path:
    """A case without raw/ names its raw material in case.yaml `raw_sources`.

    The CLI takes one --input root, so the declared files are linked into one
    folder under the recheck's own directory. Every declared path must stay
    inside the case folder; nothing else in that folder (reports, ground
    truth) is read.
    """
    case_dir = case_path.parent.resolve()
    if not isinstance(declared, list) or not declared:
        raise JourneyRecheckRequestRejected("case.yaml 的 raw_sources 格式不正確。")
    files: list[Path] = []
    for entry in declared:
        target = (case_dir / str(entry)).resolve()
        if case_dir != target and case_dir not in target.parents:
            raise JourneyRecheckRequestRejected("raw_sources 只能指向案例資料夾裡面的檔案。")
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(p for p in sorted(target.rglob("*"))
                         if p.is_file() and p.suffix.lower() in sbp_ingest.SUPPORTED_SUFFIXES)
        else:
            raise JourneyRecheckRequestRejected(f"raw_sources 列出的路徑不存在：{entry}")
    if not files:
        raise JourneyRecheckRequestRejected("raw_sources 沒有任何可讀的原始資料。")
    staged = _recheck_root() / "_raw" / out
    staged.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(files, 1):
        link = staged / f"{index:03d}-{source.name}"
        if not link.exists():
            link.symlink_to(source)
    return staged


def _resolve_journey_recheck_request(payload: object) -> dict:
    """Resolve every path server-side and return one bounded recheck context."""
    if not isinstance(payload, dict):
        raise JourneyRecheckRequestRejected("歷程重查資料格式不正確，請回到確認畫面後再試。")
    forbidden_path_fields = {
        "input", "raw", "raw_path", "evidence", "evidence_path", "from_evidence"
    }
    if forbidden_path_fields.intersection(payload):
        raise JourneyRecheckRequestRejected("raw data 與 evidence 由系統依所選案例取得，不能由瀏覽器指定路徑。")
    case_ref = payload.get("case")
    if not isinstance(case_ref, str) or not case_ref.strip():
        raise JourneyRecheckRequestRejected("請先選定案例，再重新檢查歷程。")
    case_record = next((item for item in _discover_cases() if item.get("path") == case_ref), None)
    if case_record is None:
        raise JourneyRecheckRequestRejected("選定的案例不存在或不在可用案例清單中。")
    data_root = _active_data_root().resolve()
    case_path = (data_root / case_ref).resolve()
    if data_root not in case_path.parents or not case_path.is_file():
        raise JourneyRecheckRequestRejected("選定的案例無法安全讀取。")
    raw_root = case_path.parent / "raw"
    case_for_raw = _load(case_path) if case_path.is_file() else None
    declared_raw = (case_for_raw or {}).get("raw_sources") if isinstance(case_for_raw, dict) else None
    if not raw_root.exists() and not declared_raw:
        raise JourneyRecheckRequestRejected(
            "所選案例沒有 raw/ 資料夾，case.yaml 也沒有用 raw_sources 列出原始資料放在哪裡，無法重新檢查歷程。")
    evidence_refs = case_record.get("evidence_files") or []
    if not evidence_refs:
        raise JourneyRecheckRequestRejected("所選案例缺少既有 evidence，無法重新檢查歷程。")
    evidence_path = (data_root / evidence_refs[0]).resolve()
    if data_root not in evidence_path.parents or not evidence_path.is_file():
        raise JourneyRecheckRequestRejected("所選案例的 evidence 無法安全讀取。")

    out = payload.get("out")
    if not isinstance(out, str) or not out.strip() or _safe_component(out, "") != out:
        raise JourneyRecheckRequestRejected("這次歷程重查的名稱不正確，請重新開始。")
    baseline = payload.get("baseline_journey")
    edited = payload.get("edited_journey")
    if not isinstance(baseline, dict) or not isinstance(edited, dict):
        raise JourneyRecheckRequestRejected("缺少 AI 初版或研究者最新調整的完整歷程。")
    case = _load(case_path)
    evidence = _load(evidence_path)
    errors = V.structural_errors(case, V.load_schema("case")) + V.case_errors(case)
    errors += V.structural_errors(evidence, V.load_schema("evidence"))
    errors += [f"AI 初版：{item}" for item in V.editable_journey_errors(baseline)]
    errors += [f"研究者調整：{item}" for item in V.editable_journey_errors(edited)]
    case_id = case.get("case_id") if isinstance(case, dict) else None
    if baseline.get("case_id") != case_id or edited.get("case_id") != case_id:
        errors.append("AI 初版、最新調整與所選案例必須是同一案例。")
    if errors:
        raise JourneyRecheckRequestRejected("歷程重查資料尚未完整：" + "；".join(errors[:12]))
    if not raw_root.exists():
        raw_root = _declared_raw_root(case_path, declared_raw, out)
    try:
        raw_material, source_manifest = sbp_ingest.ingest(raw_root)
    except Exception as exc:
        raise JourneyRecheckRequestRejected(f"所選案例的 raw data 無法解析：{exc}") from exc
    unreadable = _unreadable_source_message(source_manifest)
    if unreadable:
        raise JourneyRecheckSourceUnavailable(unreadable)
    if not raw_material.strip():
        raise JourneyRecheckRequestRejected("所選案例沒有可讀的 raw data。")
    digests = sbp_stages.journey_recheck_digests(
        raw_material,
        evidence,
        baseline,
        edited,
        case_id,
        include_projection_metadata=True,
    )
    result_dir = _recheck_root() / out
    return {
        "case_ref": case_ref,
        "case_path": case_path,
        "case": case,
        "raw_root": raw_root,
        "raw_material": raw_material,
        "source_manifest": source_manifest,
        "evidence_ref": evidence_refs[0],
        "evidence_path": evidence_path,
        "evidence": evidence,
        "baseline_journey": baseline,
        "edited_journey": edited,
        "researcher_note": payload.get("researcher_note") if isinstance(payload.get("researcher_note"), str) else None,
        "output_identity": out,
        "result_dir": result_dir,
        **digests,
    }


def _recheck_result_projection(context: dict, result: dict) -> dict:
    """Expose the complete candidate and evidence disclosure, never model trace."""
    return {
        "run": context["output_identity"],
        "state": "awaiting_journey_confirmation",
        "result_digest": sbp_lifecycle.canonical_content_digest(result),
        "request_digest": result["request_digest"],
        "edited_editable_digest": result["edited_editable_digest"],
        "baseline_as_is_input_projection_digest": context.get("baseline_as_is_input_projection_digest"),
        "edited_as_is_input_projection_digest": context.get("edited_as_is_input_projection_digest"),
        "as_is_input_projection_changed": context.get("as_is_input_projection_changed"),
        "candidate_editable_digest": V.editable_journey_digest(result["reconciled_journey"]),
        "reconciled_journey": result["reconciled_journey"],
        "reconciled_evidence": result["reconciled_evidence"],
        "step_assessments": result["step_assessments"],
        "journey_findings": result["journey_findings"],
    }


def _validated_recheck_result(context: dict, *, require_server_admission: bool = True) -> dict | None:
    """Load a complete result; return None for a retryable same-request run."""
    request_meta = _recheck_root() / "_requests" / context["output_identity"] / "request.json"
    if request_meta.exists():
        try:
            stored_request = json.loads(request_meta.read_text(encoding="utf-8"))
        except Exception as exc:
            raise JourneyRecheckConflict(f"輸出識別的請求資料無法驗證：{exc}") from exc
        if stored_request.get("request_digest") != context["request_digest"]:
            raise JourneyRecheckConflict("輸出識別已屬於另一份歷程調整，不能覆寫。")
    result_dir = context["result_dir"]
    if not result_dir.exists():
        return None
    state_path = result_dir / "run.json"
    if not state_path.exists():
        if sbp_run_lock.content_entries(result_dir):
            raise JourneyRecheckConflict("輸出識別已有不完整內容，請使用新的重查名稱。")
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise JourneyRecheckConflict(f"輸出識別的狀態無法驗證：{exc}") from exc
    stored_digest = state.get("request_digest")
    if stored_digest != context["request_digest"]:
        raise JourneyRecheckConflict("輸出識別已屬於另一份歷程調整，不能覆寫。")
    result_path = result_dir / "journey_recheck.yaml"
    evidence_path = result_dir / "evidence.yaml"
    if state.get("state") == "failed" and not result_path.exists():
        return None
    if state.get("state") not in {"awaiting_journey_confirmation", "rechecking"}:
        raise JourneyRecheckConflict("這個輸出識別目前不能安全重用。")
    if not result_path.exists() or not evidence_path.exists():
        if state.get("state") == "rechecking":
            raise JourneyRecheckConflict("相同的歷程重查正在處理中。")
        raise JourneyRecheckNotFound("歷程重查結果尚未完整寫入。")
    if require_server_admission and state.get("server_admitted") is not True:
        raise JourneyRecheckNotFound("歷程重查結果尚未通過系統保存驗證。")
    result = _load(result_path)
    stored_evidence = _load(evidence_path)
    evidence_ids = {
        item.get("id") for item in context["evidence"] if isinstance(item, dict) and item.get("id")
    }
    errors = V.structural_errors(result, V.load_schema("journey-recheck"))
    errors += V.journey_recheck_errors(
        result,
        source_manifest=context["source_manifest"],
        source_units=sbp_stages._recheck_source_units(
            context["raw_material"], context["source_manifest"]
        ),
        prior_evidence_ids=evidence_ids,
    )
    if result.get("request_digest") != context["request_digest"]:
        errors.append("result request digest 與目前請求不同")
    if sbp_lifecycle.canonical_content_digest(stored_evidence) != sbp_lifecycle.canonical_content_digest(
        result.get("reconciled_evidence")
    ):
        errors.append("reconciled evidence 檔案與完整結果不同")
    if errors:
        raise JourneyRecheckNotFound("歷程重查結果未通過驗證：" + "；".join(errors[:12]))
    return _recheck_result_projection(context, result)


def _context_for_stored_recheck(output_identity: str) -> dict:
    """Re-resolve a persisted result using its server-admitted case sources."""
    safe = _safe_component(output_identity or "", "")
    if not safe or safe != output_identity:
        raise JourneyRecheckNotFound("找不到歷程重查結果。")
    result_dir = _recheck_root() / safe
    state_path = result_dir / "run.json"
    if not state_path.is_file():
        raise JourneyRecheckNotFound("找不到歷程重查結果。")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    case_ref = state.get("selected_case")
    baseline_path = (_recheck_root() / "_requests" / safe / "baseline_journey.yaml")
    edited_path = (_recheck_root() / "_requests" / safe / "edited_journey.yaml")
    if not isinstance(case_ref, str) or not baseline_path.is_file() or not edited_path.is_file():
        raise JourneyRecheckNotFound("歷程重查的保存脈絡不完整。")
    context = _resolve_journey_recheck_request({
        "case": case_ref,
        "out": safe,
        "baseline_journey": _load(baseline_path),
        "edited_journey": _load(edited_path),
    })
    return context


def _journey_digest(journey: dict) -> str:
    """Stable identity for the journey the researcher actually confirmed."""
    canonical = json.dumps(journey, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _generation_request_error(payload: object) -> str | None:
    """Validate the researcher-facing generation boundary before any model call."""
    if not isinstance(payload, dict):
        return "產生藍圖的資料格式不正確，請回到確認畫面後再試。"
    case = payload.get("case")
    if not isinstance(case, str) or not case.strip():
        return "請先選定案例，再產生藍圖。"
    data_root = _active_data_root().resolve()
    case_path = (data_root / case).resolve()
    if not case_path.is_file() or data_root not in case_path.parents:
        return "選定的案例不存在或無法讀取，請回到上一步重新選擇。"
    out = payload.get("out")
    if not isinstance(out, str) or not out.strip() or _safe_component(out, "") != out:
        return "這次產出的名稱不正確，請重新開始產生。"
    # Discovery produces the candidate journey that the next screen asks the
    # researcher to confirm; it cannot, by definition, carry one yet.
    if payload.get("discover_journey") is True:
        return None
    journey = payload.get("confirmed_journey")
    if not isinstance(journey, dict):
        return "請先確認至少一個顧客歷程步驟，再產生藍圖。"
    errors = V.structural_errors(journey, V.load_schema("journey"))
    scenarios = journey.get("scenarios") or []
    primary = next((s for s in scenarios if s.get("id") == journey.get("primary_scenario")), None)
    steps = (primary or {}).get("steps") or []
    ids = [step.get("id") for step in steps if isinstance(step, dict)]
    labels = [step.get("label") for step in steps if isinstance(step, dict)]
    if errors or not primary or primary.get("status") != "confirmed" or not steps:
        return "請先確認至少一個完整的顧客歷程步驟，再產生藍圖。"
    if len(ids) != len(steps) or len(set(ids)) != len(ids) or any(not isinstance(x, str) or not x.strip() for x in labels):
        return "顧客歷程有重複或空白步驟，請修正後再產生藍圖。"
    return None


def _case_id_for(case: object) -> str | None:
    """Resolve a payload's `case` path to the case_id it declares, or None if
    unreadable. Shared by the authorized-case gate and the post-generation
    route check so both agree on which case a request targets."""
    if not isinstance(case, str) or not case.strip():
        return None
    data_root = _active_data_root().resolve()
    case_path = (data_root / case).resolve()
    if not case_path.is_file() or data_root not in case_path.parents:
        return None
    try:
        case_data = _load(case_path)
    except Exception:
        return None
    return case_data.get("case_id") if isinstance(case_data, dict) else None


def _confirmed_copy(journey: dict) -> dict:
    """Copy one server-owned candidate and apply the explicit final decision."""
    confirmed = json.loads(json.dumps(journey, ensure_ascii=False))
    for scenario in confirmed.get("scenarios") or []:
        if isinstance(scenario, dict):
            scenario["status"] = "confirmed"
    return confirmed


def _server_evidence_for_generation(case_ref: str, baseline_run: str) -> Path:
    """Prefer evidence produced with the proposal, then the admitted case file."""
    run_evidence = RUNS_DIR / baseline_run / "evidence.yaml"
    if run_evidence.is_file():
        errors = V.structural_errors(_load(run_evidence), V.load_schema("evidence"))
        if not errors:
            return run_evidence
    case_record = next((item for item in _discover_cases() if item.get("path") == case_ref), None)
    evidence_refs = (case_record or {}).get("evidence_files") or []
    if not evidence_refs:
        raise JourneyRecheckRequestRejected("找不到這份初版歷程使用的 evidence，無法產生服務藍圖。")
    data_root = _active_data_root().resolve()
    evidence_path = (data_root / evidence_refs[0]).resolve()
    if data_root not in evidence_path.parents or not evidence_path.is_file():
        raise JourneyRecheckRequestRejected("初版歷程的 evidence 無法安全讀取。")
    return evidence_path


def _resolve_generation_confirmation(payload: dict) -> dict:
    """Return the exact server-owned journey/evidence admitted for generation."""
    submitted = payload.get("confirmed_journey")
    if not isinstance(submitted, dict):
        raise JourneyRecheckRequestRejected("請先確認完整歷程，再產生服務藍圖。")
    case_ref = payload.get("case")
    output_identity = payload.get("out")
    data_root = _active_data_root().resolve()
    selected_case_path = (data_root / str(case_ref or "")).resolve()
    selected_case = _load(selected_case_path)
    selected_case_id = selected_case.get("case_id") if isinstance(selected_case, dict) else None
    recheck_ref = payload.get("recheck_result")
    if recheck_ref is not None:
        if not isinstance(recheck_ref, dict):
            raise JourneyRecheckRequestRejected("歷程重查結果的識別資料不完整。")
        recheck_run = recheck_ref.get("run")
        supplied_result_digest = recheck_ref.get("digest")
        if not isinstance(recheck_run, str) or not isinstance(supplied_result_digest, str):
            raise JourneyRecheckRequestRejected("請提供完整的歷程重查結果識別與 digest。")
        context = _context_for_stored_recheck(recheck_run)
        if context["case_ref"] != case_ref:
            raise JourneyRecheckRequestRejected("歷程重查結果不屬於目前所選案例。")
        projection = _validated_recheck_result(context)
        if projection is None:
            raise JourneyRecheckNotFound("歷程重查結果尚未完成。")
        if projection["result_digest"] != supplied_result_digest:
            raise JourneyRecheckRequestRejected("歷程重查結果已變更或識別不符，請重新載入確認頁。")
        candidate = projection["reconciled_journey"]
        try:
            submitted_digest = V.editable_journey_digest(submitted)
        except ValueError as exc:
            raise JourneyRecheckRequestRejected(str(exc)) from exc
        if submitted_digest != projection["candidate_editable_digest"]:
            raise JourneyRecheckRequestRejected("歷程在重查後又被修改，請先重新檢查再產生服務藍圖。")
        authoritative = _confirmed_copy(candidate)
        if sbp_lifecycle.canonical_content_digest(_confirmed_copy(submitted)) != sbp_lifecycle.canonical_content_digest(authoritative):
            raise JourneyRecheckRequestRejected("確認畫面的歷程內容與已保存的重查結果不一致，請重新載入。")
        evidence_path = context["result_dir"] / "evidence.yaml"
        source = "rechecked"
        baseline_run = output_identity
        result_identity = recheck_run
        result_digest = projection["result_digest"]
    else:
        baseline_run = payload.get("journey_baseline_run") or output_identity
        if not isinstance(baseline_run, str) or _safe_component(baseline_run, "") != baseline_run:
            raise JourneyRecheckRequestRejected("初版歷程的識別不正確，請重新載入確認頁。")
        proposal_path = RUNS_DIR / baseline_run / "journey_proposal.yaml"
        if not proposal_path.is_file():
            raise JourneyRecheckRequestRejected("找不到 server 保存的 AI 初版歷程，不能略過重新檢查。")
        proposal = _load(proposal_path)
        errors = V.editable_journey_errors(proposal)
        if errors:
            raise JourneyRecheckRequestRejected("AI 初版歷程無法驗證：" + "；".join(errors[:8]))
        if proposal.get("case_id") != submitted.get("case_id") or proposal.get("case_id") != selected_case_id:
            raise JourneyRecheckRequestRejected("確認歷程與 AI 初版不屬於同一案例。")
        try:
            unchanged = V.editable_journey_digest(proposal) == V.editable_journey_digest(submitted)
        except ValueError as exc:
            raise JourneyRecheckRequestRejected(str(exc)) from exc
        if not unchanged:
            raise JourneyRecheckRequestRejected("歷程已有修改，請先依 raw data 重新檢查。")
        authoritative = _confirmed_copy(proposal)
        if sbp_lifecycle.canonical_content_digest(_confirmed_copy(submitted)) != sbp_lifecycle.canonical_content_digest(authoritative):
            raise JourneyRecheckRequestRejected("確認畫面的歷程內容與 server 初版不一致，請重新載入。")
        evidence_path = _server_evidence_for_generation(case_ref, baseline_run)
        source = "unchanged"
        result_identity = None
        result_digest = None
    errors = V.structural_errors(authoritative, V.load_schema("journey"))
    evidence = _load(evidence_path)
    errors += V.structural_errors(evidence, V.load_schema("evidence"))
    evidence_ids = {item.get("id") for item in evidence if isinstance(item, dict)}
    errors += sbp_stages._journey_errors(authoritative, evidence_ids, require_confirmed=True)
    if errors:
        raise JourneyRecheckRequestRejected("最後確認的歷程無法進入建模：" + "；".join(errors[:12]))
    return {
        "journey": authoritative,
        "journey_digest": V.editable_journey_digest(authoritative),
        "as_is_input_projection_digest": sbp_stages.as_is_projection.as_is_input_projection_digest(authoritative),
        "evidence_path": evidence_path,
        "source": source,
        "baseline_run": baseline_run,
        "recheck_result": result_identity,
        "recheck_result_digest": result_digest,
    }


def _parse_multipart(content_type: str, body: bytes, original_names: list | None = None,
                     fields: dict | None = None) -> list[tuple[str, bytes]]:
    """Parse browser multipart data without trusting client paths.

    `original_names` receives each file's client-sent base name, used only as a
    display label, never as a path. `fields` receives plain text form fields."""
    message = email.message_from_bytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body,
        policy=email.policy.default,
    )
    files = []
    for part in message.iter_parts():
        filename = part.get_filename()
        if not filename:
            field = part.get_param("name", header="content-disposition")
            if fields is not None and field:
                # 瀏覽器的 FormData 文字欄位是 UTF-8，但不帶 charset。
                fields[str(field)] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            continue
        files.append((_safe_component(filename, "upload.bin"), part.get_payload(decode=True) or b""))
        if original_names is not None:
            original_names.append(Path(filename.replace("\\", "/")).name)
    return files


def _intake_name_from_files(names: list[str]) -> str:
    """rework-shell-and-project-home：研究者 2026-09-27 裁定建案不再填五欄，
    專案名稱取第一個上傳檔名（去副檔名）。"""
    first = str(names[0] if names else "")
    stem = first.rsplit(".", 1)[0] if "." in first.lstrip(".") else first
    return " ".join(stem.split()) or "case-intake"


def _unused_intake_name(name: str) -> str:
    """同名的上傳資料夾已存在時加 -2、-3……，不寫進、也不覆蓋既有專案。"""
    candidate, suffix = name, 1
    while (UPLOADS_DIR / _safe_component(candidate, "case-intake")).exists():
        suffix += 1
        candidate = f"{name}-{suffix}"
    return candidate


def _save_upload(intake_name: str, files: list[tuple[str, bytes]]) -> dict:
    intake_id = _safe_component(intake_name, "case-intake")
    raw_dir = UPLOADS_DIR / intake_id / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, (name, payload) in enumerate(files, 1):
        safe = _safe_component(name, f"upload-{index}.bin")
        target = raw_dir / safe
        if target.resolve().parent != raw_dir.resolve():
            raise ValueError("上傳檔名不得離開案例目錄")
        if target.exists():
            target = raw_dir / f"{target.stem}-{index}{target.suffix}"
        target.write_bytes(payload)
        saved.append(str(target.relative_to(_active_data_root())))
    return {"intake_id": intake_id, "raw_dir": str(raw_dir.relative_to(_active_data_root())), "files": saved}


def _write_intake_case(saved: dict, *, label: str) -> str:
    # 只寫名稱與歷程佔位欄位：服務情境、顧客、提供者、改善目標都不再由站①提供，
    # 也不用任何通用值代替（決定六增補；first-project-intake 規格）。
    case_path = UPLOADS_DIR / saved["intake_id"] / "case.yaml"
    case = {
        "case_id": f"upload-{saved['intake_id']}",
        "label": " ".join(str(label or "").split()),
        "journey_start": "待確認歷程",
        "journey_end": "待確認歷程",
        "external_partners": [],
        "steps": ["待確認歷程"],
    }
    case_path.write_text(yaml.dump(case, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(case_path.relative_to(_active_data_root()))


class ImprovementGoalRejected(ValueError):
    pass


def _resolve_case_file(case: object) -> Path:
    """One case.yaml inside cases/, or a refusal.

    Writing is the reason this is stricter than `_case_id_for`: a path that
    escapes cases/ must never reach `write_text`.
    """
    if not isinstance(case, str) or not case.strip():
        raise ImprovementGoalRejected("沒有指定要設定哪一個案例。")
    # 兩邊都要 resolve()：只解析其中一邊，遇到路徑裡有符號連結的資料根目錄
    # （macOS 的 /var → /private/var 就是）就會算不出相對路徑。
    case_path = (_active_data_root() / case).resolve()
    cases_root = _active_cases_dir().resolve()
    if cases_root not in case_path.parents or case_path.name != "case.yaml":
        raise ImprovementGoalRejected("這個位置不是案例設定檔。")
    if not case_path.is_file():
        raise ImprovementGoalRejected("找不到這個案例的設定檔。")
    return case_path


# fix-improvement-goal-source-of-truth: `case.yaml`'s `improvement_goal` is
# the pre-research baseline 決定六 preserves so a researcher can ask whether
# the research changed their framing. What actually drives downstream work is
# the run-level `operative_goal` (決定一). The interface used to offer an edit
# control that wrote the retired field — it could neither unblock the gate it
# claimed to unblock nor be undone, since case files carry no version history.
# The write helper that control used is gone; this sentence is what replaced
# it, and it has to name both the reason and the path that does work.
#
# `_resolve_case_file` above deliberately stays: `_source_case_file_for_run`
# still uses it to locate a run's case file for reading.
RETIRED_CASE_GOAL_REFUSAL = (
    "案例裡的改善目標是研究前寫下的基準，保留它是為了跟研究後的結論對照，"
    "所以不從介面改寫。要確認或改寫這次實際採用的改善目標，"
    "請在確認服務藍圖之後到下一站按「確認改善目標」，"
    "或執行 sbp-gen improvement-goal-review --confirm-file。"
)


def _load(path: Path):
    return load_safe_yaml(path.read_text(encoding="utf-8"))


def _discover_cases() -> list[dict]:
    out = []
    renamed = _project_prefs()["names"]
    for f in sorted(_active_cases_dir().rglob("case.yaml")):
        # tools/workbench/server.py's own discover_cases() already excludes
        # this project's established naming convention for disposable test
        # fixtures for the same reason (a researcher never needs to see them
        # as a real option); this list had drifted out of sync with that one.
        if f.parent.name.startswith("browser-test-"):
            continue
        try:
            c = _load(f)
        except Exception:
            continue
        evid_paths = [
            p for p in f.parent.glob("evidence*.y*ml") if "gradient" not in str(p)
        ]
        # 逐字稿原文版是 2026-08-19 起的正典檔（ADR-0004 前置決定），優先用它——
        # 不然確認畫面看到的證據池跟真的拿去生成的證據池會不一樣，使用者完全看不出來。
        evid_paths.sort(key=lambda p: ("verbatim" not in p.name, p.name))
        evid = [str(p.relative_to(_active_data_root())) for p in evid_paths]
        legacy_steps = c.get("steps", [])
        journey_path = next(iter(sorted(f.parent.glob("journey*.y*ml"))), None)
        journey_steps = None
        if journey_path:
            try:
                journey = _load(journey_path) or {}
                primary = journey.get("primary_scenario")
                scenario = next((s for s in journey.get("scenarios") or [] if s.get("id") == primary and s.get("status") == "confirmed"), None)
                journey_steps = [step.get("label") for step in (scenario or {}).get("steps") or []]
            except Exception:
                journey_path = None
        # 案例資料夾外的手動放檔（例如直接丟一份新逐字稿進去）沒有上傳流程可攔，
        # 只能在研究者點選這個案例的當下比對時間戳——journey 檔案存在，
        # 又有任何一份證據比它新，就代表這個案例的旅程步驟已經跟不上證據池了。
        journey_stale = False
        if journey_path and evid_paths:
            journey_mtime = journey_path.stat().st_mtime
            journey_stale = any(p.stat().st_mtime > journey_mtime for p in evid_paths)
        out.append(
            {
                "path": str(f.relative_to(_active_data_root())),
                "case_id": c.get("case_id", f.parent.name),
                # 研究者看到的名字。`label` 是案例自己宣告的人話名稱（單位＋服務）；
                # 沒有宣告就退回資料夾名，那至少也是人取的，不是 `henglong-refresh-001`
                # 這種內部識別碼——後者是專案規則明令不得出現在畫面上的東西。
                "name": renamed.get(c.get("case_id", f.parent.name))
                or c.get("label") or f.parent.name,
                "steps": journey_steps or legacy_steps,
                "legacy_steps": legacy_steps,
                "journey_file": str(journey_path.relative_to(_active_data_root())) if journey_path else None,
                "evidence_files": evid,
                "improvement_goal": c.get("improvement_goal"),
                "journey_stale": journey_stale,
            }
        )
    return out


# 版本選單每一列現在只寫「日期 · 產出方式」。系統其實知道每份產出是哪一代管線、
# 哪個模型、是不是模擬資料、是不是舊格式——全都在 run.json 跟 sbp_draft.yaml 裡，
# 也全都送到前端了，只是沒有一項被講出來。於是七個看起來一樣的選項，選下去畫面
# 天差地遠：有的看得到關係圖有的看不到，有的是真跑的有的是手工模擬的。
#
# 這裡把那些欄位消化成一句話。判斷順序照「會讓研究者最快做錯事」排，一份產出只講
# 最重的那一件；沒有一件成立就不講——每一列都掛一句提醒，等於沒有提醒。
def _run_provenance_note(run: dict, current_prompt_hash: str) -> str:
    meta = run.get("meta") or {}
    models = meta.get("models") or {}
    origin = meta.get("origin")
    # 一份產出只講一句，不是在既有的產出方式後面再補一句。實測把兩句串起來會變成
    # 「手動傳輸 · 手動把模型輸出搬進來的」——同一件事講兩遍，而選單只有 250px 寬。
    # 判斷順序照「會讓研究者最快做錯事」排。
    if run.get("format") == "analysis-only":
        # truthful-failed-version-labels：還沒有藍圖不等於還在跑；失敗或取消要照實講。
        if meta.get("coding_state") == "failed":
            return "編碼失敗"
        if meta.get("coding_state") == "cancelled":
            return "編碼已取消"
        if any(str(key).endswith("_state") and value == "failed" for key, value in meta.items()):
            return "分析失敗"
        return "分析進行中，還沒產生藍圖"
    if run.get("format") != "action-graph":
        return "舊格式，看不到關係"
    if "simulated" in sbp_llm.model_name(models.get("structure")) or origin == "hand_authored_demo":
        return "手工模擬的示範資料"
    if origin == "manual_transport":
        return "手動搬進來的產出"
    recorded_hash = str(meta.get("prompt_hash") or "")
    if origin == "api_run":
        if recorded_hash and current_prompt_hash and recorded_hash != current_prompt_hash:
            return "系統產出（較舊的做法）"
        return "系統產出"
    return ORIGIN_LABELS_FALLBACK.get(origin or "", "產出方式未記錄")


# runVersionLabel 那份對照表的伺服器側鏡像，只放這裡沒有專門處理的產出方式。
ORIGIN_LABELS_FALLBACK = {
    "manual_demo_from_real_evidence": "手工示範（取自真實證據）",
    "integrated-test-only": "整合測試資料",
}


# 一份 run 算不算數，看它有沒有任何一段分析產物——不是看它有沒有藍圖。
# 原本的判準是 `sbp_draft.yaml` 存在，那是這個前台還只是「藍圖檢視器」時留下的
# 同義反覆（見 ADR-0003 決定二）：當時一份 run 就等於一份藍圖草稿。後來開放編碼、
# 主軸編碼、persona、JTBD、HMW 整條加在藍圖**前面**，這個判準沒有跟著改，於是
# 一份只跑到主軸編碼的 run 在介面上完全不存在——而那正是七站裡第 2 到第 4 站
# 的工作狀態（TARGET-ARCHITECTURE.md 決定十三）。
LISTABLE_RUN_ARTIFACTS = (
    "sbp_draft.yaml",
    "open_coding.yaml",
    "axial_coding.yaml",
    "selective_coding.yaml",
    "persona_matrix.yaml",
    "persona_content.yaml",
    "jtbd.yaml",
    "job_map.yaml",
    "hmw.yaml",
    "journey.yaml",
)


def _discover_runs() -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    out = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        # `_...` directories are developer fixtures, not researcher-facing runs.
        if d.name.startswith("_"):
            continue
        meta = {}
        if (d / "run.json").exists():
            try:
                meta = json.loads((d / "run.json").read_text(encoding="utf-8"))
            except Exception:
                pass
        if any((d / name).exists() for name in LISTABLE_RUN_ARTIFACTS) or meta.get("coding_state"):
            if not (d / "sbp_draft.yaml").exists():
                # 還沒走到藍圖那一站。不要讓它掉進下面「舊格式」那條判斷——
                # 那句話會把「還沒畫」說成「畫過但看不到關係」，是假話。
                fmt = "analysis-only"
            else:
                try:
                    draft = _load(d / "sbp_draft.yaml") or {}
                    fmt = "action-graph" if "graph" in draft else "legacy"
                except Exception:
                    fmt = "unknown"
            out.append({"name": d.name, "meta": meta, "format": fmt})
    try:
        current_hash = _pipeline_prompt_hash()
    except Exception:
        # 算不出目前指紋時，不要把每一份都說成「舊的」——不知道就別講。
        current_hash = ""
    for run in out:
        run["provenance_note"] = _run_provenance_note(run, current_hash)
    return out



def _persona_matrix_projection(run_name: str) -> tuple[dict, int]:
    """把矩陣攤成畫得出來的形狀：軸、受訪者、每個人在每條軸上的落點。

    受訪者在畫面上一律用說話者描述呈現，不用 `P-1` 這種內部編號（CLAUDE.md 明列
    不得出現在畫面上的東西）。短標籤給圓點用，完整描述與一句原話留給點開時看。
    """
    if not run_name or pathnames.reject(run_name):
        return {"error": "run 不是合法的路徑片段。"}, 400
    run_dir = RUNS_DIR / run_name
    matrix = _load(run_dir / "persona_matrix.yaml") if (run_dir / "persona_matrix.yaml").is_file() else None
    if not isinstance(matrix, dict) or not matrix.get("axes"):
        return {"status": "not_generated"}, 200
    records = evidence_locator.locate_evidence(run_dir)[0] or []
    identity = framework_points._subject_identity(run_dir, records)
    axes = [{
        "id": a.get("id"), "label": a.get("label"),
        "pole_low": a.get("pole_low"), "pole_high": a.get("pole_high"),
        # persona-axis-representation: older axes carry none and read as continuum.
        "representation": a.get("representation") if a.get("representation") in ("continuum", "contrast") else "continuum",
    } for a in matrix.get("axes") or []]
    subjects = []
    for s in matrix.get("subjects") or []:
        who = identity.get(s.get("id")) or {}
        role = who.get("role") or ""
        subjects.append({
            "key": s.get("id"),
            "short": _short_speaker_label(role),
            "role": role,
            "quote": who.get("text", ""),
            "placements": {pl.get("axis_id"): pl.get("position") for pl in s.get("placements") or []},
            # persona-segmentation-reasoning-contract: stored rationales keep
            # their ids; the researcher sees readable text.
            "rationales": {pl.get("axis_id"): readable_without_ids(str(pl.get("rationale") or ""))
                           for pl in s.get("placements") or []},
        })
    # New targets are confirmed from the matrix itself. Older runs can still
    # carry AI-written quadrant portraits, but they are not a prerequisite.
    confirmed = None
    axis_selection = _load(run_dir / "segmentation_axis_selection.yaml") if (run_dir / "segmentation_axis_selection.yaml").is_file() else None
    target_selection = _load(run_dir / "target_segmentation_selection.yaml") if (run_dir / "target_segmentation_selection.yaml").is_file() else None
    persona_content = _load(run_dir / "persona_content.yaml") if (run_dir / "persona_content.yaml").is_file() else None
    axis_ids = (axis_selection or {}).get("axis_ids") if isinstance(axis_selection, dict) else None
    quadrant_id = (target_selection or {}).get("primary_quadrant_id") if isinstance(target_selection, dict) else None
    # A target whose gate went stale (e.g. the axes were re-confirmed after it)
    # is not confirmed, even though its selection file still names a cell --
    # otherwise the station shows it as settled while Persona generation refuses.
    target_current = bool(quadrant_id) and sbp_gates.resolve_gate_state(
        run_dir, "target_segmentation", now=dt.datetime.now().isoformat(timespec="seconds")) == "confirmed"
    if (isinstance(axis_ids, list) and len(axis_ids) == 2
            and all(a in {row["id"] for row in axes} for a in axis_ids)
            and target_current):
        quadrants, _ = sbp_stages.assign_target_segmentation_quadrants(matrix, axis_ids)
        quadrant = next((row for row in quadrants or [] if row.get("id") == quadrant_id), None)
        personas = (persona_content or {}).get("personas") if isinstance(persona_content, dict) else []
        persona = next((row for row in personas or [] if row.get("quadrant_id") == quadrant_id), None)
        if isinstance(quadrant, dict):
            position = {"near_low": -1, "near_high": 1}
            x, y = position.get(quadrant.get("axis_1_pole")), position.get(quadrant.get("axis_2_pole"))
            axes_by_id = {row["id"]: row for row in axes}
            selected_axes = [axes_by_id.get(axis_id) for axis_id in axis_ids]
            poles = [quadrant.get("axis_1_pole"), quadrant.get("axis_2_pole")]
            if x is not None and y is not None and all(selected_axes):
                confirmed = {
                    "axis_ids": axis_ids,
                    "quadrant": [x, y],
                    "target": {
                        "label": (persona or {}).get("label") or "",
                        "rationale": (persona or {}).get("rationale") or target_selection.get("primary_rationale") or "",
                        "member_count": len(next((entry.get("subject_ids") or [] for entry in target_selection.get("quadrant_members") or [] if entry.get("quadrant_id") == quadrant_id), quadrant.get("suggested_subject_ids") or [])),
                        "quadrant_outcome": [
                            {"axis": axis["label"], "pole": axis["pole_high"] if pole == "near_high" else axis["pole_low"]}
                            for axis, pole in zip(selected_axes, poles)
                        ],
                    },
                }
    # Confirmed axes stand on their own once segmentation_axis_selection.yaml
    # names two known axes -- the target step may not have happened yet.
    axis_known = {row["id"] for row in axes}
    confirmed_axis_ids = (list(axis_ids) if isinstance(axis_ids, list) and len(axis_ids) == 2
                          and all(a in axis_known for a in axis_ids) else None)
    # persona-matrix-hmw-scope-default: the assistant's default axis pair (a
    # default analytical projection, not the best segmentation) and whether
    # segmentation was framed by the HMW the researcher has selected now.
    briefs = _load(run_dir / "segmentation_axis_candidates.yaml") if (run_dir / "segmentation_axis_candidates.yaml").is_file() else None
    pair = (briefs or {}).get("default_pair") if isinstance(briefs, dict) else None
    selectable = {row["id"] for row in axes if row.get("pole_low") and row.get("pole_high")}
    default_axis_ids = (list(pair.get("axis_ids")) if isinstance(pair, dict) and isinstance(pair.get("axis_ids"), list)
                        and len(pair["axis_ids"]) == 2 and all(a in selectable for a in pair["axis_ids"]) else None)
    framing = hmw_scope.framing_state(run_dir, matrix)
    if framing != "stale" and isinstance(briefs, dict) and hmw_scope.framing_state(run_dir, briefs) == "stale":
        framing = "stale"
    # resolve-stale-at-researcher-gates: the researcher kept the Persona
    # against the HMW selected now; the segmentation is not re-framed.
    if framing == "stale" and _persona_hmw_retained(run_dir):
        framing = "retained"
    return {"status": "ready", "axes": axes, "subjects": subjects, "confirmed": confirmed,
            "confirmed_axis_ids": confirmed_axis_ids,
            "default_axis_ids": default_axis_ids,
            "default_basis": (pair or {}).get("basis") if default_axis_ids else None,
            "hmw_framing": {"state": framing},
            # speaker-attribution-for-persona: read-only quality summary; never changes the matrix.
            "attribution": sbp_source_attribution.attribution_report(
                matrix, records, sbp_source_attribution.load(run_dir))}, 200


EXECUTOR_LABELS = {"codex": "Codex", "claude": "Claude Code"}


AI_STAGE_LABELS = {
    "align": ("整理原始資料", "selective-coding"),
    "open_coding": ("開放編碼", "selective-coding"),
    "axial_coding": ("主軸編碼", "selective-coding"),
    "narrative_job_candidates": ("顧客任務候選", "selective-coding"),
    "selective_coding_narrative": ("研究敘事", "selective-coding"),
    "core_category_candidates": ("核心類別候選", "selective-coding"),
    "category_properties": ("屬性與面向", "primary-hmw"),
    "jtbd": ("顧客任務", "primary-hmw"),
    "job_map": ("Job Map", "primary-hmw"),
    "how_might_we": ("HMW 張力", "primary-hmw"),
    "primary_hmw_candidates": ("HMW 候選", "primary-hmw"),
    "primary_job_candidates": ("主要顧客任務候選", "primary-hmw"),
    "persona_matrix": ("Persona 分群", "persona-target-selection"),
    "segmentation_axis_candidates": ("分群軸說明", "persona-target-selection"),
    "persona_dimension_candidates": ("Persona 維度候選", "persona-target-selection"),
    "target_segmentation": ("目標族群候選", "persona-target-selection"),
    "persona_content": ("Persona", "persona-target-selection"),
    "journey": ("顧客旅程", "journey-map"),
    "compare": ("比對不同說法", "review-as-is"),
    "structure": ("服務藍圖", "review-as-is"),
    "reference": ("參考資料", "review-as-is"),
    "reconcile": ("整合修正", "review-as-is"),
    "brief": ("研究簡報", "review-as-is"),
}
AI_STAGE_STATE_KEYS = {"how_might_we": "hmw_state"}
AI_USAGE_FIELDS = ("started_at", "finished_at", "wall_seconds", "tokens", "attempts",
                   "model_requested", "effort_requested")


def _retry_count(record: dict) -> int | None:
    """fix-retry-count-display: retries actually made. A chunked or per-source
    stage records attempts per unit (`calls` / `batches`); its stage total
    already includes every unit's first call, so retries are summed per unit."""
    units = [unit.get("attempts") for key in ("calls", "batches") for unit in record.get(key) or []
             if isinstance(unit, dict) and isinstance(unit.get("attempts"), int)]
    if units:
        return sum(max(0, attempts - 1) for attempts in units)
    attempts = record.get("attempts")
    return max(0, attempts - 1) if isinstance(attempts, int) else None


def _ai_usage(run_dir: Path) -> dict:
    """run.json's per-stage execution records, as researcher-readable rows.

    Read-only. Values are copied, never derived: a field the executor did not
    report stays null. `seconds` (the runner's own report) is kept apart from
    `wall_seconds` (measured around the call). `retries` is the one derived
    field (see `_retry_count`)."""
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    rows = []
    for key, record in (state.get("usage") or {}).items():
        if not isinstance(record, dict):
            continue
        label, station = AI_STAGE_LABELS.get(key, ("其他 AI 步驟", None))
        stage_state = state.get(AI_STAGE_STATE_KEYS.get(key, f"{key}_state"))
        row = {"label": label, "station": station,
               "executor": EXECUTOR_LABELS.get(record.get("executor"), record.get("executor")),
               "runner_seconds": record.get("seconds"),
               "result": "failed" if stage_state == "failed" else "complete"}
        row.update({field: record.get(field) for field in AI_USAGE_FIELDS})
        row["retries"] = _retry_count(record)
        detail = record.get("token_detail")
        row["token_detail"] = dict(detail) if isinstance(detail, dict) else None
        calls = []
        for call in record.get("calls") or []:
            if isinstance(call, dict):
                calls.append({"source": Path(str(call.get("source") or "")).stem or None,
                              **{field: call.get(field) for field in AI_USAGE_FIELDS},
                              "runner_seconds": call.get("seconds"),
                              "token_detail": call.get("token_detail") if isinstance(call.get("token_detail"), dict) else None})
        row["calls"] = calls
        rows.append(row)
    rows.sort(key=lambda r: r.get("started_at") or next((c.get("started_at") for c in r["calls"] if c.get("started_at")), "") or "")
    tokens = [r["tokens"] for r in rows if isinstance(r.get("tokens"), int)]
    walls = [r["wall_seconds"] for r in rows if isinstance(r.get("wall_seconds"), (int, float))]
    return {"run": run_dir.name, "stages": rows,
            "totals": {"tokens": sum(tokens) if tokens else None,
                       "wall_seconds": sum(walls) if walls else None}}


AI_TOKEN_KIND_LABELS = (("input_tokens", "輸入"), ("cached_input_tokens", "快取輸入"),
                        ("output_tokens", "輸出"), ("reasoning_output_tokens", "推理"))
AI_COLLABORATION_NOTE = ("這份紀錄是 AI 協作的執行資訊，不是研究發現。模型與 effort 是當次的設定值，"
                         "執行器沒有回報實際使用的模型；時間是實際牆鐘時間；tokens 是執行器回報的數字。"
                         "沒有記錄的欄位寫「未記錄」，不估算。")


def _ai_collaboration_record(run_dir: Path) -> dict:
    """Machine-readable AI collaboration record for one version. Read-only."""
    usage = _ai_usage(run_dir)
    return {"record": "ai-collaboration", "version": 1, "run": usage["run"],
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": AI_COLLABORATION_NOTE, "totals": usage["totals"], "stages": usage["stages"]}


def _ai_collaboration_markdown(record: dict) -> str:
    def value(v):
        return "未記錄" if v is None or v == "" else str(v)

    def duration(seconds):
        if not isinstance(seconds, (int, float)):
            return "未記錄"
        seconds = int(round(seconds))
        return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"

    def tokens(n):
        return f"{n:,}" if isinstance(n, int) else "未記錄"

    lines = [f"# AI 協作紀錄", "", record["note"], "", "| 步驟 | 模型 | Effort | 時間 | Tokens | 結果 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in record["stages"]:
        lines.append(f"| {row['label']} | {value(row.get('model_requested'))} | {value(row.get('effort_requested'))} | "
                     f"{duration(row.get('wall_seconds'))} | {tokens(row.get('tokens'))} | "
                     f"{'未完成' if row.get('result') == 'failed' else '完成'} |")
    totals = record.get("totals") or {}
    lines += [f"| 合計 |  |  | {duration(totals.get('wall_seconds'))} | {tokens(totals.get('tokens'))} |  |", "",
              "## 逐步驟細節", ""]
    for row in record["stages"]:
        detail = row.get("token_detail")
        split = "、".join(f"{label} {tokens(detail.get(key))}" for key, label in AI_TOKEN_KIND_LABELS) if detail else "未分項記錄"
        retries = f"{row['retries']} 次" if isinstance(row.get("retries"), int) else "未記錄"
        lines += [f"### {row['label']}", "",
                  f"- 執行器：{value(row.get('executor'))}",
                  f"- 模型（設定）：{value(row.get('model_requested'))}",
                  f"- Effort（設定）：{value(row.get('effort_requested'))}",
                  f"- 開始／結束：{value(row.get('started_at'))} ／ {value(row.get('finished_at'))}",
                  f"- 牆鐘時間：{duration(row.get('wall_seconds'))}（執行器自報 {duration(row.get('runner_seconds'))}）",
                  f"- Tokens：{tokens(row.get('tokens'))}（{split}）",
                  f"- 重試：{retries}"]
        for call in row.get("calls") or []:
            lines.append(f"  - {value(call.get('source'))}：{duration(call.get('wall_seconds'))}，tokens {tokens(call.get('tokens'))}")
        lines.append("")
    return "\n".join(lines)


def _runner_status(executor: str) -> dict:
    """What can be observed about the named executor's CLI on this machine:
    installed, signed in, and configured to run for this Workbench. Nothing is
    read from the credentials, and there is no combined "ready" answer -- the
    layers are reported as the separate facts they are. The executor is always
    named by the caller; no executor is assumed (provider-setup).

    codex_settings 只回報「使用 Codex 設定」目前會解析到的 model／
    model_reasoning_effort 這兩項（2026-09-29 研究者裁定的選擇性繼承），
    給設定面板顯示用；不是這次工作實際用了什麼，那句話只有 Codex 真的
    在事件流裡回報時才能講。"""
    try:
        _resolve_cli_command(executor)
        configured = True
    except sbp_execution.ExecutionConfigError:
        configured = False
    if executor == "claude":
        from cli_runners import claude as claude_runner
        executable = claude_runner.find_executable()
        installed = bool(executable)
        authenticated = bool(executable) and claude_runner.is_authenticated(executable)
        return {"installed": installed, "authenticated": authenticated, "configured": configured}
    from cli_runners import codex as codex_runner
    executable = shutil.which(os.environ.get("SBP_CODEX_BIN") or "codex")
    installed = bool(executable)
    authenticated = bool(executable) and codex_runner.is_authenticated()
    return {
        "installed": installed, "authenticated": authenticated, "configured": configured,
        "codex_settings": codex_runner.read_selected_settings(),
    }


def _persona_content_response(run_dir: Path) -> dict:
    """persona_content.yaml as stored, plus the verbatim evidence its fields and
    quotation cite, so every statement on the card can open its source text.
    Read-only; the stored document is not altered."""
    path = run_dir / "persona_content.yaml"
    doc = _load(path) if path.is_file() else None
    if not isinstance(doc, dict):
        return {"personas": []}
    refs: set[str] = set()
    basis_refs: set[str] = set()
    for persona in doc.get("personas") or []:
        if not isinstance(persona, dict):
            continue
        quote = persona.get("representative_quote")
        if isinstance(quote, dict) and quote.get("evidence_ref"):
            refs.add(str(quote["evidence_ref"]))
        for row in persona.get("field_provenance") or []:
            if isinstance(row, dict):
                refs.update(str(ref) for ref in row.get("evidence_refs") or [])
        # evidence-basis-inspection: a rewritten description's AI-draft
        # evidence still feeds the card-level basis, not the excerpts.
        for edit in (persona.get("researcher_edits") or {}).values():
            if isinstance(edit, dict) and isinstance(edit.get("ai_provenance"), dict):
                basis_refs.update(str(ref) for ref in edit["ai_provenance"].get("evidence_refs") or [])
    records = (evidence_locator.locate_evidence(run_dir)[0] or []) if refs or basis_refs else []
    excerpts = {}
    for record in records:
        if isinstance(record, dict) and record.get("id") in refs:
            excerpts[record["id"]] = {
                "speaker": record.get("role") or "",
                "source": Path(str(record.get("source") or "")).stem,
                "locator": record.get("locator") or "",
                "text": record.get("text") or "",
            }
    basis_by_persona = {}
    for persona in doc.get("personas") or []:
        if not isinstance(persona, dict) or not persona.get("id"):
            continue
        try:
            basis = _persona_basis(persona, records)
        except Exception:  # a broken basis never hides the Persona itself
            basis = None
        if basis:
            basis_by_persona[str(persona["id"])] = basis
    return {**doc, "evidence_excerpts": excerpts, "basis_by_persona": basis_by_persona,
            "confirmation": _persona_confirmation(run_dir), "freshness": _persona_freshness(run_dir, doc)}


def _persona_basis(persona: dict, records: list) -> dict | None:
    """evidence-basis-inspection: the card-level 依據 for one Persona.

    Related records are card-level only: the representative quotation's
    record, then the description's evidence (the AI draft's, when the
    researcher rewrote the description) -- never the other fields', which
    would be field-level provenance. 明確推論 lists only fields still stored
    as `evidence_inference`; a field the researcher rewrote is typed
    `researcher` and is also checked against `researcher_edits`, so its old
    reasoning is never offered as the explanation of the current text."""
    provenance = {r.get("field"): r for r in persona.get("field_provenance") or [] if isinstance(r, dict)}
    edits = persona.get("researcher_edits") or {}
    refs = []
    quote = persona.get("representative_quote")
    if isinstance(quote, dict) and quote.get("evidence_ref"):
        refs.append(str(quote["evidence_ref"]))
    # A rewrite drops (or retypes) the description's provenance row; the AI
    # draft's row, kept in researcher_edits, still names what it read.
    description = (((edits.get("description") or {}).get("ai_provenance")) or {}) if "description" in edits \
        else (provenance.get("description") or {})
    refs.extend(str(ref) for ref in description.get("evidence_refs") or [])
    inferences = [{"field": field, "summary": str(row.get("reasoning_summary") or "").strip()}
                  for field, row in provenance.items()
                  if row.get("type") == "evidence_inference" and field not in edits
                  and str(row.get("reasoning_summary") or "").strip()]
    basis = {"rationale": str(persona.get("rationale") or "").strip(),
             "evidence": framework_points.basis_evidence(refs, records),
             "inferences": inferences}
    return basis if any(basis.values()) else None


def _persona_freshness(run_dir: Path, doc: dict) -> dict:
    """Whether the Persona still describes the currently confirmed target.

    Reuses the target gate's own resolver and the target selection file; no
    new dependency tracking. Stale when the target gate is no longer current
    or when the Persona was written for another cell or other members."""
    personas = [p for p in doc.get("personas") or [] if isinstance(p, dict)]
    if not personas:
        return {"state": "absent"}
    now = dt.datetime.now().isoformat(timespec="seconds")
    if sbp_gates.resolve_gate_state(run_dir, "target_segmentation", now=now) != "confirmed":
        return {"state": "stale", "reason": "target_unconfirmed"}
    selection_path = run_dir / "target_segmentation_selection.yaml"
    selection = _load(selection_path) if selection_path.is_file() else None
    if not isinstance(selection, dict):
        return {"state": "current"}
    target = selection.get("primary_quadrant_id")
    members = next((set(entry.get("subject_ids") or []) for entry in selection.get("quadrant_members") or []
                    if isinstance(entry, dict) and entry.get("quadrant_id") == target), None)
    persona = personas[0]
    if (persona.get("quadrant_id") and persona.get("quadrant_id") != target) or (
            members is not None and set(persona.get("member_subject_ids") or []) != members):
        return {"state": "stale", "reason": "target_changed"}
    return {"state": "current"}


# persona-generation-confirmation-gate: the Persona Human Gate. Confirming
# copies the reviewed candidate into this snapshot with its canonical digest;
# the candidate is current-confirmed only while its digest still matches, so an
# edit after confirmation asks for reconfirmation without deleting the old
# snapshot. Confirmation writes this one file and starts nothing.
PERSONA_CONFIRMED_FILE = "persona_confirmed.yaml"


def _persona_candidate_digest(run_dir: Path) -> str | None:
    path = run_dir / "persona_content.yaml"
    doc = _load(path) if path.is_file() else None
    if not isinstance(doc, dict) or not doc.get("personas"):
        return None
    return sbp_lifecycle.canonical_content_digest(doc)


def _persona_confirmation(run_dir: Path) -> dict:
    """Read-only: whether the current Persona candidate is the confirmed one."""
    digest = _persona_candidate_digest(run_dir)
    if digest is None:
        return {"state": "absent", "persona_digest": None, "previously_confirmed": False}
    path = run_dir / PERSONA_CONFIRMED_FILE
    snapshot = _load(path) if path.is_file() else None
    confirmed = isinstance(snapshot, dict) and snapshot.get("persona_digest") == digest
    return {
        "state": "confirmed" if confirmed else "needs_confirmation",
        "persona_digest": digest,
        "previously_confirmed": isinstance(snapshot, dict),
        "confirmed_at": snapshot.get("confirmed_at") if confirmed else None,
    }


class PersonaConfirmationRejected(Exception):
    pass


def _confirm_persona(run_dir: Path, persona_digest: object) -> dict:
    """Save the reviewed Persona candidate as confirmed. No job, no model."""
    path = run_dir / "persona_content.yaml"
    doc = _load(path) if path.is_file() else None
    if not isinstance(doc, dict) or not doc.get("personas"):
        raise PersonaConfirmationRejected("這份產出還沒有 Persona，無法確認。")
    if len(doc["personas"]) != 1:
        raise PersonaConfirmationRejected("Persona 應該只有一份，請重新產出。")
    digest = sbp_lifecycle.canonical_content_digest(doc)
    if persona_digest != digest:
        raise PersonaConfirmationRejected("畫面上的 Persona 不是最新版本，請重新載入後再確認。")
    if not sbp_gates.is_derivation_runnable(
            run_dir, "persona_content", now=dt.datetime.now().isoformat(timespec="seconds")):
        raise PersonaConfirmationRejected("目標族群還沒確認目前這一版，請先重新確認目標族群。")
    errors = V.structural_errors(doc, V.load_schema("persona-content"))
    errors += V.persona_field_provenance_errors(doc, allow_researcher=True)
    if errors:
        raise PersonaConfirmationRejected("沒有確認：" + "；".join(errors[:3]))
    _write_yaml_atomic(run_dir / PERSONA_CONFIRMED_FILE, {
        "schema_version": 1,
        "confirmed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "persona_digest": digest,
        "persona_content": doc,
    })
    return _persona_content_response(run_dir)


# persona-nine-canonical-fields (2026-10-01): the canonical units first; the
# older keys stay editable so a Persona written before then can still be
# rewritten in place without moving its data.
PERSONA_EDITABLE_FIELDS = ("person_name", "age", "profession", "description",
                           "context", "motivation_goal", "decision_pattern", "behavior_strategy",
                           "challenge", "need", "rationale", "user_story",
                           "motivation", "behavior",
                           "worldview", "emotional_tendencies", "behavioral_patterns", "linguistic_style")


class PersonaEditRejected(ValueError):
    """A persona card edit that would write an invalid or unowned value."""


def _write_persona_field(run_dir: Path, persona_id: object, field: object, value: object) -> dict:
    """The researcher rewrites one field on a persona card (2026-09-27 ruling).

    The first rewrite of a field keeps the AI draft's text and provenance in
    `researcher_edits`, so downstream citations still reach the evidence the
    draft read. The field is then typed `researcher`, except a name, which
    stays `illustrative_identity` because a rewritten name is still not a
    research claim. The representative quotation is verbatim and never edited.
    """
    if field not in PERSONA_EDITABLE_FIELDS:
        raise PersonaEditRejected("這一欄不能在卡片上改寫。")
    if not isinstance(value, str):
        raise PersonaEditRejected("內容必須是文字。")
    text = value.strip()
    if field in ("description", "rationale") and not text:
        raise PersonaEditRejected("這一欄不能留白。")
    path = run_dir / "persona_content.yaml"
    document = _load(path) if path.is_file() else None
    if not isinstance(document, dict):
        raise PersonaEditRejected("這份產出還沒有 persona。")
    persona = next((p for p in document.get("personas") or []
                    if isinstance(p, dict) and p.get("id") == persona_id), None)
    if persona is None:
        raise PersonaEditRejected("找不到這個 persona。")
    if str(persona.get(field) or "").strip() == text:
        return document
    now = dt.datetime.now().isoformat(timespec="seconds")
    records = persona.get("field_provenance")
    if records is None and persona.get("inferred_fields"):
        raise PersonaEditRejected("這份 persona 用的是舊版來源格式，不能在卡片上改寫。")
    records = [dict(r) for r in records or [] if isinstance(r, dict)]
    prior = next((r for r in records if r.get("field") == field), None)
    edits = dict(persona.get("researcher_edits") or {})
    if field not in edits:
        entry = {"edited_at": now}
        if str(persona.get(field) or "").strip():
            entry["ai_value"] = str(persona.get(field))
        if prior is not None:
            entry["ai_provenance"] = dict(prior)
        edits[field] = entry
    else:
        edits[field] = {**edits[field], "edited_at": now}
    records = [r for r in records if r.get("field") != field]
    if text and field in V.PROVENANCE_FIELDS:
        if "illustrative_identity" in (V.persona_field_provenance_policy_table().get(field) or []):
            records.append({"field": field, "type": "illustrative_identity", "not_a_research_claim": True})
        else:
            records.append({"field": field, "type": "researcher", "edited_at": now})
    if text:
        persona[field] = text
    else:
        persona.pop(field, None)
    persona["field_provenance"] = records
    persona["researcher_edits"] = edits
    errors = V.structural_errors(document, V.load_schema("persona-content"))
    errors += V.persona_field_provenance_errors(document, allow_researcher=True)
    if errors:
        raise PersonaEditRejected("沒有寫入：" + "；".join(errors[:3]))
    _write_yaml_atomic(path, document)
    return document


def _short_speaker_label(role: str) -> str:
    """圓點旁邊那一小段字：從說話者描述裡取店名與最能辨識的一段，不超過十字。"""
    if not role:
        return "受訪者"
    inner = re.search(r"（(.*)）", role)
    parts = [p.strip() for p in re.split(r"[，,]", inner.group(1) if inner else role) if p.strip()]
    if not parts:
        return role[:10]
    label = parts[0]
    if len(parts) > 1 and len(label) + len(parts[1]) <= 10:
        label = f"{label}·{parts[1]}"
    return label[:10]


def _projects(user: sbp_auth.User | None = None) -> list[dict]:
    """專案清單：一個專案一筆，底下掛它自己跑過的版本。

    取代前端原本自己把跨案例的 run 清單分組的做法——研究者的模型是先選專案、
    再選版本兩步，不是在九筆混在一起的清單裡靠日期分辨（決定十三）。
    """
    if user is not None:
        _ensure_legacy_projects(user)
        owned_case_ids = _auth_service().owned_case_ids(user.user_id)
    else:
        owned_case_ids = None
    runs = _discover_runs()
    if owned_case_ids is not None:
        runs = [
            run for run in runs
            if (run.get("meta") or {}).get("case_id") in owned_case_ids
        ]
    by_case: dict[str, list[dict]] = {}
    for run in runs:
        cid = (run.get("meta") or {}).get("case_id") or ""
        by_case.setdefault(cid, []).append(run)
    out = []
    archived_names = set(_project_prefs()["archived_versions"])
    for case in _discover_cases():
        cid = case.get("case_id")
        if owned_case_ids is not None and cid not in owned_case_ids:
            continue
        items = by_case.get(cid, [])
        versions = sorted(
            ({
                "name": r["name"],
                "label": _run_version_label(r),
                "provenance_note": r.get("provenance_note"),
            } for r in items),
            key=lambda v: v["label"], reverse=True,
        )
        # 版本列只寫日期。同一天有兩份時才補上時間，否則那兩列會變成一模一樣
        # 的字，研究者分不出來——只有在真的需要分辨時才多給那四個字。
        seen_dates: dict[str, int] = {}
        for v in versions:
            seen_dates[v["label"][:10]] = seen_dates.get(v["label"][:10], 0) + 1
        for v in versions:
            date = v["label"][:10]
            v["label"] = v["label"] if seen_dates[date] > 1 else date
        out.append({
            "case_id": cid,
            "name": case.get("name"),
            "path": case.get("path"),
            "source_count": len(list(((_active_data_root() / case["path"]).parent / "raw").iterdir()))
                if str(case.get("path") or "").startswith("cases/_uploads/")
                and ((_active_data_root() / case["path"]).parent / "raw").is_dir() else None,
            "version_count": len(versions),
            "last_updated": versions[0]["label"].split(" ")[0] if versions else None,
            "versions": [v for v in versions if v["name"] not in archived_names],
            "archived_versions": [v for v in versions if v["name"] in archived_names],
        })
    pinned = set(_project_prefs()["pinned"])
    for item in out:
        item["pinned"] = item["case_id"] in pinned
    out.sort(key=lambda c: (c["last_updated"] or ""), reverse=True)
    out.sort(key=lambda c: not c["pinned"])
    return out


# 專案首頁的釘選與改名只是研究者看到的呈現偏好：不寫進 case.yaml，也不改
# 任何 run／案例資料夾名稱。存在 runs/_workbench/（底線開頭，不會被當成 run）。
_project_prefs_lock = threading.RLock()


def _project_prefs_path() -> Path:
    return RUNS_DIR / "_workbench" / "project_prefs.yaml"


def _project_prefs() -> dict:
    path = _project_prefs_path()
    try:
        data = _load(path) if path.is_file() else {}
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    pinned = [x for x in data.get("pinned") or [] if isinstance(x, str)]
    names = {k: v for k, v in (data.get("names") or {}).items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
    archived = [x for x in data.get("archived_versions") or [] if isinstance(x, str)]
    return {"pinned": pinned, "names": names, "archived_versions": archived}


def _write_project_prefs(prefs: dict) -> None:
    path = _project_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml_atomic(path, prefs)


class ProjectActionRejected(ValueError):
    pass


def _set_version_archived(run: object, archived: object, active_run: object = None) -> dict:
    if not isinstance(archived, bool):
        raise ProjectActionRejected("請指定封存或取消封存。")
    if not isinstance(run, str) or not any(r["name"] == run for r in _discover_runs()):
        raise ProjectActionRejected("找不到這個版本。")
    if archived and run == active_run:
        raise ProjectActionRejected("目前開啟的版本不能封存；請先開啟其他版本。")
    with _job_lock:
        if archived and _job.get("out") == run and _job.get("state") in {"running", "waiting_for_model"}:
            raise ProjectActionRejected("這個版本正在處理中，請完成或取消後再封存。")
    with _project_prefs_lock:
        prefs = _project_prefs()
        prefs["archived_versions"] = [name for name in prefs["archived_versions"] if name != run]
        if archived:
            prefs["archived_versions"].append(run)
        _write_project_prefs(prefs)
    return {"ok": True, "run": run, "archived": archived}


def _project_case(case: object) -> tuple[Path, str]:
    """一個專案動作指向的 case.yaml 與它宣告的 case_id。"""
    case_path = _resolve_case_file(case) if isinstance(case, str) and case.strip() else None
    if case_path is None:
        raise ProjectActionRejected("沒有指定專案。")
    case_id = _case_id_for(str(case_path.relative_to(_active_data_root().resolve())))
    if not case_id:
        raise ProjectActionRejected("讀不到這個專案的設定。")
    return case_path, case_id


def _project_runs(case_id: str) -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    return [
        d for d in sorted(RUNS_DIR.iterdir())
        if d.is_dir() and not d.name.startswith("_") and _run_case_id_for_access(d.name) == case_id
    ]


def _set_project_pin(case: object, pinned: bool) -> dict:
    _case_path, case_id = _project_case(case)
    with _project_prefs_lock:
        prefs = _project_prefs()
        prefs["pinned"] = [x for x in prefs["pinned"] if x != case_id] + ([case_id] if pinned else [])
        _write_project_prefs(prefs)
    return {"ok": True, "pinned": pinned}


def _rename_project(case: object, name: object) -> dict:
    _case_path, case_id = _project_case(case)
    clean = " ".join(str(name or "").split())
    if not clean:
        raise ProjectActionRejected("專案名稱不能空白。")
    if len(clean) > 80:
        raise ProjectActionRejected("專案名稱太長了，請縮短到 80 字以內。")
    with _project_prefs_lock:
        prefs = _project_prefs()
        prefs["names"][case_id] = clean
        _write_project_prefs(prefs)
    return {"ok": True, "name": clean}


def _delete_project(case: object) -> dict:
    """刪除＝移到 runs/_trash/，不是抹掉：誤刪時資料還在。"""
    case_path, case_id = _project_case(case)
    case_dir = case_path.parent
    cases_root = _active_cases_dir().resolve()
    if case_dir == cases_root or case_dir == UPLOADS_DIR.resolve():
        raise ProjectActionRejected("這個專案的資料夾無法單獨移除。")
    if any(p != case_path for p in case_dir.rglob("case.yaml")):
        raise ProjectActionRejected("這個資料夾裡還有其他專案，沒有移除。")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    trash = RUNS_DIR / "_trash" / f"{stamp}-{_safe_component(case_id, 'project')}"
    (trash / "runs").mkdir(parents=True, exist_ok=True)
    for run_dir in _project_runs(case_id):
        shutil.move(str(run_dir), str(trash / "runs" / run_dir.name))
    shutil.move(str(case_dir), str(trash / "case"))
    prefs = _project_prefs()
    prefs["pinned"] = [x for x in prefs["pinned"] if x != case_id]
    prefs["names"].pop(case_id, None)
    _write_project_prefs(prefs)
    return {"ok": True}


# 匯出只收研究本身：原始資料、研究產物、研究者的選擇與確認。模型傳輸的
# 提示詞往返（_transport）、舊版備份、鎖檔、交接中的暫存都不收。BYOK 金鑰
# 從不落地，所以本來就不在這些資料夾裡。
_EXPORT_SKIP_SUFFIXES = (".lock", ".tmp", ".log", ".pyc")


def _exportable(rel: Path) -> bool:
    parts = rel.parts
    if any(part.startswith("_") or part == "__pycache__" or part.startswith(".env") for part in parts):
        return False
    if ".bak" in rel.name or rel.name.endswith(_EXPORT_SKIP_SUFFIXES) or rel.name == ".DS_Store":
        return False
    if len(parts) >= 2 and parts[0] == ".gates" and parts[1] == "transport":
        return False
    return True


def _export_project_zip(case: object) -> tuple[bytes, str]:
    import zipfile
    case_path, case_id = _project_case(case)
    name = next((c["name"] for c in _discover_cases() if c.get("case_id") == case_id), case_id)
    buffer = io.BytesIO()
    included: dict[str, list[str]] = {"source": [], "runs": []}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(case_path.parent.rglob("*")):
            rel = path.relative_to(case_path.parent)
            if path.is_file() and _exportable(rel):
                archive.write(path, f"source/{rel.as_posix()}")
                included["source"].append(rel.as_posix())
        for run_dir in _project_runs(case_id):
            files = []
            for path in sorted(run_dir.rglob("*")):
                rel = path.relative_to(run_dir)
                if path.is_file() and _exportable(rel):
                    archive.write(path, f"runs/{run_dir.name}/{rel.as_posix()}")
                    files.append(rel.as_posix())
            included["runs"].append(run_dir.name)
            record = _ai_collaboration_record(run_dir)
            if record["stages"]:
                archive.writestr(f"ai-collaboration/{run_dir.name}.json",
                                 json.dumps(record, ensure_ascii=False, indent=2))
                archive.writestr(f"ai-collaboration/{run_dir.name}.md", _ai_collaboration_markdown(record))
        manifest = {
            "project_name": name,
            "case_id": case_id,
            "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "contents": {
                "source/": "原始上傳資料與案例設定",
                "runs/<版本>/": "研究產物，以及研究者的選擇、確認與審閱紀錄",
                "ai-collaboration/<版本>.md|.json": "AI 協作紀錄（執行器、設定的模型與 effort、時間、tokens）；不是研究發現",
            },
            "runs": included["runs"],
            "excluded": "模型傳輸暫存、舊版備份、鎖檔與執行狀態；API 金鑰不會落地，因此不在匯出內",
        }
        archive.writestr("manifest.yaml", yaml.dump(manifest, allow_unicode=True, sort_keys=False))
    return buffer.getvalue(), name


def _run_version_label(run: dict) -> str:
    """版本的人話說法：日期（有記時間就帶時間）。產出方式另外一欄，不串在一起。"""
    meta = run.get("meta") or {}
    started = str(meta.get("started_at") or "")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", started)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", run.get("name") or "")
    return m.group(1) if m else "日期不明"


def _preview_run() -> dict | None:
    """Return one curated, complete run for the frontend walkthrough.

    Preview is deliberately a presentation choice: it never deletes or
    rewrites the other researcher runs discovered above.
    """
    runs = _discover_runs()
    named = next((run for run in runs if run["name"] == PREVIEW_RUN_NAME), None)
    if named:
        return named
    return next((run for run in runs if run.get("format") == "action-graph"), None)


def _preview_case(preview_run: dict | None) -> dict | None:
    """Find the case that belongs to the curated preview run, never a list default."""
    case_id = ((preview_run or {}).get("meta") or {}).get("case_id")
    if not case_id:
        return None
    return next((case for case in _discover_cases() if case.get("case_id") == case_id), None)


def _preview_journey(preview_run: dict | None, preview_case: dict | None) -> list[str]:
    """Use the curated run's confirmed primary journey for the preview axis.

    The preview used to carry a hard-coded journey for the development case.
    That made switching the curated run silently render the wrong column labels.
    Keep the fallback for older or incomplete runs, but prefer the run's own
    confirmed journey and then the case boundary.
    """
    if preview_run:
        run_name = _safe_component(preview_run.get("name", ""), "")
        draft_path = RUNS_DIR / run_name / "sbp_draft.yaml" if run_name else None
        if draft_path and draft_path.is_file():
            try:
                draft = _load(draft_path) or {}
                journey = draft.get("journey") or {}
                primary_id = journey.get("primary_scenario")
                primary = next(
                    (scenario for scenario in journey.get("scenarios") or []
                     if scenario.get("id") == primary_id),
                    None,
                )
                labels = [
                    step.get("label") for step in (primary or {}).get("steps") or []
                    if isinstance(step, dict) and isinstance(step.get("label"), str) and step.get("label").strip()
                ]
                if labels:
                    return labels
            except Exception:
                pass
    case_steps = (preview_case or {}).get("steps") or []
    return case_steps or PREVIEW_JOURNEY


def _preview_label(preview_case: dict | None) -> str:
    """Return a human-facing case label without exposing an internal run name.

    以前會在名稱前面加一個寫死的序號（「案例 1」「案例 2」只對應兩個 case_id，
    其餘退化成「完整範例」）。研究者從沒替案例編過號，而畫面上這個值的欄位標籤
    本來就寫著「案例」，序號等於同一個詞連講兩次；瀏覽器端的 runCaseLabel() 也
    同步拿掉，兩邊必須叫同一個名字。
    """
    name = unicodedata.normalize("NFC", (preview_case or {}).get("name") or "目前產出")
    return name


def _run_fingerprint(run_dir: Path) -> str | None:
    """mtime/size 指紋（沿用 `_ui_status()` 的字串形狀），偵測 run 產出是否變動。

    只看 `sbp_draft.yaml`（唯一內容檔）與 `briefing.yaml`（若存在）——這兩個是
    add-run-watch-mode 這個 change 關心的「一輪產出」範圍，不是任意檔案雜湊。
    `sbp_draft.yaml` 不存在時回傳 `None`：這個 run 還沒完成過一次投影，沒有指紋
    可言，呼叫端一律視為「沒有東西可比對」。
    """
    draft_path = run_dir / "sbp_draft.yaml"
    if not draft_path.exists():
        return None
    stat = draft_path.stat()
    parts = [f"{stat.st_mtime_ns:x}-{stat.st_size:x}"]
    briefing_path = run_dir / "briefing.yaml"
    if briefing_path.exists():
        b_stat = briefing_path.stat()
        parts.append(f"{b_stat.st_mtime_ns:x}-{b_stat.st_size:x}")
    return "-".join(parts)


class RunAdoptionRejected(ValueError):
    """The content on disk can't be registered as a baseline, and why."""


# 研究者的判斷先累積在這裡，不碰 `service_graph.yaml`。每一次拖曳都寫進正式檔案
# 的話，一個手勢就鑄出一個版本、而且每次都逼他重新確認一遍——按下「幫我檢查一遍」
# 才一起寫進去。檔名帶點是因為它是工作中的暫存，不是這個 run 的產出。
STAGED_JUDGEMENTS_FILE = ".pending_adjustments.yaml"

# 每一種判斷需要哪些欄位。未知的種類在存進去之前就擋掉：暫存區裡放一筆套用時
# 才會炸開的東西，等於把錯誤延後到最不好處理的時候。
JUDGEMENT_SHAPES = {
    "move_node": ("node_id",),
    "add_relation": ("from_id", "to_id"),
    "remove_relation": ("relation_id",),
    "redirect_relation": ("relation_id",),
    "rule_contradiction": ("conflict_id", "ruling"),
}

JudgementRejected = sbp_researcher_edits.JudgementRejected


def _staged_path(run_dir: Path) -> Path:
    return run_dir / STAGED_JUDGEMENTS_FILE


def _staged_judgements(run_dir: Path) -> list[dict]:
    """Judgements made but not yet written into the graph, oldest first."""
    path = _staged_path(run_dir)
    if not path.is_file():
        return []
    try:
        staged = load_safe_yaml(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 讀不起來當作沒有，不要讓暫存變成失敗原因
        return []
    return [item for item in (staged or []) if isinstance(item, dict)]


def _stage_judgement(run_dir: Path, judgement: dict) -> list[dict]:
    """Record one judgement without touching the confirmed content."""
    if not isinstance(judgement, dict):
        raise JudgementRejected("這一筆判斷的格式不正確")
    kind = judgement.get("kind")
    required = JUDGEMENT_SHAPES.get(kind)
    if required is None:
        raise JudgementRejected(f"不認得這種判斷（{kind}）")
    missing = [field for field in required if judgement.get(field) in (None, "")]
    if missing:
        raise JudgementRejected(f"這一筆判斷少了必要的內容（{'、'.join(missing)}）")
    if kind == "move_node" and judgement.get("step") is None and judgement.get("layer") is None:
        raise JudgementRejected("沒有指定要搬到哪裡")
    staged = _staged_judgements(run_dir)
    staged.append(dict(judgement))
    _write_yaml_atomic(_staged_path(run_dir), staged)
    return staged


def _clear_staged_judgements(run_dir: Path) -> None:
    _staged_path(run_dir).unlink(missing_ok=True)


def _refusal(reason: str) -> str:
    """Why the adjustment can't be applied — reassurance first, detail behind it.

    A researcher who presses apply and gets an error asks one question before
    any other: is what I just did gone? Answer that first. The validator's own
    strings carry internal field names, so they are the locator for whoever
    debugs this, not the message.
    """
    return f"這樣改之後這張圖就說不通了，所以沒有套用——你改的東西都還在。（{reason}）"


def _semantic_findings(graph: dict, evidence: list, journey: dict) -> list[str]:
    """What the graph says that no longer holds, without blocking anything.

    Deliberately separate from the structural check: structure decides whether
    the adjustment can be written at all, this decides what the researcher is
    told afterwards. The established rule is that findings are information,
    not a verdict — so nothing here refuses an adjustment.
    """
    evidence_ids = {item["id"] for item in evidence if isinstance(item, dict) and "id" in item}
    evidence_texts = {item["id"]: item.get("text", "") for item in evidence
                      if isinstance(item, dict) and "id" in item}
    try:
        return [str(error) for error in
                V.graph_errors(graph, evidence_ids, evidence_texts, journey=journey)]
    except Exception:  # noqa: BLE001 - 檢查本身壞掉不該讓套用失敗
        return []


def _pending_decisions(run_dir: Path) -> list[dict]:
    """Everything waiting on the researcher, from both sources, in one list.

    Pipeline-raised contradictions and post-adjustment findings share this
    component by design: they have the same shape — the assistant raises it,
    the researcher rules on it — so they should not become two things to learn.
    """
    state = _load(run_dir / "run.json") or {}
    ruled = set(state.get("contradiction_rulings") or {})
    decisions: list[dict] = []
    for conflict in _load(run_dir / "conflicts.yaml") or []:
        if isinstance(conflict, dict) and conflict.get("id") not in ruled:
            decisions.append({
                "id": str(conflict.get("id")),
                # 刻意不叫 `origin`：那個名字已經是「這份 run 怎麼產出的」
                # （api_run／manual_transport⋯）。範圍不同的兩件事共用一個詞，
                # 正是這個專案記錄過的可用性失敗。
                "raised_by": "contradiction",
                "text": str(conflict.get("description") or ""),
                "evidence_refs": list(conflict.get("evidence_refs") or []),
            })
    for index, finding in enumerate(state.get("check_findings") or []):
        decisions.append({
            "id": f"F-{index + 1}",
            "raised_by": "check",
            "text": str(finding),
            "evidence_refs": [],
        })
    return decisions


# 每一輪的圖與投影都留一份，才可能「看每一版」。
#
# 不叫 `versions/`：那個名字在這個 repo 已經是「研究者存下來要交出去的不可變快照」
# ——完全不同的東西。兩個不同範圍共用一個詞，正是 LEARNINGS #7 記錄過的可用性
# 失敗（研究者要問人才知道哪個「版本」是哪個）。
BLUEPRINT_ROUNDS_DIR = ".blueprint-rounds"


def _round_snapshot_dir(run_dir: Path, version_id: str) -> Path:
    return run_dir / BLUEPRINT_ROUNDS_DIR / _safe_component(version_id, "round")


def _blueprint_rounds(run_dir: Path) -> list[dict]:
    """What the navigator shows: the generated blueprint, then each round.

    Oldest first, because that is the order they happened in and the order a
    researcher reads a history.
    """
    state = _load(run_dir / "run.json") or {}
    current_id = (state.get("artifact_versions") or {}).get("as_is_graph")
    rounds = [{
        "version_id": None,
        "made_by": "generated",
        "summary": "AI 一開始畫的",
        "at": state.get("completed_at") or state.get("started_at"),
        "current": False,
    }]
    for index, record in enumerate(_adjustment_rounds(run_dir), start=1):
        rounds.append({
            "version_id": record.get("version_id"),
            "made_by": record.get("made_by") or "researcher",
            "summary": f"你第 {index} 次改 · {record.get('summary') or '沒有改動'}",
            "at": record.get("at"),
            "current": False,
        })
    # 生成版沒有自己的 version_id（它是被第一輪取代的那份快照），所以「現在看的
    # 是哪一版」只在有調整輪次時才標得出來。
    for entry in rounds:
        entry["current"] = bool(entry["version_id"]) and entry["version_id"] == current_id
    if current_id is None and len(rounds) == 1:
        rounds[0]["current"] = True
    return rounds


def _blueprint_round_content(run_dir: Path, version_id: str) -> dict:
    """The graph exactly as it stood at that round. Read-only; nothing moves."""
    snapshot = _round_snapshot_dir(run_dir, version_id) / "service_graph.yaml"
    if not snapshot.is_file():
        raise JudgementRejected("找不到那一次修改的紀錄")
    return _load(snapshot) or {}


@_owns_project_lock(_run_dir_argument)
def _select_blueprint_round(run_dir: Path, version_id: str) -> dict:
    """Make the round the researcher is looking at the one that gets frozen.

    History stays linear: this does not delete the later rounds, it continues
    from an earlier one. A version tree is unreadable, and the researcher asked
    for navigation and selection, not branching.
    """
    snapshot = _round_snapshot_dir(run_dir, version_id)
    graph_path = snapshot / "service_graph.yaml"
    if not graph_path.is_file():
        raise JudgementRejected("找不到那一次修改的紀錄")
    _write_yaml_atomic(run_dir / "service_graph.yaml", _load(graph_path) or {})
    draft_path = snapshot / "sbp_draft.yaml"
    if draft_path.is_file():
        _write_yaml_atomic(run_dir / "sbp_draft.yaml", _load(draft_path) or {})
    state = _load(run_dir / "run.json") or {}
    versions = dict(state.get("artifact_versions") or {})
    versions["as_is_graph"] = version_id
    state["artifact_versions"] = versions
    # 換了一版就是換了一份內容，先前的確認不再適用於現在畫面上的東西。
    for field in ("review_state", "reviewed_at", "reviewed_artifact_fingerprint"):
        state.pop(field, None)
    _write_json_atomic(run_dir / "run.json", state)
    return {"version_id": version_id}


def _adjustment_rounds(run_dir: Path) -> list[dict]:
    """Each round of adjustment this run has published, oldest first.

    Deliberately kept in the run's own state rather than on the lifecycle
    version record: the registry is one file per *case*, shared by every run
    of that case, its version records set `additionalProperties: false`, and
    a `version_id` is a hash of the record's identity fields. Per-run prose
    written there would need a schema change and would be read by a sibling
    run as its own history.
    """
    state = _load(run_dir / "run.json") or {}
    rounds = state.get("adjustment_rounds")
    return [item for item in (rounds or []) if isinstance(item, dict)]


def _label_of(graph: dict, node_id: str) -> str:
    """The card's own title — never its internal id, which must not reach a screen."""
    for node in graph.get("nodes") or []:
        if node.get("id") == node_id:
            return str(node.get("label") or "").strip() or "沒有標題的格子"
    return "已經不在圖上的格子"


def _round_summary(before: dict, judgements: list[dict]) -> str:
    """What this round changed, in the words the researcher reads on the canvas."""
    moved, drawn, removed, redirected, ruled = [], 0, 0, 0, 0
    for judgement in judgements:
        kind = judgement.get("kind")
        if kind == "move_node":
            moved.append(_label_of(before, str(judgement.get("node_id"))))
        elif kind == "add_relation":
            drawn += 1
        elif kind == "remove_relation":
            removed += 1
        elif kind == "redirect_relation":
            redirected += 1
        elif kind == "rule_contradiction":
            ruled += 1
    parts = []
    if moved:
        unique = list(dict.fromkeys(moved))
        parts.append("搬了「" + "」「".join(unique[:3]) + "」"
                     + (f"等 {len(unique)} 格" if len(unique) > 3 else ""))
    for count, phrase in ((drawn, "畫了 {} 條線"), (removed, "刪了 {} 條線"),
                          (redirected, "改了 {} 條線連到哪"), (ruled, "處理了 {} 組講法不一樣")):
        if count:
            parts.append(phrase.format(count))
    return "、".join(parts) or "沒有改動"


_JUDGEMENT_APPLIERS = {
    "move_node": lambda g, j: sbp_researcher_edits.move_node(
        g, str(j["node_id"]), step=j.get("step"), layer=j.get("layer")),
    "add_relation": lambda g, j: sbp_researcher_edits.add_relation(
        g, str(j["from_id"]), str(j["to_id"]),
        relation_type=str(j.get("relation_type") or "sequence")),
    "remove_relation": lambda g, j: sbp_researcher_edits.remove_relation(g, str(j["relation_id"])),
    "redirect_relation": lambda g, j: sbp_researcher_edits.redirect_relation(
        g, str(j["relation_id"]), from_id=j.get("from_id"), to_id=j.get("to_id")),
    # 「這組講法不一樣是不是真的有問題」不改圖，它是研究者對管線提出的東西下的
    # 判斷，記在 run 的狀態裡（見 _record_contradiction_ruling）。
    "rule_contradiction": lambda g, j: g,
}


@_owns_project_lock(_run_dir_argument)
def _apply_staged_judgements(run_dir: Path, *, recheck=None) -> dict | None:
    """Write the staged judgements into the graph, re-project, publish one round.

    `recheck` is the model-backed pass that reads the adjusted graph against
    the whole evidence pool. It is injected so the test suite never reaches a
    provider, and so a provider outage degrades instead of failing: a check
    that cannot complete leaves the applied version standing and says so.

    Returns the round record, or `None` when nothing was staged.
    """
    staged = _staged_judgements(run_dir)
    if not staged:
        return None
    graph_path = run_dir / "service_graph.yaml"
    before = _load(graph_path) or {}
    graph = before
    for judgement in staged:
        applier = _JUDGEMENT_APPLIERS.get(judgement.get("kind"))
        if applier is None:
            raise JudgementRejected(_refusal(f"不認得這種判斷（{judgement.get('kind')}）"))
        try:
            graph = applier(graph, judgement)
        except JudgementRejected as exc:
            # 每一種拒絕都要先講「你改的東西還在」。研究者按下套用之後看到一句
            # 錯誤訊息，第一個念頭是「我剛才做的白費了嗎」——先回答那個問題。
            raise JudgementRejected(_refusal(str(exc))) from exc

    errors = V.structural_errors(graph, V.load_schema("service-graph"))
    if errors:
        # 結構壞掉就拒絕，而且暫存原封不動（任務 2.4）。
        raise JudgementRejected(_refusal(str(errors[0])))

    # This apply path had no HTTP caller of any kind before this change (not
    # even for move_node/add_relation, staged since an earlier change) --
    # its first real exercise against production data surfaced a genuine
    # gap: a manual-transport run can legitimately lack evidence.yaml, and
    # _load() has no is_file() guard the way this file's other optional
    # reads do (e.g. _primary_hmw_statement). Same fallback the `or` clauses
    # already intended, just reachable now.
    evidence = _load(run_dir / "evidence.yaml") if (run_dir / "evidence.yaml").is_file() else []
    journey = _load(run_dir / "journey.yaml") if (run_dir / "journey.yaml").is_file() else {}
    case = _load(run_dir / "case.yaml") if (run_dir / "case.yaml").is_file() else {}
    conflicts = _load(run_dir / "conflicts.yaml") if (run_dir / "conflicts.yaml").is_file() else []
    evidence = evidence or []
    journey = journey or {}
    case = case or {}
    conflicts = conflicts or []
    draft = sbp_stages.project(
        graph, None, conflicts, case, journey_data=journey,
        annotations=graph.get("annotations"), evidence=evidence)
    draft.setdefault("meta", {}).update({"timestamp": dt.datetime.now().strftime("%Y-%m-%d")})

    # 圖先落地再發布：`resolve_baseline()` 會重讀檔案再比雜湊，發布的內容跟磁碟
    # 上的一分岔，之後每一次檢查都會失敗。這個坑這次 session 真的踩過。
    #
    # That ordering means a publish_batch validation failure below (this
    # path's first real HTTP caller surfaced one against a real run: a
    # confirmed_journey whose evidence_refs don't resolve because the run
    # has no evidence.yaml) would otherwise leave the two writes just below
    # standing on disk while the API reports failure -- breaking the very
    # promise `_refusal()` states to the researcher ("你改的東西都還在"),
    # which so far only held for the structural check above (raised before
    # any write). Everything from here to the function's return is wrapped
    # so any failure restores both files to what they held a moment ago.
    draft_path = run_dir / "sbp_draft.yaml"
    before_draft = _load(draft_path) if draft_path.is_file() else None
    _write_yaml_atomic(graph_path, graph)
    _write_yaml_atomic(draft_path, draft)
    try:
        return _publish_applied_round(run_dir, graph, draft, evidence, journey, case, before, staged, recheck)
    except Exception:
        _write_yaml_atomic(graph_path, before)
        if before_draft is None:
            draft_path.unlink(missing_ok=True)
        else:
            _write_yaml_atomic(draft_path, before_draft)
        raise


@_owns_project_lock(_run_dir_argument)
def _publish_applied_round(run_dir: Path, graph: dict, draft: dict, evidence: list, journey: dict,
                            case: dict, before: dict, staged: list[dict], recheck) -> dict:
    """The publish/registry/snapshot half of `_apply_staged_judgements`,
    split out so the caller can wrap it in a rollback on failure without a
    deeply nested try block."""
    state = _load(run_dir / "run.json") or {}
    case_id = str(case.get("case_id") or (graph.get("meta") or {}).get("case_id") or run_dir.name)
    declared_registry = state.get("artifact_registry")
    registry_path = (
        storage_locations.resolve_registry_pointer(
            declared_registry, run_dir, RUNS_DIR, case_id=case_id
        )
        if declared_registry
        else _default_run_registry_path(run_dir)
    )
    scenario_id = (graph.get("meta") or {}).get("scenario_id") or journey.get("primary_scenario")
    evidence_ids = {item["id"] for item in evidence if isinstance(item, dict) and "id" in item}
    now = dt.datetime.now().isoformat(timespec="seconds")
    publication = sbp_lifecycle.publish_batch(
        registry_path,
        [
            {
                "key": "evidence", "artifact_type": "evidence", "case_id": case_id,
                "content": evidence,
                "validate": lambda data: V.structural_errors(data, V.load_schema("evidence")),
                "dependencies": {},
            },
            {
                "key": "confirmed_journey", "artifact_type": "confirmed_journey", "case_id": case_id,
                "content": journey,
                "validate": lambda data: (
                    V.structural_errors(data, V.load_schema("journey"))
                    + sbp_stages._journey_errors(data, evidence_ids, require_confirmed=True)
                ),
                "dependencies": {"evidence": "@evidence"},
            },
            {
                "key": "as_is_graph", "artifact_type": "as_is_graph", "case_id": case_id,
                "scenario_id": scenario_id, "content": graph,
                # 結構驗證已經在上面做過；語意檢查的發現是資訊不是判決，
                # 走「待你決定」清單，不在這裡擋住發布。
                "validate": lambda data: V.structural_errors(data, V.load_schema("service-graph")),
                "dependencies": {"evidence": "@evidence", "confirmed_journey": "@confirmed_journey"},
            },
        ],
        now,
        ledger_path=run_dir / ".gates" / "ledger.yaml",
    )

    round_record = {
        "version_id": publication["version_ids"]["as_is_graph"],
        # 刻意不叫 `origin`：那個名字已經是「這份 run 怎麼產出的」，值域是
        # api_run／manual_transport⋯。同一個欄位名底下擺兩套字彙，就是這個
        # 專案記錄過的那個「範圍不同卻共用一個詞」的可用性失敗。
        "made_by": "researcher",
        "summary": _round_summary(before, staged),
        "at": now,
    }
    # 這一輪的圖與投影各留一份，導覽才看得回去。留內容而不是靠判斷重播：
    # 重播要假設每一步都能原樣重現，而這裡的目的正好是「當時到底長怎樣」。
    snapshot = _round_snapshot_dir(run_dir, round_record["version_id"])
    _write_yaml_atomic(snapshot / "service_graph.yaml", graph)
    _write_yaml_atomic(snapshot / "sbp_draft.yaml", draft)
    if not declared_registry:
        state["artifact_registry"] = storage_locations.normalized_registry_pointer(
            registry_path, run_dir, RUNS_DIR
        )
    state["artifact_versions"] = publication["version_ids"]
    state["adjustment_rounds"] = _adjustment_rounds(run_dir) + [round_record]
    # 檢查的發現：套用之後才算，而且不擋任何東西——它走「要你決定」清單。
    # 機械那一半永遠跑得動；模型那一半跑不動就少幾條，不會讓已套用的東西倒退。
    findings = _semantic_findings(graph, evidence, journey)
    state["check_incomplete"] = False
    if recheck is not None:
        try:
            findings += [
                str(item.get("text") or "").strip()
                for item in (recheck(graph, evidence, journey, case, staged) or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
        except Exception:  # noqa: BLE001 - 見 docstring：檢查跑不完不能推翻已套用的東西
            state["check_incomplete"] = True
    state["check_findings"] = findings
    _write_json_atomic(run_dir / "run.json", state)
    _clear_staged_judgements(run_dir)
    return round_record


def _default_run_registry_path(run_dir: Path) -> Path:
    """Same layout `sbp_gen.__main__` gives a freshly generated run's registry."""
    case_path = run_dir / "case.yaml"
    case_id = str((_load(case_path) or {}).get("case_id") or run_dir.name)
    return storage_locations.derive_registry_path(
        case_id, storage_locations.runs_root_for_run(run_dir)
    )


def _adoptable_run_content(run_dir: Path, state: dict) -> tuple[str, list, dict, dict]:
    """Read and structurally validate a run's evidence/journey/graph for adoption.

    Raises `RunAdoptionRejected` naming the first blocking problem. Deliberately
    does not run `sbp_stages._validate_graph()`'s semantic checks (scenario/step
    consistency, disposition coverage) — those are exactly what already renders
    as "N 個地方沒通過檢查" on a registered run without blocking its confirmation
    (explain-blocked-as-is-confirmation: 裁定事實是研究者的事). A run adopted
    from outside this pipeline gets the same treatment, not a stricter one: the
    researcher decides whether the content is good enough, the schema only
    guarantees it is *readable* enough to register and review at all.
    """
    case = _load(run_dir / "case.yaml") if (run_dir / "case.yaml").exists() else None
    if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
        raise RunAdoptionRejected("找不到這個 run 的案例資料，無法辨識案例編號。")
    try:
        evidence = sbp_as_is_research._baseline_evidence(run_dir, state)
    except sbp_as_is_research.ResearchOperationError as exc:
        raise RunAdoptionRejected(exc.user_message) from exc
    evidence_errors = V.structural_errors(evidence, V.load_schema("evidence"))
    if evidence_errors:
        raise RunAdoptionRejected("這個 run 的證據內容不符合系統能讀的格式：" + "；".join(evidence_errors[:3]))
    journey_path = run_dir / "journey_confirmed_input.yaml"
    if not journey_path.exists():
        journey_path = run_dir / "journey.yaml"
    if not journey_path.exists():
        raise RunAdoptionRejected("找不到這個 run 的歷程檔案。")
    journey = load_safe_yaml(journey_path.read_text(encoding="utf-8"))
    evidence_ids = {item["id"] for item in evidence if isinstance(item, dict) and "id" in item}
    journey_errors = V.structural_errors(journey, V.load_schema("journey")) + sbp_stages._journey_errors(
        journey, evidence_ids, require_confirmed=True
    )
    if journey_errors:
        raise RunAdoptionRejected("這個 run 的歷程還沒有標成已確認，或格式不符：" + "；".join(journey_errors[:3]))
    graph_path = run_dir / "service_graph.yaml"
    if not graph_path.exists():
        raise RunAdoptionRejected("找不到這個 run 的服務藍圖檔案。")
    graph = load_safe_yaml(graph_path.read_text(encoding="utf-8"))
    graph_errors = V.structural_errors(graph, V.load_schema("service-graph"))
    if graph_errors:
        raise RunAdoptionRejected("這個 run 的服務藍圖不符合系統能讀的格式：" + "；".join(graph_errors[:3]))
    # A graph made before canonical metadata existed carries no schema_version
    # and no meta.artifact_type/scenario_id, and downstream provenance checks
    # refuse it outright ("legacy graph 只能唯讀"). Apply the same normalization
    # a fresh run applies at the end of generation — every field it writes is
    # derived from this run's own case.yaml and journey.yaml (the constants 2
    # and "as_is_graph", the case id, the journey's primary scenario, and a
    # hash of that same journey), so nothing is invented. It states "this graph
    # is registered against this journey", which is exactly the pair being
    # published together here. Where the two disagree, that disagreement still
    # surfaces to the researcher as findings on the blueprint — it is not
    # silenced by this.
    graph = copy.deepcopy(graph)
    sbp_stages._canonicalize_graph_metadata(graph, case, journey)
    canonical_errors = V.canonical_graph_dependency_errors(graph)
    if canonical_errors:
        raise RunAdoptionRejected("這份藍圖缺少後續步驟需要的欄位：" + "；".join(canonical_errors[:3]))
    return case["case_id"], evidence, journey, graph


def _source_case_file_for_run(state: dict) -> Path | None:
    """The case file a run recorded for itself, or None.

    Only uses paths the run wrote down at generation time — `selected_case`
    when present, otherwise the directory of the evidence file it was built
    from. Never guesses by `case_id`: two case files in this repo deliberately
    share one (the authorization-gate browser fixture impersonates the real
    MUJI case on purpose), so a lookup by id has no single right answer.
    """
    for candidate in (state.get("selected_case"), state.get("from_evidence")):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        path = (_active_data_root() / candidate)
        case_path = path if path.name == "case.yaml" else path.parent / "case.yaml"
        try:
            return _resolve_case_file(str(case_path.relative_to(_active_data_root())))
        except (ImprovementGoalRejected, ValueError):
            continue
    return None


def _sync_run_improvement_goal(run_dir: Path, state: dict) -> None:
    """Copy the case's improvement goal into this run's frozen case snapshot.

    The goal is a review-time research decision, but `resolve_baseline()` reads
    it from `<run>/case.yaml` — a copy taken when the run was generated, before
    any goal existed. So the gate could display 改善目標：… (read from the case)
    while confirmation failed with 請先補充改善目標 (read from the snapshot):
    two sources of truth for one sentence, disagreeing on screen.

    Copies only when the snapshot has no goal of its own — a goal already
    recorded against this baseline is not overwritten by a later edit to the
    case, because that one is what the researcher confirmed against.
    """
    snapshot_path = run_dir / "case.yaml"
    if not snapshot_path.is_file():
        return
    snapshot = _load(snapshot_path)
    if not isinstance(snapshot, dict) or str(snapshot.get("improvement_goal") or "").strip():
        return
    case_path = _source_case_file_for_run(state)
    if case_path is None:
        return
    case = _load(case_path)
    goal = " ".join(str((case or {}).get("improvement_goal") or "").split())
    if not goal or not isinstance(case, dict) or case.get("case_id") != snapshot.get("case_id"):
        return
    lines = snapshot_path.read_text(encoding="utf-8").splitlines()
    rendered = yaml.safe_dump(
        {"improvement_goal": goal}, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=10_000).rstrip("\n")
    anchor = next((i for i, line in enumerate(lines) if line.startswith("case_id:")), len(lines) - 1)
    lines.insert(anchor + 1, rendered)
    updated = "\n".join(lines).rstrip("\n") + "\n"
    reparsed = load_safe_yaml(updated)
    if not isinstance(reparsed, dict) or reparsed.get("improvement_goal") != goal:
        return
    temporary = snapshot_path.with_suffix(".yaml.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, snapshot_path)


@_owns_project_lock(_run_dir_argument)
def _adopt_run_into_registry(run_dir: Path, state: dict) -> dict | None:
    """Register a run's existing content as a lifecycle baseline, once.

    A researcher decided this directly: a run produced outside the normal
    pipeline (hand-run, imported, pre-dating version tracking) should still be
    usable as a proposal baseline as long as its evidence and blueprint are
    there — not turned away with "start over" just because nothing published
    it to the registry at the time. This does that publication now, at the
    moment the researcher presses 開始提案 — the same moment a normal run's
    confirmation freezes its baseline, just later than usual.

    Returns the updated run state on success, or `None` if this run already
    has one (no-op) or its content can't be adopted (caller falls back to the
    existing regenerate-instead path; nothing here raises for that case).
    """
    if isinstance(state.get("artifact_versions"), dict):
        return None
    try:
        case_id, evidence, journey, graph = _adoptable_run_content(run_dir, state)
    except RunAdoptionRejected:
        return None
    # The canonical metadata has to reach the file too, not just the registry:
    # `resolve_baseline()` re-reads service_graph.yaml from disk and compares
    # its digest against the published record, so publishing the canonicalized
    # graph while leaving the original on disk would make every later check
    # fail with 現況藍圖已更新. Written before publication, and byte-identical
    # on a repeat, so an interrupted adoption leaves a file the next attempt
    # canonicalizes to exactly the same bytes.
    graph_path = run_dir / "service_graph.yaml"
    rendered_graph = yaml.safe_dump(graph, allow_unicode=True, sort_keys=False)
    if graph_path.read_text(encoding="utf-8") != rendered_graph:
        temporary = graph_path.with_suffix(".yaml.tmp")
        temporary.write_text(rendered_graph, encoding="utf-8")
        os.replace(temporary, graph_path)
    registry_path = _default_run_registry_path(run_dir)
    now = dt.datetime.now().isoformat(timespec="seconds")
    scenario_id = (graph.get("meta") or {}).get("scenario_id") or journey.get("primary_scenario")
    evidence_ids = {item["id"] for item in evidence if isinstance(item, dict) and "id" in item}
    publication = sbp_lifecycle.publish_batch(
        registry_path,
        [
            {
                "key": "evidence", "artifact_type": "evidence", "case_id": case_id,
                "content": evidence,
                "validate": lambda data: V.structural_errors(data, V.load_schema("evidence")),
                "dependencies": {},
            },
            {
                "key": "confirmed_journey", "artifact_type": "confirmed_journey", "case_id": case_id,
                "content": journey,
                "validate": lambda data: (
                    V.structural_errors(data, V.load_schema("journey"))
                    + sbp_stages._journey_errors(data, evidence_ids, require_confirmed=True)
                ),
                "dependencies": {"evidence": "@evidence"},
            },
            {
                "key": "as_is_graph", "artifact_type": "as_is_graph", "case_id": case_id,
                "scenario_id": scenario_id,
                "content": graph,
                # Structural only — see _adoptable_run_content's docstring for why
                # the semantic graph_errors() checks are deliberately not run here.
                "validate": lambda data: V.structural_errors(data, V.load_schema("service-graph")),
                "dependencies": {"evidence": "@evidence", "confirmed_journey": "@confirmed_journey"},
            },
        ],
        now,
        ledger_path=run_dir / ".gates" / "ledger.yaml",
    )
    state = dict(state)
    state["artifact_registry"] = storage_locations.normalized_registry_pointer(
        registry_path, run_dir, RUNS_DIR
    )
    state["artifact_versions"] = publication["version_ids"]
    _write_json_atomic(run_dir / "run.json", state)
    return state


# 這兩句是研究者在按鈕旁邊會讀到的字。第一版寫成「沒有留下可回溯的產出紀錄⋯
# 請重新產生一次這份藍圖」——研究者回報完全看不懂要怎麼進到下一步，而且查證後
# 發現更糟：那個「重新產生」在這個 run 的畫面上根本按不到（②只給「繼續」）。
# 現在講的是這個介面裡真的走得到的那條路，並且旁邊配一顆直接把他帶過去的按鈕。
PROPOSAL_INELIGIBLE_NO_VERSIONS = (
    "這份藍圖是從外部匯入的，不是在這裡產生的，所以沒辦法接著做提案。"
    "用同一個案例重新產生一次藍圖就可以。"
)
PROPOSAL_INELIGIBLE_UNREADABLE = (
    "系統讀不到這個版本的紀錄檔，沒辦法判斷能不能提案。"
    "用同一個案例重新產生一次藍圖就可以。"
)


def _proposal_eligibility(run_dir: Path | None) -> tuple[bool, str | None]:
    """能不能從這個 run 開始提案，以及不能的話為什麼。

    `POST /api/review` 只有在 run 的狀態帶著 `artifact_versions` 時才會啟動調研
    （publication 需要它才能在失敗時保住既有版本）。判斷寫在這裡一份，endpoint
    與 runtime 共用，介面就不會開放一個伺服器不會執行的動作。

    一個 run 沒有 `artifact_versions` 不代表它不能提案：外部匯入、手動產生、
    早於版本追蹤機制的 run，只要證據與藍圖讀得出來，一樣可以在按下「開始提案」
    的當下登記成基線（見 `_adopt_run_into_registry`）——研究者已經決定，這件事
    看來源，不看是不是這套系統自己產出的。這裡先用同一套結構檢查跑一次（不寫
    檔），讓畫面上的按鈕跟伺服器真的會不會接受一致；實際登記留到確認那一刻才
    真正發生。
    """
    state_path = (run_dir / "run.json") if run_dir else None
    if state_path is None or not state_path.is_file():
        return False, PROPOSAL_INELIGIBLE_UNREADABLE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return False, PROPOSAL_INELIGIBLE_UNREADABLE
    if isinstance(state.get("artifact_versions"), dict):
        return True, None
    try:
        _adoptable_run_content(run_dir, state)
    except RunAdoptionRejected as exc:
        return False, str(exc)
    return True, None


DRAFT_UNREADABLE_FINDING = "系統讀不到這個版本存下來的藍圖檔案，無法檢查內容。"


# A failed generation carried only a log the researcher never sees, and the
# surface told them to fix their case data. That is a guess, and on the first
# real failure it was the wrong one: the run died because the server process
# had no credential for the external service, which no amount of editing the
# case would repair. Classify the cause here, where the log is, and hand the
# surface a researcher-facing account of it. Provider and model names stay on
# this side of the boundary — the existing rule that they never reach the
# screen holds for failures too.
_MISSING_CREDENTIAL_MARKERS = (
    "Could not resolve authentication method",
    "authentication_error",
    "_API_KEY",
    "api_key",
    "Incorrect API key",
    "invalid_api_key",
)
_RATE_LIMIT_MARKERS = ("rate_limit", "429", "Too Many Requests", "quota")
_UNREACHABLE_MARKERS = (
    "Connection error", "Temporary failure in name resolution",
    "getaddrinfo", "Read timed out", "Max retries exceeded",
)
_CLI_RUNNER_MARKERS = ("CLI runner", "CLI 執行器")
_CLI_RUNNER_CALL_FAILED_RE = re.compile(r"CLI runner 回報這次工作失敗(?:（(\w+)）)?")
# preserve-cli-runner-failure-cause: the runner's own one-line report, already
# cleaned twice (wrapper and adapter). It is shown as the runner's report, never
# as the sentence that names the cause.
_CLI_RUNNER_REPORT_RE = re.compile(r"執行器回報：([^\n]+)")
_CLI_RUNNER_CALL_FAILURE_REASONS = {
    "runner_empty_reply": "執行器這次沒有回覆任何內容。",
    "runner_timed_out": "執行器在時限內沒有回應。",
    "runner_reply_malformed": "執行器回覆的格式讀不出來。",
    "runner_reply_supplies_source_content": "執行器回覆夾帶了不該直接提供的原文內容。",
    "runner_request_malformed": "送給執行器的請求格式有誤。",
    "runner_cli_usage_error": "執行器不接受這次的指令參數或設定。",
    "runner_rate_limited": "執行器那端的使用次數或額度已達上限。",
    "runner_model_unavailable": "執行器目前用不了指定的模型。",
    "runner_request_rejected": "執行器那端拒絕了這次請求（例如內容太長或格式不被接受）。",
    "runner_network_unavailable": "執行器連不上模型服務（網路或連線問題）。",
    "runner_failed": "執行器內部發生未預期的錯誤。",
}

# Shared by every job kind (Persona segmentation, blueprint, ...), so it names
# "this step", never one artifact.
GENERATION_FAILURE_UNKNOWN = {
    "cause": "unknown",
    "what_happened": "這一步的產生工作中途停下來了，伺服器沒有回報可辨識的原因。",
    "why_it_matters": "沒有寫出新的研究產物；你目前看到的版本沒有被改動。",
    "next_action": "再試一次；連續失敗的話，請看伺服器視窗最後幾行輸出。",
}


def _infra_failure_cause(text: str) -> dict | None:
    """The three infra-level failure categories shared by every job kind
    (missing credential / rate limit / unreachable) -- extracted so a
    second, job-kind-specific projection (see
    _persona_content_failure_projection below) can check these before
    falling back to its own kind's wording, instead of duplicating the
    marker lists."""
    call_failed = _CLI_RUNNER_CALL_FAILED_RE.search(text)
    if call_failed:
        code = call_failed.group(1)
        report_match = _CLI_RUNNER_REPORT_RE.search(text)
        report = sbp_cli_runner.clean_diagnostic(report_match.group(1)) if report_match else None
        runner_fields = {"runner_code": code, **({"upstream": report, "upstream_source": "runner"} if report else {})}
        if code == "runner_missing":
            return {
                "cause": "cli_runner_unavailable",
                "what_happened": "CLI 執行器找不到可執行的模型程式。",
                "why_it_matters": "這次工作沒有產出新的研究產物。",
                "next_action": "請檢查 CLI 安裝與執行設定，再開始這一步。",
                **runner_fields,
            }
        if code == "runner_not_authenticated":
            return {
                "cause": "cli_runner_authentication_required",
                "what_happened": "CLI 執行器回報尚未登入。",
                "why_it_matters": "這次工作沒有產出新的研究產物。",
                "next_action": "請完成 CLI 登入，再開始這一步。",
                **runner_fields,
            }
        reason = _CLI_RUNNER_CALL_FAILURE_REASONS.get(code, "執行器回報這次呼叫失敗，沒有更詳細的原因。")
        return {
            "cause": "cli_runner_call_failed",
            "what_happened": f"CLI 執行器這次呼叫沒有成功：{reason}",
            "why_it_matters": "沒有寫出新的研究產物；目前版本與研究資料沒有被改動。",
            # A usage limit does not clear by retrying now; say to wait for it.
            "next_action": ("等執行器那端的使用額度恢復後再開始這一步；可先到執行器的用量頁面確認何時可以再用。"
                            if code == "runner_rate_limited"
                            else "可以直接重試一次；同一個原因連續發生，再檢查執行器那端的狀態。"),
            **runner_fields,
        }
    if any(marker in text for marker in ("CLI runner 尚未設定", "CLI runner 設定無法解析")):
        return {
            "cause": "cli_runner_unavailable",
            "what_happened": "CLI 執行器尚未正確設定，這次工作沒有開始。",
            "why_it_matters": "沒有寫出新的研究產物；目前版本與研究資料沒有被改動。",
            "next_action": "請檢查 CLI 執行設定，再重新開始這一步。",
        }
    if any(marker in text for marker in _CLI_RUNNER_MARKERS):
        return {
            "cause": "cli_runner_call_failed",
            "what_happened": "CLI 執行器這次呼叫沒有完成，原因尚未明確。",
            "why_it_matters": "沒有寫出新的研究產物；目前版本與研究資料沒有被改動。",
            "next_action": "請檢查執行器狀態後重試；若持續失敗，查看伺服器錯誤記錄。",
        }
    if any(marker in text for marker in _MISSING_CREDENTIAL_MARKERS):
        return {
            "cause": "missing_credential",
            "what_happened": "這台伺服器沒有可用的外部服務金鑰，所以呼叫不出去。",
            "why_it_matters": "這不是案例資料的問題——改逐字稿、改歷程都不會讓它成功。",
            "next_action": "請在啟動伺服器時載入金鑰，然後再試一次。",
        }
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return {
            "cause": "rate_limited",
            "what_happened": "外部服務暫時擋下了這次呼叫（次數或額度上限）。",
            "why_it_matters": "這不是案例資料的問題，資料本身沒有被改動。",
            "next_action": "等幾分鐘再試一次。",
        }
    if any(marker in text for marker in _UNREACHABLE_MARKERS):
        return {
            "cause": "unreachable",
            "what_happened": "連不上外部服務。",
            "why_it_matters": "這不是案例資料的問題，資料本身沒有被改動。",
            "next_action": "確認這台機器連得上網路，然後再試一次。",
        }
    return None


def _generation_failure_cause(log: list[str] | None) -> dict:
    """Name a failed run's cause in the researcher's terms, or say it is unknown."""
    text = "\n".join(str(line) for line in (log or []))
    if not text.strip():
        return dict(GENERATION_FAILURE_UNKNOWN)
    infra = _infra_failure_cause(text)
    if infra:
        return infra
    # The model kept answering in prose instead of the required JSON, and the
    # corrective retries in _generate did not recover it.
    if "回覆裡找不到 JSON" in text or "[回覆不是 JSON]" in text:
        return {
            "cause": "model_reply_unreadable",
            "what_happened": "模型沒有照要求的格式回覆，重試之後仍然讀不出內容。",
            "why_it_matters": "沒有寫出新的研究產物；你目前看到的版本沒有被改動。",
            "next_action": "再試一次；這通常是單次回覆的問題，重試多半就會成功。",
        }
    # The command refused its own arguments before doing anything; its reason
    # is already written for a person, so say it rather than "unknown".
    refusal = next((str(line).split("error:", 1)[1].strip() for line in log or []
                    if str(line).startswith("sbp-gen: error:")), None)
    if refusal:
        return {
            "cause": "request_refused",
            "what_happened": f"工作沒有開始：{refusal}",
            "why_it_matters": "沒有寫出任何新東西；目前的版本沒有被改動。",
            "next_action": "這是工作台送出的內容不完整，不是你的資料有問題；請回報這一行。",
        }
    return dict(GENERATION_FAILURE_UNKNOWN)


def _persona_content_failure_projection(log: list[str] | None) -> dict:
    """merge-persona-matrix-selection-into-one-screen task 3.4: unlike
    _generation_failure_cause (written for the as-is blueprint pipeline,
    falling back to a generic "產生這份藍圖..." message for anything it
    doesn't recognise), `sbp-gen persona-content`'s own refusals are
    already researcher-facing prose (_main_persona_content's own stderr
    lines, e.g. "target_segmentation_selection.yaml 沒有記錄主要目標象限
    ——...") -- design.md's contract requires showing that text verbatim,
    not reclassifying it into a generic message. Only true infra breakage
    (missing credential / rate limit / unreachable) still gets the shared
    generic wording, since that text is not researcher-actionable CLI prose.
    """
    lines = [str(line) for line in (log or []) if str(line).strip()]
    text = "\n".join(lines)
    infra_cause = _infra_failure_cause(text) if text else None
    if infra_cause is not None:
        return infra_cause
    if not lines:
        return dict(GENERATION_FAILURE_UNKNOWN)
    return {
        "cause": "persona_content_cli_error",
        "what_happened": lines[-1],
        "why_it_matters": "沒有寫出新的 persona；這一格既有的內容沒有被改動。",
        "next_action": "依訊息處理後再試一次。",
    }

# researcher_finding 從這裡搬到 validate_sbp，因為判斷判準報告也要把內部編號換成
# 卡片名稱，而「畫面上不准出現裸編號」是一條規則、不該有兩份實作（CLAUDE.md
# 自問 #3）。伺服器沿用同一個名字，指向同一支函式。
_researcher_finding = V.researcher_finding


def _load_run_artifacts(run_dir: Path):
    """Load a run's draft, evidence ids, case, and journey once.

    declare-analysis-chain-and-journey-coverage: extracted out of
    `_run_validity_findings` so the blocking findings function and the
    advisory findings function below it read the run's artifacts from one
    place instead of two independently-maintained copies.

    Returns `None` when the draft has no `graph` key — the old-format runs
    `_run_validity_findings`'s own docstring already treats as passing —
    so both callers skip the same way without repeating the check.
    """
    draft = _load(run_dir / "sbp_draft.yaml") or {}
    if "graph" not in draft:
        return None
    case = None
    case_path = run_dir / "case.yaml"
    if case_path.exists():
        case = _load(case_path)
    evidence_ids = None
    ev_path = run_dir / "evidence.yaml"
    if not ev_path.exists() and case_path.exists():
        run_json_path = run_dir / "run.json"
        if run_json_path.exists():
            meta = json.loads(run_json_path.read_text(encoding="utf-8"))
            if meta.get("from_evidence"):
                ev_path = _active_data_root() / meta["from_evidence"]
    if ev_path.exists():
        evidence_ids = {e["id"] for e in _load(ev_path) or []}
    journey = None
    journey_path = run_dir / "journey.yaml"
    if journey_path.exists():
        journey = _load(journey_path)
    # The draft is projected onto the confirmed journey's step axis, so that is
    # the axis coverage has to be checked against. `case.yaml` is only the same
    # axis for runs the legacy pipeline wrote it back into; the standalone
    # `structure` command deliberately leaves it as the researcher authored it.
    # `case_with_journey()` validates confirmation itself and raises when the
    # journey can't define an axis — that raise is the fallback signal, so no
    # second confirmation rule gets written here.
    if isinstance(case, dict) and journey is not None:
        try:
            case = sbp_stages.case_with_journey(case, journey)
        except (ValueError, KeyError, TypeError):
            pass
    return draft, evidence_ids, case, journey


def _node_labels(draft: dict) -> dict[str, str]:
    return {
        node.get("id"): node.get("label")
        for node in ((draft.get("graph") or {}).get("nodes") or [])
        if isinstance(node, dict) and node.get("id") and node.get("label")
    }


def _run_validity_findings(run_dir: Path) -> list[str]:
    """重用生成管線本來就在跑的驗證（`sbp_gen/__main__.py` 的同一組呼叫），不是
    另外發明一套規則（add-run-watch-mode design.md 決定三），但**保留**它算出來的
    問題清單。

    以前這裡只回傳「有沒有問題」，把問題本身丟掉，於是畫面上只剩一個「產出失敗」
    的狀態詞、沒有任何原因可講。清單保留下來、編號換成卡片名稱之後才交出去。

    舊格式（`sbp_draft.yaml` 沒有 `graph` 欄位）一律視為通過——這個 change
    要保護的是「正在被目前這條生成管線覆寫的 run」，不是幫舊格式重建驗證。
    任何解析或驗證過程中的例外都當成「還沒驗證通過」，不是伺服器錯誤：
    半寫的 YAML 是輪詢期間預期會遇到的暫態，不是 bug。
    """
    try:
        loaded = _load_run_artifacts(run_dir)
        if loaded is None:
            return []
        draft, evidence_ids, case, journey = loaded
        errors = V.structural_errors(draft, V.load_schema("sbp-draft"))
        errors += V.projection_errors(draft, evidence_ids, case, journey=journey)
        labels = _node_labels(draft)
        return [_researcher_finding(error, labels) for error in errors]
    except Exception:
        return [DRAFT_UNREADABLE_FINDING]


def _run_validity(run_dir: Path) -> bool:
    """這個 run 存下來的藍圖有沒有通過驗證。

    `_cached_run_status()` 與 `/api/run-status` 只需要布林值，簽名維持不變；
    需要講出「哪裡不對」的地方改呼叫 `_run_validity_findings()`。
    """
    return not _run_validity_findings(run_dir)


def _run_advisory_findings(run_dir: Path) -> list[str]:
    """Non-blocking judgment findings for the previewed run.

    declare-analysis-chain-and-journey-coverage: a second findings function
    beside `_run_validity_findings`, sharing `_load_run_artifacts` with it.
    These come from `graph_warnings()` — the same warning path
    `structural_errors`/`projection_errors` never draw from — so nothing
    here can affect `_run_validity()`. Any exception while reading the run's
    artifacts yields no advisory findings rather than propagating: advisory
    display must never change the blocking path's behavior.

    Defined after `_run_validity` on purpose: `test_judgment_criteria.py`'s
    `test_the_confirm_gate_does_not_consult_judgment_findings_at_all` slices
    the source text between `_run_validity_findings` and `_run_validity` and
    asserts that window never mentions `graph_warnings`. Placing this
    function inside that window (it legitimately calls `graph_warnings`)
    would fail a correct guard rather than reveal a real problem.
    """
    try:
        loaded = _load_run_artifacts(run_dir)
        if loaded is None:
            return []
        draft, evidence_ids, _case, journey = loaded
        graph = {
            "nodes": (draft.get("graph") or {}).get("nodes", []),
            "relations": draft.get("relations", []),
            "markers": draft.get("markers", []),
        }
        warnings = V.graph_warnings(graph, evidence_ids, journey=journey)
        labels = _node_labels(draft)
        return [_researcher_finding(w, labels) for w in warnings]
    except Exception:
        return []


# run 名稱 -> 上次算出的 (fingerprint, valid)。單純的計算結果快取，不是任何東西的
# 正本，行程重啟就清空——指紋沒變就不用重新解析 YAML、重新跑驗證
# （add-run-watch-mode design.md 決定四），研究者開著分頁輪詢時才不會把 runs/
# 底下每個 run 都重複驗證一次。
_run_status_cache: dict[str, tuple[str, bool]] = {}


def _cached_run_status(run_name: str) -> dict | None:
    safe_name = _safe_component(run_name or "", "")
    run_dir = RUNS_DIR / safe_name if safe_name else None
    fingerprint = _run_fingerprint(run_dir) if run_dir else None
    if fingerprint is None:
        _run_status_cache.pop(safe_name, None)
        return None
    cached = _run_status_cache.get(safe_name)
    if cached is not None and cached[0] == fingerprint:
        valid = cached[1]
    else:
        valid = _run_validity(run_dir)
        _run_status_cache[safe_name] = (fingerprint, valid)
    return {"fingerprint": fingerprint, "valid": valid}


def _run_status_payload(names: list[str]) -> dict:
    """`GET /api/run-status` 的邏輯本體，抽成純函式方便離線測試（跟 `_mark_run_reviewed`
    同一種手法）。不存在的 run 名稱直接省略，不是錯誤（spec「Requested run no longer
    exists」）。"""
    runs = {}
    for name in names:
        status = _cached_run_status(name)
        if status is not None:
            runs[name] = status
    return {"runs": runs}


class ReviewRunNotFound(Exception):
    pass


class ReviewStateRejected(Exception):
    pass


class AsIsResearchRequestRejected(ValueError):
    pass


class AsIsResearchPrerequisiteRejected(ValueError):
    """A precondition for starting proposal work was not met.

    Carries `source` so the dialog can say where the sentence came from. These
    are almost always this machine's own checks; calling them "外部服務回覆"
    sends the researcher to look at the network and the keys instead of at the
    button they have not pressed yet.
    """

    def __init__(self, message: str, *, source: str = "local"):
        super().__init__(message)
        self.source = source


class AsIsResearchConflict(ValueError):
    pass


class ToBeRequestRejected(ValueError):
    pass


class ToBeConflict(ValueError):
    pass


# Identity comes from the trusted application boundary, never from a validation
# request body. Deployments can replace these values when they add real auth;
# the local researcher workbench remains deterministic and offline-capable.
AUTHENTICATED_VALIDATION_ACTOR = {
    "actor_id": os.environ.get("SBP_ACTOR_ID", "USR-LOCAL"),
    "actor_type": os.environ.get("SBP_ACTOR_TYPE", "researcher"),
    "display_name": os.environ.get("SBP_ACTOR_NAME", "本機研究者"),
}

_AUTH_SERVICE = None
_AUTH_CONFIG = None
_VALIDATION_ACTOR = contextvars.ContextVar("sbp_validation_actor", default=None)


def _auth_service() -> sbp_auth.AuthService:
    """Return the process-local auth service, rebuilding it when env changes."""
    global _AUTH_SERVICE, _AUTH_CONFIG
    config = sbp_auth.AuthConfig.from_env()
    if _AUTH_SERVICE is None or _AUTH_CONFIG != config:
        service = sbp_auth.AuthService(config)
        service.ensure_ready()
        _AUTH_SERVICE = service
        _AUTH_CONFIG = config
    return _AUTH_SERVICE


def reset_auth_service() -> None:
    """Test hook; production code relies on environment-driven lazy setup."""
    global _AUTH_SERVICE, _AUTH_CONFIG
    _AUTH_SERVICE = None
    _AUTH_CONFIG = None


def _current_validation_actor() -> dict:
    actor = _VALIDATION_ACTOR.get()
    return actor or AUTHENTICATED_VALIDATION_ACTOR


# ai-execution-info: what this request asked the job to run with. Set while
# validating the execution, read by _replace_job on the same request thread,
# so the job carries its own configuration, not the page's current settings.
_REQUEST_EXECUTION_SUMMARY: contextvars.ContextVar = contextvars.ContextVar("request_execution_summary", default=None)
EXECUTOR_LABELS = {"codex": "Codex", "claude": "Claude Code"}


def _execution_summary(execution: sbp_execution.ExecutionConfig) -> dict | None:
    """Executor and configured model/effort -- configuration, never presented
    as what the executor confirmed it ran."""
    if execution.mode == "cli":
        executor = execution.executor
        model, effort = execution.codex_model, execution.codex_effort
        source = "request"
        if executor == "codex" and not (model and effort):
            try:
                from cli_runners import codex as codex_runner
                settings = codex_runner.read_selected_settings()
            except Exception:
                settings = {}
            model = model or settings.get("model")
            effort = effort or settings.get("model_reasoning_effort")
            source = "codex_settings" if not execution.codex_model else "request"
        return {"executor": EXECUTOR_LABELS.get(executor, executor), "model": model, "effort": effort,
                "configured_by": source}
    if execution.mode == "api":
        return {"executor": f"API · {execution.provider or 'openai'}", "model": None, "effort": None,
                "configured_by": "request"}
    return None


def _execution_for_payload(payload: object, *, allow_legacy: bool = True) -> sbp_execution.ExecutionConfig:
    """Validate one Workbench job's adapter without persisting credentials.

    Authenticated staging requests must choose CLI or API explicitly. The
    unauthenticated local callers keep the pre-adapter environment contract;
    authenticated staging requests must choose an adapter explicitly. The
    environment bridge is never rendered as a product choice.
    """
    if isinstance(payload, dict) and "execution" in payload:
        execution = sbp_execution.ExecutionConfig.from_payload(payload.get("execution"))
    elif allow_legacy and not _auth_service().enabled:
        execution = sbp_execution.ExecutionConfig.from_environment()
    else:
        raise sbp_execution.ExecutionConfigError("請先選擇 CLI 或 API 執行方式。")
    _ensure_execution_ready(execution)
    _REQUEST_EXECUTION_SUMMARY.set(_execution_summary(execution))
    return execution


def _resolve_cli_command(executor: str) -> list[str]:
    """The one resolution of an executor's execution command.

    Both the pre-admission readiness check and the settings status use it, so
    the panel cannot say "已登入" for an executor whose request would be refused
    for lack of a configured command (desktop-runtime-foundation)."""
    try:
        command = sbp_cli_runner._command(executor)
    except sbp_cli_runner.CLIRunnerError as exc:
        raise sbp_execution.ExecutionConfigError(str(exc)) from exc
    if shutil.which(command[0]) is None:
        raise sbp_execution.ExecutionConfigError("找不到設定的 CLI 執行器，請檢查執行設定。")
    return command


def _connection_check_invoke(request: dict, **kwargs):
    """The shipped-wrapper protocol for a connection check, with the wrapper process tracked
    so that closing the server ends it."""
    return sbp_cli_runner.invoke(request, run=child_processes.run, **kwargs)


def _ensure_execution_ready(execution: sbp_execution.ExecutionConfig) -> None:
    """Reject local prerequisites known to fail before a model job is admitted.

    Custom wrappers have no standard auth probe; runtime failures still belong
    to the wrapper. No provider call or credential read happens here.
    """
    if execution.mode != "cli":
        return
    executor = execution.executor
    command = _resolve_cli_command(executor)
    shipped_wrappers = {
        "codex": Path(__file__).resolve().parents[1] / "cli_runners" / "codex.py",
        "claude": Path(__file__).resolve().parents[1] / "cli_runners" / "claude.py",
    }
    shipped_wrapper = shipped_wrappers.get(executor)
    if shipped_wrapper is None or len(command) < 2 or Path(command[1]).resolve() != shipped_wrapper.resolve():
        return
    status = _runner_status(executor)
    label = EXECUTOR_LABELS.get(executor, executor)
    if not status["installed"]:
        raise sbp_execution.ExecutionConfigError(f"找不到 {label} CLI，請先安裝或改用 API 執行。")
    if not status["authenticated"]:
        raise sbp_execution.ExecutionConfigError(f"{label} 尚未登入，請先完成登入或改用 API 執行。")


def _require_execution(execution: object) -> sbp_execution.ExecutionConfig:
    """Fail closed when a worker is started without a validated adapter.

    Workers never fall back to transport (or any other mode) on their own;
    manual/debug callers must pass ``ExecutionConfig(mode="transport")``.
    """
    if not isinstance(execution, sbp_execution.ExecutionConfig):
        raise sbp_execution.ExecutionConfigError("這個工作沒有指定執行方式。")
    return execution


def _execution_for_context(context: dict) -> sbp_execution.ExecutionConfig:
    """Return the validated request adapter carried by a worker context."""
    return _require_execution(context.get("execution"))


def _exception_text(error: BaseException) -> str:
    """Researcher-safe text for a handler's generic failure branch."""
    if isinstance(error, (sbp_execution.ExecutionConfigError, sbp_run_lock.RunLockBusy)):
        return str(error)
    return f"{type(error).__name__}: {error}"


def _redact_execution_text(value: object, config: sbp_execution.ExecutionConfig | None = None) -> str:
    """Keep a request-scoped BYOK out of in-memory job logs and projections."""
    text = str(value)
    key = config.api_key if config is not None else None
    return text.replace(key, "[redacted]") if key else text


def _run_case_id_for_access(run_name: object) -> str | None:
    safe_name = _safe_component(run_name, "") if isinstance(run_name, str) else ""
    if not safe_name:
        return None
    run_dir = RUNS_DIR / safe_name
    if not run_dir.is_dir():
        return None
    state_path = run_dir / "run.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("case_id"), str):
                return state["case_id"]
        except Exception:
            return None
    try:
        return _case_id_for_run(run_dir)
    except Exception:
        return None


def _ensure_legacy_projects(user: sbp_auth.User) -> None:
    service = _auth_service()
    cases = [
        (str(case["case_id"]), str(case["path"]))
        for case in _discover_cases()
        if case.get("case_id") and case.get("path")
    ]
    service.backfill_projects(owner_user_id=user.user_id, cases=cases)


def _user_owns_run(user: sbp_auth.User, run_name: object) -> bool:
    case_id = _run_case_id_for_access(run_name)
    if not case_id:
        return False
    project = _auth_service().project_for_case(case_id)
    return project is not None and project.owner_user_id == user.user_id


def _user_owns_case(user: sbp_auth.User, case_id: object) -> bool:
    if not isinstance(case_id, str) or not case_id:
        return False
    project = _auth_service().project_for_case(case_id)
    return project is not None and project.owner_user_id == user.user_id


def _validation_error(exc: sbp_validation.ValidationOperationError) -> dict:
    return {
        "error": {
            "code": exc.code,
            "message": exc.user_message,
            "retryable": exc.retryable,
        }
    }


def _validation_error_status(exc: sbp_validation.ValidationOperationError) -> int:
    if exc.code in {
        "validation_baseline_changed",
        "pilot_already_started",
        "pilot_fingerprint_stale",
        "pilot_already_completed",
        "validation_outcome_already_recorded",
    }:
        return 409
    if exc.code == "validation_publication_failed":
        return 503
    return 422


def _research_run(run_name: object) -> tuple[str, Path, dict]:
    if not isinstance(run_name, str) or _safe_component(run_name, "") != run_name:
        raise AsIsResearchRequestRejected("run 必須是安全且非空的名稱。")
    run_dir = RUNS_DIR / run_name
    state_path = run_dir / "run.json"
    if not state_path.is_file():
        raise AsIsResearchPrerequisiteRejected("找不到可供調研的服務藍圖。")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsIsResearchPrerequisiteRejected("服務藍圖狀態無法驗證。") from exc
    return run_name, run_dir, state


def _research_baseline(run_name: object, *, require_reviewed: bool = True):
    _safe, run_dir, state = _research_run(run_name)
    versions = state.get("artifact_versions")
    registry = state.get("artifact_registry")
    if not isinstance(versions, dict) or not isinstance(registry, str):
        raise AsIsResearchPrerequisiteRejected("這份服務藍圖尚未建立可追溯版本。")
    try:
        registry_path = storage_locations.resolve_registry_pointer(registry, run_dir, RUNS_DIR)
        return sbp_as_is_research.resolve_baseline(
            run_dir,
            registry_path=registry_path,
            expected_as_is_version=versions.get("as_is_graph"),
            require_reviewed=require_reviewed,
        )
    except storage_locations.RegistryPointerError as exc:
        raise AsIsResearchPrerequisiteRejected("這份服務藍圖的版本紀錄位置無法驗證。") from exc
    except sbp_as_is_research.ResearchOperationError as exc:
        raise AsIsResearchPrerequisiteRejected(exc.user_message, source=exc.source) from exc


def _as_is_research_projection(run_name: object) -> dict:
    """Read one operation without creating, retrying, or persisting anything."""
    _safe, run_dir, _state = _research_run(run_name)
    operation = sbp_as_is_research.load_current_operation(run_dir)
    if operation is None:
        raise AsIsResearchPrerequisiteRejected("這份服務藍圖尚未開始外部調研。")
    projection = sbp_as_is_research.project_operation(operation)
    try:
        baseline = _research_baseline(run_name, require_reviewed=True)
        expected = sbp_as_is_research.request_digest(
            baseline,
            model_policy=sbp_as_is_research.DEFAULT_MODEL_POLICY,
        )
        if expected != operation.get("request_digest"):
            raise AsIsResearchPrerequisiteRejected("服務藍圖已更新。")
        result_ids = set((operation.get("result") or {}).values())
        if result_ids:
            registry = load_safe_yaml(baseline.registry_path.read_text(encoding="utf-8"))
            states = sbp_lifecycle.resolve_lifecycle(registry)
            if any(states.get(version_id) != "current" for version_id in result_ids):
                raise AsIsResearchPrerequisiteRejected("調研結果已不是目前版本。")
    except AsIsResearchPrerequisiteRejected:
        message, _retryable = sbp_as_is_research.ERROR_MESSAGES["baseline_changed"]
        projection.update({
            "state": "stale",
            "retryable": False,
            "error": {"code": "baseline_changed", "user_message": message},
        })
    return projection


def _as_is_research_detail(run_name: object) -> dict:
    """Return canonical research display records without starting or retrying work."""
    projection = _as_is_research_projection(run_name)
    if projection.get("state") != "done" or projection.get("phase") != "research_ready":
        raise AsIsResearchPrerequisiteRejected("調研資料尚未準備好。")
    _safe, run_dir, _state = _research_run(run_name)
    operation = sbp_as_is_research.load_current_operation(run_dir) or {}
    stage_dir = sbp_as_is_research.operation_path(
        run_dir, str(operation.get("request_digest") or "")
    ).parent / "staged"
    try:
        issues = _load(stage_dir / "issue_package.yaml")
        research = _load(stage_dir / "research_catalog.yaml")
    except (OSError, yaml.YAMLError) as exc:
        raise AsIsResearchPrerequisiteRejected("調研資料無法安全讀取。") from exc
    if not isinstance(issues, dict) or not isinstance(research, dict):
        raise AsIsResearchPrerequisiteRejected("調研資料無法安全讀取。")
    expected = operation.get("staged_digests") or {}
    if (
        sbp_lifecycle.canonical_content_digest(issues) != expected.get("issue_package")
        or sbp_lifecycle.canonical_content_digest(research) != expected.get("research_catalog")
    ):
        raise AsIsResearchPrerequisiteRejected("調研資料完整性無法驗證。")
    return {
        "issues": list(issues.get("issues") or []),
        "sources": list(research.get("sources") or []),
        "claims": list(research.get("claims") or []),
    }


def _to_be_baseline(run_name: object):
    _safe, run_dir, state = _research_run(run_name)
    registry = state.get("artifact_registry")
    if not isinstance(registry, str):
        raise ToBeRequestRejected("這份案例尚未建立可追溯版本。")
    try:
        registry_path = storage_locations.resolve_registry_pointer(registry, run_dir, RUNS_DIR)
        return sbp_to_be.resolve_baseline(run_dir, registry_path=registry_path)
    except storage_locations.RegistryPointerError as exc:
        raise ToBeRequestRejected("這份案例的版本紀錄位置無法驗證。") from exc
    except sbp_to_be.ToBeOperationError as exc:
        raise ToBeRequestRejected(exc.user_message) from exc


def _to_be_detail(run_name: object) -> dict:
    """Read the current proposal and review record without mutation."""
    baseline = _to_be_baseline(run_name)
    operation = sbp_to_be.load_current_operation(baseline.run_dir)
    research_operation = sbp_as_is_research.load_current_operation(baseline.run_dir)
    research_projection = (
        sbp_as_is_research.project_operation(research_operation)
        if research_operation is not None else None
    )
    response = {
        "operation": sbp_to_be.project_operation(operation) if operation else None,
        "bundle": None,
        "review": None,
        "as_is": {
            "nodes": [{
                key: node.get(key)
                for key in ("id", "kind", "label", "step", "layer")
            } for node in baseline.graph.get("nodes") or [] if isinstance(node, dict)],
        },
    }
    try:
        current, review = sbp_to_be.load_current_review(baseline)
    except sbp_to_be.ToBeOperationError:
        response["state"] = _derive_to_be_runtime(
            _project_workbench_contract(), research_projection, operation, None
        )
        return response
    response["bundle"] = current.bundle
    response["review"] = {
        "fingerprint": current.fingerprint,
        "state": review.get("state"),
        "updated_at": review.get("updated_at"),
    }
    response["state"] = _derive_to_be_runtime(
        _project_workbench_contract(), research_projection, operation, review
    )
    return response


def _decide_to_be(
    run_name: object,
    *,
    fingerprint: object,
    decision: object,
    impact_confirmed: object = False,
) -> dict:
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ToBeRequestRejected("fingerprint 不可省略。")
    if not isinstance(decision, str):
        raise ToBeRequestRejected("decision 不可省略。")
    baseline = _to_be_baseline(run_name)
    try:
        review = sbp_to_be.decide_current_to_be(
            baseline,
            expected_fingerprint=fingerprint,
            decision=decision,
            impact_confirmed=impact_confirmed is True,
        )
    except sbp_to_be.ToBeOperationError as exc:
        if exc.code == "stale_to_be_fingerprint":
            raise ToBeConflict(exc.user_message) from exc
        raise ToBeRequestRejected(exc.user_message) from exc
    return {
        "fingerprint": review["fingerprint"],
        "state": review["state"],
        "updated_at": review["updated_at"],
    }


def _validation_baseline(run_name: object, *, require_accepted: bool = False):
    safe, run_dir, state = _research_run(run_name)
    registry = state.get("artifact_registry")
    if not isinstance(registry, str):
        raise sbp_validation.ValidationOperationError("validation_not_eligible")
    baseline = sbp_validation.resolve_baseline(
        run_dir,
        registry_path=registry,
        require_accepted=require_accepted,
    )
    return safe, baseline


def _validation_artifact_projection(value) -> dict | None:
    if value is None:
        return None
    content = value.record if isinstance(value, sbp_validation.CurrentPilot) else value.outcome
    return {
        "version_id": value.version_id,
        "content_digest": value.content_digest,
        "fingerprint": value.fingerprint,
        "lifecycle_state": value.lifecycle_state,
        "artifact": content,
    }


def _validation_detail(run_name: object) -> dict:
    """Project persisted validation truth without creating or repairing state."""
    safe, baseline = _validation_baseline(run_name, require_accepted=False)
    pilot = sbp_validation.load_current_pilot(baseline)
    outcome = sbp_validation.load_current_outcome(baseline)
    accepted = baseline.review.get("state") == "accepted"

    if pilot is not None and pilot.lifecycle_state != "current":
        state, actions = "validation_stale", ["export_validation"]
    elif not accepted:
        state, actions = "validation_locked", []
    elif pilot is None:
        state, actions = "pilot_ready", ["start_pilot"]
    elif pilot.record.get("state") == "collecting":
        protocol_refs = {
            item.get("proposal_ref") for item in pilot.record.get("protocol") or []
            if isinstance(item, dict)
        }
        covered_refs = {
            item.get("proposal_ref") for item in pilot.record.get("observations") or []
            if isinstance(item, dict)
        } | {
            item.get("proposal_ref") for item in pilot.record.get("not_observed") or []
            if isinstance(item, dict)
        }
        has_evidence = bool(
            (pilot.record.get("observations") or [])
            or (pilot.record.get("not_observed") or [])
            or (pilot.record.get("deviations") or [])
            or (pilot.record.get("adverse_effects") or [])
        )
        if protocol_refs and protocol_refs <= covered_refs:
            state, actions = "pilot_in_progress", ["complete_pilot"]
        elif has_evidence:
            state, actions = "pilot_evidence_incomplete", ["append_observations"]
        else:
            state, actions = "pilot_in_progress", ["append_observations"]
    elif outcome is None or outcome.lifecycle_state != "current":
        state, actions = "pilot_ready_for_decision", ["record_outcome", "export_validation"]
    else:
        decision = outcome.outcome.get("final_decision")
        state = {
            "continue": "validation_continue",
            "revise": "validation_revise",
            "stop": "validation_stop",
        }.get(decision, "validation_failed")
        actions = ["export_validation"]
        if decision == "revise":
            actions.insert(0, "revise_to_be")

    return {
        "schema_version": 1,
        "run": safe,
        "state": state,
        "accepted_to_be": {
            "version_id": baseline.current_to_be.version_id,
            "content_digest": baseline.current_to_be.content_digest,
            "fingerprint": baseline.current_to_be.fingerprint,
            "review_state": baseline.review.get("state"),
        },
        "pilot": _validation_artifact_projection(pilot),
        "outcome": _validation_artifact_projection(outcome),
        "allowed_actions": actions,
    }


def _start_validation_pilot(
    run_name: object,
    *,
    to_be_fingerprint: object,
    actual_scope: object,
    scope_deviation: object = None,
) -> dict:
    if not isinstance(to_be_fingerprint, str) or not to_be_fingerprint:
        raise sbp_validation.ValidationOperationError("validation_baseline_changed")
    if not isinstance(actual_scope, str) or not actual_scope.strip():
        raise sbp_validation.ValidationOperationError("pilot_observation_invalid")
    if scope_deviation is not None and not isinstance(scope_deviation, str):
        raise sbp_validation.ValidationOperationError("pilot_observation_invalid")
    safe, baseline = _validation_baseline(run_name, require_accepted=True)
    result = sbp_validation.start_pilot(
        baseline.run_dir,
        registry_path=baseline.registry_path,
        expected_to_be_fingerprint=to_be_fingerprint,
        actual_scope=actual_scope,
        scope_deviation=scope_deviation,
        actor=_current_validation_actor(),
    )
    return {
        "mutation": {
            key: result[key]
            for key in (
                "reused", "request_digest", "version_id", "content_digest",
                "fingerprint", "lifecycle_state",
            )
        },
        "validation": _validation_detail(safe),
    }


def _validation_mutation_response(safe: str, result: dict) -> dict:
    return {
        "mutation": {
            key: result[key]
            for key in (
                "reused", "request_digest", "version_id", "content_digest",
                "fingerprint", "lifecycle_state",
            )
        },
        "validation": _validation_detail(safe),
    }


def _append_validation_observations(
    run_name: object,
    *,
    pilot_fingerprint: object,
    observations: object = None,
    not_observed: object = None,
    deviations: object = None,
    adverse_effects: object = None,
) -> dict:
    collections = (observations, not_observed, deviations, adverse_effects)
    if not isinstance(pilot_fingerprint, str) or not pilot_fingerprint:
        raise sbp_validation.ValidationOperationError("pilot_fingerprint_stale")
    if any(value is not None and not isinstance(value, list) for value in collections):
        raise sbp_validation.ValidationOperationError("pilot_observation_invalid")
    safe, baseline = _validation_baseline(run_name, require_accepted=True)
    result = sbp_validation.append_pilot_data(
        baseline.run_dir,
        registry_path=baseline.registry_path,
        expected_pilot_fingerprint=pilot_fingerprint,
        actor=_current_validation_actor(),
        observations=observations or [],
        not_observed=not_observed or [],
        deviations=deviations or [],
        adverse_effects=adverse_effects or [],
    )
    return _validation_mutation_response(safe, result)


def _complete_validation_pilot(
    run_name: object, *, pilot_fingerprint: object
) -> dict:
    if not isinstance(pilot_fingerprint, str) or not pilot_fingerprint:
        raise sbp_validation.ValidationOperationError("pilot_fingerprint_stale")
    safe, baseline = _validation_baseline(run_name, require_accepted=True)
    result = sbp_validation.complete_pilot(
        baseline.run_dir,
        registry_path=baseline.registry_path,
        expected_pilot_fingerprint=pilot_fingerprint,
        actor=_current_validation_actor(),
    )
    return _validation_mutation_response(safe, result)


def _run_validation_revision(context: dict) -> None:
    try:
        operation, _reused, classification = sbp_to_be.request_validation_revision(
            context["run_dir"],
            registry_path=context["registry_path"],
            expected_to_be_fingerprint=context["to_be_fingerprint"],
            pilot_version_ref=context["pilot_version_ref"],
            validation_outcome_version_ref=context["outcome_version_ref"],
        )
        projection = sbp_to_be.project_operation(operation)
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="done" if operation.get("state") == "done" else "failed",
                phase=operation.get("phase"),
                result_endpoint=f"/api/to-be?run={quote(context['run'])}",
                error=projection.get("error"),
                revision_classification=classification,
            )
    except sbp_to_be.ToBeOperationError as exc:
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="failed",
                phase="synthesizing",
                error={"code": exc.code, "user_message": exc.user_message},
            )


def _enqueue_validation_revision(
    safe: str,
    baseline,
    *,
    pilot_version_ref: str,
    outcome_version_ref: str,
) -> dict:
    with _job_lock:
        if _job.get("state") == "running":
            return {
                "kind": sbp_to_be.OPERATION_KIND,
                "state": "blocked",
                "phase": "synthesizing",
                "retryable": True,
                "summary": "驗證結果已保存；目前分析完成後可重試完整方案調整。",
            }
        _replace_job(
            kind=sbp_to_be.OPERATION_KIND,
            state="running",
            phase="synthesizing",
            log=[],
            out=safe,
            output_identity=safe,
            result_endpoint=f"/api/to-be?run={quote(safe)}",
        )
    context = {
        "run": safe,
        "run_dir": baseline.run_dir,
        "registry_path": baseline.registry_path,
        "to_be_fingerprint": baseline.current_to_be.fingerprint,
        "pilot_version_ref": pilot_version_ref,
        "outcome_version_ref": outcome_version_ref,
    }
    threading.Thread(target=_run_validation_revision, args=(context,), daemon=True).start()
    return {
        "kind": sbp_to_be.OPERATION_KIND,
        "state": "queued",
        "phase": "synthesizing",
        "summary": "正在依試行結果建立完整替代方案。",
    }


def _record_validation_outcome(
    run_name: object,
    *,
    pilot_fingerprint: object,
    assessments: object,
    final_decision: object,
    rationale: object,
    deviation_rationale: object = None,
    impact_confirmed: object = False,
) -> dict:
    if (
        not isinstance(pilot_fingerprint, str)
        or not pilot_fingerprint
        or not isinstance(assessments, list)
        or not isinstance(final_decision, str)
        or not isinstance(rationale, str)
        or (deviation_rationale is not None and not isinstance(deviation_rationale, str))
    ):
        raise sbp_validation.ValidationOperationError("validation_outcome_invalid")
    safe, baseline = _validation_baseline(run_name, require_accepted=True)
    pilot = sbp_validation.load_current_pilot(baseline)
    result = sbp_validation.record_outcome(
        baseline.run_dir,
        registry_path=baseline.registry_path,
        expected_pilot_fingerprint=pilot_fingerprint,
        assessments=assessments,
        final_decision=final_decision,
        rationale=rationale,
        deviation_rationale=deviation_rationale,
        impact_confirmed=impact_confirmed is True,
        actor=_current_validation_actor(),
    )
    response = _validation_mutation_response(safe, result)
    if final_decision == "revise" and pilot is not None:
        response["to_be_revision"] = _enqueue_validation_revision(
            safe,
            baseline,
            pilot_version_ref=pilot.version_id,
            outcome_version_ref=result["version_id"],
        )
    return response


def _export_validation(run_name: object) -> dict:
    _safe, baseline = _validation_baseline(run_name, require_accepted=False)
    return sbp_validation.export_validation(
        baseline.run_dir, registry_path=baseline.registry_path
    )


def _validation_failure_response(
    run_name: object,
    exc: sbp_validation.ValidationOperationError,
    *,
    submitted: object = None,
) -> dict:
    response = _validation_error(exc)
    if submitted is not None:
        response["submitted"] = submitted
    try:
        response["validation"] = _validation_detail(run_name)
    except Exception:
        pass
    return response


def _run_to_be_revision(context: dict) -> None:
    try:
        cmd = [
            sys.executable, "-m", "sbp_gen", "to-be-revision",
            "--run-dir", str(context["run_dir"]),
            "--registry", str(context["registry_path"]),
            "--fingerprint", context["fingerprint"],
            "--instruction", context["instruction"],
        ]
        proc = child_processes.run(
            cmd,
            cwd=REPO / "tools",
            env=_execution_for_context(context).child_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        output = proc.stdout if proc.returncode == 0 else proc.stderr
        projection = json.loads(output.strip().splitlines()[-1])
        if not isinstance(projection, dict) or projection.get("state") not in {"done", "failed"}:
            raise ValueError("invalid revision worker result")
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="done" if proc.returncode == 0 and projection["state"] == "done" else "failed",
                phase=projection.get("phase"),
                result_endpoint=f"/api/to-be?run={quote(context['run'])}",
                error=projection.get("error"),
                revision_classification=projection.get("revision_classification"),
            )
    except Exception as exc:  # pragma: no cover - model/process boundary
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="failed",
                phase="synthesizing",
                error={
                    "code": "to_be_configuration_missing",
                    "user_message": sbp_to_be.ERROR_MESSAGES["to_be_configuration_missing"][0],
                },
            )
            _job["log"].append(f"[To-be revision failure: {type(exc).__name__}]")


def _enqueue_to_be_revision(
    run_name: object, *, fingerprint: object, instruction: object,
    execution: sbp_execution.ExecutionConfig | None = None,
) -> dict:
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ToBeRequestRejected("fingerprint 不可省略。")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ToBeRequestRejected("請輸入希望調整方案的具體說明。")
    safe, run_dir, _state = _research_run(run_name)
    baseline = _to_be_baseline(safe)
    current = sbp_to_be.resolve_current_to_be(baseline)
    if current.fingerprint != fingerprint:
        raise ToBeConflict(sbp_to_be.ERROR_MESSAGES["stale_to_be_fingerprint"][0])
    registry_path = baseline.registry_path
    with _job_lock:
        if _job.get("state") == "running":
            raise ToBeConflict("目前已有分析工作正在進行，完成後再試。")
        _replace_job(
            kind=sbp_to_be.OPERATION_KIND,
            state="running",
            phase="synthesizing",
            log=[],
            out=safe,
            output_identity=safe,
            result_endpoint=f"/api/to-be?run={quote(safe)}",
        )
    context = {
        "run": safe,
        "run_dir": run_dir,
        "registry_path": registry_path,
        "fingerprint": fingerprint,
        "instruction": " ".join(instruction.split()),
        "execution": _require_execution(execution),
    }
    threading.Thread(target=_run_to_be_revision, args=(context,), daemon=True).start()
    return {
        "kind": sbp_to_be.OPERATION_KIND,
        "state": "queued",
        "phase": "synthesizing",
        "retryable": False,
        "summary": "正在依調整說明建立完整替代方案。",
        "counts": {"proposals": 0, "blueprint_changes": 0},
        "result": {},
        "error": None,
    }


def _enqueue_to_be_retry(
    run_name: object, *, execution: sbp_execution.ExecutionConfig | None = None
) -> dict:
    """Retry one exact failed initial To-be request from server-owned state."""
    safe, run_dir, _state = _research_run(run_name)
    operation = sbp_to_be.load_current_operation(run_dir)
    if not isinstance(operation, dict) or operation.get("state") != "failed":
        raise ToBeRequestRejected("目前沒有失敗的提案可重新產生。")
    if not bool(operation.get("retryable")):
        raise ToBeRequestRejected("這次失敗必須先修正畫面指出的前置條件。")
    if ((operation.get("revision_context") or {}).get("kind") or "initial") != "initial":
        raise ToBeRequestRejected("方案調整失敗請沿用原調整說明重試。")
    research_operation = sbp_as_is_research.load_current_operation(run_dir)
    handoff = (
        research_operation.get("to_be_handoff")
        if isinstance(research_operation, dict) else None
    )
    if not isinstance(handoff, dict):
        raise ToBeRequestRejected("目前調研結果沒有可安全重用的方案產生交接。")
    baseline = _to_be_baseline(safe)
    with _job_lock:
        if _job.get("state") == "running":
            raise ToBeConflict("目前已有分析工作正在進行，完成後再試。")
        _replace_job(
            kind=sbp_to_be.OPERATION_KIND,
            state="running",
            phase="synthesizing",
            log=[],
            out=safe,
            output_identity=safe,
            result_endpoint=f"/api/to-be?run={quote(safe)}",
        )
    threading.Thread(
        target=_run_to_be_handoff_cli,
        args=({"run": safe, "baseline": baseline, "handoff": handoff,
               "execution": _require_execution(execution)},),
        daemon=True,
    ).start()
    return {
        "kind": sbp_to_be.OPERATION_KIND,
        "state": "queued",
        "phase": "synthesizing",
        "retryable": False,
        "summary": "正在沿用目前已確認資料重新產生完整方案。",
        "counts": {"proposals": 0, "blueprint_changes": 0},
        "result": {},
        "error": None,
    }


def _build_as_is_research_cmd(baseline) -> list[str]:
    return [
        sys.executable,
        "-m",
        "sbp_gen",
        "as-is-research",
        "--run-dir",
        str(baseline.run_dir),
        "--registry",
        str(baseline.registry_path),
        "--as-is-version",
        baseline.as_is_version_id,
    ]


def _build_to_be_cmd(baseline, handoff: dict) -> list[str]:
    """Build the private server-to-worker handoff; browsers never supply it."""
    return [
        sys.executable,
        "-m",
        "sbp_gen",
        "to-be",
        "--run-dir",
        str(baseline.run_dir),
        "--registry",
        str(baseline.registry_path),
        "--handoff",
        json.dumps(handoff, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ]


def _run_to_be_handoff_cli(context: dict) -> None:
    """Run one server-owned handoff without changing the completed research."""
    try:
        handoff = context["handoff"]
        baseline = sbp_to_be.resolve_baseline(
            context["baseline"].run_dir,
            registry_path=context["baseline"].registry_path,
            expected_versions={
                "as_is_graph": handoff["as_is_version_id"],
                "issue_package": handoff["issue_version_id"],
                "research_catalog": handoff["research_version_id"],
            },
        )
        if baseline.improvement_goal_digest != handoff.get("improvement_goal_digest"):
            raise sbp_to_be.ToBeOperationError("to_be_baseline_not_current")
    except (KeyError, TypeError, sbp_to_be.ToBeOperationError) as exc:
        error = (
            exc
            if isinstance(exc, sbp_to_be.ToBeOperationError)
            else sbp_to_be.ToBeOperationError("missing_to_be_baseline", detail=exc)
        )
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="failed",
                phase="synthesizing",
                error={"code": error.code, "user_message": error.user_message},
            )
        return
    cmd = _build_to_be_cmd(context["baseline"], context["handoff"])
    try:
        proc = child_processes.spawn(
            cmd,
            cwd=REPO / "tools",
            env=_execution_for_context(context).child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), _execution_for_context(context)))
        code = proc.wait()
        operation = sbp_to_be.load_current_operation(context["baseline"].run_dir)
        projection = (
            sbp_to_be.project_operation(operation)
            if operation is not None
            else {
                "state": "failed",
                "phase": "synthesizing",
                "error": {
                    "code": "to_be_configuration_missing",
                    "user_message": sbp_to_be.ERROR_MESSAGES["to_be_configuration_missing"][0],
                },
            }
        )
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="done" if code == 0 else "failed",
                phase=projection.get("phase"),
                result_endpoint=f"/api/to-be?run={quote(context['run'])}",
                error=projection.get("error"),
            )
    except Exception as exc:  # pragma: no cover - process boundary
        with _job_lock:
            _job.update(
                kind=sbp_to_be.OPERATION_KIND,
                state="failed",
                phase="synthesizing",
                error={
                    "code": "to_be_configuration_missing",
                    "user_message": sbp_to_be.ERROR_MESSAGES["to_be_configuration_missing"][0],
                },
            )
            _job["log"].append(f"[To-be worker failure: {type(exc).__name__}]")


def _run_as_is_research_cli(context: dict) -> None:
    cmd = _build_as_is_research_cmd(context["baseline"])
    try:
        proc = child_processes.spawn(
            cmd,
            cwd=REPO / "tools",
            env=_execution_for_context(context).child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), _execution_for_context(context)))
        code = proc.wait()
        try:
            projection = _as_is_research_projection(context["run"])
        except AsIsResearchPrerequisiteRejected:
            projection = sbp_as_is_research.project_operation(context["operation"])
        operation = sbp_as_is_research.load_current_operation(context["baseline"].run_dir)
        handoff = operation.get("to_be_handoff") if isinstance(operation, dict) else None
        should_handoff = (
            code == 0
            and projection.get("state") == "done"
            and projection.get("phase") == "research_ready"
            and isinstance(handoff, dict)
            # start-external-research-stop-at-review: a fresh-path run stops at
            # the research review; To-be is a separate later decision.
            and not _journey_is_candidate_path(Path(context["baseline"].run_dir))
        )
        with _job_lock:
            _job.update(
                state="running" if should_handoff else ("done" if code == 0 else "failed"),
                phase=projection.get("phase"),
                request_digest=projection.get("request_digest"),
                result_endpoint=f"/api/as-is-research?run={quote(context['run'])}",
                error=projection.get("error"),
            )
        if should_handoff:
            _run_to_be_handoff_cli({
                "run": context["run"],
                "baseline": context["baseline"],
                "handoff": handoff,
                "execution": _execution_for_context(context),
            })
    except Exception as exc:  # pragma: no cover - process boundary
        with _job_lock:
            _job.update(
                state="failed",
                phase="researching",
                error={
                    "code": "research_configuration_missing",
                    "user_message": sbp_as_is_research.ERROR_MESSAGES["research_configuration_missing"][0],
                },
            )
            _job["log"].append(f"[research worker failure: {type(exc).__name__}]")


def _prepare_fresh_research_start(run_name: object) -> None:
    """start-external-research-stop-at-review: before a fresh-path run starts
    research, require a current confirmed As-is and a confirmed operative goal
    and refuse a busy queue -- all before any write -- then pin the research
    baseline with the existing registry adoption (a no-op when one exists).
    Older runs go straight to the existing enqueue."""
    safe = _safe_component(run_name or "", "") if isinstance(run_name, str) else ""
    run_dir = RUNS_DIR / safe if safe else None
    if run_dir is None or not _journey_is_candidate_path(run_dir):
        return
    gate = _improvement_goal_gate(run_dir)
    if gate is None or not gate["as_is_current"]:
        raise AsIsResearchConflict("SBP 還沒確認目前這一版，請先回到服務藍圖確認。")
    if not _journey_uses_solution_selection(run_dir) and not gate["confirmed_goal"]:
        raise AsIsResearchConflict("改善目標還沒確認，請先確認改善目標。")
    with _job_lock:
        if _job.get("state") == "running":
            raise AsIsResearchConflict(f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。")
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    if _adopt_run_into_registry(run_dir, state) is None and not isinstance(state.get("artifact_versions"), dict):
        try:
            _adoptable_run_content(run_dir, state)
        except RunAdoptionRejected as exc:
            raise AsIsResearchPrerequisiteRejected(str(exc)) from exc


def _enqueue_as_is_research(
    run_name: object, *, execution: sbp_execution.ExecutionConfig | None = None
) -> tuple[int, dict]:
    """Prepare one replay-safe operation under the process-wide job lock."""
    safe, _run_dir, _state = _research_run(run_name)
    baseline = _research_baseline(safe, require_reviewed=True)
    digest = sbp_as_is_research.request_digest(
        baseline, model_policy=sbp_as_is_research.DEFAULT_MODEL_POLICY
    )
    with _job_lock:
        current = sbp_as_is_research.load_current_operation(baseline.run_dir)
        if (
            current
            and current.get("request_digest") == digest
            and current.get("state") == "done"
            and current.get("phase") in sbp_as_is_research.TERMINAL_PHASES
        ):
            return 200, sbp_as_is_research.project_operation(current)
        if _job.get("state") == "running":
            if (
                _job.get("kind") == sbp_as_is_research.OPERATION_KIND
                and _job.get("request_digest") == digest
                and current is not None
            ):
                return 202, sbp_as_is_research.project_operation(current)
            raise AsIsResearchConflict("目前已有另一項分析工作正在進行，完成後再試。")
        try:
            operation, _reused = sbp_as_is_research.prepare_operation(
                baseline,
                model_policy=sbp_as_is_research.DEFAULT_MODEL_POLICY,
            )
        except sbp_as_is_research.ResearchOperationConflict as exc:
            raise AsIsResearchConflict(exc.user_message) from exc
        _replace_job(
            kind=sbp_as_is_research.OPERATION_KIND,
            state="running",
            phase=operation.get("phase"),
            log=[],
            out=safe,
            output_identity=safe,
            result_endpoint=f"/api/as-is-research?run={quote(safe)}",
            request_digest=operation.get("request_digest"),
        )
    context = {
        "run": safe,
        "baseline": baseline,
        "operation": operation,
        "execution": _require_execution(execution),
    }
    threading.Thread(target=_run_as_is_research_cli, args=(context,), daemon=True).start()
    return 202, sbp_as_is_research.project_operation(operation)


@_owns_project_lock(_named_run_dir)
def _mark_run_reviewed(run_name: str | None, review_state: str | None, reviewer: object = "") -> dict:
    """更新一個 run 的 `review_state`；只動這一個欄位與下面這兩個附屬欄位，不碰
    `generation_state`／`origin`／`quality_state`／`reference_state`
    （stabilize-sbp-task-flow-and-run-trust design.md 決定三）。

    只開放 `"reviewed"`／`"unreviewed"` 這兩個方向（allow-unmarking-reviewed-runs
    決定一）——研究者可以標記，也可以取消標記；`"in_review"` 沒有可靠的觸發點，不開放。

    標記為 `"reviewed"` 時一併寫入 `reviewed_artifact_fingerprint`（當下的
    `_run_fingerprint()`）與 `reviewer`（可為空字串）；改回 `"unreviewed"` 時
    連同 `reviewed_at` 一起清除這兩個欄位——前端用 `reviewed_artifact_fingerprint`
    跟 run 當前的 fingerprint 比對，決定要顯示「已確認」還是「確認已失效」
    （tier-blueprint-review-status-disclosure design.md 決定九）。
    """
    if review_state not in ("reviewed", "unreviewed"):
        raise ReviewStateRejected(
            f"review_state 只接受 'reviewed' 或 'unreviewed'，收到 {review_state!r}"
        )
    safe_name = _safe_component(run_name or "", "")
    run_dir = RUNS_DIR / safe_name if safe_name else None
    run_json_path = run_dir / "run.json" if run_dir else None
    if not safe_name or not run_json_path.exists():
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    state = sbp_run_files.read_state(run_json_path)
    state["review_state"] = review_state
    if review_state == "reviewed":
        state["reviewed_at"] = dt.datetime.now().isoformat(timespec="seconds")
        state["reviewed_artifact_fingerprint"] = _run_fingerprint(run_dir)
        state["reviewer"] = reviewer.strip() if isinstance(reviewer, str) else ""
        # 確認鍵不再被檢查結果鎖住，所以「確認的當下還有什麼沒解決」必須留下來，
        # 否則軟性提醒會連硬性關卡至少隱含過的那點資訊都丟掉。存的是畫面上研究者
        # 實際讀到的那份文字，不是驗證器的原始輸出。
        outstanding = _run_validity_findings(run_dir)
        if outstanding:
            state["reviewed_with_open_findings"] = outstanding
        else:
            state.pop("reviewed_with_open_findings", None)
    else:
        state.pop("reviewed_at", None)
        state.pop("reviewed_with_open_findings", None)
        state.pop("reviewed_artifact_fingerprint", None)
        state.pop("reviewer", None)
    _write_json_atomic(run_json_path, state)
    return {"ok": True}


class CellCoverageRejected(Exception):
    pass


RESEARCHER_ASSIGNABLE_COVERAGE_STATES = ("confirmed_absent", "not_applicable")


def _set_cell_coverage(run_name: str | None, step: object, layer: object, coverage_state: str | None,
                        absence_evidence_ref: str | None = None) -> dict:
    """研究者把一個空格標成「本來就不會發生」或「不適用於這個服務」，寫回該
    run 的 `sbp_draft.yaml`（surface-blank-cell-meaning 決定三）。系統本身
    的投影邏輯永遠不會自己指派這兩個狀態——這是研究裁定，不是算得出來的事實。

    只動目標格子的 `coverage_state`／`blank`／`absence_evidence_ref`，
    既有的 `blank_note` 原樣保留（schema 要求空格必須有 blank_note，這一步
    不重新生成，沿用投影當時寫的那一句）。
    """
    if coverage_state not in RESEARCHER_ASSIGNABLE_COVERAGE_STATES:
        raise CellCoverageRejected(
            f"coverage_state 只接受 {RESEARCHER_ASSIGNABLE_COVERAGE_STATES}，收到 {coverage_state!r}"
        )
    if not isinstance(step, int) or not isinstance(layer, str) or not layer:
        raise CellCoverageRejected("step 必須是整數、layer 必須是非空字串")
    safe_name = _safe_component(run_name or "", "")
    run_dir = RUNS_DIR / safe_name if safe_name else None
    draft_path = run_dir / "sbp_draft.yaml" if run_dir else None
    if not safe_name or not draft_path.exists():
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    if coverage_state == "confirmed_absent" and not (isinstance(absence_evidence_ref, str) and absence_evidence_ref.strip()):
        raise CellCoverageRejected("標記「本來就不會發生」需要先選一條證據佐證")
    draft = load_safe_yaml(draft_path.read_text(encoding="utf-8")) or {}
    cell = next((c for c in draft.get("cells") or [] if c.get("step") == step and c.get("layer") == layer), None)
    if cell is None:
        raise ReviewRunNotFound(f"run {run_name} 沒有 step={step} layer={layer} 這一格")
    if cell.get("node_refs"):
        raise CellCoverageRejected("這一格已經有內容，不是空格，不能標記")
    cell["coverage_state"] = coverage_state
    if coverage_state == "confirmed_absent":
        cell["blank"] = "confirmed_absent"
        cell["absence_evidence_ref"] = absence_evidence_ref
    else:
        cell.pop("blank", None)
        cell.pop("absence_evidence_ref", None)
    draft_path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "step": step, "layer": layer, "coverage_state": coverage_state}


class MuteInferenceFlagRejected(Exception):
    pass


def _set_muted_inference_flag(run_name: str | None, node_id: object, muted: object) -> dict:
    """研究者確認過一張卡片的「無證據推論」徽章之後，可以把它設成靜音，不再
    出現在畫布上（simplify-inference-markers-and-coverage-states 決定一）。
    系統本身不會自動靜音任何徽章，這是研究裁定，寫回該 run 的
    `sbp_draft.yaml` 的 `graph.nodes[].muted_inference_flag`。
    """
    if not isinstance(node_id, str) or not node_id:
        raise MuteInferenceFlagRejected("node_id 必須是非空字串")
    if not isinstance(muted, bool):
        raise MuteInferenceFlagRejected("muted 必須是布林值")
    safe_name = _safe_component(run_name or "", "")
    run_dir = RUNS_DIR / safe_name if safe_name else None
    draft_path = run_dir / "sbp_draft.yaml" if run_dir else None
    if not safe_name or not draft_path.exists():
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    draft = load_safe_yaml(draft_path.read_text(encoding="utf-8")) or {}
    node = next((n for n in (draft.get("graph") or {}).get("nodes") or [] if n.get("id") == node_id), None)
    if node is None:
        raise ReviewRunNotFound(f"run {run_name} 沒有 node_id={node_id} 這個節點")
    if muted:
        node["muted_inference_flag"] = True
    else:
        node.pop("muted_inference_flag", None)
    draft_path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "node_id": node_id, "muted": muted}


def _set_muted_inference_relation(run_name: str | None, relation_id: object, muted: object) -> dict:
    """研究者確認過一條 AI 推論關係之後，可以把它單獨標成靜音（不影響同一張
    卡上其他關係、也不影響 claim 層級的 muted_inference_flag），寫回該 run 的
    `sbp_draft.yaml` 的 `relations[].muted`（simplify-inference-card-presentation
    決定一：每條關係各自記錄，不是整張卡一個開關）。
    """
    if not isinstance(relation_id, str) or not relation_id:
        raise MuteInferenceFlagRejected("relation_id 必須是非空字串")
    if not isinstance(muted, bool):
        raise MuteInferenceFlagRejected("muted 必須是布林值")
    safe_name = _safe_component(run_name or "", "")
    run_dir = RUNS_DIR / safe_name if safe_name else None
    draft_path = run_dir / "sbp_draft.yaml" if run_dir else None
    if not safe_name or not draft_path.exists():
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    draft = load_safe_yaml(draft_path.read_text(encoding="utf-8")) or {}
    relation = next((r for r in draft.get("relations") or [] if r.get("id") == relation_id), None)
    if relation is None:
        raise ReviewRunNotFound(f"run {run_name} 沒有 relation_id={relation_id} 這條關係")
    if muted:
        relation["muted"] = True
    else:
        relation.pop("muted", None)
    draft_path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "relation_id": relation_id, "muted": muted}


class VersionSaveRejected(Exception):
    pass


class SavedVersionNotFound(Exception):
    pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode_data_url(value: object, expected: str) -> bytes:
    if not isinstance(value, str) or not value.startswith(expected + ";base64,"):
        raise VersionSaveRejected(f"儲存版本需要完整的 {expected.split('/')[1].upper()} 匯出檔")
    try:
        return base64.b64decode(value.split(",", 1)[1], validate=True)
    except Exception as exc:
        raise VersionSaveRejected("匯出檔格式不正確，請重新儲存版本") from exc


@_owns_project_lock(_named_run_dir)
def _save_reviewed_version(run_name: str | None, name: object, *, notes: object = "", reviewer: object = "",
                          svg_data: object = None, png_data: object = None) -> dict:
    """Create an immutable, atomically-published version bundle for one run."""
    safe_run = _safe_component(run_name or "", "")
    version_name = str(name or "").strip()
    if not safe_run or not version_name:
        raise VersionSaveRejected("請為這個版本填寫名稱")
    if len(version_name) > 120:
        raise VersionSaveRejected("版本名稱不能超過 120 個字")
    run_dir = RUNS_DIR / safe_run
    draft = run_dir / "sbp_draft.yaml"
    journey = run_dir / "journey.yaml"
    run_json = run_dir / "run.json"
    if not all(path.exists() for path in (draft, journey, run_json)):
        raise ReviewRunNotFound(f"找不到可儲存的完整產出：{run_name}")
    svg = _decode_data_url(svg_data, "image/svg+xml")
    png = _decode_data_url(png_data, "image/png")
    if not svg or not png:
        raise VersionSaveRejected("匯出檔是空的，請重新儲存版本")
    state = json.loads(run_json.read_text(encoding="utf-8"))
    version_id = f"v-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    versions_root = run_dir / "versions"
    target = versions_root / version_id
    temp = run_dir / f".version-{uuid.uuid4().hex}"
    temp.mkdir(parents=True, exist_ok=False)
    try:
        copied = [draft, journey, run_json]
        briefing = run_dir / "briefing.yaml"
        if briefing.exists():
            copied.append(briefing)
        for source in copied:
            shutil.copy2(source, temp / source.name)
        (temp / "service-blueprint.svg").write_bytes(svg)
        (temp / "service-blueprint.png").write_bytes(png)
        files = [{"name": path.name, "sha256": _sha256_file(path)} for path in sorted(temp.iterdir()) if path.is_file()]
        artifact_digest = hashlib.sha256("".join(item["sha256"] for item in files).encode("ascii")).hexdigest()
        manifest = {
            "schema_version": 1, "id": version_id, "name": version_name, "source_run": safe_run,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "state": "reviewed" if state.get("review_state") == "reviewed" else "saved",
            "journey_digest": state.get("confirmed_journey_digest") or _sha256_file(journey),
            "artifact_digest": artifact_digest, "files": files,
        }
        if isinstance(notes, str) and notes.strip():
            manifest["notes"] = notes.strip()
        if isinstance(reviewer, str) and reviewer.strip():
            manifest["reviewer"] = reviewer.strip()
        errors = V.structural_errors(manifest, V.load_schema("reviewed-blueprint-version"))
        if errors:
            raise VersionSaveRejected("版本資料驗證失敗：" + "；".join(errors))
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        versions_root.mkdir(exist_ok=True)
        os.replace(temp, target)
        return {"ok": True, "id": version_id, "name": version_name, "state": manifest["state"], "created_at": manifest["created_at"]}
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        raise


def _discover_versions(run_name: str | None) -> list[dict]:
    safe_run = _safe_component(run_name or "", "")
    root = RUNS_DIR / safe_run / "versions"
    if not safe_run or not root.exists():
        return []
    versions = []
    for path in sorted(root.iterdir(), reverse=True):
        manifest = path / "manifest.json"
        if not manifest.exists():
            continue
        try:
            versions.append(json.loads(manifest.read_text(encoding="utf-8")))
        except Exception:
            continue
    return versions


def _saved_version_dir(run_name: str | None, version_id: str | None) -> tuple[Path, dict]:
    """Resolve a published immutable snapshot and verify its inventory.

    A snapshot is a read-only deliverable, not another mutable run.  Every
    access goes through this boundary so an accidental or manual edit cannot
    silently be presented as the version the researcher saved.
    """
    safe_run = _safe_component(run_name or "", "")
    safe_version = _safe_component(version_id or "", "")
    root = (RUNS_DIR / safe_run / "versions").resolve()
    path = (root / safe_version).resolve()
    if not safe_run or not safe_version or root not in path.parents:
        raise SavedVersionNotFound("找不到已儲存版本")
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise SavedVersionNotFound("找不到已儲存版本")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SavedVersionNotFound("已儲存版本無法讀取") from exc
    if manifest.get("source_run") != safe_run or manifest.get("id") != safe_version:
        raise SavedVersionNotFound("已儲存版本資料不一致")
    if V.structural_errors(manifest, V.load_schema("reviewed-blueprint-version")):
        raise SavedVersionNotFound("已儲存版本資料無法驗證")
    for item in manifest.get("files") or []:
        file_name = item.get("name")
        stored = path / str(file_name or "")
        if not stored.is_file() or _sha256_file(stored) != item.get("sha256"):
            raise SavedVersionNotFound("已儲存版本檔案已變更或不完整")
    return path, manifest


def _analyse_saved_version(run_name: str | None, version_id: str | None) -> dict:
    path, manifest = _saved_version_dir(run_name, version_id)
    view = _analyse(path)
    view["viewing_version"] = {
        key: manifest.get(key) for key in ("id", "name", "created_at", "state", "reviewer", "notes", "source_run")
        if key in manifest
    }
    view["read_only"] = True
    return view


def _saved_version_export(run_name: str | None, version_id: str | None, extension: str) -> tuple[bytes, str, str]:
    """Return an export as saved, never a render of the current draft."""
    if extension not in ("svg", "png"):
        raise SavedVersionNotFound("不支援的匯出格式")
    path, manifest = _saved_version_dir(run_name, version_id)
    filename = f"service-blueprint.{extension}"
    inventory = {item.get("name"): item.get("sha256") for item in manifest.get("files") or []}
    target = path / filename
    if not target.is_file() or inventory.get(filename) != _sha256_file(target):
        raise SavedVersionNotFound("已儲存版本的匯出檔無法驗證")
    human = _safe_component(str(manifest.get("name") or "service-blueprint"), "service-blueprint")
    return target.read_bytes(), ("image/svg+xml" if extension == "svg" else "image/png"), f"{human}-service-blueprint.{extension}"


def _resolve_steps(case_id: str | None, cells: list[dict]) -> tuple[list[dict], str]:
    """把 draft 的步驟編號換成歷程名稱（ADR-0005 決定六）。

    以 meta.case_id 去比對 cases/**/case.yaml。比對不到、或名稱數量與 draft 的
    步驟數對不上，就退回「步驟 N」並回報 steps_source="fallback"——這是要被
    看見的降級，不是靜默 fallback（設計文件 Implementation Contract）。
    """
    step_nos = sorted({c["step"] for c in cells if c.get("step") is not None})
    if case_id:
        for c in _discover_cases():
            candidate_steps = c["steps"] if len(c["steps"]) == len(step_nos) else c.get("legacy_steps", [])
            if c["case_id"] == case_id and len(candidate_steps) == len(step_nos) and step_nos:
                return (
                    [{"no": no, "name": name} for no, name in zip(step_nos, candidate_steps)],
                    "case",
                )
    return [{"no": no, "name": f"步驟 {no}"} for no in step_nos], "fallback"


# 每個研究缺口 type 對應的人話標題——不能直接顯示 g["type"] 這個內部列舉值原文
# （add-research-briefing-layer 決定：boilerplate 清單不能再用內部詞彙當標題）。
GAP_TYPE_TITLES = {"expected_unverified": "待驗證的服務環節", "unserved_pain_point": "沒有服務動作處理的顧客痛點"}


def _todo_item(gap: dict) -> dict:
    """單一研究缺口的確認清單項目——`cost` 一定要帶這個缺口自己的內容（角色、
    問題文字），不能是套在每一項上的同一句話，那句話已經被拿掉了。"""
    question = gap.get("question", "")
    kind = gap.get("type")
    if kind == "expected_unverified":
        roles = "、".join(gap.get("ask_roles") or []) or "相關角色"
        cost = f"目前只有推論，還沒有{roles}針對「{question}」提供第一手資料佐證"
    elif kind == "unserved_pain_point":
        cost = f"{question}——目前沒有找到對應的服務動作處理它"
    else:
        cost = question or "這項研究缺口目前沒有更多說明"
    return {
        "kind": "gap",
        "anchor": f"{gap.get('step') or 1}|{gap.get('layer') or '顧客行動'}",
        "title": GAP_TYPE_TITLES.get(kind, "研究缺口"),
        "why": question,
        "cost": cost,
    }


def evidence_roles(evidence: dict) -> set[str]:
    """證據池裡出現過的 `role` 字串集合——判定第一手的第一段訊號來源。

    受控欄位的集合成員判定，不是自由文字比對（distinguish-first-hand-from
    -reported-claims design.md 決定一）。證據池為空時回傳空集合。
    """
    return {e.get("role") for e in (evidence or {}).values() if e.get("role")}


_PLACEHOLDER_LOCATOR_RE = re.compile(r"^(?:[QA]\s*[:：]\s*)?(?:none|null|n/a|na)\b", re.IGNORECASE)


def _drop_placeholder_locator(value: str) -> str:
    """align.md 讓模型自由決定 locator 的格式（時間戳、題目名稱⋯都可以），
    模型有時會用「Q: <題目>」這種慣例引用逐字稿的訪談問題；抓不到對應問題時
    有時會寫成英文占位字 "Q: None" 而不是留空（browser_jargon_leak_smoke.py
    早就把 "Q: None" 列為禁止出現的字面，但這裡是第一個真正擋住它的地方）。
    只清掉「開頭就是占位字」（不管有沒有 Q:／A: 前綴、後面還有沒有其他文
    字），不影響「Q: 搭車追蹤」這種抓到真題目的情況。
    """
    return "" if _PLACEHOLDER_LOCATOR_RE.match(value.strip()) else value


# 證據自己記著它是「顧客那邊」還是「第一線／後台人員」講的（evidence.schema.json
# 的 tier 欄位）。生成端的 validator 就是拿這個欄位判斷一個前台／後台動作的行為者
# 到底有沒有被觀察到——沒有任何一筆後台證據時，模型很容易自己發明一個行為者
# （2026-08-29 那份 MUJI 產出就是這樣長出「門市現場指示系統」的）。
#
# 這裡只算「整個證據池是哪一側的」，不把 tier 掛到每一則 claim 上：實測 MUJI 那份
# 73 筆證據全是顧客側，逐卡片標示會讓 24 張卡印出 24 次同一句話。那是案例的性質，
# 不是卡片的性質，所以在案例層級講一次。
#
# 對外一律用「顧客這一側／店家這一側」的說法，不用 frontstage／backstage：那是
# schema 列舉值（畫面禁用），而且跟藍圖自己的泳道名稱、卡片高度的 tier 詞彙都撞名。
_EVIDENCE_SIDE_BY_TIER = {
    "frontstage": "customer_side",
    "backstage": "provider_side",
    "interpretation": "derived_interpretation",
}


def evidence_base_summary(evidence: dict) -> dict:
    """Count which side of the service the evidence pool came from.

    Records whose tier is missing or unrecognised are counted in `total` but in
    none of the three sides, so "nobody classified this pool" stays
    distinguishable from "this pool is empty" and from "this pool is one-sided".
    Both of those produce no researcher-facing claim about composition.
    """
    counts = {"customer_side": 0, "provider_side": 0, "derived_interpretation": 0}
    records = []
    for ref, record in (evidence or {}).items():
        side = _EVIDENCE_SIDE_BY_TIER.get(str((record or {}).get("tier") or "").strip())
        if not side:
            continue
        counts[side] += 1
        records.append({
            "id": ref,
            "side": side,
            "role": str((record or {}).get("role") or "").strip(),
            "locator": _drop_placeholder_locator(str((record or {}).get("locator") or "").strip()),
        })
    return {**counts, "classified": len(records), "total": len(evidence or {}), "records": records}


def _question_context_field(record: dict | None) -> dict:
    """The stored interviewer question as a view field, only when there is one.

    A short answer ("對啊") is readable only next to the question it answers.
    The key is left out entirely when the stored record carries no question,
    so a card never renders a blank or invented question line.
    """
    question = str((record or {}).get("question_context") or "").strip()
    return {"question_context": question} if question else {}


def _evidence_provenance(record: dict, evidence_ref: str | None = None) -> dict:
    """Translate evidence metadata into a locator a researcher can revisit.

    Old evidence files only have `source` and `locator`, so this adapter is
    deliberately conservative: absent metadata stays absent rather than
    turning a free-text locator into fabricated page or paragraph data.
    """
    record = record or {}
    source_file = _safe_component(str(record.get("source_file") or record.get("file") or ""), "")
    title = str(record.get("document_title") or record.get("source_title") or record.get("title") or record.get("source") or "").strip()
    fallback = not bool(title)
    if not title:
        title = source_file or "來源資訊不完整"
    return {
        "document_title": title,
        "participant_or_role": str(record.get("participant") or record.get("role") or "").strip(),
        "section": _drop_placeholder_locator(str(record.get("section") or "").strip()),
        "page": record.get("page"),
        "paragraph": record.get("paragraph"),
        "locator": _drop_placeholder_locator(str(record.get("locator") or "").strip()),
        "source_file": source_file,
        "quote": str(record.get("text") or "").strip(),
        "incomplete": fallback,
        "internal_id": evidence_ref or record.get("id") or "",
    }


def claim_attribution(node: dict, claim: dict, roles: set[str], evidence: dict) -> dict:
    """一句 claim 是當事人自己說的、還是別人轉述的——確定性判定，不呼叫模型。

    兩段式，強訊號優先（design.md 決定一）：
    第一段只在 `actor_boundary == "customer"` 時適用——顧客是具體、可受訪的
    個人角色，`actor` 不在證據池角色集合中就足以確定沒有第一手說法。非顧客
    的聚合／組織角色（機構、供應商等）不是一種可能出現在 `evidence[].role`
    裡的人格，這條捷徑對它沒有判定力，一律落入第二段。
    第二段比對 `claim.speaker` 與 `actor` 的字面：對得上判第一手，對不上判
    無法判定——絕不因為對不上就回頭判定轉述，猜錯的方向不對稱。
    """
    actor = node.get("actor")
    speaker = claim.get("speaker")
    if not actor:
        return {"state": "undetermined", "speaker": speaker, "actor": actor,
                "reason": "沒有記錄這個動作是誰做的，無法判斷是不是當事人自己說的"}
    if not speaker:
        return {"state": "undetermined", "speaker": speaker, "actor": actor,
                "reason": "沒有記錄這句話是誰說的，無法判斷是不是當事人自己說的"}
    if node.get("actor_boundary") == "customer" and actor not in roles:
        return {"state": "reported", "speaker": speaker, "actor": actor,
                "reason": f"證據池裡沒有「{actor}」這個角色的第一手說法"}
    ev = (evidence or {}).get(claim.get("evidence_ref"), {}) or {}
    ev_role = ev.get("role")
    if actor in speaker or (ev_role and ev_role == actor):
        return {"state": "first_hand", "speaker": speaker, "actor": actor,
                "reason": f"說話者與「{actor}」對得上，判定為當事人自己說的"}
    return {"state": "undetermined", "speaker": speaker, "actor": actor,
            "reason": f"「{actor}」與說話者的身分對不上，也無法從證據角色確認，無法判定是不是當事人自己說的"}


# add-channels-to-journey-map: the eight channels nodes record, folded into
# the four A0 presents (openspec/LANGUAGE.md's table, 2026-09-05/09 decision).
# Folding happens here, at projection time, never in storage.
CHANNEL_FOLD_TO_PRESENTED = {
    "physical_service": "people",
    "physical_communication": "people",
    "physical_environment": "physical_environment",
    "physical_product": "physical_environment",
    "digital_product": "digital_tools",
    "digital_environment": "digital_environments",
    "digital_service": "digital_environments",
    "digital_communication": "digital_environments",
}
PRESENTED_CHANNEL_IDS = ("people", "physical_environment", "digital_tools", "digital_environments")
# schema/evidence.schema.json (align's own output) and schema/journey.schema.json's
# step definition both lack any field for these -- verified absences, not
# assumptions (see design.md's "實作後補記"). Named once here so the
# response and any future caller agree on where the gap sits.
UNCOLLECTED_UPSTREAM_STAGE = "align"


def _project_journey_channel_map(nodes: list, annotations: list, step_index_by_ref: dict, step_count: int) -> dict:
    """Fold node/annotation channel values into a step-by-presented-channel
    grid. Pure and read-only: takes already-loaded data, returns plain
    dicts, never touches disk and never returns a node, annotation, or
    evidence identifier -- only labels and states.

    `assessed` is False when no node carries a channel field at all (a run
    from before this capability existed); the caller renders "this analysis
    did not assess channels" instead of an all-empty grid in that case.
    """
    assessed = any(isinstance(n, dict) and n.get("channel") for n in nodes or [])
    content: dict[tuple[int, str], list[str]] = {}
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        channel = node.get("channel")
        step = node.get("step")
        presented = CHANNEL_FOLD_TO_PRESENTED.get(channel)
        if presented is None or not isinstance(step, int):
            continue
        content.setdefault((step, presented), []).append(node.get("label") or "")

    absence: dict[tuple[int, str], list[str]] = {}
    for annotation in annotations or []:
        if not isinstance(annotation, dict) or annotation.get("type") != "channel_absence":
            continue
        presented = CHANNEL_FOLD_TO_PRESENTED.get(annotation.get("channel"))
        step = step_index_by_ref.get(annotation.get("step_ref"))
        if presented is None or step is None:
            continue
        absence.setdefault((step, presented), []).append(annotation.get("text") or "")

    rows = {}
    for presented in PRESENTED_CHANNEL_IDS:
        cells = []
        for step in range(1, step_count + 1):
            labels = content.get((step, presented)) or []
            missing = absence.get((step, presented)) or []
            # A presented row folds two or three of the eight channels
            # together, so a cell can genuinely hold both at once -- e.g.
            # a staff member attended (physical_service, content) while
            # nobody explained the process (physical_communication,
            # absent), both folding into "people" at the same step.
            # Preferring one would silently drop the other.
            state = "content" if labels else ("absent" if missing else "empty")
            cells.append({"step": step, "state": state, "labels": labels, "absent_labels": missing})
        rows[presented] = cells
    return {"assessed": assessed, "rows": rows}


JOURNEY_MAP_NOT_CHOSEN_YET = "尚未選定"


def _journey_map_run_dir(run_name: object) -> Path:
    safe_name = _safe_component(str(run_name or ""), "")
    if not safe_name:
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    run_dir = RUNS_DIR / safe_name
    if not (run_dir / "service_graph.yaml").is_file():
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    return run_dir


def _journey_canonical_document(run_dir: Path) -> dict:
    """Load the lifecycle-current Journey used by Branch interactions.

    The channel map predates Branch v3 and historically read the Journey
    embedded in ``sbp_draft.yaml``.  Branch reads and writes must use the
    canonical Journey file instead; the draft remains the source for the
    existing graph/channel cells only.
    """
    journey_path = run_dir / "journey.yaml"
    if journey_path.is_file():
        value = _load(journey_path) or {}
        if not isinstance(value, dict):
            raise JudgementRejected("目前的 Journey 格式無法呈現。")
        return value
    draft = _load(run_dir / "sbp_draft.yaml") if (run_dir / "sbp_draft.yaml").is_file() else {}
    value = draft.get("journey") if isinstance(draft, dict) else {}
    return value if isinstance(value, dict) else {}


def _journey_branch_views(journey: dict, evidence_by_id: dict[str, dict] | None) -> list[dict]:
    """Researcher-safe Branch read projection for the Journey station."""
    branches = []
    for branch in journey.get("branches") or []:
        if not isinstance(branch, dict):
            continue
        steps = []
        for step in branch.get("steps") or []:
            if not isinstance(step, dict):
                continue
            refs = [ref for ref in (step.get("evidence_refs") or []) if ref]
            evidence = []
            unresolved = 0
            for ref in refs:
                record = evidence_by_id.get(ref) if evidence_by_id else None
                if not record:
                    unresolved += 1
                    continue
                evidence.append({
                    "evidence_ref": ref,
                    "role": record.get("role") or record.get("participant_role") or "研究來源",
                    "locator": record.get("locator") or "",
                    "text": record.get("text") or record.get("excerpt") or "",
                })
            steps.append({
                "id": step.get("id") or "",
                "label": step.get("label") or "",
                "description": step.get("description") or "",
                "provenance": step.get("provenance"),
                "evidence": evidence,
                "unresolved_evidence_count": unresolved,
            })
        branches.append({
            "branch_id": branch.get("branch_id") or "",
            "label": branch.get("label") or "條件分支",
            "divergence_step_id": branch.get("divergence_step_id") or "",
            "merge_step_id": branch.get("merge_step_id"),
            "topology_provenance": branch.get("topology_provenance"),
            "steps": steps,
        })
    return branches


def _new_journey_internal_id(prefix: str, used: set[str]) -> str:
    """Allocate one opaque document ID without deriving it from presentation."""
    candidate = f"{prefix}-{uuid.uuid4().hex}"
    if candidate in used:
        # A collision is an operational failure, not something to repair by
        # renaming or selecting a nearby identity.
        raise JudgementRejected("無法安全配置新的 Journey 識別。")
    return candidate


@_owns_project_lock(_payload_run_dir)
def _journey_operation_payload(payload: object) -> dict:
    """Apply one existing Phase 3 operation through the canonical commit path."""
    if not isinstance(payload, dict):
        raise JudgementRejected("Journey 操作資料格式不正確。")
    run_dir = _journey_map_run_dir(payload.get("run"))
    operation_name = str(payload.get("operation") or "")
    if operation_name not in sbp_journey_operations.OPERATIONS:
        raise JudgementRejected("這項 Journey 操作目前無法使用。")
    current = _journey_canonical_document(run_dir)
    used_steps = sbp_journey_operations._all_step_ids(current)
    used_branches = {
        item.get("branch_id")
        for item in current.get("branches") or []
        if isinstance(item, dict) and item.get("branch_id")
    }
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    rationale = payload.get("rationale")
    presentation_only: set[str] = set()

    def branch_id() -> str:
        value = str(args.get("branch_id") or "")
        if not value:
            raise JudgementRejected("找不到要操作的條件分支。")
        return value

    if operation_name == "create_branch":
        label = str(args.get("label") or "").strip()
        divergence = str(args.get("divergence_step_id") or "")
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        if not label or not divergence or not steps:
            raise JudgementRejected("請填寫條件分支名稱、分流點與至少一個步驟。")
        allocated_branch = _new_journey_internal_id("branch", used_branches)
        allocated_steps = []
        for item in steps:
            if not isinstance(item, dict):
                raise JudgementRejected("條件分支步驟格式不正確。")
            step = dict(item)
            step["id"] = _new_journey_internal_id("branch-step", used_steps)
            used_steps.add(step["id"])
            step.setdefault("evidence_refs", [])
            allocated_steps.append(step)
        operation = lambda journey: sbp_journey_operations.create_branch(
            journey,
            branch_id=allocated_branch,
            label=label,
            divergence_step_id=divergence,
            merge_step_id=args.get("merge_step_id"),
            topology_provenance=str(args.get("topology_provenance") or "inferred"),
            steps=allocated_steps,
        )
    elif operation_name == "rename_branch":
        operation = lambda journey: sbp_journey_operations.rename_branch(journey, branch_id=branch_id(), label=str(args.get("label") or ""))
    elif operation_name == "set_divergence":
        operation = lambda journey: sbp_journey_operations.set_divergence(journey, branch_id=branch_id(), divergence_step_id=str(args.get("divergence_step_id") or ""))
    elif operation_name == "set_merge":
        operation = lambda journey: sbp_journey_operations.set_merge(journey, branch_id=branch_id(), merge_step_id=args.get("merge_step_id"))
    elif operation_name == "add_branch_step":
        item = args.get("step") if isinstance(args.get("step"), dict) else {}
        if not str(item.get("label") or "").strip():
            raise JudgementRejected("請填寫步驟名稱。")
        step = dict(item)
        step["id"] = _new_journey_internal_id("branch-step", used_steps)
        step.setdefault("evidence_refs", [])
        operation = lambda journey: sbp_journey_operations.add_branch_step(journey, branch_id=branch_id(), step=step, position=args.get("position"))
    elif operation_name == "rename_branch_step":
        step_id = str(args.get("step_id") or "")
        presentation_only.add(step_id)
        operation = lambda journey: sbp_journey_operations.rename_branch_step(journey, branch_id=branch_id(), step_id=step_id, label=str(args.get("label") or ""))
    elif operation_name == "reorder_branch_steps":
        operation = lambda journey: sbp_journey_operations.reorder_branch_steps(journey, branch_id=branch_id(), step_ids=list(args.get("step_ids") or []))
    elif operation_name == "remove_branch_step":
        operation = lambda journey: sbp_journey_operations.remove_branch_step(journey, branch_id=branch_id(), step_id=str(args.get("step_id") or ""))
    elif operation_name == "delete_branch":
        operation = lambda journey: sbp_journey_operations.delete_branch(journey, branch_id=branch_id())
    elif operation_name == "scenario_to_branch":
        allocated_branch = _new_journey_internal_id("branch", used_branches)
        operation = lambda journey: sbp_journey_operations.scenario_to_branch(
            journey, scenario_id=str(args.get("scenario_id") or ""), branch_id=allocated_branch,
            divergence_step_id=str(args.get("divergence_step_id") or ""), merge_step_id=args.get("merge_step_id"),
            topology_provenance=str(args.get("topology_provenance") or "inferred"), rationale=str(rationale or ""),
        )
    elif operation_name == "branch_to_scenario":
        scenario = args.get("scenario") if isinstance(args.get("scenario"), dict) else {}
        scenario = dict(scenario)
        scenario["id"] = _new_journey_internal_id("scenario", {item.get("id") for item in current.get("scenarios") or [] if isinstance(item, dict)})
        operation = lambda journey: sbp_journey_operations.branch_to_scenario(
            journey, branch_id=branch_id(), scenario=scenario, rationale=str(rationale or ""),
        )
    else:
        raise JudgementRejected("這項 Journey 操作目前無法使用。")

    before_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}
    before_projection = before_state.get("as_is_input_projection_digest")
    if not before_projection:
        registry_value = before_state.get("artifact_registry")
        if registry_value:
            registry_path = storage_locations.resolve_registry_pointer(
                registry_value, run_dir, RUNS_DIR
            )
            if registry_path.is_file():
                registry = _load(registry_path) or {}
                pointer = next((item for item in registry.get("current", []) if item.get("artifact_type") == "confirmed_journey"), {})
                record = next((item for item in registry.get("versions", []) if item.get("version_id") == pointer.get("version_ref")), {})
                before_projection = record.get("as_is_input_projection_digest")
    try:
        result = sbp_journey_operations.commit_journey_operation(
            run_dir, operation, operation_name=operation_name, rationale=rationale,
            presentation_only_step_ids=presentation_only,
        )
    except sbp_journey_operations.JourneyOperationError as exc:
        # Canonical validators quite deliberately name internal identities so
        # tests and logs can diagnose a rejected write.  Those identifiers are
        # not researcher-facing concepts, however; keep the inspector error
        # useful without leaking branch/step/scenario IDs into the canvas.
        message = str(exc)
        if "merge_step_id 不得早於" in message:
            message = "合流點必須位於分流點之後。"
        elif any(token in message for token in (
            "branch_id", "step_id", "scenario_id", "document-wide", "primary step",
        )):
            message = "目前的 Journey 結構已變更，請重新載入後再試。"
        raise JudgementRejected(message) from exc
    after_projection = result.get("state", {}).get("as_is_input_projection_digest")
    return {
        "ok": True,
        "journey": result["journey"],
        "as_is_projection_changed": before_projection != after_projection,
        "journey_state": result.get("state", {}).get("journey_state"),
        "artifact_version": (result.get("state", {}).get("artifact_versions") or {}).get("confirmed_journey"),
        "map": _journey_channel_map_response(run_dir),
    }


def _primary_hmw_statement(run_dir: Path) -> str | None:
    """The primary how-might-we, quoted exactly as chosen -- never re-worded.

    The selection schema is a oneOf: a reference to an existing hmw.yaml
    entry (resolved back to that entry's own statement), or a researcher-
    authored one (whose own statement is used directly).
    """
    path = run_dir / "primary_hmw_selection.yaml"
    if not path.is_file():
        return None
    selection = _load(path)
    if not isinstance(selection, dict):
        return None
    if "hmw_id" in selection:
        hmw_path = run_dir / "hmw.yaml"
        if not hmw_path.is_file():
            return None
        hmw_doc = _load(hmw_path) or {}
        entry = next(
            (e for e in hmw_doc.get("entries") or [] if e.get("id") == selection.get("hmw_id")), None
        )
        return (entry or {}).get("statement")
    return selection.get("statement")


def _primary_target_name(run_dir: Path) -> str | None:
    """The primary target's researcher-facing name -- never the quadrant id.

    Resolved through persona_content.yaml's quadrant_id field (added by
    quadrant-members-are-designer-assigned). Real runs generated before
    that regeneration have no persona carrying that field, in which case
    this returns None the same as a genuinely unmade selection -- there is
    nothing honest to show either way.
    """
    path = run_dir / "target_segmentation_selection.yaml"
    if not path.is_file():
        return None
    selection = _load(path)
    if not isinstance(selection, dict):
        return None
    quadrant_id = selection.get("primary_quadrant_id")
    if not quadrant_id:
        return None
    persona_path = run_dir / "persona_content.yaml"
    if not persona_path.is_file():
        return None
    persona_doc = _load(persona_path) or {}
    persona = next(
        (p for p in persona_doc.get("personas") or [] if p.get("quadrant_id") == quadrant_id), None
    )
    return (persona or {}).get("label")


def _journey_step_evidence_views(steps: list[dict], evidence_by_id: dict[str, dict] | None) -> list[dict]:
    """Expose step evidence for the confirmed Journey inspector.

    This is a read-only projection of the step's recorded evidence_refs.  A
    missing resolver result is kept as an unresolved count rather than being
    replaced with a guessed source or a generic claim.
    """
    views = []
    for step in steps:
        refs = [ref for ref in (step.get("evidence_refs") or []) if ref]
        records = []
        unresolved = 0
        for ref in refs:
            record = evidence_by_id.get(ref) if evidence_by_id else None
            if not record:
                unresolved += 1
                continue
            records.append({
                "evidence_ref": ref,
                "role": record.get("role") or record.get("participant_role") or "研究來源",
                "locator": record.get("locator") or "",
                "text": record.get("text") or record.get("excerpt") or "",
                "source": record.get("source") or "",
            })
        views.append({"evidence": records, "unresolved_count": unresolved})
    return views


def _journey_scenario_views(journey: dict, evidence_by_id: dict[str, dict] | None) -> list[dict]:
    primary_id = journey.get("primary_scenario")
    scenarios = []
    for scenario in journey.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        steps = [step for step in scenario.get("steps") or [] if isinstance(step, dict)]
        evidence_views = _journey_step_evidence_views(steps, evidence_by_id)
        scenarios.append({
            "id": scenario.get("id") or "",
            "label": scenario.get("label") or "",
            "description": scenario.get("description") or "",
            "customer_goal": scenario.get("customer_goal") or "",
            "primary": scenario.get("id") == primary_id,
            "branch_relation": "primary_path" if scenario.get("id") == primary_id else "alternative_path",
            "status": scenario.get("status") or "",
            "steps": [
                {
                    "id": step.get("id") or "",
                    "label": step.get("label") or "",
                    "description": step.get("description") or "",
                    "evidence": evidence_views[index]["evidence"],
                    "unresolved_evidence_count": evidence_views[index]["unresolved_count"],
                }
                for index, step in enumerate(steps)
            ],
        })
    return scenarios


JOURNEY_ANNOTATIONS_FILE = "journey_annotations.yaml"
JOURNEY_ANNOTATIONS_SCHEMA = REPO / "schema" / "journey-annotations.schema.json"
JOURNEY_ANNOTATION_LEVEL_FIELDS = ("emotion", "satisfaction")
JOURNEY_ANNOTATION_TEXT_FIELDS = ("solutions", "pain_points")


class JourneyAnnotationRejected(ValueError):
    """A journey annotation request that would write an invalid or unowned value."""


def _journey_annotation_errors(document: object) -> list[str]:
    from jsonschema import Draft7Validator

    schema = json.loads(JOURNEY_ANNOTATIONS_SCHEMA.read_text(encoding="utf-8"))
    return [error.message for error in Draft7Validator(schema).iter_errors(document)]


def _journey_annotations(run_dir: Path) -> dict:
    """The run's researcher annotations; an absent file means none."""
    path = run_dir / JOURNEY_ANNOTATIONS_FILE
    if not path.is_file():
        return {"schema_version": 1, "steps": {}}
    try:
        document = _load(path)
    except Exception:  # noqa: BLE001 - an unreadable file is reported, never overwritten
        document = None
    if not isinstance(document, dict) or _journey_annotation_errors(document):
        return {"schema_version": 1, "steps": {}, "unreadable": True}
    return document


def _primary_journey_step_ids(run_dir: Path) -> set[str]:
    journey = _journey_canonical_document(run_dir)
    if not journey.get("scenarios") and (run_dir / "journey_proposal.yaml").is_file():
        # accepted-surface restoration: a fresh-path Journey is still a candidate
        # until confirmed; its steps take Emotion / Pain Point annotations too.
        journey = _load(run_dir / "journey_proposal.yaml") or {}
    scenario = next(
        (s for s in journey.get("scenarios") or [] if s.get("id") == journey.get("primary_scenario")),
        None,
    )
    return {step.get("id") for step in (scenario or {}).get("steps") or [] if step.get("id")}


def _write_journey_annotation(run_dir: Path, step_id: object, field: object, value: object) -> dict:
    """Write one researcher annotation after validating it; nothing else changes."""
    if not isinstance(step_id, str) or step_id not in _primary_journey_step_ids(run_dir):
        raise JourneyAnnotationRejected("這一步不在目前的歷程裡，無法加註記。")
    if field not in JOURNEY_ANNOTATION_LEVEL_FIELDS + JOURNEY_ANNOTATION_TEXT_FIELDS:
        raise JourneyAnnotationRejected("不認得這種註記。")
    document = _journey_annotations(run_dir)
    if document.pop("unreadable", False):
        raise JourneyAnnotationRejected("註記檔的格式不正確，沒有寫入，以免覆蓋既有內容。")
    steps = document.setdefault("steps", {})
    entry = dict(steps.get(step_id) or {})
    if value is None:
        entry.pop(field, None)
    else:
        stamp = {"source": "researcher", "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        if field in JOURNEY_ANNOTATION_LEVEL_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                raise JourneyAnnotationRejected("程度要是 1 到 5。")
            entry[field] = {"level": value, **stamp}
        else:
            if not isinstance(value, list) or not all(isinstance(text, str) and text.strip() for text in value):
                raise JourneyAnnotationRejected("每一條內容都要有文字。")
            entry[field] = {"texts": [text.strip() for text in value], **stamp}
    if entry:
        steps[step_id] = entry
    else:
        steps.pop(step_id, None)
    errors = _journey_annotation_errors(document)
    if errors:
        raise JourneyAnnotationRejected("這筆註記的格式不正確。")
    _write_yaml_atomic(run_dir / JOURNEY_ANNOTATIONS_FILE, document)
    return document


JOURNEY_ANNOTATION_DRAFT_TIMEOUT = 1800


def _draft_journey_annotations(run_dir: Path, execution: "sbp_execution.ExecutionConfig") -> dict:
    """Run the AI drafting step for the confirmed steps; return the merged annotations."""
    import subprocess
    import tempfile

    journey = _journey_canonical_document(run_dir)
    scenario = next(
        (s for s in journey.get("scenarios") or [] if s.get("id") == journey.get("primary_scenario")),
        None,
    )
    steps = [
        {"id": step.get("id"), "label": step.get("label"), "description": step.get("description"),
         "evidence_refs": step.get("evidence_refs") or []}
        for step in (scenario or {}).get("steps") or [] if step.get("id")
    ]
    if not steps:
        raise JourneyAnnotationRejected("這份歷程沒有可以起草的步驟。")
    saved = _journey_annotations(run_dir)
    if saved.get("unreadable"):
        raise JourneyAnnotationRejected("註記檔的格式不正確，沒有起草，以免覆蓋既有內容。")
    draft = _load(run_dir / "sbp_draft.yaml") if (run_dir / "sbp_draft.yaml").is_file() else {}
    pain_points: dict[str, list[str]] = {}
    for annotation in (draft or {}).get("annotations") or []:
        if isinstance(annotation, dict) and annotation.get("type") == "pain_point" and annotation.get("step_ref"):
            pain_points.setdefault(annotation["step_ref"], []).append(annotation.get("text") or "")
    for step_id, entry in (saved.get("steps") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("pain_points"), dict):
            pain_points[step_id] = list(entry["pain_points"].get("texts") or [])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump({"steps": steps, "pain_points": pain_points}, handle, ensure_ascii=False)
        request_path = handle.name
    try:
        result = child_processes.run(
            [sys.executable, "-m", "sbp_gen", "journey-annotations", "--run", str(run_dir), "--input", request_path],
            cwd=str(REPO / "tools"), capture_output=True, text=True,
            env=execution.child_environment(), timeout=JOURNEY_ANNOTATION_DRAFT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise JourneyAnnotationRejected("起草超過時限，沒有完成。") from exc
    finally:
        Path(request_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise JourneyAnnotationRejected("起草沒有完成，既有的註記沒有被改動。")
    return _journey_annotations(run_dir)


# build-journey-candidate-after-persona: Station 5's new path. A run with no
# Blueprint draft and no confirmed Journey gets one explicit 建立顧客旅程
# action, admitted only while the target and the current Persona are
# confirmed; it runs the existing `sbp-gen journey --propose` and stops at the
# candidate. Runs that already carry a draft or a confirmed Journey keep the
# older station untouched (None below).
def _journey_prerequisite_reason(run_dir: Path) -> str | None:
    if not (run_dir / "evidence.yaml").is_file():
        return "這份產出還沒有證據，無法建立顧客旅程。"
    if not sbp_gates.is_derivation_runnable(
            run_dir, "persona_content", now=dt.datetime.now().isoformat(timespec="seconds")):
        return "目標族群還沒確認目前這一版，請先回到 Persona 重新確認。"
    if _persona_confirmation(run_dir)["state"] != "confirmed":
        return "Persona 還沒確認，請先回到 Persona 確認後再建立顧客旅程。"
    return None


def _journey_confirmation_recorded(run_dir: Path) -> bool:
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and bool(state.get("journey_confirmation"))


def _journey_is_candidate_path(run_dir: Path) -> bool:
    """A fresh-path run: its Journey is a candidate from 建立顧客旅程, and either
    no Blueprint draft exists yet or the Journey was confirmed on Station 5
    (generate-as-is-blueprint-candidate: a draft generated from that Journey
    keeps the run on the fresh path). Older runs never record a Station 5
    confirmation and keep the legacy station."""
    if not (run_dir / "journey_proposal.yaml").is_file():
        return False
    return not (run_dir / "sbp_draft.yaml").is_file() or _journey_confirmation_recorded(run_dir)


def _journey_uses_solution_selection(run_dir: Path) -> bool:
    path = run_dir / "journey.yaml"
    if not path.is_file():
        path = run_dir / "journey_proposal.yaml"
    return sbp_stages.journey_uses_solution_selection(_load(path) if path.is_file() else {})


def _journey_build_state(run_dir: Path) -> dict | None:
    if _journey_is_candidate_path(run_dir):
        if not _journey_persona_current(run_dir):
            reason = _journey_prerequisite_reason(run_dir)
            return {"state": "persona_changed", "available": reason is None, "reason": reason}
        return {"state": "candidate", "available": False, "reason": None}
    if (run_dir / "sbp_draft.yaml").is_file() or (run_dir / "journey.yaml").is_file():
        return None
    reason = _journey_prerequisite_reason(run_dir)
    return {"state": "not_eligible" if reason else "eligible", "available": reason is None, "reason": reason}


# confirm-journey-candidate-save-only: the Journey Human Gate for a fresh-path
# candidate. The confirmed Journey is the canonical `journey.yaml` (same
# validation as `sbp-gen journey --confirm-file`); `run.json` records the
# fingerprint of the candidate that was confirmed, so the Journey counts as
# confirmed only while the current candidate still matches it. Confirming
# writes those two files and starts nothing.
# sbp-solution-adoption-and-core-problem (2026-10-19 researcher ruling): Solutions
# are developed and adopted at the SBP station, so confirming the CJM covers the
# journey and its core problems, not the Solutions recorded on its steps.
JOURNEY_SOLUTION_SIDE_FIELDS = ("solution_candidates", "selected_solution_ids", "solution_analysis", "solution_basis")
JOURNEY_SOLUTION_SIDE_KINDS = {"solution", "delivery", "solution_candidate", "solution_selection",
                               "candidate_delivery", "candidate_delivery_add"}


def _journey_candidate_legacy_digest(proposal: dict) -> str:
    """The CJM digest recorded before the ruling (it also covered Solutions)."""
    comparable = copy.deepcopy(proposal)
    for scenario in comparable.get("scenarios") or []:
        if isinstance(scenario, dict):
            scenario.pop("status", None)
    return sbp_lifecycle.canonical_content_digest(comparable)


def _journey_candidate_digest(proposal: dict) -> str:
    comparable = copy.deepcopy(proposal)
    for scenario in comparable.get("scenarios") or []:
        if isinstance(scenario, dict):
            scenario.pop("status", None)
            for step in scenario.get("steps") or []:
                if isinstance(step, dict):
                    for field in JOURNEY_SOLUTION_SIDE_FIELDS:
                        step.pop(field, None)
    return sbp_lifecycle.canonical_content_digest(comparable)


def _upgrade_legacy_journey_confirmation(run_dir: Path, proposal: dict) -> None:
    """A CJM confirmed under the earlier digest stays confirmed when its
    Solutions are next edited: before that edit, the record is rewritten to the
    current digest of the very proposal it confirmed."""
    state_path = run_dir / "run.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, ValueError):
        return
    record = state.get("journey_confirmation") or {}
    if record and record.get("proposal_digest") == _journey_candidate_legacy_digest(proposal) \
            and record.get("proposal_digest") != _journey_candidate_digest(proposal):
        state["journey_confirmation"] = {**record, "proposal_digest": _journey_candidate_digest(proposal)}
        _write_json_atomic(state_path, state)


@_owns_project_lock(_run_dir_argument)
def _sync_confirmed_solutions(run_dir: Path) -> None:
    """While the CJM stays confirmed, Solutions developed, adopted or edited at
    the SBP station are carried onto the confirmed journey the blueprint is
    built from, so its freshness sees them."""
    if _journey_confirmation(run_dir)["state"] != "confirmed":
        return
    proposal = _load(run_dir / "journey_proposal.yaml") or {}
    journey_path = run_dir / "journey.yaml"
    journey = _load(journey_path) if journey_path.is_file() else None
    if not isinstance(journey, dict):
        return
    def steps_of(doc: dict) -> list:
        scenario = next((s for s in doc.get("scenarios") or []
                         if isinstance(s, dict) and s.get("id") == doc.get("primary_scenario")), {}) or {}
        return [step for step in scenario.get("steps") or [] if isinstance(step, dict)]
    source = {step.get("id"): step for step in steps_of(proposal)}
    changed = False
    for step in steps_of(journey):
        origin = source.get(step.get("id"))
        if origin is None:
            continue
        for field in JOURNEY_SOLUTION_SIDE_FIELDS:
            if origin.get(field) != step.get(field):
                if field in origin:
                    step[field] = copy.deepcopy(origin[field])
                else:
                    step.pop(field, None)
                changed = True
    if changed:
        _write_yaml_atomic(journey_path, journey)


def _persona_lens_digest(run_dir: Path) -> str | None:
    """The confirmed Persona a CJM may use as its lens right now: confirmed, and
    still describing the confirmed axes / target. None when there is none."""
    confirmation = _persona_confirmation(run_dir)
    if confirmation["state"] != "confirmed":
        return None
    doc = _load(run_dir / "persona_content.yaml") if (run_dir / "persona_content.yaml").is_file() else None
    if not isinstance(doc, dict) or _persona_freshness(run_dir, doc).get("state") != "current":
        return None
    return confirmation["persona_digest"]


class StaleDecisionRejected(ValueError):
    pass


def _run_state(run_dir: Path) -> dict:
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def _persona_hmw_retained(run_dir: Path) -> bool:
    """resolve-stale-at-researcher-gates: the researcher kept the Persona
    against the HMW that is selected now."""
    now = hmw_scope.current_digest(run_dir)
    basis = _run_state(run_dir).get("persona_hmw_basis") or {}
    return bool(now) and basis.get("retained_against_hmw_digest") == now


def _persona_hmw_framing(run_dir: Path) -> str:
    """current / stale / retained / unframed for the Persona's segmentation."""
    framings = [hmw_scope.framing_state(run_dir, _load(run_dir / name))
                for name in ("persona_matrix.yaml", "segmentation_axis_candidates.yaml") if (run_dir / name).is_file()]
    if "stale" not in framings:
        return "current" if "current" in framings else "unframed"
    return "retained" if _persona_hmw_retained(run_dir) else "stale"


@_owns_project_lock(_run_dir_argument)
def _retain_persona_against_hmw(run_dir: Path) -> dict:
    """「保留目前 Persona」: no model call, no Persona change; records that the
    current Persona is accepted against the HMW selected now."""
    doc = _load(run_dir / "persona_content.yaml") if (run_dir / "persona_content.yaml").is_file() else None
    if not (isinstance(doc, dict) and doc.get("personas")):
        raise StaleDecisionRejected("還沒有 Persona，沒有需要保留的內容。")
    if _persona_hmw_framing(run_dir) != "stale":
        raise StaleDecisionRejected("Persona 目前沒有需要決定保留或重新產生的 HMW 變更。")
    with sbp_run_files.transaction(run_dir / "run.json") as state:
        state["persona_hmw_basis"] = {"retained_against_hmw_digest": hmw_scope.current_digest(run_dir),
                                      "retained_at": dt.datetime.now().isoformat(timespec="seconds")}
    return {"hmw_framing": {"state": _persona_hmw_framing(run_dir)}}


@_owns_project_lock(_run_dir_argument)
def _retain_journey_against_persona(run_dir: Path) -> dict:
    """「保留目前顧客旅程」: no model call, no Journey change; records that the
    current Journey is accepted against the Persona confirmed now. The
    Persona it was built through (persona_digest) stays as recorded."""
    if _journey_persona_current(run_dir):
        raise StaleDecisionRejected("顧客旅程目前沒有需要決定保留或重新建立的 Persona 變更。")
    now = _persona_lens_digest(run_dir)
    if not now:
        raise StaleDecisionRejected("Persona 還沒確認目前這一版，請先回到 Persona 確認。")
    with sbp_run_files.transaction(run_dir / "run.json") as state:
        lens = dict(state.get("journey_persona_lens") or {})
        if not lens.get("persona_digest"):
            raise StaleDecisionRejected("這份顧客旅程沒有記錄是依哪一份 Persona 建立的，請依目前 Persona 重新建立。")
        lens["retained_against_persona_digest"] = now
        lens["retained_at"] = dt.datetime.now().isoformat(timespec="seconds")
        state["journey_persona_lens"] = lens
    return {"journey_current": _journey_persona_current(run_dir)}


def _journey_persona_current(run_dir: Path) -> bool:
    """Persona is the CJM's analytical lens (2026-09-30 ruling): a new-workflow
    CJM stays current only while the confirmed Persona it was built from is still
    the confirmed, current Persona. Changing the axes, the target, the Persona
    content or its confirmation makes it non-current; nothing is deleted or
    regenerated. Legacy (pre-Solution) Journeys never recorded a lens and keep
    their historical behavior -- a bounded, explicit compatibility exemption."""
    if not _journey_uses_solution_selection(run_dir):
        return True
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}
    except (OSError, ValueError):
        state = {}
    lens = (state or {}).get("journey_persona_lens") or {}
    recorded = lens.get("persona_digest")
    now = _persona_lens_digest(run_dir)
    # resolve-stale-at-researcher-gates (2026-10-06): an HMW change is decided
    # at the Persona gate, so the CJM no longer compares the HMW itself; it is
    # stale only when the Persona it was built through changed and the
    # researcher has not kept it against the current Persona.
    return bool(now) and (recorded == now or lens.get("retained_against_persona_digest") == now)


def _journey_confirmation(run_dir: Path) -> dict:
    proposal_path = run_dir / "journey_proposal.yaml"
    proposal = _load(proposal_path) if proposal_path.is_file() else None
    if not isinstance(proposal, dict):
        return {"state": "absent", "journey_digest": None, "previously_confirmed": False}
    digest = _journey_candidate_digest(proposal)
    state_path = run_dir / "run.json"
    try:
        recorded = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}).get(
            "journey_confirmation") or {}
    except (OSError, ValueError):
        recorded = {}
    confirmed = bool(recorded) and recorded.get("proposal_digest") in (digest, _journey_candidate_legacy_digest(proposal)) \
        and (run_dir / "journey.yaml").is_file()
    if not _journey_persona_current(run_dir):
        # The CJM was built through another Persona; it stays readable but is not
        # current, so nothing downstream (SBP) treats it as confirmed.
        return {"state": "persona_changed", "journey_digest": digest,
                "previously_confirmed": bool(recorded), "confirmed_at": None}
    return {
        "state": "confirmed" if confirmed else "needs_confirmation",
        "journey_digest": digest,
        "previously_confirmed": bool(recorded),
        "confirmed_at": recorded.get("confirmed_at") if confirmed else None,
    }


class JourneyCandidateRejected(Exception):
    pass


def _journey_candidate_evidence_ids(run_dir: Path) -> set[str]:
    evidence = _load(run_dir / "evidence.yaml") if (run_dir / "evidence.yaml").is_file() else []
    return {item.get("id") for item in evidence or [] if isinstance(item, dict) and item.get("id")}


@_owns_project_lock(_run_dir_argument)
def _confirm_journey_candidate(run_dir: Path, journey_digest: object) -> dict:
    if not _journey_is_candidate_path(run_dir):
        raise JourneyCandidateRejected("這份產出沒有待確認的旅程候選。")
    proposal = _load(run_dir / "journey_proposal.yaml") or {}
    if journey_digest != _journey_candidate_digest(proposal):
        raise JourneyCandidateRejected("畫面上的旅程不是最新版本，請重新載入後再確認。")
    reason = _journey_prerequisite_reason(run_dir)
    if reason:
        raise JourneyCandidateRejected(reason)
    confirmed = copy.deepcopy(proposal)
    for scenario in confirmed.get("scenarios") or []:
        if isinstance(scenario, dict):
            scenario["status"] = "confirmed"
    errors = sbp_stages._journey_errors(confirmed, _journey_candidate_evidence_ids(run_dir), require_confirmed=True,
                                        allow_split_stages=_journey_uses_solution_selection(run_dir))
    if errors:
        raise JourneyCandidateRejected("沒有確認：" + "；".join(errors[:3]))
    # sbp-solution-adoption-and-core-problem: a core problem without a Solution, or a
    # stale adopted Solution, no longer refuses the CJM; it refuses building the SBP.
    now = dt.datetime.now().isoformat(timespec="seconds")
    _write_yaml_atomic(run_dir / "journey.yaml", confirmed)
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    state["journey_state"] = "complete"
    state["journey_confirmation"] = {"proposal_digest": journey_digest, "confirmed_at": now}
    _write_json_atomic(state_path, state)
    return _journey_channel_map_response(run_dir)


JOURNEY_CANDIDATE_EDIT_KINDS = {"step_label", "stage_label", "action", "solution", "touchpoint", "delivery",
                                "action_texts", "pain_points", "emotion", "satisfaction",
                                "solution_candidate", "solution_selection", "candidate_delivery",
                                # cjm-experience-and-moment-solutions: the researcher selects
                                # moments to develop, and edits qualitative satisfaction.
                                "develop_moment", "satisfaction_text",
                                # cjm-v1-delta-one-solution: a Channel cell holds 0..N items.
                                "candidate_delivery_add",
                                # 2026-10-02: Pain and Satisfaction are 0..N items, each edited alone.
                                "pain_point", "pain_point_add", "satisfaction_item", "satisfaction_item_add",
                                # accepted-surface restoration (2026-09-27 rework-journey-station):
                                # ←→ / 刪除 on a step, and 步驟之間可插入.
                                "move_step", "delete_step", "insert_step",
                                # sbp-solution-adoption-and-core-problem: the researcher rewrites the core problem.
                                "core_problem"}


@_owns_project_lock(_run_dir_argument)
def _edit_journey_candidate(run_dir: Path, payload: dict) -> dict:
    """Rewrite one text on the candidate. Evidence refs and Channel never
    change; clearing an action, solution or touchpoint removes that item."""
    if not _journey_is_candidate_path(run_dir):
        raise JourneyCandidateRejected("這份產出沒有可以編輯的旅程候選。")
    kind = payload.get("kind")
    value = payload.get("value")
    if kind not in JOURNEY_CANDIDATE_EDIT_KINDS or not isinstance(value, str):
        raise JourneyCandidateRejected("不認得這個欄位。")
    value = value.strip()
    path = run_dir / "journey_proposal.yaml"
    proposal = _load(path) or {}
    scenario = next((s for s in proposal.get("scenarios") or []
                     if isinstance(s, dict) and s.get("id") == proposal.get("primary_scenario")), None)
    if scenario is None:
        raise JourneyCandidateRejected("找不到主要旅程。")
    if kind in JOURNEY_SOLUTION_SIDE_KINDS:
        _upgrade_legacy_journey_confirmation(run_dir, proposal)

    def pick(items: list, index: object) -> dict:
        if not isinstance(index, int) or not 0 <= index < len(items):
            raise JourneyCandidateRejected("這一項已經不在了，請重新載入。")
        return items[index]

    if kind in ("move_step", "delete_step", "insert_step"):
        steps = scenario.get("steps") or []
        index = next((i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("id") == payload.get("step_id")), None)
        if index is None:
            raise JourneyCandidateRejected("這一步已經不在了，請重新載入。")
        if kind == "move_step":
            target = {"left": index - 1, "right": index + 1}.get(value)
            if target is None or not 0 <= target < len(steps):
                raise JourneyCandidateRejected("這一步不能再往那邊移。")
            # Fresh CJM: the step moves as one atomic column and keeps its own stage;
            # crossing a stage boundary is allowed and may split a stage into two
            # segments, which the table shows rather than silently reassigning.
            steps[index], steps[target] = steps[target], steps[index]
            # Fresh CJM: stage membership is part of the atomic column.
            # Historical candidates retain their accepted contiguous-stage
            # behavior for compatibility.
            if not any("solution_candidates" in step for step in steps):
                neighbour = steps[target - 1] if target > 0 else steps[target + 1] if target + 1 < len(steps) else None
                if isinstance(neighbour, dict) and neighbour.get("stage_id") and steps[target].get("stage_id"):
                    steps[target]["stage_id"] = neighbour["stage_id"]
        elif kind == "delete_step":
            if len(steps) <= 1:
                raise JourneyCandidateRejected("旅程至少要留一個步驟。")
            steps.pop(index)
        else:
            if not value:
                raise JourneyCandidateRejected("步驟名稱不能是空的。")
            existing = {s.get("id") for s in steps if isinstance(s, dict)}
            number = 1
            while f"step-added-{number}" in existing:
                number += 1
            new_step = {"id": f"step-added-{number}", "label": value, "evidence_refs": []}
            if any("solution_candidates" in step for step in steps):
                # Emotion / Satisfaction stay unset -- an unset level is not neutral.
                new_step.update({"customer_actions": [], "pain_points": [],
                                 "solution_candidates": [], "selected_solution_ids": []})
                if any("develop_solution" in step for step in steps if isinstance(step, dict)):
                    # cjm-v1-delta-one-solution: a moment the researcher adds is a moment
                    # like any generated one -- it can be selected to develop a Solution.
                    new_step["develop_solution"] = False
            if steps[index].get("stage_id"):
                new_step["stage_id"] = steps[index]["stage_id"]
            steps.insert(index + (1 if payload.get("position") == "after" else 0), new_step)
        used = {s.get("stage_id") for s in steps if isinstance(s, dict)}
        if scenario.get("stages"):
            scenario["stages"] = [stage for stage in scenario["stages"] if stage.get("id") in used]
    elif kind == "stage_label":
        stage = next((s for s in scenario.get("stages") or [] if s.get("id") == payload.get("stage_id")), None)
        if stage is None:
            raise JourneyCandidateRejected("這個階段已經不在了，請重新載入。")
        if not value:
            raise JourneyCandidateRejected("階段名稱不能是空的。")
        stage["label"] = value
    else:
        step = next((s for s in scenario.get("steps") or [] if s.get("id") == payload.get("step_id")), None)
        if step is None:
            raise JourneyCandidateRejected("這一步已經不在了，請重新載入。")
        if kind == "step_label":
            if not value:
                raise JourneyCandidateRejected("步驟名稱不能是空的。")
            step["label"] = value
        elif kind in ("action_texts", "pain_points"):
            texts = [line.strip() for line in value.splitlines() if line.strip()]
            if kind == "action_texts":
                old = [item for item in step.get("customer_actions") or [] if isinstance(item, dict)]
                step["customer_actions"] = [
                    {"text": text, "evidence_refs": list(old[i].get("evidence_refs") or []) if i < len(old) else []}
                    for i, text in enumerate(texts)
                ]
            else:
                step["pain_points"] = texts
                if "develop_solution" not in step and step.get("solution_candidates") \
                        and not sbp_stages._journey_step_is_problematic(step):
                    raise JourneyCandidateRejected("這一步已有候選做法；請先保留痛點或負面情緒，再調整候選做法。")
        elif kind in ("emotion", "satisfaction"):
            if not value.isdigit() or not 1 <= int(value) <= 5:
                raise JourneyCandidateRejected("程度要是 1 到 5。")
            step[kind] = int(value)
            if kind == "emotion" and "develop_solution" not in step and step.get("solution_candidates") \
                    and not sbp_stages._journey_step_is_problematic(step):
                raise JourneyCandidateRejected("這一步已有候選做法；請先保留痛點或負面情緒，再調整候選做法。")
        elif kind == "core_problem":
            if "develop_solution" not in step:
                raise JourneyCandidateRejected("這份舊格式的旅程沒有核心問題可以改。")
            if value:
                step["core_problem"] = value
            else:
                step.pop("core_problem", None)
        elif kind == "develop_moment":
            if "develop_solution" not in step:
                raise JourneyCandidateRejected("這份旅程的解方是跟旅程一起起草的，不用另外選時刻。")
            if value not in ("true", "false"):
                raise JourneyCandidateRejected("不認得這個選擇。")
            step["develop_solution"] = value == "true"
            # 2026-10-02: checking 發展解方 is the adoption. A checked moment adopts the
            # Solution it already has; unchecking keeps it readable but adopts nothing.
            step["selected_solution_ids"] = ([item["id"] for item in step.get("solution_candidates") or []
                                              if isinstance(item, dict) and item.get("id")]
                                             if step["develop_solution"] else [])
        elif kind in ("satisfaction_text", "satisfaction_item", "satisfaction_item_add"):
            evaluation = str(payload.get("evaluation") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            items = sbp_stages.satisfaction_items(step.get("satisfaction"))
            index = 0 if kind == "satisfaction_text" else payload.get("index")
            if bool(evaluation) != bool(reason):
                raise JourneyCandidateRejected("滿意度要同時有評價與理由；兩個都清空代表刪掉這一則。")
            if kind == "satisfaction_item_add":
                if evaluation:
                    items.append({"evaluation": evaluation, "reason": reason, "evidence_refs": [], "source": "researcher"})
            else:
                if not isinstance(index, int) or not (0 <= index < max(len(items), 1)):
                    raise JourneyCandidateRejected("找不到這一則滿意度。")
                if not evaluation:
                    if index < len(items):
                        items.pop(index)
                elif index < len(items):
                    items[index] = {**items[index], "evaluation": evaluation, "reason": reason, "source": "researcher"}
                else:
                    items.append({"evaluation": evaluation, "reason": reason, "evidence_refs": [], "source": "researcher"})
            if items:
                step["satisfaction"] = items
            else:
                step.pop("satisfaction", None)
        elif kind in ("pain_point", "pain_point_add"):
            pains = [str(text) for text in step.get("pain_points") or []]
            if kind == "pain_point_add":
                if value:
                    pains.append(value)
            else:
                index = payload.get("index")
                if not isinstance(index, int) or not 0 <= index < len(pains):
                    raise JourneyCandidateRejected("找不到這一則痛點。")
                if value:
                    pains[index] = value
                else:
                    pains.pop(index)
            step["pain_points"] = pains
            if "develop_solution" not in step and step.get("solution_candidates") \
                    and not sbp_stages._journey_step_is_problematic(step):
                raise JourneyCandidateRejected("這一步已有候選做法；請先保留痛點或負面情緒，再調整候選做法。")
        elif kind == "solution_selection":
            chosen = payload.get("selected_solution_ids")
            available = {item.get("id") for item in step.get("solution_candidates") or [] if isinstance(item, dict)}
            if not isinstance(chosen, list) or any(not isinstance(cid, str) or cid not in available for cid in chosen) \
                    or len(chosen) != len(set(chosen)):
                raise JourneyCandidateRejected("選取的 Solution 不是目前候選。")
            step["selected_solution_ids"] = chosen
        elif kind == "solution_candidate":
            candidates = step.get("solution_candidates") or []
            candidate = next((item for item in candidates if item.get("id") == payload.get("solution_id")), None)
            if candidate is None or not value:
                raise JourneyCandidateRejected("找不到這個 Solution，或文字是空的。")
            candidate["text"] = value
            candidate["basis"] = "researcher"
        elif kind == "candidate_delivery_add":
            candidate = next((item for item in step.get("solution_candidates") or []
                              if item.get("id") == payload.get("solution_id")), None)
            channel = payload.get("channel")
            if candidate is None or channel not in PRESENTED_CHANNEL_IDS:
                raise JourneyCandidateRejected("找不到這個解方或管道。")
            if value:
                delivery = candidate.get("delivery") if isinstance(candidate.get("delivery"), dict) else {}
                roles = delivery.get(channel) or []
                roles = roles if isinstance(roles, list) else [roles]
                roles.append({"text": value, "basis": "researcher"})
                delivery[channel] = roles
                candidate["delivery"] = delivery
        elif kind == "candidate_delivery":
            candidate = next((item for item in step.get("solution_candidates") or []
                              if item.get("id") == payload.get("solution_id")), None)
            channel = payload.get("channel")
            index = payload.get("index")
            if candidate is None or channel not in PRESENTED_CHANNEL_IDS or not isinstance(index, int):
                raise JourneyCandidateRejected("找不到這個交付元素。")
            delivery = candidate.get("delivery") or {}
            roles = delivery.get(channel) or []
            roles = roles if isinstance(roles, list) else [roles]
            role = pick(roles, index)
            if value:
                role["text"] = value
                role["basis"] = "researcher"
            else:
                roles.pop(index)
            if roles:
                delivery[channel] = roles
            else:
                delivery.pop(channel, None)
            candidate["delivery"] = delivery
        elif kind == "action":
            actions = step.get("customer_actions") or []
            action = pick(actions, payload.get("index"))
            if value:
                action["text"] = value
            else:
                actions.remove(action)
        else:
            solutions = step.get("as_is_solutions") or []
            solution = pick(solutions, payload.get("index"))
            if kind == "solution":
                if value:
                    solution["text"] = value
                else:
                    solutions.remove(solution)
            elif kind == "delivery":
                # 研究者改寫某個管道的角色：來源記成 researcher，原本的引用留著。
                channel = payload.get("channel")
                if channel not in PRESENTED_CHANNEL_IDS:
                    raise JourneyCandidateRejected("不認得這個管道。")
                delivery = solution.get("delivery") if isinstance(solution.get("delivery"), dict) else None
                if delivery is None:
                    raise JourneyCandidateRejected("這份舊格式的旅程沒有逐管道的內容可以改。")
                if value:
                    role = delivery.get(channel) if isinstance(delivery.get(channel), dict) else {}
                    delivery[channel] = {**role, "text": value, "basis": "researcher"}
                else:
                    delivery.pop(channel, None)
                    if not delivery:
                        raise JourneyCandidateRejected("每個做法至少要留一個管道。")
            else:
                touchpoints = solution.get("touchpoints") or []
                touchpoint = pick(touchpoints, payload.get("touchpoint_index"))
                if value:
                    touchpoint["text"] = value
                else:
                    touchpoints.remove(touchpoint)
    fresh_model = any("solution_candidates" in step for step in scenario.get("steps") or [] if isinstance(step, dict))
    errors = sbp_stages._journey_errors(proposal, _journey_candidate_evidence_ids(run_dir),
                                        allow_split_stages=fresh_model)
    if errors:
        raise JourneyCandidateRejected("沒有寫入：" + "；".join(errors[:3]))
    _write_yaml_atomic(path, proposal)
    if kind in JOURNEY_SOLUTION_SIDE_KINDS or kind == "develop_moment":
        _sync_confirmed_solutions(run_dir)
    return _journey_channel_map_response(run_dir)


def _solution_delivery_view(solution: dict) -> dict | None:
    """各管道為這個做法扮演的角色：只給文字與來源型別，不給證據編號。
    舊格式（只有 channel）回 None，畫面照舊格式呈現。"""
    delivery = solution.get("delivery")
    if not isinstance(delivery, dict):
        return None
    return {
        channel: {"text": role.get("text") or "", "basis": role.get("basis")}
        for channel, role in delivery.items()
        if channel in PRESENTED_CHANNEL_IDS and isinstance(role, dict) and role.get("text")
    }


def _satisfaction_items_view(value: object) -> list[dict]:
    return [{"evaluation": str(item.get("evaluation") or ""), "reason": str(item.get("reason") or "")}
            for item in sbp_stages.satisfaction_items(value) if item.get("evaluation") or item.get("reason")]


def _solution_analysis_view(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {"problem": str(value.get("problem") or ""), "why_it_matters": str(value.get("why_it_matters") or ""),
            "constraints": [str(item) for item in value.get("constraints") or [] if str(item).strip()]}


def _journey_moment_solution_blockers(proposal: dict, run_dir: Path | None = None) -> list[str]:
    """What keeps a moment-selection CJM from being confirmed: a selected moment
    with no Solutions yet, or an adopted Solution developed from a step that has
    since changed. Older CJMs never carry `develop_solution` and pass."""
    scenario = next((s for s in proposal.get("scenarios") or []
                     if isinstance(s, dict) and s.get("id") == proposal.get("primary_scenario")), {}) or {}
    # solution-reconfirmation: only a changed step the researcher has not kept or
    # redeveloped refuses Journey confirmation; an HMW change alone never does.
    if run_dir is not None:
        applicability = sbp_solution_applicability.journey_applicability(run_dir, proposal)
    else:
        applicability = {step.get("id"): {"reasons": ["step_changed"]} for step in scenario.get("steps") or []
                         if isinstance(step, dict) and sbp_stages.journey_step_solutions_stale(step)}
    blockers = []
    for step in scenario.get("steps") or []:
        if not isinstance(step, dict) or step.get("develop_solution") is not True:
            continue
        label = step.get("label") or ""
        if not step.get("solution_candidates"):
            blockers.append(f"「{label}」納入了要解決，但還沒有發展解方；請在服務藍圖產生體驗維度，或在顧客旅程取消納入。")
        elif step.get("selected_solution_ids") and "step_changed" in (applicability.get(step.get("id")) or {}).get("reasons", []):
            blockers.append(f"「{label}」改過了，已採用的解方是根據改之前的內容發展的；請選「保留目前解方」或重新發展。")
    return blockers


def _journey_candidate_response(run_dir: Path, build: dict) -> dict:
    """Read-only Station 5 projection; each candidate retains its AI Channels."""
    proposal = _load(run_dir / "journey_proposal.yaml") or {}
    scenario = next(
        (s for s in proposal.get("scenarios") or []
         if isinstance(s, dict) and s.get("id") == proposal.get("primary_scenario")),
        None,
    ) or {}
    steps = [step for step in scenario.get("steps") or [] if isinstance(step, dict)]
    # accepted-surface restoration: the other scenarios are the 「另一種走法」
    # the researcher could see before the candidate renderer; shown, not asserted.
    alternatives = [
        {"label": s.get("label") or "", "description": s.get("description") or "",
         "steps": [{"label": step.get("label") or ""} for step in s.get("steps") or [] if isinstance(step, dict)]}
        for s in proposal.get("scenarios") or []
        if isinstance(s, dict) and s.get("id") != proposal.get("primary_scenario")
    ]
    if any("solution_candidates" in step for step in steps):
        confirmation = _journey_confirmation(run_dir)
        applicability = sbp_solution_applicability.journey_applicability(run_dir, proposal)

        def analytical_refs(step: dict) -> list[str]:
            refs = list(step.get("evidence_refs") or [])
            refs += [ref for a in step.get("customer_actions") or [] if isinstance(a, dict)
                     for ref in a.get("evidence_refs") or []]
            for candidate in step.get("solution_candidates") or []:
                if isinstance(candidate, dict):
                    refs += list(candidate.get("evidence_refs") or [])
                    for roles in (candidate.get("delivery") or {}).values():
                        for role in roles if isinstance(roles, list) else [roles]:
                            if isinstance(role, dict):
                                refs += list(role.get("evidence_refs") or [])
            return list(dict.fromkeys(refs))

        return {
            "state": "candidate", "build": build, "solution_selection_model": True,
            "alternatives": alternatives,
            "step_evidence": [item["evidence"] for item in _journey_step_evidence_views(
                [{"evidence_refs": analytical_refs(step)} for step in steps], _evidence_lookup(run_dir))],
            "confirmation": confirmation,
            "journey_confirmed": confirmation["state"] == "confirmed",
            "header": {
                "primary_hmw": _primary_hmw_statement(run_dir) or JOURNEY_MAP_NOT_CHOSEN_YET,
                # formation-time-basis-display: the HMW this journey was formed from, verified by
                # the digest its build recorded, or "unknown"; primary_hmw stays the current choice.
                "topic": formation_basis.journey_formation_hmw(run_dir),
                "primary_target": _primary_target_name(run_dir) or JOURNEY_MAP_NOT_CHOSEN_YET,
            },
            "stages": [{"id": stage.get("id"), "label": stage.get("label")}
                       for stage in scenario.get("stages") or [] if isinstance(stage, dict)],
            "steps": [{"step": i, "id": step.get("id"), "label": step.get("label") or "",
                       "stage_id": step.get("stage_id"),
                       "actions": [item.get("text") for item in step.get("customer_actions") or []
                                   if isinstance(item, dict) and item.get("text")],
                       "pain_points": list(step.get("pain_points") or []),
                       "emotion": sbp_stages._experience_level_value(step.get("emotion")),
                       "satisfaction": sbp_stages._experience_level_value(step.get("satisfaction")),
                       "satisfaction_items": _satisfaction_items_view(step.get("satisfaction")),
                       "solution_candidates": [{"id": item.get("id"), "text": item.get("text"),
                                                "channels": sbp_stages.solution_candidate_channels(item)}
                                               for item in step.get("solution_candidates") or [] if isinstance(item, dict)],
                       "selected_solution_ids": list(step.get("selected_solution_ids") or []),
                       # cjm-experience-and-moment-solutions: present only on a CJM whose
                       # Solutions are developed for researcher-selected moments.
                       "moment_selection": "develop_solution" in step,
                       "develop_solution": step.get("develop_solution") is True,
                       "solution_analysis": _solution_analysis_view(step.get("solution_analysis")),
                       # sbp-solution-adoption-and-core-problem: the researcher's own core problem, when written.
                       "core_problem": step.get("core_problem") or None,
                       # solution-reconfirmation: one applicability function decides it.
                       "solutions_stale": (applicability.get(step.get("id")) or {}).get("state") == "needs_reconfirmation",
                       "solution_applicability": applicability.get(step.get("id")),
                       }
                      for i, step in enumerate(steps, 1)],
        }
    evidence_by_id = _evidence_lookup(run_dir)

    def solutions(step: dict) -> list[dict]:
        return [solution for solution in step.get("as_is_solutions") or [] if isinstance(solution, dict)]

    def cited(step: dict) -> list[str]:
        refs = list(step.get("evidence_refs") or [])
        refs += [ref for a in step.get("customer_actions") or [] if isinstance(a, dict) for ref in a.get("evidence_refs") or []]
        for solution in solutions(step):
            refs += list(solution.get("evidence_refs") or [])
            refs += [ref for t in solution.get("touchpoints") or [] if isinstance(t, dict) for ref in t.get("evidence_refs") or []]
        return list(dict.fromkeys(refs))

    confirmation = _journey_confirmation(run_dir)
    return {
        "state": "candidate",
        "build": build,
        "annotations": _journey_annotations(run_dir),
        "alternatives": alternatives,
        "confirmation": confirmation,
        "journey_confirmed": confirmation["state"] == "confirmed",
        "header": {
            "primary_hmw": _primary_hmw_statement(run_dir) or JOURNEY_MAP_NOT_CHOSEN_YET,
            "topic": formation_basis.journey_formation_hmw(run_dir),
            "primary_target": _primary_target_name(run_dir) or JOURNEY_MAP_NOT_CHOSEN_YET,
        },
        "stages": [{"id": stage.get("id"), "label": stage.get("label")}
                   for stage in scenario.get("stages") or [] if isinstance(stage, dict)],
        "steps": [{"step": i, "id": step.get("id") or "", "label": step.get("label") or "",
                   "stage_id": step.get("stage_id"),
                   # ground-journey-candidate-channel-in-as-is-solution: Channel
                   # comes only from the step's evidenced As-is Solutions.
                   "as_is_solutions": [{"text": solution.get("text") or "", "channel": solution.get("channel"),
                                        "delivery": _solution_delivery_view(solution),
                                        "touchpoints": [t.get("text") for t in solution.get("touchpoints") or []
                                                        if isinstance(t, dict) and t.get("text")]}
                                       for solution in solutions(step)]}
                  for i, step in enumerate(steps, 1)],
        # A Journey generated under the As-is Solution contract carries the
        # field on its steps (possibly empty); an older candidate does not,
        # and shows no Channel rows rather than empty placeholders.
        "channels_assessed": any("as_is_solutions" in step for step in steps),
        "actions": [
            {"step": i, "texts": [a.get("text") for a in step.get("customer_actions") or []
                                  if isinstance(a, dict) and a.get("text")]}
            for i, step in enumerate(steps, 1)
        ],
        # A step's source text includes what its customer actions, As-is
        # Solutions and Touchpoints cite, so each can be traced to the original.
        "step_evidence": [item["evidence"] for item in _journey_step_evidence_views(
            [{"evidence_refs": cited(step)} for step in steps], evidence_by_id)],
    }


def _journey_channel_map_response(run_dir: Path) -> dict:
    """Assemble the full customer journey map: actions and pain points from
    the run's own artefacts, emotion/thoughts as an honest "not collected"
    statement, and the four-channel grid via `_project_journey_channel_map`.

    Projects graph/channel cells from `sbp_draft.yaml` while resolving Journey
    structure from the lifecycle-current `journey.yaml`; this adds no
    generation stage and keeps Branch reads on canonical authority. Returns a
    "not generated yet" shape when the run has no draft, rather than raising.
    """
    draft_path = run_dir / "sbp_draft.yaml"
    build = _journey_build_state(run_dir)
    if build and build["state"] in {"candidate", "persona_changed"}:
        # A CJM built through an earlier Persona stays readable; its confirmation
        # state says it is not current and the gate offers 建立顧客旅程.
        return _journey_candidate_response(run_dir, build)
    if not draft_path.is_file():
        return {"state": "not_generated", "build": build}
    draft = _load(draft_path) or {}
    # The graph/channel projection may still come from the draft, but the
    # Journey structure itself must come from the lifecycle-current canonical
    # document so v3 Branches cannot disappear behind an older embedded copy.
    journey = _journey_canonical_document(run_dir)
    evidence_by_id = _evidence_lookup(run_dir)
    scenario = next(
        (s for s in journey.get("scenarios") or [] if s.get("id") == journey.get("primary_scenario")),
        None,
    )
    steps = (scenario or {}).get("steps") or []
    step_labels = [s.get("label", "") for s in steps]
    step_index_by_ref = {s.get("id"): i for i, s in enumerate(steps, 1)}

    nodes = (draft.get("graph") or {}).get("nodes") or []
    annotations = draft.get("annotations") or []
    channel_map = _project_journey_channel_map(nodes, annotations, step_index_by_ref, len(steps))

    pain_points: dict[int, list[str]] = {}
    for annotation in annotations:
        if not isinstance(annotation, dict) or annotation.get("type") != "pain_point":
            continue
        step = step_index_by_ref.get(annotation.get("step_ref"))
        if step is None:
            continue
        pain_points.setdefault(step, []).append(annotation.get("text") or "")

    unassigned = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("channel"):
            continue
        step = node.get("step")
        step_label = step_labels[step - 1] if isinstance(step, int) and 1 <= step <= len(step_labels) else ""
        unassigned.append({"node_id": node.get("id"), "label": node.get("label") or "", "step_label": step_label})

    response = {
        "state": "ready",
        "journey_confirmed": (scenario or {}).get("status") == "confirmed",
        "header": {
            "primary_hmw": _primary_hmw_statement(run_dir) or JOURNEY_MAP_NOT_CHOSEN_YET,
            "topic": formation_basis.journey_formation_hmw(run_dir),
            "primary_target": _primary_target_name(run_dir) or JOURNEY_MAP_NOT_CHOSEN_YET,
        },
        "steps": [{"step": i, "id": step.get("id") or "", "label": label, "stage_id": step.get("stage_id")} for i, (step, label) in enumerate(zip(steps, step_labels), 1)],
        "stages": [{"id": stage.get("id"), "label": stage.get("label")} for stage in (scenario or {}).get("stages") or [] if isinstance(stage, dict)],
        "actions": [
            {"step": i, "id": step.get("id") or "", "texts": [a.get("text") for a in step.get("customer_actions") or [] if isinstance(a, dict) and a.get("text")]}
            for i, step in enumerate(steps, 1)
        ],
        "step_evidence": [
            item["evidence"] for item in _journey_step_evidence_views(steps, evidence_by_id)
        ],
        "scenarios": _journey_scenario_views(journey, evidence_by_id),
        "pain_points": [
            {"step": step, "texts": pain_points.get(step, [])} for step in range(1, len(steps) + 1)
        ],
        # Verified absences (design.md's "實作後補記"), not inferred ones:
        # schema/evidence.schema.json and schema/journey.schema.json's step
        # definition both genuinely have no field for either.
        "emotions": {"collected": False, "upstream_stage": UNCOLLECTED_UPSTREAM_STAGE},
        "thoughts": {"collected": False, "upstream_stage": UNCOLLECTED_UPSTREAM_STAGE},
        "channels": channel_map,
        "annotations": _journey_annotations(run_dir),
        "unassigned": unassigned,
        "unassigned_count": len(unassigned),
        # Channel classification is intentionally outside confirmed-journey
        # lifecycle validation; expose that existing rule rather than making
        # the UI reverse-engineer it from a missing gate.
        "channel_work_blocking": False if (scenario or {}).get("status") == "confirmed" else None,
        "staged_count": len(_staged_judgements(run_dir)),
    }
    if journey.get("schema_version") == 3:
        response["branches"] = _journey_branch_views(journey, evidence_by_id)
    return response


# rebuild-axial-coding-review-screen: the selective-coding framework-point
# stage's own review content (theme + verbatim quotes + folded-in codes,
# plus the "備查" backlog of evidence no category covers). Writes here go
# straight into axial_coding.yaml / the new adjustments file -- unlike the
# journey-map's channel judgements there is no downstream projection to
# recompute, so the staged-then-applied mechanism above is unneeded ceremony
# for these three small, independently-valid actions.
AXIAL_CODING_ADJUSTMENTS_FILE = "axial_coding_adjustments.yaml"


def _axial_coding_run_dir(run_name: object) -> Path:
    safe_name = _safe_component(str(run_name or ""), "")
    if not safe_name:
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    run_dir = RUNS_DIR / safe_name
    if not run_dir.is_dir():
        raise ReviewRunNotFound(f"找不到 run: {run_name}")
    return run_dir


def _load_axial_coding_adjustments(run_dir: Path) -> dict:
    path = run_dir / AXIAL_CODING_ADJUSTMENTS_FILE
    if not path.is_file():
        return {"schema_version": 1, "excluded_evidence_refs": []}
    data = _load(path)
    return data if isinstance(data, dict) else {"schema_version": 1, "excluded_evidence_refs": []}


def _evidence_lookup(run_dir: Path) -> dict[str, dict] | None:
    """None means this run's evidence genuinely cannot be located, distinct
    from an empty run.

    Reuses evidence_locator.locate_evidence() -- the same shared resolver
    tools/workbench/server.py already uses -- rather than only checking for
    a local evidence.yaml. Real runs disagree on where evidence lives: the
    flagship 2026-09-04-manual-transport-muji-full has no local copy at all,
    only a run.json pointer to the case-level document; checking only the
    local file (this function's own first version) reported "no evidence"
    for a run whose 73 records are in fact resolvable, and would have led to
    copying a duplicate of the case-level file into the run to work around
    it -- a real duplication risk the shared resolver already exists to
    avoid.
    """
    records, _status = evidence_locator.locate_evidence(run_dir)
    if records is None:
        return None
    return {item["id"]: item for item in records if isinstance(item, dict) and item.get("id")}


def _open_coding_concepts(run_dir: Path) -> list[dict] | None:
    """None means "this run has no open_coding.yaml" -- an ordinary, optional
    absence (add-open-coding-stage), not a gap to flag."""
    path = run_dir / "open_coding.yaml"
    if not path.is_file():
        return None
    data = _load(path) or {}
    return [c for c in (data.get("concepts") or []) if isinstance(c, dict)]


def _project_axial_category_concepts(
    categories: list[dict], concepts: list[dict] | None, evidence_by_id: dict[str, dict]
) -> dict[str, list[dict] | None]:
    """rebuild-axial-coding-and-narrative-screen: one card per open-coding
    concept whose evidence overlaps a category's evidence, carrying the
    concept's name, the verbatim quotes and speakers behind it, and whether
    it also rests in another category. Supersedes the flat name-only
    `codes` projection this replaced -- a concept is shown once per
    category it genuinely rests on, not deduplicated by name, because two
    concepts can share a name while being distinct records (design.md
    "A concept spanning two categories is marked, not duplicated").

    Every category maps to None when `concepts` is None, the same "no open
    coding happened for this run" signal the superseded projection used."""
    if concepts is None:
        return {category.get("id"): None for category in categories}
    # axial-explicit-membership (2026-10-03): a category that records
    # `concept_refs` contains exactly those open codes; one without them (an
    # older run) keeps the evidence-overlap membership.
    explicit_membership = {category.get("id"): set(category.get("concept_refs") or [])
                           for category in categories if category.get("concept_refs")}
    category_refs = [(category.get("id"), set(category.get("evidence_refs") or [])) for category in categories]

    def belongs(cat_id, cat_refs, concept) -> bool:
        if cat_id in explicit_membership:
            return concept.get("id") in explicit_membership[cat_id]
        return bool(cat_refs & set(concept.get("evidence_refs") or []))

    overlap_counts = [sum(1 for cat_id, cat_refs in category_refs if belongs(cat_id, cat_refs, concept))
                      for concept in concepts]
    result: dict[str, list[dict] | None] = {}
    for cat_id, cat_refs in category_refs:
        cards = []
        for index, concept in enumerate(concepts):
            if not belongs(cat_id, cat_refs, concept):
                continue
            quotes = [
                {"text": evidence_by_id[ref].get("text") or "", "role": evidence_by_id[ref].get("role") or ""}
                for ref in concept.get("evidence_refs") or []
                if ref in evidence_by_id
            ]
            cards.append({
                "name": concept.get("name"),
                "quotes": quotes,
                "shared": overlap_counts[index] > 1,
            })
        result[cat_id] = cards
    return result


def _project_axial_category_conflicts(categories: list[dict], conflicts: list[dict]) -> dict[str, list[str]]:
    """rebuild-axial-coding-and-narrative-screen: a recorded conflict's
    description sits beside every category its evidence overlaps, and no
    other (design.md "A contradicting conflict is shown beside the category
    it contradicts") -- the same evidence-overlap derivation the concept
    cards use, applied to conflicts instead."""
    result: dict[str, list[str]] = {}
    for category in categories:
        cat_refs = set(category.get("evidence_refs") or [])
        result[category.get("id")] = [
            conflict.get("description") or ""
            for conflict in conflicts
            if isinstance(conflict, dict) and (set(conflict.get("evidence_refs") or []) & cat_refs)
        ]
    return result


def _axial_coding_backlog(
    categories: list[dict], evidence_by_id: dict[str, dict], excluded_refs: set[str]
) -> list[dict]:
    """Every evidence record referenced by no category's evidence_refs and
    not recorded as excluded -- the "備查" list. rebuild-axial-coding-and-
    narrative-screen: this computation and its endpoints stay; only the
    axial-coding review screen stops rendering it (the researcher's
    explicit deferral, recorded in that change's Non-Goals)."""
    categorized: set[str] = set()
    for category in categories:
        categorized.update(category.get("evidence_refs") or [])
    return [
        {"evidence_ref": evidence_id, "text": record.get("text") or "", "role": record.get("role") or ""}
        for evidence_id, record in evidence_by_id.items()
        if evidence_id not in categorized and evidence_id not in excluded_refs
    ]


def _axial_coding_review_response(run_dir: Path) -> dict:
    ax_path = run_dir / "axial_coding.yaml"
    if not ax_path.is_file():
        return {"state": "not_generated"}
    doc = _load(ax_path) or {}
    categories = doc.get("categories") or []
    evidence_by_id = _evidence_lookup(run_dir)
    if evidence_by_id is None:
        return {
            "state": "no_evidence",
            "categories": [
                {"id": c.get("id"), "label": c.get("label"),
                 "category_name": c.get("category_name") or "",
                 "description": c.get("discriminating_basis") or "", "quotes": []}
                for c in categories
            ],
            "regenerate_core_category_command": _regenerate_core_category_command(run_dir),
            "narrative": _selective_coding_narrative_view(run_dir),
            "pair_candidates": _narrative_job_candidates_view(run_dir),
            "pair_candidates_state": _narrative_job_candidates_state(run_dir),
            "pair_selection": _narrative_job_selection_view(run_dir),
            "propose_pairs_command": _propose_narrative_job_candidates_command(run_dir),
            "generate_narrative_command": _generate_narrative_command(run_dir),
        }
    adjustments = _load_axial_coding_adjustments(run_dir)
    excluded_refs = set(adjustments.get("excluded_evidence_refs") or [])
    open_concepts = _open_coding_concepts(run_dir)
    # One response shares one quote lookup; never retain it between requests.
    quote_context = {"concepts": {c.get("id"): c for c in open_concepts or []},
                     "evidence": evidence_by_id}
    concepts_by_category = _project_axial_category_concepts(
        categories, open_concepts, evidence_by_id
    )
    conflicts_path = run_dir / "conflicts.yaml"
    conflicts = (_load(conflicts_path) or []) if conflicts_path.is_file() else []
    conflicts_by_category = _project_axial_category_conflicts(categories, conflicts)
    category_views = []
    for category in categories:
        quotes = [
            {"text": evidence_by_id[ref].get("text") or "", "role": evidence_by_id[ref].get("role") or ""}
            for ref in category.get("evidence_refs") or []
            if ref in evidence_by_id
        ]
        view = {
            "id": category.get("id"), "label": category.get("label"),
            "category_name": category.get("category_name") or "",
            "description": category.get("discriminating_basis") or "", "quotes": quotes,
        }
        # axial-explicit-membership: the short definition and the chosen
        # representative quotes, when this run's axial coding carries them.
        if category.get("definition"):
            view["definition"] = category["definition"]
        picked = [ref for ref in category.get("representative_evidence_refs") or [] if ref in evidence_by_id]
        if category.get("representative_quote_refs"):
            # converge-coding-contracts: the member open codes' readable quotes.
            view["representative_quotes"] = _open_code_quote_views(
                run_dir, category["representative_quote_refs"], context=quote_context)
        elif picked:
            view["representative_quotes"] = [
                {"text": evidence_by_id[ref].get("text") or "", "role": evidence_by_id[ref].get("role") or ""}
                for ref in picked]
        concepts = concepts_by_category.get(category.get("id"))
        if concepts is not None:
            view["concepts"] = concepts
        conflicts_here = conflicts_by_category.get(category.get("id"))
        if conflicts_here:
            view["conflicts"] = conflicts_here
        category_views.append(view)
    # evidence-basis-inspection: the JTBD basis reuses the theme quotes this
    # response already resolves, instead of re-reading the coding per candidate.
    selective_view = _selective_themes_view(run_dir, categories, evidence_by_id, quote_context=quote_context)
    return {
        "state": "ready",
        "categories": category_views,
        "formation_note": doc.get("formation_note") or "",
        "research_purpose": _research_purpose_view(run_dir),
        "sources": _run_source_names(run_dir),
        "selective_themes": selective_view,
        "open_codes": _open_codes_by_source(open_concepts, evidence_by_id),
        "backlog": _axial_coding_backlog(categories, evidence_by_id, excluded_refs),
        "regenerate_core_category_command": _regenerate_core_category_command(run_dir),
        "narrative": _selective_coding_narrative_view(run_dir),
        "pair_candidates": _narrative_job_candidates_view(run_dir, selective_view),
        "pair_candidates_state": _narrative_job_candidates_state(run_dir),
        "pair_selection": _narrative_job_selection_view(run_dir),
        "propose_pairs_command": _propose_narrative_job_candidates_command(run_dir),
        "generate_narrative_command": _generate_narrative_command(run_dir),
    }


def _run_case_folder(run_dir: Path) -> Path | None:
    """The case folder a run was coded from (run.json `selected_case`).

    secure-data-root-writes: the stored pointer is honored only when it resolves
    (symbolic links and `..` included) inside the active data root's cases/.
    A copied or restored corpus keeps the absolute paths it was coded with, so
    an outside pointer is treated as no recorded folder and callers fall back
    to their own lookup inside the data root; the stored value is not rewritten.
    """
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    selected = state.get("selected_case")
    if not isinstance(selected, str) or not selected.strip():
        return None
    path = Path(selected)
    if not path.is_absolute():
        path = _active_data_root() / path
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if _active_cases_dir().resolve() not in resolved.parents:
        return None
    return resolved.parent if resolved.parent.is_dir() else None


def _run_source_names(run_dir: Path) -> list[str] | None:
    """separate-evidence-pool: the documents this run read, from its own
    source_manifest.yaml -- the raw material, kept apart from the open codes
    derived from it. None when the run recorded no readable manifest."""
    try:
        manifest = _load(run_dir / "source_manifest.yaml")
    except Exception:
        return None
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list):
        return None
    names = [Path(str(source.get("filename"))).stem for source in sources
             if isinstance(source, dict) and str(source.get("filename") or "").strip()]
    return list(dict.fromkeys(names))


def _research_purpose_view(run_dir: Path) -> dict:
    """add-selective-coding-layer: the run's research purpose, falling back to
    the case folder's when the run has not copied one yet."""
    text = sbp_selective.read_purpose(run_dir)
    if not text:
        folder = _run_case_folder(run_dir)
        text = sbp_selective.read_purpose(folder) if folder else ""
    return {"text": text}


def _open_code_quote_views(run_dir: Path, code_ids: list, *, context: dict | None = None) -> list[dict]:
    """converge-coding-contracts: chosen open codes' readable quotes as the
    station shows them (text, optional question, the speaker -- the cited
    evidence's shared label when more specific -- and the segment's
    location). The quote text is never rewritten here."""
    from sbp_gen import semantic_segments as sbp_segments
    if context is None:
        context = {"concepts": {c.get("id"): c for c in _open_coding_concepts(run_dir) or []},
                   "evidence": {e["id"]: e for e in (evidence_locator.locate_evidence(run_dir)[0] or [])
                                if isinstance(e, dict) and e.get("id")} if code_ids else {}}
    if "segments" not in context:
        context["segments"] = sbp_segments.segments_by_id(sbp_segments.load(run_dir))
    concepts, segments, evidence_by_id = context["concepts"], context["segments"], context["evidence"]
    views = []
    for ref in code_ids or []:
        code = concepts.get(ref) or {}
        view = sbp_segments.quote_view(code, segments, evidence_by_id)
        if view:
            views.append({"text": view["answer"], "role": view["role"], "locator": view["locator"],
                          "source": _open_code_source(code, evidence_by_id),
                          **({"question": view["question"]} if view.get("question") else {})})
    return views


def _open_code_source(code: dict, evidence_by_id: dict) -> str:
    """readable-source-location: the stored file(s) an open code's quote can be
    traced back to -- the cited evidence's `source`, distinct and in first-seen
    order, else the transcript the code was coded from. Read-only display data.
    Each source is reduced to its file name before joining, so the screen,
    which keeps only the text after the last folder separator, lists them all."""
    sources: list[str] = []
    for ref in code.get("evidence_refs") or []:
        value = re.split(r"[\\/]", str((evidence_by_id.get(ref) or {}).get("source") or "").strip())[-1]
        if value and value not in sources:
            sources.append(value)
    return "、".join(sources) or str(code.get("source") or "").strip()


def _selective_themes_view(run_dir: Path, categories: list[dict], evidence_by_id: dict,
                           *, quote_context: dict | None = None) -> dict:
    """The first station's selective section: state plus themes with names,
    never internal ids as display text (ids are kept only for edits)."""
    view = dict(sbp_selective.freshness(run_dir))
    if (sbp_selective.state_from_run(run_dir) == "failed") and view["state"] in ("none", "needs_purpose"):
        view["state"] = "failed"
    with _job_lock:
        if _job.get("kind") == SELECTIVE_THEMES_OPERATION_KIND and _job.get("state") == "running" \
                and _job.get("output_identity") == run_dir.name:
            view["forming"] = True
    doc = sbp_selective.load_document(run_dir)
    if doc is None:
        return view
    names = {c.get("id"): c.get("category_name") or c.get("label") or "" for c in categories}
    concepts = (quote_context["concepts"] if quote_context is not None else
                {c.get("id"): c for c in _open_coding_concepts(run_dir) or []})
    view["themes"] = [{
        "id": theme.get("id"), "tier": theme.get("tier"), "name": theme.get("name"),
        "core_sentence": theme.get("core_sentence"), "insight": theme.get("insight"),
        "key_insights": list(theme.get("key_insights") or []),
        "axial_names": [names.get(ref, "") for ref in theme.get("axial_refs") or [] if names.get(ref)],
        "open_code_names": [concepts[ref].get("name") for ref in theme.get("concept_refs") or [] if ref in concepts],
        "quotes": (_open_code_quote_views(run_dir, theme["representative_quote_refs"], context=quote_context)
                   if theme.get("representative_quote_refs") else
                   [{"role": evidence_by_id[ref].get("role") or "", "text": evidence_by_id[ref].get("text") or "",
                     "locator": evidence_by_id[ref].get("locator") or "",
                     "source": evidence_by_id[ref].get("source") or ""}
                    for ref in theme.get("representative_evidence_refs") or [] if ref in evidence_by_id]),
    } for theme in doc.get("themes") or [] if isinstance(theme, dict)]
    return view


def _research_purpose_case_folder(case_id: object) -> Path | None:
    case = next((c for c in _discover_cases() if c.get("case_id") == case_id), None) if case_id else None
    return (_active_data_root() / case["path"]).parent if case and case.get("path") else None


def _handle_research_purpose(handler, payload: object) -> None:
    """POST /api/research-purpose {case_id, run?, text}: written to the case
    folder, and to the run when one is given."""
    payload = payload if isinstance(payload, dict) else {}
    run_dir = None
    folder = None
    if payload.get("run"):
        try:
            run_dir = _axial_coding_run_dir(payload.get("run"))
        except ReviewRunNotFound as exc:
            return handler._send({"error": str(exc)}, 404)
        folder = _run_case_folder(run_dir)
    if folder is None and payload.get("case_id"):
        case = next((c for c in _discover_cases() if c.get("case_id") == payload.get("case_id")), None)
        if case and case.get("path"):
            folder = (_active_data_root() / case["path"]).parent
    if folder is None and run_dir is None:
        return handler._send({"error": "找不到這個案例。"}, 404)
    # cross-project-state-isolation: the purpose is case-level research input. Its identity
    # is the case folder's purpose (the one the screen read by case_id), checked under the
    # case folder's lock -- and the run's lock too when the run also receives a copy.
    target = folder if folder is not None else run_dir
    try:
        with contextlib.ExitStack() as locks:
            for directory in (folder, run_dir):
                if directory is not None:
                    locks.enter_context(sbp_run_lock.hold(directory))
            try:
                artifact_identity.check(target, "research-purpose", payload.get("basis"))
            except artifact_identity.StaleBasis:
                body, status = artifact_identity.refusal()
                return handler._send(body, status)
            if folder is not None:
                sbp_selective.write_purpose(folder, payload.get("text"))
            if run_dir is not None:
                sbp_selective.write_purpose(run_dir, payload.get("text"))
            identities = {"research-purpose": artifact_identity.compute(target, "research-purpose")}
    except sbp_run_lock.RunLockBusy:
        return handler._send({"error": sbp_run_lock.BUSY_MESSAGE, "reason": "project_busy"}, 409)
    except sbp_selective.SelectiveError as exc:
        return handler._send({"error": str(exc)}, 409)
    return handler._send({"research_purpose": {"text": str(payload.get("text") or "").strip()},
                          "selective_themes": dict(sbp_selective.freshness(run_dir)) if run_dir else None,
                          "identities": identities})


def _run_selective_themes_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "selective-themes", "--run", str(run_dir)]
    try:
        code = _stream_stage(cmd, "形成選擇編碼", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"選擇編碼沒有跑起來：{exc}"
        return
    with _job_lock:
        _job["proc"] = None
        if _job.get("state") == "cancelled":
            return
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = "已經把提示詞準備好，等模型回覆"
        elif code == 0:
            _job["state"] = "done"
            _job["phase"] = "選擇編碼已形成"
        else:
            _job["state"] = "failed"
            _job["phase"] = "選擇編碼沒有形成，原本的內容沒有變動"
        chain = _job.get("state") == "done" and _selective_continues_to_candidates(run_dir)
        if chain:
            # chain-jtbd-candidates-after-selective: no researcher judgement
            # lies between Selective Coding and the candidates, so a run with
            # no candidates yet goes on to the JTBD choice in the same job.
            _replace_job(kind=JTBD_CANDIDATES_OPERATION_KIND, state="running", phase="正在形成顧客任務候選",
                         log=[], out=run_dir.name, output_identity=run_dir.name)
    if chain:
        _run_jtbd_candidates_cli(run_dir, execution)


def _selective_continues_to_candidates(run_dir: Path) -> bool:
    """Only a run with no candidates goes on; existing ones, current or
    stale, are left for the researcher (stale is marked, never re-formed)."""
    if (run_dir / "narrative_job_candidates.yaml").is_file():
        return False
    try:
        sbp_selective.candidates_input(run_dir)
    except sbp_selective.SelectiveError:
        return False
    return True


def _handle_generate_selective_themes(handler, payload: object) -> None:
    """POST /api/selective-themes/generate {run}: form or re-form on request."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    try:
        sbp_selective.structure_input(run_dir)
    except sbp_selective.SelectiveError as exc:
        return handler._send({"error": str(exc)}, 409)
    if sbp_selective.load_document(run_dir) is not None and sbp_selective.freshness(run_dir)["state"] != "stale":
        return handler._send({"error": "選擇編碼目前是最新的，不需要重新形成。"}, 409)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        _replace_job(kind=SELECTIVE_THEMES_OPERATION_KIND, state="running", phase="正在形成選擇編碼",
                     log=[], out=run_dir.name, output_identity=run_dir.name)
    _start_job_thread(_run_selective_themes_cli, (run_dir, execution))
    return handler._send({"ok": True, "kind": SELECTIVE_THEMES_OPERATION_KIND, "state": "running"}, 202)


def _handle_edit_selective_theme(handler, payload: object) -> None:
    """POST /api/selective-themes/edit {run, theme_id, field, text}."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    doc = sbp_selective.load_document(run_dir)
    if doc is None:
        return handler._send({"error": "這份 run 還沒有選擇編碼。"}, 409)
    try:
        edited = sbp_selective.apply_edit(doc, str(payload.get("theme_id") or ""), str(payload.get("field") or ""),
                                          payload.get("text"))
    except sbp_selective.SelectiveError as exc:
        return handler._send({"error": str(exc)}, 400)
    sbp_selective.write_document(run_dir, edited)
    return handler._send({"ok": True})


def _open_codes_by_source(concepts: list[dict] | None, evidence_by_id: dict) -> list[dict] | None:
    """axial-explicit-membership: the run's open codes grouped by transcript,
    each with its verbatim evidence (role, text, locator). No internal id is
    returned for display. None when the run has no open coding."""
    if concepts is None:
        return None
    groups: list[dict] = []
    by_source: dict[str, dict] = {}
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        source = str(concept.get("source") or "")
        group = by_source.get(source)
        if group is None:
            group = {"source": source, "codes": []}
            by_source[source] = group
            groups.append(group)
        group["codes"].append({
            "name": concept.get("name") or "",
            "description": concept.get("description") or "",
            "quotes": [{"role": evidence_by_id[ref].get("role") or "", "text": evidence_by_id[ref].get("text") or "",
                        "locator": evidence_by_id[ref].get("locator") or "",
                        "source": evidence_by_id[ref].get("source") or ""}
                       for ref in concept.get("evidence_refs") or [] if ref in evidence_by_id],
        })
    return groups


def _narrative_job_candidates_view(run_dir: Path, selective_view: dict | None = None) -> list[dict] | None:
    """rebuild-stage-two-as-cards-modal-and-pair-picker: the run's three
    paired candidates, or None when none have been produced.

    None and an empty list mean different things to the screen: None is
    "nobody has run the producer", which gets a plain statement and the
    command; an empty list would mean the producer ran and returned
    nothing, which its own schema forbids.
    """
    path = run_dir / "narrative_job_candidates.yaml"
    if not path.is_file():
        return None
    doc = _load(path) or {}
    candidates = [c for c in (doc.get("candidates") or []) if isinstance(c, dict)]
    themes = {t.get("id"): t for t in ((sbp_selective.load_document(run_dir) or {}).get("themes") or [])
              if isinstance(t, dict) and t.get("id")}
    # Theme quotes as the selective section already shows them; resolved here
    # (once per theme) only when the caller has no selective view to share.
    quotes = {t.get("id"): t.get("quotes") or [] for t in (selective_view or {}).get("themes") or []
              if isinstance(t, dict) and t.get("id")}
    out = []
    for candidate in candidates:
        try:
            for ref in candidate.get("selective_refs") or []:
                if ref in themes and ref not in quotes:
                    quotes[ref] = _theme_quote_views(run_dir, themes[ref])
            basis = _jtbd_candidate_basis(candidate, themes, quotes)
        except Exception:  # a broken basis never hides the candidate itself
            basis = None
        out.append({**candidate, "basis": basis} if basis else candidate)
    return out


def _theme_quote_views(run_dir: Path, theme: dict) -> list[dict]:
    """A selective theme's representative quotes as the coding station shows
    them: open-code quotes for the current method, evidence records for a
    theme that cites evidence directly."""
    if theme.get("representative_quote_refs"):
        return _open_code_quote_views(run_dir, theme["representative_quote_refs"])
    refs = theme.get("representative_evidence_refs") or []
    records = (evidence_locator.locate_evidence(run_dir)[0] or []) if refs else []
    return framework_points.basis_evidence(refs, records, limit=len(refs))


def _jtbd_candidate_basis(candidate: dict, themes: dict, quotes: dict) -> dict | None:
    """evidence-basis-inspection: why this JTBD candidate exists, from the
    selective themes it was formed from and its own stored rationale. None
    when nothing stored can explain it."""
    cited = [themes[ref] for ref in candidate.get("selective_refs") or [] if ref in themes]
    shown_themes = [{"name": str(t.get("name") or ""), "core_sentence": str(t.get("core_sentence") or ""),
                     "support_summary": str(t.get("support_summary") or "")} for t in cited]
    shown_themes = [t for t in shown_themes if any(t.values())]
    evidence = []
    for ref in candidate.get("selective_refs") or []:
        for quote in quotes.get(ref) or []:
            if len(evidence) >= framework_points.BASIS_EVIDENCE_LIMIT:
                break
            # readable-source-location: the basis is a judgement aid, not a provenance
            # trace -- the coding station's quotes carry a source file; the basis does not.
            quote = {key: value for key, value in quote.items() if key != "source"}
            if quote not in evidence:
                evidence.append(quote)
    meaning = str(candidate.get("rationale") or "").strip()
    if not (shown_themes or meaning or evidence):
        return None
    return {"themes": shown_themes, "design_meaning": meaning, "evidence": evidence}


def _narrative_job_selection_view(run_dir: Path) -> dict | None:
    """The recorded JTBDs as a list (multi-jtbd-single-job-source), or None
    when the researcher has not chosen yet. A single-object selection reads
    as one item; `stale` when Selective Coding changed since the choice."""
    path = run_dir / "narrative_job_selection.yaml"
    if not path.is_file():
        return None
    doc = _load(path) or {}
    if not isinstance(doc, dict):
        return None
    legacy = doc.get("schema_version") != 2
    framed = (doc.get("framed_by") or {}).get("selective_themes")
    current = sbp_selective.themes_digest(run_dir)
    return {"selections": narrative_framing.selections(doc), "legacy": legacy,
            "stale": bool(not legacy and current and framed != current)}


def _narrative_job_candidates_state(run_dir: Path) -> dict:
    """What the first station needs to offer 形成顧客任務候選: candidates come
    only from Selective Coding (no axial-only fallback)."""
    path = run_dir / "narrative_job_candidates.yaml"
    doc = (_load(path) or {}) if path.is_file() else None
    themes = sbp_selective.load_document(run_dir)
    current = sbp_selective.themes_digest(run_dir)
    state = {"selective_ready": themes is not None, "formed": doc is not None}
    if isinstance(doc, dict):
        state["legacy"] = doc.get("schema_version") != 2
        framed = (doc.get("framed_by") or {}).get("selective_themes")
        state["stale"] = bool(not state["legacy"] and current and framed != current)
        reason = (doc.get("insufficient_evidence") or {}).get("reason")
        if reason:
            state["insufficient_reason"] = reason
    with _job_lock:
        if _job.get("kind") == JTBD_CANDIDATES_OPERATION_KIND and _job.get("state") == "running" \
                and _job.get("output_identity") == run_dir.name:
            state["forming"] = True
    return state


def _run_jtbd_candidates_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "narrative-job-candidates", "--run", str(run_dir)]
    try:
        code = _stream_stage(cmd, "形成顧客任務候選", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"顧客任務候選沒有跑起來：{exc}"
        return
    with _job_lock:
        _job["proc"] = None
        if _job.get("state") == "cancelled":
            return
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = "已經把提示詞準備好，等模型回覆"
        elif code == 0:
            _job["state"] = "done"
            _job["phase"] = "顧客任務候選已形成"
        else:
            _job["state"] = "failed"
            _job["phase"] = "顧客任務候選沒有形成，原本的候選沒有變動"


def _handle_generate_jtbd_candidates(handler, payload: object) -> None:
    """POST /api/narrative-job-candidates/generate {run}: only from Selective Coding."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    try:
        sbp_selective.candidates_input(run_dir)
    except sbp_selective.SelectiveError as exc:
        return handler._send({"error": str(exc)}, 409)
    state = _narrative_job_candidates_state(run_dir)
    if state.get("formed") and not state.get("stale") and not state.get("legacy"):
        return handler._send({"error": "顧客任務候選目前是最新的，不需要重新形成。"}, 409)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        _replace_job(kind=JTBD_CANDIDATES_OPERATION_KIND, state="running", phase="正在形成顧客任務候選",
                     log=[], out=run_dir.name, output_identity=run_dir.name)
    _start_job_thread(_run_jtbd_candidates_cli, (run_dir, execution))
    return handler._send({"ok": True, "kind": JTBD_CANDIDATES_OPERATION_KIND, "state": "running"}, 202)


def _propose_narrative_job_candidates_command(run_dir: Path) -> str:
    """The real invocation that produces the pairs -- offered, never run."""
    return f"cd {REPO / 'tools'} && python3 -m sbp_gen narrative-job-candidates --run {run_dir}"


def _record_narrative_job_selection(run_dir: Path, answer: dict) -> dict:
    """Write the answer to a temp file and invoke the existing selection
    command, returning exactly what it says.

    No validation happens here, deliberately: the same rule the workbench's
    own selection route follows. A second implementation of the three
    checks would drift from the first, and the drift stays invisible until
    the two disagree.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.dump(answer, tmp, allow_unicode=True)
        tmp_path = tmp.name
    try:
        with sbp_run_lock.hold(run_dir) as lease:
            result = child_processes.run(
                [sys.executable, "-m", "sbp_gen", "narrative-job-selection",
                 "--run", str(run_dir), "--selection-file", tmp_path],
                cwd=str(REPO / "tools"), capture_output=True, text=True, run_lock=lease,
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


def _regenerate_core_category_command(run_dir: Path) -> str:
    """The same invocation tools/workbench/server.py's own `propose_command()`
    builds for the 'selective-coding' mechanism (`cd {TOOLS_DIR} && python3
    -m sbp_gen selective-coding --run {run_dir} --propose`), duplicated
    rather than imported because design.md's Non-Goals forbid touching
    tools/workbench/ in this change. A binding test in
    test_axial_coding_screen_sections.py ties the two literal formats
    together so a future edit to either side is caught. design.md
    "Regenerating recommendations offers its command rather than running
    it" -- this screen only offers the string, it never executes it."""
    return f"cd {REPO / 'tools'} && python3 -m sbp_gen selective-coding --run {run_dir} --propose"


def _generate_narrative_command(run_dir: Path) -> str:
    """The real `sbp-gen selective-coding-narrative` invocation -- offered,
    never executed, same rule as `_regenerate_core_category_command`."""
    return f"cd {REPO / 'tools'} && python3 -m sbp_gen selective-coding-narrative --run {run_dir}"


def _selective_coding_narrative_view(run_dir: Path) -> dict | None:
    """None means no core category has been chosen yet at all -- distinct
    from a chosen core category whose narrative has not been generated
    (that state is `{"text": None, "composed_by": None}`)."""
    path = run_dir / "selective_coding.yaml"
    if not path.is_file():
        return None
    doc = _load(path) or {}
    narrative = doc.get("narrative")
    if not narrative:
        return {"text": None, "composed_by": None}
    return {"text": narrative, "composed_by": doc.get("narrative_composed_by")}


def _require_axial_coding_doc(run_dir: Path) -> dict:
    """Same class of gap this session already found and fixed once in
    _apply_staged_judgements: _load() has no is_file() guard, so a missing
    axial_coding.yaml would otherwise surface as a raw FileNotFoundError
    (leaking a temp/run path) instead of a clean, researcher-facing refusal."""
    ax_path = run_dir / "axial_coding.yaml"
    if not ax_path.is_file():
        raise sbp_axial_coding_edits.JudgementRejected("這個案例還沒有產出主軸編碼，無法指派或建立類別。")
    return _load(ax_path) or {}


def _apply_axial_coding_assign(run_dir: Path, category_id: str, evidence_ref: str) -> dict:
    ax_path = run_dir / "axial_coding.yaml"
    doc = _require_axial_coding_doc(run_dir)
    result = sbp_axial_coding_edits.assign_evidence(doc, category_id, evidence_ref)
    errors = V.structural_errors(result, V.load_schema("axial-coding"))
    if errors:
        raise sbp_axial_coding_edits.JudgementRejected(str(errors[0]))
    _write_yaml_atomic(ax_path, result)
    return _axial_coding_review_response(run_dir)


def _apply_axial_coding_create_category(run_dir: Path, label: str, rationale: str, evidence_ref: str) -> dict:
    ax_path = run_dir / "axial_coding.yaml"
    doc = _require_axial_coding_doc(run_dir)
    result = sbp_axial_coding_edits.create_category(doc, label, rationale, evidence_ref)
    errors = V.structural_errors(result, V.load_schema("axial-coding"))
    if errors:
        raise sbp_axial_coding_edits.JudgementRejected(str(errors[0]))
    _write_yaml_atomic(ax_path, result)
    return _axial_coding_review_response(run_dir)


def _apply_axial_coding_exclude(run_dir: Path, evidence_ref: str) -> dict:
    adjustments_path = run_dir / AXIAL_CODING_ADJUSTMENTS_FILE
    doc = _load_axial_coding_adjustments(run_dir)
    result = sbp_axial_coding_edits.exclude_evidence(doc, evidence_ref)
    errors = V.structural_errors(result, V.load_schema("axial-coding-adjustments"))
    if errors:
        raise sbp_axial_coding_edits.JudgementRejected(str(errors[0]))
    _write_yaml_atomic(adjustments_path, result)
    return _axial_coding_review_response(run_dir)


def _selective_coding_narrative_bounds() -> tuple[int, int]:
    """rebuild-axial-coding-and-narrative-screen: the length bound named in
    a refusal is read from the schema, not duplicated as a literal here --
    a future edit to the methodology's stated length only has one place to
    change."""
    schema = V.load_schema("selective-coding")
    prop = (schema.get("properties") or {}).get("narrative") or {}
    return prop.get("minLength", 0), prop.get("maxLength", 10**9)


def _apply_selective_coding_narrative(run_dir: Path, narrative: str) -> dict:
    """rebuild-axial-coding-and-narrative-screen: records a researcher-
    written narrative, with the researcher recorded as its author.
    `core_category_id` and `rationale` are read from the existing document
    and carried through untouched -- this never re-derives or overwrites
    the researcher's already-made core-category choice (design.md
    "The narrative may be the assistant's or the researcher's, and the
    author is recorded")."""
    selective_path = run_dir / "selective_coding.yaml"
    if not selective_path.is_file():
        raise sbp_axial_coding_edits.JudgementRejected("這個案例還沒有選定核心類別，無法記錄敘事。")
    doc = _load(selective_path) or {}
    text = (narrative or "").strip()
    min_len, max_len = _selective_coding_narrative_bounds()
    if len(text) < min_len:
        raise sbp_axial_coding_edits.JudgementRejected(f"敘事至少要 {min_len} 字，目前 {len(text)} 字。")
    if len(text) > max_len:
        raise sbp_axial_coding_edits.JudgementRejected(f"敘事最多 {max_len} 字，目前 {len(text)} 字。")
    result = {**doc, "narrative": text, "narrative_composed_by": "researcher"}
    errors = V.structural_errors(result, V.load_schema("selective-coding"))
    if errors:
        raise sbp_axial_coding_edits.JudgementRejected(str(errors[0]))
    _write_yaml_atomic(selective_path, result)
    return result


def _framework_point_get(mechanism: str, run_name: str) -> tuple[dict, int]:
    """框架點的候選與現況，直接讀 run 目錄。

    2026-09-15 之前這裡是 HTTP proxy，把請求轉給另一個 port 上的框架點工作台。
    那支已經退役——研究者只用一個網址、只啟動一支程式，所以邏輯改成直接呼叫
    `tools/framework_points.py`（就是那支拆掉 HTTP 外殼之後留下來的同一份程式碼）。
    """
    if mechanism not in framework_points.MECHANISMS:
        return {"error": f"未知框架點：{mechanism}"}, 404
    if not run_name:
        return {"error": "run query 參數不可省略。"}, 400
    if pathnames.reject(run_name):
        return {"error": "run query 參數不是合法的路徑片段。"}, 400
    return framework_points.resolve_run_response(mechanism, RUNS_DIR / run_name), 200


def _framework_point_select(mechanism: str, body: dict) -> tuple[dict, int]:
    if mechanism not in framework_points.MECHANISMS:
        return {"error": f"未知框架點：{mechanism}"}, 404
    run_name = (body or {}).get("run") or ""
    if not run_name or pathnames.reject(run_name):
        return {"error": "run 不是合法的路徑片段。"}, 400
    answer = (body or {}).get("answer")
    if not isinstance(answer, dict):
        return {"error": "answer 必須是物件。"}, 400
    try:
        result = framework_points.run_selection_cli(mechanism, RUNS_DIR / run_name, answer)
        if result.get("busy"):
            return {"error": result["stderr"], "reason": "project_busy"}, 409
        return result, 200
    except Exception as exc:  # noqa: BLE001 - CLI 失敗原話要照實回給研究者
        return {"error": str(exc)}, 400


TO_BE_NODE_FIELDS = ("future_state", "anchors", "necessity", "external_refs", "journey_step_ref")
ANCHOR_NOT_FOUND = {"selected_solution": "（找不到對應的解方）", "journey_step": "（找不到對應的步驟）",
                    "service_node": "（找不到對應的卡片）"}


def _to_be_node_view(node: dict, nodes: dict, journey: dict) -> dict:
    """to-be-blueprint-provenance-presentation: a To-be node's own fields, unchanged,
    plus a researcher-facing name for each anchor so the page never shows an id.
    A node without these fields (any older blueprint) gets nothing."""
    view = {field: node[field] for field in TO_BE_NODE_FIELDS if node.get(field) not in (None, [], "")}
    anchors = [a for a in node.get("anchors") or [] if isinstance(a, dict)]
    if anchors:
        steps = [s for sc in (journey or {}).get("scenarios") or [] for s in sc.get("steps") or [] if isinstance(s, dict)]
        solutions = {str(c.get("id")): str(c.get("text") or "").strip() for s in steps
                     for c in s.get("solution_candidates") or [] if isinstance(c, dict)}
        step_labels = {str(s.get("id")): str(s.get("label") or "").strip() for s in steps}
        def label(kind: str, ref: str) -> str:
            found = {"selected_solution": solutions, "journey_step": step_labels}.get(kind)
            text = found.get(ref) if found is not None else (
                str((nodes.get(ref) or {}).get("label") or "").strip() if kind == "service_node" else "")
            return text or ANCHOR_NOT_FOUND.get(kind, "（找不到對應的對象）")
        view["anchor_display"] = [{"kind": a.get("kind"), "ref": a.get("ref"), "label": label(str(a.get("kind")), str(a.get("ref")))}
                                  for a in anchors]
    return view


def _analyse_graph(run_dir: Path, draft: dict, evidence: dict, conflicts: list, meta: dict) -> dict:
    """New-format adapter.  It only creates a view model; it never rewrites runs."""
    inference_reviews = sbp_inference_reviews.load(run_dir)
    nodes = {n["id"]: n for n in (draft.get("graph") or {}).get("nodes", []) if n.get("id")}
    raw_relations = list(draft.get("relations") or [])
    journey = draft.get("journey") or {}
    primary = journey.get("primary_scenario")
    scenario = next((s for s in journey.get("scenarios") or [] if s.get("id") == primary), None)
    # A confirmed journey is the authority for the displayed stage axis.  Old
    # drafts can predate an expanded journey (for example, a later split of
    # "arrival" into arrival, cancellation, and feedback).  Re-project a
    # node only when its own evidence uniquely identifies one confirmed step;
    # otherwise retain the recorded position instead of guessing from text.
    step_by_evidence = {}
    for index, step in enumerate((scenario or {}).get("steps") or [], 1):
        for ref in step.get("evidence_refs") or []:
            step_by_evidence.setdefault(ref, set()).add(index)
    projected_steps = {}
    for node_id, node in nodes.items():
        refs = [claim.get("evidence_ref") for claim in node.get("claims") or [] if isinstance(claim, dict) and claim.get("evidence_ref")]
        candidates = [next(iter(step_by_evidence[ref])) for ref in refs if len(step_by_evidence.get(ref, ())) == 1]
        if candidates:
            counts = Counter(candidates)
            best, best_count = counts.most_common(1)[0]
            if list(counts.values()).count(best_count) == 1:
                projected_steps[node_id] = best
    # The journey the draft was built from carries the researcher's adopted
    # Solutions; without it a support process that an adopted Solution needs
    # (selected_solution_ref) was dropped as an unanchored background fact.
    anchored_support = V.journey_anchored_support_ids({"meta": draft.get("meta") or {}, "nodes": list(nodes.values()),
                                                       "relations": raw_relations,
                                                       "topology_flags": draft.get("topology_flags") or [],
                                                       "external_references": draft.get("external_references") or []}, journey)
    omitted_background_ids = {
        node_id for node_id, node in nodes.items()
        if node.get("layer") == "支援流程" and node_id not in anchored_support
    }
    # A historical run can be structurally readable yet narratively wrong.
    # Keep its raw record intact, but do not let an unanchored fleet or policy
    # fact masquerade as a customer-stage support card.
    visible_nodes = {node_id: node for node_id, node in nodes.items() if node_id not in omitted_background_ids}
    markers = {}
    for marker in draft.get("markers") or []:
        markers.setdefault(marker.get("node_ref"), []).append(marker)
    n_sourced = n_inferred = 0
    roles = evidence_roles(evidence)
    attribution_layer_counts: dict[str, Counter] = {}
    unheard_actors: dict[str, dict] = {}
    def view_node(node):
        nonlocal n_sourced, n_inferred
        claims = []
        layer = node.get("layer")
        for cl in node.get("claims") or []:
            sourced = cl.get("source") == "evidence"
            n_sourced += int(sourced); n_inferred += int(not sourced)
            ev = evidence.get(cl.get("evidence_ref"), {})
            attribution = claim_attribution(node, cl, roles, evidence)
            attribution_layer_counts.setdefault(layer, Counter())[attribution["state"]] += 1
            claims.append({**cl, "source_level": cl.get("source"), "role": ev.get("role", ""), "source_text": ev.get("text", ""), "source_locator": ev.get("locator", ""), **_question_context_field(ev), "provenance": _evidence_provenance(ev, cl.get("evidence_ref")), "attribution": attribution})
        node_actor = node.get("actor")
        if node.get("kind") == "action" and node_actor and node_actor not in roles:
            entry = unheard_actors.setdefault(node_actor, {"actor": node_actor, "action_count": 0, "speaker_roles": []})
            entry["action_count"] += 1
            for claim in claims:
                role = claim.get("role")
                if role and role not in entry["speaker_roles"]:
                    entry["speaker_roles"].append(role)
        view = {"id": node["id"], "label": node.get("label", ""), "description": node.get("description", ""), "kind": node.get("kind"), "step": projected_steps.get(node["id"], node.get("step")), "layer": node.get("layer"), "actor": node.get("actor"), "actor_boundary": node.get("actor_boundary"), "visibility": node.get("visibility"), "service_role": node.get("service_role"), "scenario_refs": node.get("scenario_refs", []), "object_refs": node.get("object_refs", []), "claims": claims, "markers": [{**m, "role": evidence.get(m.get("evidence_ref"), {}).get("role", ""), "source_text": evidence.get(m.get("evidence_ref"), {}).get("text", ""), **_question_context_field(evidence.get(m.get("evidence_ref"))), "provenance": _evidence_provenance(evidence.get(m.get("evidence_ref"), {}), m.get("evidence_ref"))} for m in markers.get(node["id"], [])], "muted_inference_flag": bool(node.get("muted_inference_flag")) or sbp_inference_reviews.decision(inference_reviews, "node", node["id"]) == "plausible", "inference_review": sbp_inference_reviews.decision(inference_reviews, "node", node["id"]), "status": node.get("status"), "committed_actor": node.get("committed_actor"), "effective": node.get("effective")}
        view.update(_to_be_node_view(node, nodes, journey))
        return view
    viewed_nodes = {node_id: view_node(node) for node_id, node in visible_nodes.items()}
    attribution_summary = {"actors_without_first_hand": list(unheard_actors.values())}
    def view_relation(relation: dict) -> dict:
        endpoints = list(relation.get("endpoints") or [relation.get("from"), relation.get("to")])
        endpoints = [node_id for node_id in endpoints if node_id]
        refs = list(relation.get("evidence_refs") or relation.get("claim_refs") or [])
        evidence_view = []
        for ref in refs:
            if not isinstance(ref, str):
                continue
            ev = evidence.get(ref)
            if ev:
                evidence_view.append({"id": ref, "source": ev.get("source", ""), "locator": ev.get("locator", ""), "role": ev.get("role", ""), "text": ev.get("text", ""), **_question_context_field(ev)})
        return {
            **relation,
            "endpoints": endpoints,
            "evidence_refs": refs,
            "evidence": evidence_view,
            "unresolved_endpoints": [node_id for node_id in endpoints if node_id not in nodes],
            "unresolved_evidence_refs": [ref for ref in refs if isinstance(ref, str) and ref not in evidence],
            # 研究者判為合理的推論在畫布上跟舊的「已核對」一樣安靜下來；判斷本身另外給。
            "muted": bool(relation.get("muted")) or sbp_inference_reviews.decision(inference_reviews, "relation", relation.get("id") or "") == "plausible",
            "inference_review": sbp_inference_reviews.decision(inference_reviews, "relation", relation.get("id") or ""),
        }
    relations = [
        view_relation(relation) for relation in raw_relations
        if all(node_id in viewed_nodes for node_id in (relation.get("endpoints") or [relation.get("from"), relation.get("to")]) if node_id)
    ]
    cells, gaps, placement_raw = [], [], []
    for raw in draft.get("cells") or []:
        cell_nodes = [viewed_nodes[r] for r in raw.get("node_refs") or [] if r in viewed_nodes]
        cell = {**raw, "nodes": cell_nodes, "claims": [cl for n in cell_nodes for cl in n["claims"]], "markers": [m for n in cell_nodes for m in n["markers"]]}
        if raw.get("blank") == "no_evidence":
            gaps.append({"step": raw["step"], "layer": raw["layer"], "note": raw.get("blank_note", "")})
        elif raw.get("coverage_state") == "expected_unverified" and not raw.get("blank"):
            # 內容其實在別的步驟，不是真的缺資料——跟上面 gaps 分開放，不流入待查
            # 清單（fix-fabricated-research-gaps-from-step-axis-collapse 決定三）。
            placement_raw.append(raw)
        cells.append(cell)
    # 非主情境節點沒有畫進 cells，但仍在 graph.nodes 裡——④畫面要能明說「這張圖
    # 畫的是哪個情境、還有哪些路徑沒畫進來」（separate-non-primary-scenarios
    # -in-blueprint 決定三）。用 viewed_nodes 已經算好的證據解析結果，不重算。
    other_scenarios_view = []
    for group in draft.get("other_scenarios") or []:
        group_node_ids = [n.get("id") for n in group.get("nodes") or [] if isinstance(n, dict) and n.get("id")]
        other_scenarios_view.append({
            "scenario_id": group.get("scenario_id", ""),
            "label": group.get("label", ""),
            "customer_goal": group.get("customer_goal", ""),
            "nodes": [viewed_nodes[nid] for nid in group_node_ids if nid in viewed_nodes],
        })
    primary_scenario_view = {"label": scenario.get("label", ""), "customer_goal": scenario.get("customer_goal", "")} if scenario else None

    expectations = {x.get("id"): x for x in draft.get("expectations") or []}
    refs = []
    for cell in cells:
        cell["expectations"] = [expectations[x] for x in cell.get("expectation_refs", []) if x in expectations]
        refs.extend(cell["expectations"])
    if scenario:
        steps = [{"no": i, "id": step.get("id"), "name": step.get("label")} for i, step in enumerate(scenario.get("steps") or [], 1)]
        source = "confirmed_journey"
    else:
        steps, source = _resolve_steps((draft.get("meta") or {}).get("case_id"), cells)
    step_name_by_no = {s["no"]: s["name"] for s in steps}
    placement_questions = [
        {
            "step": raw["step"],
            "step_name": step_name_by_no.get(raw["step"], f"步驟 {raw['step']}"),
            "layer": raw["layer"],
            "note": raw.get("blank_note", ""),
        }
        for raw in placement_raw
    ]
    annotations = []
    for annotation in draft.get("annotations") or []:
        evs = [evidence.get(ref, {}) for ref in annotation.get("evidence_refs") or []]
        annotations.append({**annotation, "sources": [{"evidence_ref": ref, "text": ev.get("text", ""), "source": ev.get("source", ""), "locator": ev.get("locator", ""), "role": ev.get("role", "")} for ref, ev in zip(annotation.get("evidence_refs") or [], evs)]})
    research_gaps = draft.get("research_gaps") or []
    todo_items = [_todo_item(g) for g in research_gaps[:5]]
    briefing = _resolve_briefing(run_dir, evidence, viewed_nodes, conflicts)
    # remove-internal-ids-and-jargon 3.1: resolve each conflict's evidence_refs
    # into role/source/locator view objects, the same way view_relation()
    # resolves relation evidence above — the frontend never receives raw
    # evidence.yaml records otherwise, so a bare id is all it could show.
    conflicts_view = [
        {**conflict, "evidence": [
            {"id": ref, "source": evidence[ref].get("source", ""), "locator": evidence[ref].get("locator", ""), "role": evidence[ref].get("role", ""), "text": evidence[ref].get("text", ""),
             "provenance": _evidence_provenance(evidence[ref], ref)}
            for ref in conflict.get("evidence_refs") or [] if isinstance(ref, str) and ref in evidence
        ]}
        for conflict in conflicts
    ]
    result = {"format": "action-graph", "meta": {**draft.get("meta", {}), **meta}, "cells": cells, "nodes": list(viewed_nodes.values()), "relations": relations, "journey": journey, "annotations": annotations, "research_gaps": research_gaps, "steps": steps, "steps_source": source, "gaps": gaps, "placement_questions": placement_questions, "absents": [], "triage": [], "conflicts": conflicts_view, "expectations": refs, "todo": {"items": todo_items, "total": len(research_gaps), "more": max(0, len(research_gaps)-5)}, "stats": {"claims": n_sourced+n_inferred, "sourced": n_sourced, "inferred": n_inferred, "no_evidence": len(gaps), "confirmed_absent": 0, "conflicts": len(conflicts)}, "attribution_summary": attribution_summary, "evidence_base": evidence_base_summary(evidence), "operational_background": {"omitted_count": len(omitted_background_ids)}, "primary_scenario": primary_scenario_view, "other_scenarios": other_scenarios_view}
    if briefing is not None:
        result["briefing"] = briefing
    if draft.get("topology_flags"):
        result["topology_flags"] = _topology_flags_view(draft.get("topology_flags"), scenario)
    if draft.get("external_references"):
        result["external_references"] = [dict(ref) for ref in draft["external_references"] if isinstance(ref, dict)]
    result.update(_carry_through_view(draft, journey, scenario))
    return result


def _solution_on_step(scenario: dict | None, solution_ref, step_ref) -> dict:
    """An adopted Solution's text and its step name, as the pending list shows them."""
    step = next((s for s in (scenario or {}).get("steps") or [] if isinstance(s, dict) and s.get("id") == step_ref), {})
    solution = next((c for c in step.get("solution_candidates") or []
                     if isinstance(c, dict) and c.get("id") == solution_ref), {})
    return {"solution": str(solution.get("text") or "").strip(), "step": str(step.get("label") or "").strip()}


def _topology_flags_view(flags: list, scenario: dict | None) -> list[dict]:
    """to-be-blueprint-generation: each flagged Solution with its text and the
    step name, for the pending-confirmation list."""
    return [{**_solution_on_step(scenario, flag.get("solution_ref"), flag.get("step_ref")),
             "reason": str(flag.get("reason") or "").strip()}
            for flag in flags or [] if isinstance(flag, dict)]


def _carry_through_view(draft: dict, journey: dict, scenario: dict | None) -> dict:
    """to-be-solution-carry-through: adopted Solutions the displayed To-be
    blueprint does not carry, a non-blocking pending item. The key is present
    only when non-empty; an older blueprint has none."""
    graph = {"meta": draft.get("meta") or {}, "nodes": (draft.get("graph") or {}).get("nodes", []),
             "topology_flags": draft.get("topology_flags") or [],
             "external_references": draft.get("external_references") or []}
    adopted = V.to_be_adopted_solutions(journey)
    uncarried = [_solution_on_step(scenario, solution_id, adopted.get(solution_id))
                 for solution_id, state in V.to_be_solution_coverage(graph, journey).items() if state == "not_carried"]
    return {"uncarried_solutions": uncarried} if uncarried else {}


def _resolve_briefing(run_dir: Path, evidence: dict, viewed_nodes: dict, conflicts: list) -> dict | None:
    """`briefing` is absent from the payload entirely (no key, not an empty
    object) when the run has none — a legacy run or a run whose briefing
    failed must be indistinguishable from "this key doesn't exist yet" to
    the screen, so its fallback rendering has one condition to check
    (add-research-briefing-layer design.md "Citations are reachable").
    """
    path = run_dir / "briefing.yaml"
    if not path.exists():
        return None
    briefing = _load(path) or {}
    conflicts_by_id = {c.get("id"): c for c in conflicts if isinstance(c, dict)}
    findings = []
    for finding in briefing.get("findings") or []:
        evidence_view = [{"id": ref, **{k: evidence.get(ref, {}).get(k, "") for k in ("text", "source", "locator", "role")}} for ref in finding.get("evidence_refs") or []]
        node_view = [{"id": ref, "label": viewed_nodes.get(ref, {}).get("label", "")} for ref in finding.get("node_refs") or []]
        conflict_view = [{"id": ref, "description": conflicts_by_id.get(ref, {}).get("description", "")} for ref in finding.get("conflict_refs") or []]
        findings.append({**finding, "evidence": evidence_view, "resolved_nodes": node_view, "resolved_conflicts": conflict_view})
    interpretation = briefing.get("interpretation") or {}
    items = []
    for item in interpretation.get("items") or []:
        refs = item.get("evidence_refs") or []
        items.append({
            **item,
            "evidence": [{"id": ref, **{k: evidence.get(ref, {}).get(k, "") for k in ("text", "source", "locator", "role")}} for ref in refs],
            "resolved_nodes": [{"id": ref, "label": viewed_nodes.get(ref, {}).get("label", "")} for ref in item.get("node_refs") or []],
        })
    return {"schema_version": briefing.get("schema_version", 1), "summary": briefing.get("summary", ""), "findings": findings,
            "interpretation": {"operation_summary": interpretation.get("operation_summary", ""), "items": items} if interpretation else None}


def _analyse(run_dir: Path) -> dict:
    """把一次 run 的產出整理成前台要顯示的結構。

    這裡算的是 ADR-0002 決定四的引用複用次數排行（純計數，不需要 AI 判斷），
    以及空白三態統計。**不算假引用率**——那是語意判斷，必須靠盲測合併查核。
    """
    draft = _load(run_dir / "sbp_draft.yaml")
    conflicts = []
    if (run_dir / "conflicts.yaml").exists():
        conflicts = _load(run_dir / "conflicts.yaml") or []

    meta = {}
    if (run_dir / "run.json").exists():
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    # 讓前端在同一次 /api/run 回應裡就拿到「當下」指紋，跟 run.json 裡存的
    # reviewed_artifact_fingerprint 比對，不用等 add-run-watch-mode 的 3 秒輪詢
    # 才知道確認是否已經失效（tier-blueprint-review-status-disclosure design.md 決定九）。
    meta["current_artifact_fingerprint"] = _run_fingerprint(run_dir)

    # 證據原文：優先用 run 自己產的，其次用 run.json 記的正典檔
    evidence = {}
    ev_path = run_dir / "evidence.yaml"
    if not ev_path.exists() and meta.get("from_evidence"):
        ev_path = _active_data_root() / meta["from_evidence"]
    if ev_path.exists():
        evidence = {e["id"]: e for e in _load(ev_path)}

    if "graph" in draft:
        return _analyse_graph(run_dir, draft, evidence, conflicts, meta)

    reuse = Counter()
    claims_by_ref: dict[str, list] = {}
    gaps, absents, cells = [], [], []
    n_inferred = n_sourced = n_general = 0

    for c in draft.get("cells", []):
        claims = c.get("claims") or []
        for idx, cl in enumerate(claims):
            ref = cl.get("evidence_ref")
            if cl.get("inferred"):
                n_inferred += 1
            else:
                n_sourced += 1
            if cl.get("scope") == "general_pattern":
                n_general += 1
            if ref:
                reuse[ref] += 1
                claims_by_ref.setdefault(ref, []).append(
                    {
                        "step": c["step"],
                        "layer": c["layer"],
                        "idx": idx,
                        "text": cl.get("text", ""),
                        "scope": cl.get("scope"),
                        "inferred": bool(cl.get("inferred")),
                    }
                )
        if not claims:
            entry = {
                "step": c["step"],
                "layer": c["layer"],
                "note": c.get("blank_note", ""),
            }
            (gaps if c.get("blank") == "no_evidence" else absents).append(entry)
        cells.append(
            {
                "step": c["step"],
                "layer": c["layer"],
                # 藍圖節點上顯示的短動作描述（ADR-0004 決定一）。
                # 舊 run 沒有這個欄位，前端會退回顯示引用計數。
                "label": c.get("label"),
                "blank": c.get("blank"),
                "blank_note": c.get("blank_note", ""),
                # 整格是不是 AI 推論：全部 claim 都是推論才算。
                # 這決定節點在圖上是否畫成虛線（推論要一眼看到，不只在 popup）。
                "inferred": bool(claims) and all(cl.get("inferred") for cl in claims),
                # 節點符號（失誤點／等候點／決策點），ADR-0005 決定八。
                # 附證據原文與定位，供點開節點時的細節層顯示（review-checklist
                # spec「Marker detail on open」）——跟 claims 的 source_text 同一個做法。
                "markers": [
                    {
                        "type": m.get("type"),
                        "evidence_ref": m.get("evidence_ref"),
                        "note": m.get("note", ""),
                        "role": (evidence.get(m.get("evidence_ref")) or {}).get("role", ""),
                        "source_text": (evidence.get(m.get("evidence_ref")) or {}).get(
                            "text", ""
                        ),
                        "source_locator": (
                            evidence.get(m.get("evidence_ref")) or {}
                        ).get("locator", ""),
                        **_question_context_field(evidence.get(m.get("evidence_ref"))),
                    }
                    for m in c.get("markers", [])
                ],
                "claims": [
                    {
                        "text": cl.get("text", ""),
                        "evidence_ref": cl.get("evidence_ref"),
                        "inferred": bool(cl.get("inferred")),
                        "scope": cl.get("scope"),
                        # 來源等級：evidence／inference／researcher（ADR-0005 決定五）。
                        # 叫 source_level 而不是 source，因為底下的 "source" key 是
                        # evidence.yaml 自己的欄位（訪談逐字稿／SOP／⋯），完全是另一件事。
                        "source_level": cl.get("source"),
                        # role 是規則四在 popup 成立的依據——歸屬從格子移到
                        # 展開層，這裡必須帶出來，不然歸屬就真的消失了。
                        "role": (evidence.get(cl.get("evidence_ref")) or {}).get("role", ""),
                        "source": (evidence.get(cl.get("evidence_ref")) or {}).get("source", ""),
                        "source_text": (evidence.get(cl.get("evidence_ref")) or {}).get(
                            "text", ""
                        ),
                        "source_locator": (
                            evidence.get(cl.get("evidence_ref")) or {}
                        ).get("locator", ""),
                        **_question_context_field(evidence.get(cl.get("evidence_ref"))),
                    }
                    for cl in claims
                ],
            }
        )

    triage = [
        {
            "ref": ref,
            "count": n,
            "source_text": (evidence.get(ref) or {}).get("text", ""),
            "claims": claims_by_ref.get(ref, []),
        }
        for ref, n in reuse.most_common()
    ]

    relations = draft.get("relations", [])
    steps, steps_source = _resolve_steps((draft.get("meta") or {}).get("case_id"), cells)
    steps_by_no = {s["no"]: s["name"] for s in steps}

    return {
        "meta": {**draft.get("meta", {}), **meta},
        "cells": cells,
        "relations": relations,
        "steps": steps,
        "steps_source": steps_source,
        "gaps": gaps,
        "absents": absents,
        "triage": triage,
        "conflicts": conflicts,
        "proposed_steps": draft.get("proposed_steps", []),
        "todo": _todo(cells, relations, steps_by_no),
        "stats": {
            "claims": n_sourced + n_inferred,
            "sourced": n_sourced,
            "inferred": n_inferred,
            "general_pattern": n_general,
            "no_evidence": len(gaps),
            "confirmed_absent": len(absents),
            "conflicts": len(conflicts),
        },
    }


try:
    import openpyxl as _openpyxl

    _HAS_OPENPYXL = True
except ImportError:  # 盤點會退回只看檔名，見 _inventory 的 degraded 欄位
    _openpyxl = None
    _HAS_OPENPYXL = False


def _inventory(paths: list[str]) -> dict:
    """不花錢的盤點：只看檔名與工作表名，不讀內容、不呼叫 LLM。

    ADR-0004 決定六。這份資料的 role 本來就是「由 source 欄位機械性推導」
    （見 evidence_chongzheng.yaml 開頭註記），所以受訪者是誰在讀內容之前
    就知道了。分兩層的理由：一眼看到只訪了司機跟督導的話，可以在花任何錢
    之前決定先去補訪。
    """
    n_sheets = 0
    n_rows = 0
    sheets: list[str] = []
    for rel in paths:
        p = REPO / rel
        if not p.exists():
            continue
        if p.suffix.lower() in {".xlsx", ".xls"} and _HAS_OPENPYXL:
            try:
                # 不用 read_only：那個模式下 max_row 常常回 None 或不可靠，
                # 而列數是這個畫面用來估 align 成本的依據，不能是 0。
                wb = _openpyxl.load_workbook(p, data_only=True)
                for name in wb.sheetnames:
                    if "insight" in name.lower():  # 團隊自己的詮釋，不是受訪者
                        continue
                    n_sheets += 1
                    sheets.append(name)
                    ws = wb[name]
                    # 只算「回答」欄有內容的列——空白列與只有問題沒回答的列
                    # 不會變成證據，算進去會高估成本。
                    n_rows += sum(
                        1
                        for row in ws.iter_rows(min_row=2, max_col=2, values_only=True)
                        if row and len(row) > 1 and row[1]
                    )
                wb.close()
            except Exception:
                sheets.append(p.stem)
                n_sheets += 1
        else:
            sheets.append(p.stem)
            n_sheets += 1
            try:
                n_rows += len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            except Exception:
                pass

    # 從工作表名猜角色。純關鍵字比對，猜不到就歸「未辨識」——
    # 不要假裝猜得很準，猜不到讓人自己看比亂分類好。
    ROLE_HINTS = {
        "司機": ["司機"],
        "督導": ["督導"],
        "客服行政": ["客服", "行政", "小姐"],
        "站長／組長": ["站長", "組長", "總幹事"],
        "乘客／家屬": ["乘客", "家屬", "顧客"],
    }
    found: dict[str, list[str]] = {}
    unknown = []
    for s in sheets:
        for role, keys in ROLE_HINTS.items():
            if any(k in s for k in keys):
                found.setdefault(role, []).append(s)
                break
        else:
            unknown.append(s)

    return {
        "n_files": len(paths),
        "n_sheets": n_sheets,
        "n_rows": n_rows,
        "roles": [{"role": r, "sheets": v} for r, v in found.items()],
        "unknown_sheets": unknown,
        "missing": [r for r in ("乘客／家屬", "客服行政") if r not in found],
        # 沒有 openpyxl 就只能看檔名，讀不到工作表名與列數。這時盤點會嚴重
        # 低估（例如「機構人員逐字稿.xlsx」的工作表名才有「督導」二字）。
        # 前端要把這件事講出來，不要讓人以為盤點結果是準的。
        "degraded": not _HAS_OPENPYXL,
    }


TODO_CAP = 5

# 這一列整層空掉，比零星一格空掉嚴重——顧客行動整列沒有第一手資料，
# 代表整張圖的顧客側都是二手轉述。
LAYER_ORDER = ["顧客行動", "前台行動", "後台行動", "支援流程", "實體證據"]


def _todo(cells: list, relations: list, steps_by_no: dict) -> dict:
    """需要研究者判斷的事，以格為單位編號，四種 kind（ADR-0005 決定五、七、十三）：

      gap        沒有任何證據覆蓋這一格
      inference  格子裡有 AI 推論的內容
      researcher 格子裡有研究者指定的內容
      break      相鄰兩步驟都有內容，卻沒有任何 sequence 關係連接

    排序規則寫死在這裡，不讓 AI 自己挑——AI 挑等於它自己決定哪些問題重要，
    而「哪裡最需要人來看」正是不該交給受測對象的判斷（沿用 ADR-0004 決定八）。
    """
    items = []

    # 資料缺口：先算每層是不是整層都空，決定排序優先度（整層空掉比零星一格
    # 空掉嚴重——顧客行動整列沒有第一手資料，代表整張圖的顧客側都是二手轉述）。
    gap_steps_by_layer: dict[str, set] = {}
    for c in cells:
        if not c["claims"]:
            gap_steps_by_layer.setdefault(c["layer"], set()).add(c["step"])
    n_steps = len(steps_by_no) or 1

    for c in cells:
        step, layer = c["step"], c["layer"]
        step_name = steps_by_no.get(step, f"步驟 {step}")
        title = f"{layer} × {step_name}"

        if not c["claims"]:
            whole_layer = len(gap_steps_by_layer.get(layer, ())) >= n_steps
            items.append(
                {
                    "kind": "gap",
                    "anchor": f"{step}|{layer}",
                    "title": title,
                    "why": c.get("blank_note") or "沒有任何證據覆蓋這一區",
                    "cost": "留白會被讀成「這裡沒有服務發生」",
                    "_rank": (
                        0 if whole_layer else 4,
                        LAYER_ORDER.index(layer) if layer in LAYER_ORDER else 9,
                        step,
                    ),
                }
            )
            continue

        inferred_claims = [cl for cl in c["claims"] if cl.get("source_level") == "inference"]
        researcher_claims = [cl for cl in c["claims"] if cl.get("source_level") == "researcher"]

        if inferred_claims:
            whole_cell = len(inferred_claims) == len(c["claims"])
            name = c.get("label") or inferred_claims[0].get("text", "")[:24]
            items.append(
                {
                    "kind": "inference",
                    "anchor": f"{step}|{layer}",
                    "title": title,
                    "why": f"「{name}」沒有直接證據，是推論",
                    "cost": "推論會被當成事實引用",
                    "_rank": (3 if whole_cell else 5, step),
                    # 三種特例（break／researcher／inference）刻意排在一般缺口
                    # 之前——它們比較稀少，混在一堆「顧客行動整批空著」裡會被
                    # 淹沒，而它們正是最需要一眼看到的例外（ADR-0005 風險二、三）。
                }
            )

        if researcher_claims:
            name = c.get("label") or researcher_claims[0].get("text", "")[:24]
            items.append(
                {
                    "kind": "researcher",
                    "anchor": f"{step}|{layer}",
                    "title": title,
                    "why": f"「{name}」的內容由研究者指定，沒有逐字稿出處",
                    "cost": "沒有標記的話，會被誤認為訪談證據",
                    "_rank": (2, step),
                }
            )

    # 斷點：相鄰步驟兩邊都有內容，卻沒有任何 sequence 關係一端在 n、另一端在 n+1。
    non_empty_steps = {c["step"] for c in cells if c["claims"]}
    seq_pairs = set()
    for r in relations:
        if r.get("type") != "sequence":
            continue
        refs = r.get("claim_refs", [])
        if len(refs) != 2:
            continue
        s1, s2 = refs[0].get("step"), refs[1].get("step")
        if s1 is not None and s2 is not None:
            seq_pairs.add((min(s1, s2), max(s1, s2)))

    all_steps = sorted(steps_by_no) if steps_by_no else sorted(non_empty_steps)
    for a, b in zip(all_steps, all_steps[1:]):
        if b - a != 1:
            continue
        if a in non_empty_steps and b in non_empty_steps and (a, b) not in seq_pairs:
            name_a = steps_by_no.get(a, f"步驟 {a}")
            name_b = steps_by_no.get(b, f"步驟 {b}")
            items.append(
                {
                    "kind": "break",
                    "anchor": f"{a}→{b}",
                    "title": f"{name_a} → {name_b}（銜接）",
                    "why": "沒有任何證據說明這兩個歷程怎麼銜接",
                    "cost": "圖上的斷點會被當成漏畫",
                    "_rank": (1, a),
                }
            )

    items.sort(key=lambda x: x["_rank"])
    for it in items:
        del it["_rank"]

    total = len(items)
    return {
        "items": items[:TODO_CAP],
        "total": total,
        "more": max(0, total - TODO_CAP),
    }


def _discovery_input_path(payload: dict) -> str | None:
    """Where a fresh discovery reads the project's files from.

    A project made from uploaded files keeps them in `raw/` beside its
    case.yaml. The home page's 開始整理服務歷程 did not send that folder, so
    the CLI refused with "--input 與 --from-evidence 至少要給一個" (walkthrough
    2026-09-27). The request's own `input` wins; otherwise a discovery on a
    case with a `raw/` folder reads it. Never used when evidence is given.
    """
    explicit = payload.get("input")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    if payload.get("from_evidence") or not payload.get("discover_journey"):
        return None
    case = payload.get("case")
    if not isinstance(case, str) or not case:
        return None
    raw = (_active_data_root() / case).parent / "raw"
    if not raw.is_dir():
        return None
    try:
        return str(raw.resolve().relative_to(_active_data_root().resolve()))
    except ValueError:
        return None


def _build_generate_cmd(
    case_path: str,
    from_evidence: str | None,
    out_name: str,
    effort: str,
    researcher_note: str | None = None,
    journey_path: Path | None = None,
    input_path: str | None = None,
    discover_journey: bool = False,
) -> list[str]:
    """組出 CLI 指令。抽成獨立函式，不用真的開 subprocess 就能測空值判斷
    （researcher-note-passthrough spec「Server omits the CLI flag when no
    note was sent」／「Server passes the note through as a CLI argument」）。"""
    cmd = [
        sys.executable, "-m", "sbp_gen",
        "--case", str(_active_data_root() / case_path),
        "--out", str(RUNS_DIR / out_name),
        "--build-effort", effort,
    ]
    if from_evidence:
        cmd += ["--from-evidence", str(_active_data_root() / from_evidence)]
    elif input_path:
        cmd += ["--input", str(_active_data_root() / input_path)]
    if researcher_note:
        cmd += ["--researcher-note", researcher_note]
    if journey_path:
        cmd += ["--journey-file", str(journey_path)]
    if discover_journey:
        cmd += ["--discover-journey"]
    return cmd


def _build_journey_recheck_cmd(context: dict, baseline_path: Path, edited_path: Path) -> list[str]:
    return [
        sys.executable, "-m", "sbp_gen", "journey-recheck",
        "--case", str(context["case_path"]),
        "--input", str(context["raw_root"]),
        "--evidence", str(context["evidence_path"]),
        "--baseline-journey", str(baseline_path),
        "--edited-journey", str(edited_path),
        "--out", str(context["result_dir"]),
    ]


def _run_journey_recheck_cli(context: dict) -> None:
    """Persist server-resolved inputs, run the bounded CLI, then admit result."""
    safe = context["output_identity"]
    try:
        request_dir = _recheck_root() / "_requests" / safe
        request_dir.mkdir(parents=True, exist_ok=True)
        request_meta = request_dir / "request.json"
        if request_meta.exists():
            stored = json.loads(request_meta.read_text(encoding="utf-8"))
            if stored.get("request_digest") != context["request_digest"]:
                raise JourneyRecheckConflict("輸出識別已屬於另一份歷程調整，不能覆寫。")
        else:
            request_meta.write_text(json.dumps({
                "request_digest": context["request_digest"],
                "case_id": context["case"].get("case_id"),
                "selected_case": context["case_ref"],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        baseline_path = request_dir / "baseline_journey.yaml"
        edited_path = request_dir / "edited_journey.yaml"
        baseline_path.write_text(
            yaml.dump(context["baseline_journey"], allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        edited_path.write_text(
            yaml.dump(context["edited_journey"], allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        if context.get("researcher_note"):
            (request_dir / "researcher_note.txt").write_text(context["researcher_note"], encoding="utf-8")
        cmd = _build_journey_recheck_cmd(context, baseline_path, edited_path)
        with _job_lock:
            _job["log"] = [f"$ {' '.join(cmd[2:])}"]
            _job["phase"] = "處理中"
        proc = child_processes.spawn(
            cmd,
            run_lock=_current_job_lease(),
            cwd=REPO / "tools",
            env=_execution_for_context(context).child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), _execution_for_context(context)))
        code = proc.wait()
        if code != 0:
            with _job_lock:
                _job.update(
                    state="failed",
                    phase="產出失敗",
                    error=_recheck_failure_projection(),
                    result_endpoint=None,
                )
            return
        projection = _validated_recheck_result(context, require_server_admission=False)
        if projection is None:
            raise JourneyRecheckNotFound("CLI 完成但沒有可確認的完整結果。")
        state_path = context["result_dir"] / "run.json"
        with sbp_run_files.transaction(state_path) as state:
            state.update({
                "server_admitted": True,
                "selected_case": context["case_ref"],
                "source_evidence": context["evidence_ref"],
                "result_digest": projection["result_digest"],
            })
        with _job_lock:
            _job.update(
                state="done",
                phase="待確認",
                result_endpoint=f"/api/journey-recheck?run={quote(safe)}",
                request_digest=context["request_digest"],
                result_digest=projection["result_digest"],
            )
    except Exception as exc:
        with _job_lock:
            _job["log"].append(f"[例外] {exc}")
            _job.update(
                state="failed",
                phase="產出失敗",
                error=_recheck_failure_projection(),
                result_endpoint=None,
            )


def _build_persona_content_cmd(run_dir: Path) -> list[str]:
    return [sys.executable, "-m", "sbp_gen", "persona-content", "--run", str(run_dir)]



# ── 在介面上實際跑一步（決定十五：大腦是本機 agent，不是外部 API）──────────
# 每一步都用手動傳輸 provider 執行：管線跑到要說話的那一層就停下來，把真正
# 要送出去的提示詞寫進 run 目錄，離開碼 3 代表「等模型回覆」。研究者按一下
# 就看得到現在卡在哪一階段、要餵什麼回去；回覆放好之後再按一次繼續，同一道
# 指令會把它吃掉並往下走。整條路上沒有任何外部 API 呼叫。
STAGE_COMMANDS = {
    "open_coding.yaml": ("open-coding", "開放編碼"),
    "axial_coding.yaml": ("axial-coding", "主軸編碼"),
    "narrative_job_candidates.yaml": ("narrative-job-candidates", "顧客任務候選"),
    "persona_matrix.yaml": ("persona-matrix", "Persona 矩陣"),
    "segmentation_axis_candidates.yaml": ("segmentation-axes", "目標族群軸候選"),
    "persona_content.yaml": ("persona-content", "Persona 內容"),
}
TRANSPORT_SUSPENDED_EXIT = 3
STAGE_RUN_OPERATION_KIND = "stage_run"


def _stage_run_cmd(cli: str, run_dir: Path) -> list[str]:
    cmd = [sys.executable, "-m", "sbp_gen", cli, "--run", str(run_dir)]
    if cli == "segmentation-axes":
        cmd.append("--propose")
    return cmd



def _researcher_readable_log(lines: list[str]) -> list[str]:
    """把命令列的原始輸出濾成研究者讀得懂的幾行。

    手動傳輸暫停時會印一段給終端機操作者看的指引——打開哪個檔案、存成哪個檔案、
    再跑哪一道指令——那三行對研究者沒有意義，而且帶著絕對路徑與檔名，正是
    CLAUDE.md 明列不得出現在畫面上的東西。這裡整段濾掉，只留真正講「發生了什麼」
    的那幾行，並把殘留的路徑收斂成檔案自己的名字。
    """
    root = str(REPO) + "/"
    out = []
    for line in lines or []:
        stripped = line.strip()
        if stripped.startswith(("$ ", "1. ", "2. ", "3. ", "python3 -m sbp_gen")):
            continue
        if "把它整份交給" in stripped or "SBP_TRANSPORT_MODEL" in stripped:
            continue
        # 「[手動傳輸] <內部階段名> 第 N 輪：提示詞已存好」——這件事狀態列已經
        # 用人話講過了，這行只多帶一個研究者不需要知道的內部階段名。
        if stripped.startswith("[手動傳輸]"):
            continue
        # Progress counts already appear beside the processing message.
        if stripped.startswith("[進度]"):
            continue
        cleaned = stripped.replace(root, "")
        cleaned = re.sub(r"[^\s]*/([\w.-]+\.(?:yaml|md|txt|json))", r"\1", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _run_stage_cli(
    cli: str, plain_name: str, run_dir: Path,
    execution: sbp_execution.ExecutionConfig | None = None,
    final: bool = True,
) -> None:
    execution = _require_execution(execution)
    env = execution.child_environment()
    if execution.mode == "transport":
        env["SBP_TRANSPORT_MODEL"] = "claude-opus-5"
    cmd = _stage_run_cmd(cli, run_dir)
    with _job_lock:
        _job["log"] = [f"$ {' '.join(cmd[2:])}"]
        _job["phase"] = f"正在跑{plain_name}"
    try:
        proc = child_processes.spawn(
            cmd, cwd=REPO / "tools", env=env,
            run_lock=_current_job_lease(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        with _job_lock:
            _job["proc"] = proc
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), execution))
        code = proc.wait()
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"{plain_name}沒有跑起來：{exc}"
        return
    with _job_lock:
        if _job.get("state") == "cancelled":
            return
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = f"{plain_name}已經把提示詞準備好，等模型回覆"
        elif code == 0 and not final:
            _job["phase"] = f"{plain_name}完成"
        else:
            _job["state"] = "done" if code == 0 else "failed"
            _job["phase"] = f"{plain_name}完成" if code == 0 else f"{plain_name}沒有完成"


def _run_persona_segmentation_cli(
    run_dir: Path, execution: sbp_execution.ExecutionConfig, step: str = "matrix",
) -> None:
    """One explicit researcher action: full-evidence matrix, then axis briefs."""
    if step == "matrix":
        _run_stage_cli("persona-matrix", "Persona 分群矩陣", run_dir, execution, final=False)
        with _job_lock:
            if _job.get("state") != "running":
                return
        matrix = _load(run_dir / "persona_matrix.yaml") or {}
        if len(matrix.get("axes") or []) < 2:
            with _job_lock:
                _job.update(state="done", phase="證據不足，尚無法形成兩條可比較的分群軸", proc=None,
                            segmentation_step="insufficient_evidence")
            return
        with _job_lock:
            _job["segmentation_step"] = "axes"
    _run_stage_cli("segmentation-axes", "分群軸候選", run_dir, execution)


# form-hmw-from-confirmed-narrative: the HMW station's one AI action. The five
# HMW method steps are no longer runnable file by file from the interface;
# `sbp-gen form-hmw` runs them and decides which to reuse from what each step
# actually reads (see sbp_gen/hmw_formation.py). It replaces the per-file
# stage runner and the earlier narrative rebuild chain for this station.
def _hmw_formation_job_view(job: dict) -> dict:
    try:
        run_dir = _axial_coding_run_dir(job.get("out"))
    except Exception:
        return job
    formation = sbp_hmw_formation.formation_state(run_dir)
    labels = sbp_hmw_formation.STAGE_LABELS
    active = [step for step in (formation.get("active_stages") or [formation.get("active_stage")]) if step in labels]
    if job.get("state") == "running" and active:
        # Steps that run side by side are named together, each with the count
        # it last reported (e.g. 屬性與面向 3／16 類).
        progress = _job_progress(job.get("log"))
        job["phase"] = "正在整理" + "、".join(_with_progress(labels[step], labels[step], progress) for step in active)
    elif job.get("state") == "failed":
        failed = formation.get("failed_stage")
        job["phase"] = f"{labels[failed]}沒有完成" if failed in labels else "形成 HMW 沒有完成"
        job["failure"] = formation.get("failure")
    job["log"] = _researcher_readable_log(job.get("log"))
    return job


def _mark_hmw_formation_cancelled(out: object) -> None:
    try:
        run_dir = _axial_coding_run_dir(out)
    except Exception:
        return
    sbp_hmw_formation.update_state(run_dir, state="cancelled", active_stage=None)


def _run_hmw_formation_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "form-hmw", "--run", str(run_dir)]
    try:
        code = _stream_stage(cmd, "形成 HMW", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"形成 HMW 沒有跑起來：{exc}"
        return
    with _job_lock:
        _job["proc"] = None
        cancelled = _job.get("state") == "cancelled"
    if cancelled:
        _mark_hmw_formation_cancelled(run_dir.name)
        return
    with _job_lock:
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = "已經把提示詞準備好，等模型回覆"
        elif code == 0:
            _job["state"] = "done"
            _job["phase"] = "HMW 候選已形成"
        else:
            _job["state"] = "failed"


def _preserve_superseded_journey(run_dir: Path) -> Path:
    """Copy the CJM built through an earlier Persona before an explicit rebuild.
    The originals stay in place until the new CJM exists, so a failed rebuild
    loses nothing."""
    backup = run_dir / ".superseded-journeys" / dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("journey_proposal.yaml", "journey.yaml", "journey_annotations.yaml"):
        if (run_dir / name).is_file():
            shutil.copy2(run_dir / name, backup / name)
    return backup


def _run_journey_generation_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig,
                                superseded_backup: Path | None = None) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "journey", "--run", str(run_dir), "--propose"]
    try:
        code = _stream_stage(cmd, "建立顧客旅程", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"建立顧客旅程沒有跑起來：{exc}"
        return
    with _job_lock:
        _job["proc"] = None
        if _job.get("state") == "cancelled":
            return
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = "已經把提示詞準備好，等模型回覆"
        elif code == 0:
            _job["state"] = "done"
            _job["phase"] = "旅程候選已建立"
            if superseded_backup is not None and (run_dir / "journey_annotations.yaml").is_file():
                # Annotations are keyed by step id; the new CJM may reuse those ids,
                # so the old notes move to the backup instead of attaching to it.
                (run_dir / "journey_annotations.yaml").replace(superseded_backup / "journey_annotations.yaml")
        else:
            _job["state"] = "failed"
            _job["phase"] = "建立顧客旅程沒有完成"


def _handle_build_journey(handler, payload: object) -> None:
    """POST /api/build-journey. Every refusal happens before any subprocess starts."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    build = _journey_build_state(run_dir)
    if build is None:
        return handler._send({"error": "這份產出已經有旅程或藍圖，不在這裡重新建立。"}, 409)
    if build["state"] == "candidate":
        return handler._send({"error": "旅程候選已經建立，請先檢視這一份。"}, 409)
    if build["state"] == "persona_changed" and payload.get("rebuild") is not True:
        # resolve-stale-at-researcher-gates: an existing journey is replaced only
        # when the researcher chose 「依目前 Persona 重新建立顧客旅程」.
        return handler._send({"error": "已經有顧客旅程。請在顧客旅程這一站選「保留目前顧客旅程」或「依目前 Persona 重新建立顧客旅程」。"}, 409)
    if not build["available"]:
        return handler._send({"error": build["reason"]}, 409)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        _replace_job(kind=JOURNEY_GENERATION_OPERATION_KIND, state="running", phase="正在建立顧客旅程",
                     log=[], out=run_dir.name, output_identity=run_dir.name)
    backup = _preserve_superseded_journey(run_dir) if build["state"] == "persona_changed" else None
    _start_job_thread(_run_journey_generation_cli, (run_dir, execution, backup))
    return handler._send({"ok": True, "kind": JOURNEY_GENERATION_OPERATION_KIND, "state": "running"}, 202)


def _run_journey_solutions_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "journey-solutions", "--run", str(run_dir)]
    try:
        code = _stream_stage(cmd, "發展解方", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"發展解方沒有跑起來：{exc}"
        return
    # sbp-solution-adoption-and-core-problem: Solutions developed while the CJM stays
    # confirmed reach the confirmed journey the SBP is built from, before the job reads as done.
    if code == 0:
        _sync_confirmed_solutions(run_dir)
    with _job_lock:
        _job["proc"] = None
        if _job.get("state") == "cancelled":
            return
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = "已經把提示詞準備好，等模型回覆"
        elif code == 0:
            _job["state"] = "done"
            _job["phase"] = "解方已發展"
        else:
            _job["state"] = "failed"
            _job["phase"] = "發展解方沒有完成，旅程沒有變動"


def _journey_solutions_prerequisite_reason(run_dir: Path) -> str | None:
    if not _journey_is_candidate_path(run_dir):
        return "這份產出沒有可以發展解方的顧客旅程。"
    if not _journey_persona_current(run_dir):
        return "顧客旅程不是根據目前的 Persona 與 HMW 建立的，請先重新建立顧客旅程。"
    proposal = _load(run_dir / "journey_proposal.yaml") or {}
    if not sbp_stages.selected_solution_moments(proposal):
        return "請先在旅程上勾選要發展解方的時刻。"
    return None


# add-chart-guides：三站底部的導讀。讀、改、起草都只碰 chart_guides.yaml。
def _chart_guide_kind(value: object) -> str:
    kind = str(value or "")
    if kind not in sbp_chart_guides.KINDS:
        raise ValueError("不認得這種導讀。")
    return kind


def _chart_guide_payload(run_dir: Path, kind: str) -> dict:
    view = sbp_chart_guides.guide_view(run_dir, kind)
    with _job_lock:
        drafting = (_job.get("kind") == CHART_GUIDE_OPERATION_KIND and _job.get("state") == "running"
                    and _job.get("output_identity") == f"{run_dir.name}:{kind}")
        job = {k: _job.get(k) for k in ("state", "phase")} if _job.get("output_identity") == f"{run_dir.name}:{kind}" else None
    return {**view, "drafting": drafting, "job": job}


def _run_chart_guide_cli(run_dir: Path, kind: str, execution: sbp_execution.ExecutionConfig) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "chart-guides", "--run", str(run_dir), "--kind", kind]
    try:
        code = _stream_stage(cmd, "起草導讀", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"起草導讀沒有跑起來：{exc}"
        return
    with _job_lock:
        _job["proc"] = None
        if _job.get("state") == "cancelled":
            return
        if code == TRANSPORT_SUSPENDED_EXIT:
            _job["state"] = "waiting_for_model"
            _job["phase"] = "已經把提示詞準備好，等模型回覆"
        elif code == 0:
            _job["state"] = "done"
            _job["phase"] = "導讀已起草"
        else:
            _job["state"] = "failed"
            _job["phase"] = "起草導讀沒有完成，原本的導讀沒有變動"


def _handle_draft_chart_guide(handler, payload: object) -> None:
    """POST /api/chart-guide/draft {run, kind}: starts one drafting job."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
        kind = _chart_guide_kind(payload.get("kind"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    except ValueError as exc:
        return handler._send({"error": str(exc)}, 400)
    if sbp_chart_guides.input_projection(run_dir, kind) is None:
        return handler._send({"error": "這一站還沒有可以寫導讀的內容。"}, 409)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        _replace_job(kind=CHART_GUIDE_OPERATION_KIND, state="running", phase="正在起草導讀",
                     log=[], out=run_dir.name, output_identity=f"{run_dir.name}:{kind}")
    _start_job_thread(_run_chart_guide_cli, (run_dir, kind, execution))
    return handler._send({"ok": True, "kind": CHART_GUIDE_OPERATION_KIND, "state": "running"}, 202)


def _handle_edit_chart_guide(handler, payload: object) -> None:
    """POST /api/chart-guide/edit {run, kind, block, text}: the researcher's words."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
        kind = _chart_guide_kind(payload.get("kind"))
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        projection = sbp_chart_guides.input_projection(run_dir, kind)
        digest = sbp_chart_guides.projection_digest(projection) if projection is not None else None
        document = sbp_chart_guides.apply_edit(sbp_chart_guides.load_document(run_dir), kind,
                                               str(payload.get("block") or ""), payload.get("text"), now, digest)
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    except ValueError as exc:
        return handler._send({"error": str(exc)}, 400)
    sbp_chart_guides.write_document(run_dir, document)
    return handler._send(_chart_guide_payload(run_dir, kind))


def _handle_build_journey_solutions(handler, payload: object) -> None:
    """POST /api/build-journey-solutions. Every refusal happens before any subprocess starts."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    reason = _journey_solutions_prerequisite_reason(run_dir)
    if reason:
        return handler._send({"error": reason}, 409)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    # A CJM confirmed under the earlier digest stays confirmed through this development.
    _upgrade_legacy_journey_confirmation(run_dir, _load(run_dir / "journey_proposal.yaml") or {})
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        _replace_job(kind=JOURNEY_SOLUTIONS_OPERATION_KIND, state="running", phase="正在發展解方",
                     log=[], out=run_dir.name, output_identity=run_dir.name)
    _start_job_thread(_run_journey_solutions_cli, (run_dir, execution))
    return handler._send({"ok": True, "kind": JOURNEY_SOLUTIONS_OPERATION_KIND, "state": "running"}, 202)


# generate-as-is-blueprint-candidate: Station 6's explicit 產生現況服務藍圖 for
# a fresh-path run. It runs the existing standalone `compare --run` and
# `structure --run` on the run itself -- evidence.yaml is read in place, never
# replaced (no --from-evidence / --input) -- and stops at the candidate.
def _as_is_build_state(run_dir: Path) -> dict | None:
    if not _journey_is_candidate_path(run_dir) or (run_dir / "sbp_draft.yaml").is_file():
        return None
    if _journey_confirmation(run_dir)["state"] != "confirmed":
        return {"state": "not_eligible", "available": False,
                "reason": "顧客旅程還沒確認目前這一版，請先回到顧客旅程確認後再產生服務藍圖。"}
    # sbp-solution-adoption-and-core-problem: moments without a Solution, or stale adopted
    # Solutions, now refuse the build instead of the CJM confirmation.
    blockers = _journey_moment_solution_blockers(_journey_working_copy(run_dir), run_dir)
    if blockers:
        return {"state": "not_eligible", "available": False, "reason": blockers[0]}
    research = _implementation_research_gate(run_dir)
    # cjm-builds-sbp: the station must know a solution-selection run without
    # waiting for the journey view, since such a run is built from the CJM.
    solution_selection = sbp_stages.journey_uses_solution_selection(_load(run_dir / "journey.yaml") or {})
    if research and research["state"] in sbp_implementation_research.BLOCKING:
        return {"state": "research_unresolved", "available": False, "reason": research["reason"],
                "research_blocked": True, "solution_selection": solution_selection}
    return {"state": "eligible", "available": True, "reason": None, "solution_selection": solution_selection}


# pre-blueprint-implementation-research：建立 SBP 前可選的落地研究。開始了就要
# 審查完或略過才能建立；解方或勾選改了，舊的研究保留但不算目前的。
def _implementation_research_job_active(run_dir: Path) -> bool:
    with _job_lock:
        return (_job.get("kind") == IMPLEMENTATION_RESEARCH_OPERATION_KIND and _job.get("state") == "running"
                and _job.get("out") == run_dir.name)


def _implementation_research_gate(run_dir: Path) -> dict | None:
    if not _journey_uses_solution_selection(run_dir):
        return None
    return sbp_implementation_research.gate(run_dir, job_active=_implementation_research_job_active(run_dir))


UNCERTAINTY_LABELS = {"bounded": "適用範圍有限定", "uncertain": "還不確定能否套用", "disputed": "來源之間有爭議"}


def _implementation_research_view(run_dir: Path) -> dict | None:
    """What the blueprint station shows before 建立 SBP; None when the run
    has no adopted Solutions or the blueprint already exists."""
    if not _journey_uses_solution_selection(run_dir) or (run_dir / "sbp_draft.yaml").is_file():
        return None
    if _journey_confirmation(run_dir)["state"] != "confirmed":
        return None
    IR = sbp_implementation_research
    questions = IR.questions(_load(run_dir / "journey.yaml") or {})
    if not questions:
        return None
    ticked = set(IR.load_selection(run_dir))
    gate = _implementation_research_gate(run_dir) or {"state": IR.NOT_STARTED, "reason": None}
    operation = IR.current_operation(run_dir)
    view = {
        "questions": [{"id": q["id"], "text": q["text"], "step_label": q["step_label"], "ticked": q["id"] in ticked}
                      for q in questions],
        "state": gate["state"],
        "reason": gate.get("reason"),
        "locked": operation is not None,
        "stale": operation is None and bool(IR.stale_operations(run_dir)),
        "claims": [],
    }
    if gate["state"] == IR.NO_ADMISSIBLE:
        view["message"] = IR.NO_ADMISSIBLE_MESSAGE
    if operation and operation.get("state") == "complete":
        catalog = IR.load_catalog(run_dir, operation["digest"]) or {}
        decisions = IR.saved_decisions(run_dir, operation["digest"])
        sources = {src.get("id"): src for src in catalog.get("sources") or []}

        def source_view(link):
            src = sources.get(link.get("source_ref")) or {}
            return {"title": src.get("title"), "publisher": src.get("authors_or_publisher"),
                    "url": src.get("url") or src.get("doi")}

        for claim in catalog.get("claims") or []:
            uncertainty = claim.get("uncertainty") or {}
            links = claim.get("source_links") or []
            view["claims"].append({
                "id": claim.get("id"),
                "text": claim.get("text"),
                "applicability_context": claim.get("applicability_context"),
                "uncertainty": UNCERTAINTY_LABELS.get(uncertainty.get("state"), ""),
                "uncertainty_explanation": uncertainty.get("explanation"),
                "supports": [source_view(link) for link in links if link.get("relationship") == "supports"],
                "limits": [source_view(link) for link in links if link.get("relationship") == "limits"],
                "decision": (decisions.get(str(claim.get("id"))) or {}).get("decision"),
                "rationale": (decisions.get(str(claim.get("id"))) or {}).get("rationale") or "",
            })
    return view


def _implementation_research_run_dir(payload: dict) -> Path:
    run_dir = _axial_coding_run_dir(payload.get("run"))
    if _implementation_research_view(run_dir) is None:
        raise ImplementationResearchRejected("這份產出現在不能做落地研究：要先確認含已採用解方的顧客旅程，而且還沒建立 SBP。")
    return run_dir


class ImplementationResearchRejected(Exception):
    pass


def _build_implementation_research_cmd(run_dir: Path) -> list[str]:
    return [sys.executable, "-m", "sbp_gen", "implementation-research", "--run", str(run_dir)]


def _run_implementation_research_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig) -> None:
    try:
        code = _stream_stage(_build_implementation_research_cmd(run_dir), "落地研究", execution)
    except OSError as exc:
        with _job_lock:
            _job["state"] = "failed"
            _job["phase"] = f"落地研究沒有跑起來：{exc}"
        return
    with _job_lock:
        _job["proc"] = None
        if _job.get("state") == "cancelled":
            return
        _job["state"] = "done" if code == 0 else "failed"
        _job["phase"] = "落地研究已完成" if code == 0 else "落地研究沒有完成"


def _handle_implementation_research(handler, action: str, payload: object) -> None:
    """POST /api/implementation-research/{select,start,review,skip}. Only
    `start` runs a job; every refusal happens before any subprocess starts."""
    payload = payload if isinstance(payload, dict) else {}
    IR = sbp_implementation_research
    try:
        run_dir = _implementation_research_run_dir(payload)
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    except ImplementationResearchRejected as exc:
        return handler._send({"error": str(exc)}, 409)
    if _implementation_research_job_active(run_dir):
        return handler._send({"error": "落地研究進行中，完成後再試。"}, 409)
    try:
        if action == "select":
            IR.save_selection(run_dir, payload.get("ticked"))
        elif action == "skip":
            IR.skip(run_dir)
        elif action == "review":
            decisions = payload.get("decisions") if isinstance(payload.get("decisions"), list) else []
            incomplete = _incomplete_claim_positions(run_dir, decisions)
            if incomplete:
                return handler._send({"error": "第 " + "、".join(map(str, incomplete)) + " 條還沒選擇，或還沒寫理由。",
                                      "incomplete": incomplete}, 400)
            IR.record_review(run_dir, decisions)
        elif action == "start":
            if payload.get("spend_acknowledged") is not True:
                return handler._send({"error": "開始落地研究會呼叫 AI 搜尋外部資料，請先確認會使用額度。"}, 400)
            if not IR.load_selection(run_dir):
                return handler._send({"error": "請先勾選至少一個要研究的問題。"}, 400)
            try:
                execution = _execution_for_payload(payload, allow_legacy=False)
            except sbp_execution.ExecutionConfigError as exc:
                return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
            with _job_lock:
                if _job.get("state") == "running":
                    return handler._send(
                        {"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
                IR.begin(run_dir)
                _replace_job(kind=IMPLEMENTATION_RESEARCH_OPERATION_KIND, state="running", phase="正在進行落地研究",
                             log=[], out=run_dir.name, output_identity=run_dir.name)
            _start_job_thread(_run_implementation_research_cli, (run_dir, execution))
            return handler._send({"ok": True, "kind": IMPLEMENTATION_RESEARCH_OPERATION_KIND, "state": "running"}, 202)
        else:
            return handler._send({"error": "不認得的落地研究動作。"}, 404)
    except IR.ImplementationResearchError as exc:
        return handler._send({"error": str(exc)}, 400)
    except sbp_research_catalog_review.ReviewError as exc:
        return handler._send({"error": _implementation_review_error_text(exc, run_dir)}, 400)
    return handler._send({"research": _implementation_research_view(run_dir), "build": _as_is_build_state(run_dir)})


def _incomplete_claim_positions(run_dir: Path, decisions: list) -> list[int]:
    """1-based positions of claims with no decision or a blank rationale, in
    the order the station shows them."""
    given = {str(d.get("claim_ref")): d for d in decisions if isinstance(d, dict)}
    positions = []
    for index, claim in enumerate((_implementation_research_view(run_dir) or {}).get("claims") or [], 1):
        entry = given.get(str(claim.get("id"))) or {}
        rationale = entry.get("researcher_rationale")
        if entry.get("decision") not in ("select", "hold", "reject") or not (isinstance(rationale, str) and rationale.strip()):
            positions.append(index)
    return positions


def _implementation_review_error_text(exc, run_dir: Path) -> str:
    return "審查沒有存到，請重新載入後再試。"


# confirm-as-is-save-only: the As-is Human Gate for a fresh-path run reuses
# the existing review record (`review_state` + `reviewed_artifact_fingerprint`
# written by `_mark_run_reviewed`). Confirming writes only that record: no
# goal, no case edit, no registry adoption, no research, no job.
def _as_is_confirmation(run_dir: Path) -> dict | None:
    if not _journey_is_candidate_path(run_dir) or not (run_dir / "sbp_draft.yaml").is_file():
        return None
    try:
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    fingerprint = _run_fingerprint(run_dir)
    reviewed = state.get("review_state") == "reviewed"
    confirmed = bool(reviewed and fingerprint and state.get("reviewed_artifact_fingerprint") == fingerprint)
    journey_current = _journey_confirmation(run_dir)["state"] == "confirmed"
    return {
        "state": "confirmed" if confirmed else "needs_confirmation",
        "fingerprint": fingerprint,
        "previously_confirmed": reviewed and not confirmed,
        "journey_current": journey_current,
        # The next Golden Path decision (Improvement Goal) is eligible only
        # while both the As-is and the Journey it was built from are current.
        "downstream_eligible": confirmed and journey_current,
        # solution-reconfirmation: `unresolved` (read-only, cannot be confirmed for the
        # current inputs) or `reconsidered` (Solutions were reconfirmed after this
        # blueprint was confirmed; it stays identifiable as earlier until confirmed again).
        "upstream": "unresolved" if _sbp_upstream_block_reason(run_dir) else _sbp_solutions_state(run_dir),
        "upstream_reason": _sbp_upstream_block_reason(run_dir),
    }


class AsIsConfirmationRejected(Exception):
    pass


# solution-reconfirmation ----------------------------------------------------
class SolutionReconfirmRejected(ValueError):
    pass


def _journey_working_copy(run_dir: Path) -> dict:
    for name in ("journey_proposal.yaml", "journey.yaml"):
        doc = _load(run_dir / name) if (run_dir / name).is_file() else None
        if isinstance(doc, dict):
            return doc
    return {}


@_owns_project_lock(_run_dir_argument)
def _reconfirm_solution(run_dir: Path, payload: dict) -> dict:
    """「保留目前解方」: append one reconfirmation bound to exactly what the screen
    showed. No model call; `solution_basis`, the Solution and earlier records stay."""
    journey = _journey_working_copy(run_dir)
    step_id = payload.get("step_id")
    step = next((st for st in sbp_solution_applicability.primary_steps(journey) if st.get("id") == step_id), None)
    if step is None or not sbp_solution_applicability.has_adopted_solution(step):
        raise SolutionReconfirmRejected("這一步沒有已採用的解方。")
    current = sbp_solution_applicability.journey_applicability(run_dir, journey)[step_id]
    if current["state"] == "current":
        raise SolutionReconfirmRejected("這一步的解方目前不需要重新確認。")
    if (payload.get("step_digest") != current["step_digest"] or payload.get("hmw_digest") != current["hmw_digest"]
            or payload.get("solution_ids") != current["solution_ids"]):
        raise SolutionReconfirmRejected("畫面上的內容不是最新版本，請重新載入後再保留。")
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    records = state.setdefault(sbp_solution_applicability.RECONFIRMATIONS_KEY, {}).setdefault(step_id, [])
    records.append(sbp_solution_applicability.reconfirmation_record(
        step, current["hmw_digest"], sbp_solution_applicability.now_iso()))
    _write_json_atomic(state_path, state)
    return {"solution_applicability": sbp_solution_applicability.journey_applicability(run_dir, journey)[step_id]}


def _sbp_solutions_state(run_dir: Path | None) -> str | None:
    """`unresolved` while an adopted Solution needs reconfirmation; `reconsidered`
    when Solutions were reconfirmed after the blueprint was last confirmed (the
    blueprint then stays identifiable as built before that); otherwise None."""
    if run_dir is None or not _journey_uses_solution_selection(run_dir):
        return None
    journey = _journey_working_copy(run_dir)
    if sbp_solution_applicability.unresolved(run_dir, journey):
        return "unresolved"
    state = sbp_solution_applicability.run_state(run_dir)
    reviewed = sbp_solution_applicability.parse_time(state.get("reviewed_at")) if state.get("review_state") == "reviewed" else None
    latest = sbp_solution_applicability.latest_reconfirmation_at(run_dir, journey)
    return "reconsidered" if reviewed and latest and latest > reviewed else None


def _sbp_upstream_block_reason(run_dir: Path) -> str | None:
    """Why the blueprint cannot be confirmed, kept or regenerated against the
    current inputs right now; None when nothing upstream is unresolved."""
    if not _journey_uses_solution_selection(run_dir):
        return None
    if _journey_confirmation(run_dir)["state"] != "confirmed":
        return "顧客旅程有變更還沒確認，請先回到顧客旅程確認。"
    blockers = _journey_moment_solution_blockers(_journey_working_copy(run_dir), run_dir)
    if blockers:
        return blockers[0]
    if _sbp_solutions_state(run_dir) == "unresolved":
        return "有已採用的解方需要重新確認，請選「保留目前解方」或重新發展。"
    return None


def _confirm_as_is(run_dir: Path, fingerprint: object) -> dict:
    current = _as_is_confirmation(run_dir)
    name = "SBP" if _journey_uses_solution_selection(run_dir) else "服務藍圖"
    blocked = _sbp_upstream_block_reason(run_dir)
    if blocked:
        raise AsIsConfirmationRejected(blocked)
    if current is None:
        raise AsIsConfirmationRejected(f"這份產出沒有可以在這裡確認的{name}。")
    if fingerprint != current["fingerprint"]:
        raise AsIsConfirmationRejected(f"畫面上的{name}不是最新版本，請重新載入後再確認。")
    if not current["journey_current"]:
        raise AsIsConfirmationRejected("顧客旅程在確認後又改過，請先回到顧客旅程重新確認。")
    # track-sbp-upstream-freshness (researcher ruling 2026-10-03): a blueprint
    # whose upstream changed is confirmed only after the researcher keeps it
    # (retained) or regenerates it.
    if sbp_basis.sbp_freshness(run_dir)["state"] == "stale":
        raise AsIsConfirmationRejected(
            f"{name}依據的內容改過了。請先選「保留目前服務藍圖」或「依目前內容更新服務藍圖」，再確認。")
    _mark_run_reviewed(run_dir.name, "reviewed")
    # Confirming freezes the SBP, so its baseline is pinned now: the confirmed
    # SBP can be previewed and exported without first starting Research (the
    # E2E walkthrough found the Service Blueprint deliverable unavailable until
    # Research ran). A no-op when the run already has a published baseline.
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    _adopt_run_into_registry(run_dir, state)
    return {"confirmation": _as_is_confirmation(run_dir)}


# confirm-improvement-goal-save-only: Station 7's Improvement Goal Human Gate.
# The operative goal stays the existing `improvement_goal_review.yaml`, written
# only through `improvement_goal_review.confirm()` (shared with the CLI).
def _improvement_goal_gate(run_dir: Path) -> dict | None:
    as_is = _as_is_confirmation(run_dir)
    if as_is is None:
        return None
    confirmed_goal = sbp_improvement_goal_review.read_operative_goal(run_dir)
    as_is_current = bool(as_is["downstream_eligible"])
    if _journey_uses_solution_selection(run_dir):
        return {
            "method": "sbp_evaluation",
            "as_is_current": as_is_current,
            "confirmed_goal": None,
            "research_eligible": as_is_current,
            "research_questions": sbp_as_is_research.sbp_evaluation_questions(run_dir),
            "research": _fresh_research_projection(run_dir),
            "research_review": _research_review_state(run_dir) if as_is_current else None,
            "to_be": None,
        }
    return {
        "as_is_current": as_is_current,
        "original_goal": sbp_improvement_goal_review.read_original_goal(run_dir),
        "hmw": _primary_hmw_statement(run_dir),
        "confirmed_goal": confirmed_goal,
        # Later external research needs both a current As-is and a goal the
        # researcher actually confirmed.
        "research_eligible": as_is_current and bool(confirmed_goal),
        # start-external-research-stop-at-review: read-only; never starts it.
        "research": _fresh_research_projection(run_dir),
        # research-claim-review-gate: read-only review state for the result.
        "research_review": _research_review_state(run_dir) if as_is_current and confirmed_goal else None,
        # form-to-be-draft-explicit: read-only; the draft is only formed by 形成改善方案.
        "to_be": _fresh_to_be_projection(run_dir),
    }


def _fresh_to_be_projection(run_dir: Path) -> dict | None:
    operation = sbp_to_be.load_current_operation(run_dir)
    return sbp_to_be.project_operation(operation) if isinstance(operation, dict) else None


def _enqueue_fresh_to_be(
    run_name: object, *, execution: sbp_execution.ExecutionConfig | None = None
) -> dict:
    """form-to-be-draft-explicit: 形成改善方案 on a fresh-path run. Every
    refusal happens before any job or subprocess; the one existing To-be
    worker then runs once from the research operation's recorded handoff."""
    safe, run_dir, _state = _research_run(run_name)
    if not _journey_is_candidate_path(run_dir):
        raise ToBeRequestRejected("這份產出不在這裡形成改善方案。")
    if _journey_uses_solution_selection(run_dir):
        raise ToBeRequestRejected("目前這條研究流程是評估已確認的 SBP，不另設改善目標或形成第二張方案藍圖。")
    gate = _improvement_goal_gate(run_dir)
    if gate is None or not gate["as_is_current"]:
        raise ToBeRequestRejected("服務藍圖還沒確認目前這一版，請先回到服務藍圖確認。")
    if not gate["confirmed_goal"]:
        raise ToBeRequestRejected("改善目標還沒確認，請先確認改善目標。")
    research = gate.get("research") or {}
    if research.get("phase") != "research_ready" or research.get("error"):
        raise ToBeRequestRejected("外部調研還沒完成，或結果已經不是目前版本。")
    if (gate.get("research_review") or {}).get("state") != "current":
        raise ToBeRequestRejected("外部資料的審查還沒保存，或審查的不是目前這一次調研。")
    if gate.get("to_be") is not None:
        raise ToBeRequestRejected("這份產出已經有改善方案，請先檢視這一份。")
    research_operation = sbp_as_is_research.load_current_operation(run_dir)
    handoff = research_operation.get("to_be_handoff") if isinstance(research_operation, dict) else None
    if not isinstance(handoff, dict):
        raise ToBeRequestRejected("目前調研結果沒有可以接著形成方案的紀錄。")
    execution = _require_execution(execution)
    baseline = _to_be_baseline(safe)
    with _job_lock:
        if _job.get("state") == "running":
            raise ToBeConflict(f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。")
        _replace_job(kind=sbp_to_be.OPERATION_KIND, state="running", phase="synthesizing", log=[],
                     out=safe, output_identity=safe, result_endpoint=f"/api/to-be?run={quote(safe)}")
    threading.Thread(
        target=_run_to_be_handoff_cli,
        args=({"run": safe, "baseline": baseline, "handoff": handoff, "execution": execution},),
        daemon=True,
    ).start()
    return {"kind": sbp_to_be.OPERATION_KIND, "state": "queued"}


# research-claim-review-gate: Station 7's research claim review. The review is
# the existing research catalog review file, bound to the producing research
# operation and the exact catalog content (the same binding To-be resolves),
# written only through `research_review_record.record_decisions()`.
def _fresh_research_catalog(run_dir: Path) -> dict | None:
    projection = _fresh_research_projection(run_dir)
    if not projection or projection.get("state") != "done" or projection.get("phase") != "research_ready" \
            or projection.get("error"):
        return None
    operation = sbp_as_is_research.load_current_operation(run_dir) or {}
    request_digest = operation.get("request_digest")
    stage_path = sbp_as_is_research.operation_path(run_dir, str(request_digest or "")).parent / "staged" / "research_catalog.yaml"
    catalog = _load(stage_path) if stage_path.is_file() else None
    if not isinstance(request_digest, str) or not isinstance(catalog, dict) or \
            sbp_lifecycle.canonical_content_digest(catalog) != (operation.get("staged_digests") or {}).get("research_catalog"):
        return None
    graph = _load(run_dir / "service_graph.yaml") or {}
    meta = graph.get("meta") or {}
    return {"catalog": catalog, "request_digest": request_digest,
            "catalog_path": str(stage_path.relative_to(run_dir)),
            "case_id": str(meta.get("case_id") or ""), "scenario_id": str(meta.get("scenario_id") or "")}


def _research_review_state(run_dir: Path) -> dict | None:
    context = _fresh_research_catalog(run_dir)
    if context is None:
        return None
    try:
        sbp_research_catalog_review.resolve(
            run_dir, context["catalog"], catalog_request_digest=context["request_digest"],
            case_id=context["case_id"], scenario_id=context["scenario_id"])
    except sbp_research_catalog_review.ReviewError as exc:
        state = {sbp_research_catalog_review.MISSING: "absent",
                 sbp_research_catalog_review.STALE: "stale"}.get(exc.code, "invalid")
        return {"state": state, "decisions": {}}
    review = sbp_research_catalog_review.load(run_dir)
    return {"state": "current", "decisions": {
        entry["claim_ref"]: {"decision": entry["decision"], "rationale": entry["researcher_rationale"]}
        for entry in review.get("claim_decisions") or []}}


class ResearchReviewRejected(Exception):
    pass


def _save_research_review(run_dir: Path, decisions: object) -> dict:
    gate = _improvement_goal_gate(run_dir)
    if gate is None or not gate["research_eligible"]:
        raise ResearchReviewRejected("SBP 不是目前確認的版本，請先回到服務藍圖確認。" if _journey_uses_solution_selection(run_dir)
                                     else "現況或改善目標不是目前確認的版本，請先回去確認。")
    context = _fresh_research_catalog(run_dir)
    if context is None:
        raise ResearchReviewRejected("外部調研還沒完成，沒有可以審查的資料。")
    if not isinstance(decisions, list):
        raise ResearchReviewRejected("審查內容格式不正確。")
    try:
        sbp_research_review_record.record_decisions(
            run_dir, context["catalog"], catalog_request_digest=context["request_digest"],
            catalog_path=context["catalog_path"], case_id=context["case_id"], scenario_id=context["scenario_id"],
            decisions=decisions, reviewed_at=dt.datetime.now().isoformat(timespec="seconds"))
    except sbp_research_catalog_review.ReviewError as exc:
        raise ResearchReviewRejected("每一條外部資料都要選一個決定並寫理由。") from exc
    return {"goal": _improvement_goal_gate(run_dir)}


def _research_review_files(run_dir: Path) -> tuple[str, ...]:
    """cross-project-state-isolation: the research review is decided on the current
    research catalog; its file depends on the research operation."""
    try:
        context = _fresh_research_catalog(run_dir)
    except Exception:  # noqa: BLE001 - no current catalog: the review file alone
        context = None
    catalog = (context or {}).get("catalog_path")
    return ((str(catalog),) if catalog else ()) + (sbp_research_catalog_review.REVIEW_FILENAME,)


artifact_identity.RESOLVERS["research-review"] = _research_review_files


def _implementation_research_files(run_dir: Path) -> tuple[str, ...]:
    """cross-project-state-isolation: the claim review is decided on the current
    operation's catalog; which operation is current depends on the ticked questions."""
    IR = sbp_implementation_research
    base = artifact_identity.ARTIFACTS["implementation-research"]
    try:
        operation = IR.current_operation(run_dir)
    except Exception:  # noqa: BLE001 - no readable operation: the decision files alone
        operation = None
    if not operation or not operation.get("digest"):
        return base
    folder = f"{IR.DIRNAME}/{operation['digest']}"
    return base + (f"{folder}/{IR.OPERATION_FILENAME}", f"{folder}/{IR.CATALOG_FILENAME}")


artifact_identity.RESOLVERS["implementation-research"] = _implementation_research_files


def _fresh_research_projection(run_dir: Path) -> dict | None:
    if sbp_as_is_research.load_current_operation(run_dir) is None:
        return None
    try:
        return _as_is_research_projection(run_dir.name)
    except AsIsResearchPrerequisiteRejected:
        return None


class ImprovementGoalRejected(Exception):
    pass


@_owns_project_lock(_run_dir_argument)
def _confirm_improvement_goal(run_dir: Path, operative_goal: object) -> dict:
    gate = _improvement_goal_gate(run_dir)
    if gate is None:
        raise ImprovementGoalRejected("這份產出沒有可以在這裡確認的改善目標。")
    if not gate["as_is_current"]:
        raise ImprovementGoalRejected("服務藍圖在確認後又改過，請先回到服務藍圖重新確認。")
    if not isinstance(operative_goal, str):
        raise ImprovementGoalRejected("改善目標要是一段文字。")
    _doc, errors = sbp_improvement_goal_review.confirm(run_dir, operative_goal.strip())
    if errors:
        raise ImprovementGoalRejected("改善目標不能是空的。")
    return {"goal": _improvement_goal_gate(run_dir)}


def _research_search_supported(mode: str, executor: str | None) -> bool:
    """external-research-honest-status: whether an execution method can search
    the web, read from the shipped CLI runner itself (API and relay can)."""
    if mode != "cli":
        return True
    from cli_runners import claude as claude_runner, codex as codex_runner
    runner = {"codex": codex_runner.CodexRunner, "claude": claude_runner.ClaudeRunner}.get(executor or "codex")
    return bool(getattr(runner, "supports_web_search", False))


def _external_research_status_view(run_dir: Path) -> dict:
    """What external research actually happened, from its operation record, and
    which execution methods can search -- so the journey station can say before
    建立藍圖 that this build will not research."""
    try:
        status = sbp_implementation_research.research_status(
            run_dir, job_active=_implementation_research_job_active(run_dir))
    except Exception:  # noqa: BLE001 - a run without the inputs simply has no status
        status = {"state": "not_applicable"}
    return {"external_research": status,
            "research_search": {"codex": _research_search_supported("cli", "codex"),
                                "claude": _research_search_supported("cli", "claude"),
                                "openai": _research_search_supported("api", None)}}


def _run_as_is_blueprint_cli(run_dir: Path, execution: sbp_execution.ExecutionConfig) -> None:
    fresh_sbp = _journey_uses_solution_selection(run_dir)
    # built-in-implementation-research: a solution-selection build researches
    # before generating; research never stops the build.
    steps = (("compare", "比對說法"),) + ((("implementation-research", "落地研究"),) if fresh_sbp else ()) \
        + (("structure", "建立 SBP" if fresh_sbp else "產生服務藍圖"),)
    optional = {"implementation-research"}
    if fresh_sbp and not _research_search_supported(execution.mode, execution.executor):
        # external-research-honest-status: no web search, no research -- record
        # why (a finished research is reused) and build from the case alone.
        steps = tuple(step for step in steps if step[0] != "implementation-research")
        try:
            sbp_implementation_research.record_search_unavailable(run_dir)
        except Exception as exc:  # noqa: BLE001 - the record never stops the build
            with _job_lock:
                _job["log"].append(f"未能記錄外部研究狀態：{exc}")
        with _job_lock:
            _job["log"].append("目前的 AI 執行方式不支援網路搜尋，這次未執行外部研究")
    for command, label in steps:
        cmd = [sys.executable, "-m", "sbp_gen", command, "--run", str(run_dir)]
        try:
            code = _stream_stage(cmd, label, execution)
        except OSError as exc:
            with _job_lock:
                _job["state"] = "failed"
                _job["phase"] = f"{label}沒有跑起來：{exc}"
            return
        with _job_lock:
            _job["proc"] = None
            if _job.get("state") == "cancelled":
                return
            if code == TRANSPORT_SUSPENDED_EXIT:
                _job["state"] = "waiting_for_model"
                _job["phase"] = "已經把提示詞準備好，等模型回覆"
                return
            if code != 0 and command in optional:
                _job["log"].append(f"{label}沒有完成，這次不帶參考資料")
                continue
            if code != 0:
                _job["state"] = "failed"
                _job["phase"] = f"{label}沒有完成"
                return
    with _job_lock:
        _job["state"] = "done"
        _job["phase"] = "SBP 已建立" if fresh_sbp else "服務藍圖候選已產生"


def _handle_generate_as_is(handler, payload: object) -> None:
    """POST /api/generate-as-is. Every refusal happens before any subprocess starts."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    blocked = _sbp_upstream_block_reason(run_dir)
    if blocked:
        return handler._send({"error": blocked}, 409)
    regenerate = payload.get("regenerate") is True
    if regenerate:
        # track-sbp-upstream-freshness：只有上游真的改過，研究者明確要求才重新產生。
        # solution-reconfirmation：解方在藍圖確認後重新確認過，也算研究者可以選擇重新產生。
        if sbp_basis.sbp_freshness(run_dir)["state"] != "stale" and _sbp_solutions_state(run_dir) != "reconsidered":
            return handler._send({"error": "服務藍圖目前沒有需要重新產生的變更。"}, 409)
        research = _implementation_research_gate(run_dir)
        if research and research["state"] in sbp_implementation_research.BLOCKING:
            return handler._send({"error": research["reason"]}, 409)
    else:
        build = _as_is_build_state(run_dir)
        if build is None:
            name = "SBP" if _journey_uses_solution_selection(run_dir) else "服務藍圖"
            return handler._send({"error": f"這份產出沒有可以在這裡產生的{name}。"}, 409)
        if not build["available"]:
            return handler._send({"error": build["reason"]}, 409)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        phase = "正在建立 SBP" if _journey_uses_solution_selection(run_dir) else "正在產生服務藍圖"
        _replace_job(kind=AS_IS_BLUEPRINT_OPERATION_KIND, state="running", phase=phase,
                     log=[], out=run_dir.name, output_identity=run_dir.name)
    if regenerate:
        sbp_basis.backup_before_regenerate(run_dir)
    _start_job_thread(_run_as_is_blueprint_cli, (run_dir, execution))
    return handler._send({"ok": True, "kind": AS_IS_BLUEPRINT_OPERATION_KIND, "state": "running"}, 202)


def _handle_form_hmw(handler, payload: object) -> None:
    """POST /api/form-hmw. Every refusal happens before any subprocess starts."""
    payload = payload if isinstance(payload, dict) else {}
    try:
        run_dir = _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound as exc:
        return handler._send({"error": str(exc)}, 404)
    refusal = sbp_hmw_formation.readiness(run_dir)
    if refusal:
        return handler._send(refusal, 400)
    try:
        execution = _execution_for_payload(payload, allow_legacy=False)
    except sbp_execution.ExecutionConfigError as exc:
        return handler._send({"reason": "runner_unavailable", "error": str(exc)}, 400)
    with _job_lock:
        if _job.get("state") == "running":
            return handler._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
        if all(item["action"] == "reuse" for item in sbp_hmw_formation.plan(run_dir)):
            return handler._send({"ok": True, "state": "done"}, 200)
        _replace_job(kind=HMW_FORMATION_OPERATION_KIND, state="running", phase="正在準備形成 HMW",
                     log=[], out=run_dir.name, output_identity=run_dir.name)
    _start_job_thread(_run_hmw_formation_cli, (run_dir, execution))
    return handler._send({"ok": True, "kind": HMW_FORMATION_OPERATION_KIND, "state": "running"}, 202)



# Persona 站的兩個關卡（segmentation_axes、target_segmentation）各自透過
# gates.py 的 preview_mutation/apply_mutation 確認，重建只跑到下一個未確認
# 的關卡就停（openspec/TARGET-ARCHITECTURE.md 決定十九 A2，取代了這裡原本
# 「上游改了、下游整批 unlink 掉重新跑到底，不保留舊版本、沒有停下來等
# 確認」那個更早的舊行為）。
def _stream_stage(
    cmd: list[str], plain_name: str,
    execution: sbp_execution.ExecutionConfig | None = None,
) -> int:
    execution = _require_execution(execution)
    env = execution.child_environment()
    if execution.mode == "transport":
        env["SBP_TRANSPORT_MODEL"] = "claude-opus-5"
    with _job_lock:
        _job["phase"] = f"正在跑{plain_name}"
        _job["log"].append(f"$ {' '.join(cmd[2:])}")
    proc = child_processes.spawn(cmd, run_lock=_current_job_lease(), cwd=REPO / "tools", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    with _job_lock:
        _job["proc"] = proc
    for line in proc.stdout:
        with _job_lock:
            _job["log"].append(_redact_execution_text(line.rstrip(), execution))
    return proc.wait()


def _case_id_for_run(run_dir: Path) -> str:
    case_path = run_dir / "case.yaml"
    if case_path.is_file():
        case = _load(case_path) or {}
        if case.get("case_id"):
            return case["case_id"]
    return run_dir.name


def _build_gate_decision(run_dir: Path, gate_name: str, raw: dict) -> dict:
    """Wraps a raw {field: value} payload from the client into the exact
    doc shape `_main_segmentation_axes`/`_main_target_segmentation` build
    from a --selection-file, so the HTTP and CLI entry points validate and
    apply identically (single write authority)."""
    case_id = _case_id_for_run(run_dir)
    if gate_name == "segmentation_axes":
        return {
            "schema_version": 1, "case_id": case_id,
            "axis_ids": raw.get("axis_ids"),
            "business_rationale": raw.get("business_rationale") or "",
        }
    if gate_name == "target_segmentation":
        quadrant_id = raw.get("primary_quadrant_id")
        matrix = _load(run_dir / "persona_matrix.yaml") or {}
        axes = _load(run_dir / "segmentation_axis_selection.yaml") or {}
        axis_ids = axes.get("axis_ids") or []
        quadrants, _ = (
            sbp_stages.assign_target_segmentation_quadrants(matrix, axis_ids)
            if len(axis_ids) == 2 else ([], []))
        if quadrant_id is None and raw.get("primary_axis_poles"):
            # The plot only ever computes a pole pair (near_low/near_high on
            # each of the 2 axes) -- it has no reason to know the internal
            # QUADRANT-* id (CLAUDE.md: internal ids don't belong on
            # screen). Resolve it server-side against the candidates file,
            # same lookup _run_persona_chain used to do before it was
            # retired.
            poles = tuple(raw["primary_axis_poles"])
            match = next((q for q in quadrants if (q.get("axis_1_pole"), q.get("axis_2_pole")) == poles), None)
            quadrant_id = match.get("id") if match else None
        doc = {
            "schema_version": 1, "case_id": case_id,
            "primary_quadrant_id": quadrant_id,
            "primary_rationale": raw.get("primary_rationale") or "",
        }
        if raw.get("quadrant_members") is not None:
            doc["quadrant_members"] = raw.get("quadrant_members")
        else:
            # Default to every quadrant's own suggested members, same as
            # the retired chain's auto-built payload -- the researcher can
            # still override with an explicit quadrant_members list.
            doc["quadrant_members"] = [
                {"quadrant_id": q["id"], "subject_ids": list(q.get("suggested_subject_ids") or q.get("member_subject_ids") or [])}
                for q in quadrants if q.get("suggested_subject_ids")
            ]
        for key in ("secondary_quadrant_id", "secondary_rationale", "unplaced_dispositions"):
            if raw.get(key) is not None:
                doc[key] = raw.get(key)
        return doc
    raise ValueError(f"unknown gate: {gate_name!r}")


def _gate_validator(run_dir: Path, gate_name: str):
    """The same content validator `_main_segmentation_axes`/
    `_main_target_segmentation` build for `apply_mutation`, reconstructed
    here so the server's confirm endpoint enforces the identical rules
    (single write authority: the CLI and the server both call
    gates.apply_mutation, never write a selection file directly)."""
    if gate_name == "segmentation_axes":
        persona_matrix_doc = _load(run_dir / "persona_matrix.yaml") or {}
        axis_ids = {a.get("id") for a in (persona_matrix_doc.get("axes") or [])
                    if isinstance(a, dict) and a.get("id")}
        return lambda d: sbp_stages._segmentation_axis_selection_errors(d, axis_ids)
    if gate_name == "target_segmentation":
        persona_matrix_doc = _load(run_dir / "persona_matrix.yaml") or {}
        axes = _load(run_dir / "segmentation_axis_selection.yaml") or {}
        axis_ids = axes.get("axis_ids") or []
        quadrants, unclassified = (
            sbp_stages.assign_target_segmentation_quadrants(persona_matrix_doc, axis_ids)
            if len(axis_ids) == 2 else ([], []))
        quadrant_ids = {q["id"] for q in quadrants}
        non_empty_quadrant_ids = {
            q["id"] for q in quadrants if q.get("suggested_subject_ids")
        }
        unclassified_subject_ids = set(unclassified)
        subject_ids = {s.get("id") for s in (persona_matrix_doc.get("subjects") or [])
                       if isinstance(s, dict) and s.get("id")}
        return lambda d: sbp_stages._target_segmentation_selection_errors(
            d, quadrant_ids, subject_ids, unclassified_subject_ids, non_empty_quadrant_ids)
    raise ValueError(f"unknown gate: {gate_name!r}")


def _impact_preview_json(preview: "sbp_gates.ImpactPreview") -> dict:
    return {
        "gate": preview.gate,
        "is_mutation": preview.is_mutation,
        "is_noop": preview.is_noop,
        "rebuild_plan": preview.rebuild_plan,
        "stops_at": preview.stops_at,
        "preview_id": preview.preview_id,
        "plain_summary": preview.plain_summary,
    }


def _gates_step_runner(name: str, execution: sbp_execution.ExecutionConfig | None = None):
    """Wraps one gates.py DERIVATIONS entry's CLI invocation as a
    gates.StepRunner: run it through the existing manual-transport
    `_stream_stage` machinery, translate its exit code into
    StepProduced/StepSuspended/StepFailed. gates.py itself never shells out
    or knows about transport/subprocess mechanics (coding-boundaries.md)."""
    spec = sbp_gates.DERIVATIONS[name]
    plain_name = framework_points.ARTIFACT_PLAIN_NAMES.get(spec.file, spec.file)

    def runner(run_dir: Path):
        cmd = [sys.executable, "-m", "sbp_gen", spec.operation.command, "--run", str(run_dir), *spec.operation.args]
        code = _stream_stage(cmd, plain_name, execution)
        if code == TRANSPORT_SUSPENDED_EXIT:
            return sbp_gates.StepSuspended()
        if code != 0:
            with _job_lock:
                tail = "\n".join(_job.get("log", [])[-5:])
            return sbp_gates.StepFailed(tail or f"{plain_name}沒有完成")
        path = run_dir / spec.file
        if not path.is_file():
            return sbp_gates.StepFailed(f"{plain_name}沒有產生 {spec.file}")
        return sbp_gates.StepProduced(_load(path))

    return runner


def _run_persona_rebuild(
    run_dir: Path, operation_id: str,
    execution: sbp_execution.ExecutionConfig | None = None,
) -> None:
    runners = {name: _gates_step_runner(name, execution) for name in sbp_gates.DERIVATIONS}
    try:
        now = dt.datetime.now().isoformat(timespec="seconds")
        result = sbp_gates.run_rebuild(run_dir, operation_id, runners, now=now)
        with _job_lock:
            if _job.get("state") == "cancelled":
                return
            if result.state == "done":
                _job.update(state="done", phase="Persona 完成", proc=None)
            elif result.state == "waiting_for_model":
                _job.update(state="waiting_for_model", phase="已經把要問的整理好，等 AI 回答", proc=None)
            else:
                _job.update(state="failed", phase=result.error or "沒有完成", proc=None)
    except Exception as exc:  # noqa: BLE001 - 原話回給研究者
        with _job_lock:
            _job["log"].append(f"[例外] {exc}")
            _job.update(state="failed", phase="沒有完成", proc=None)


def _run_persona_content_cli(
    run_dir: Path, out_name: str,
    execution: sbp_execution.ExecutionConfig | None = None,
) -> None:
    """merge-persona-matrix-selection-into-one-screen: the persona-generation
    trigger's own job runner, mirroring _run_journey_recheck_cli's shape
    (build cmd, stream stdout+stderr into _job["log"], flip state on exit)
    rather than inventing a new one -- this is the same single _job
    singleton every other job kind already shares."""
    cmd = _build_persona_content_cmd(run_dir)
    with _job_lock:
        _job["log"] = [f"$ {' '.join(cmd[2:])}"]
        _job["phase"] = "處理中"
    try:
        proc = child_processes.spawn(
            cmd,
            run_lock=_current_job_lease(),
            cwd=REPO / "tools",
            env=_require_execution(execution).child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with _job_lock:
            _job["proc"] = proc
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), execution))
        code = proc.wait()
        with _job_lock:
            if _job.get("state") == "cancelled":
                pass
            else:
                _job["state"] = "done" if code == 0 else "failed"
                _job["phase"] = "完成" if code == 0 else "產出失敗"
            _job["proc"] = None
            if code == 0:
                _job["result_endpoint"] = f"/api/persona-matrix/target-segmentation-picker?run={quote(out_name)}"
    except Exception as exc:
        with _job_lock:
            _job["log"].append(f"[例外] {exc}")
            _job.update(state="failed", phase="產出失敗", proc=None)


_GENERATION_STAGE_LABELS = {
    "align": "正在整理原始資料",
    "semantic_segments": "正在整理語意段落",
    "open_coding": "正在整理開放編碼",
    "axial_coding": "正在整理主軸編碼",
    "narrative_jtbd": "正在形成顧客任務候選",
    "journey": "正在整理服務歷程草案",
    "compare": "正在比對不同說法",
    "structure": "正在畫服務藍圖",
    "projection": "正在生成藍圖與提案草稿",
    "publication": "正在保存研究產物",
    "brief": "正在整理研究簡報",
}


def _generation_phase_label(out_name: str) -> str | None:
    """Read the producer's explicit active stage, never guess from logs or gaps."""
    run_json = RUNS_DIR / out_name / "run.json"
    try:
        state = json.loads(run_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    if state.get("state") in {"awaiting_journey", "failed"}:
        return None
    active = state.get("active_stage")
    if not isinstance(active, str):
        return None
    return _GENERATION_STAGE_LABELS.get(active)


class FreshCodingRequestRejected(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _fresh_coding_context(payload: object) -> dict:
    """Bind an uploaded case, its raw source version, and one run identity."""
    if not isinstance(payload, dict):
        raise FreshCodingRequestRejected("編碼請求格式不正確。")
    case_ref, out = payload.get("case"), payload.get("out")
    if not isinstance(case_ref, str) or not isinstance(out, str) or pathnames.reject(out):
        raise FreshCodingRequestRejected("請選擇已上傳的專案與有效的產出名稱。")
    root = _active_data_root().resolve()
    uploads = UPLOADS_DIR.resolve()
    case_path = (root / case_ref).resolve()
    if (case_path.name != "case.yaml" or case_path.parent.parent != uploads
            or not case_path.is_file() or root not in case_path.parents):
        raise FreshCodingRequestRejected("請選擇由工作台上傳的專案。")
    case_id = _case_id_for(case_ref)
    raw_dir = case_path.parent / "raw"
    if not case_id or not raw_dir.is_dir():
        raise FreshCodingRequestRejected("專案或原始資料尚未準備好。", 422)
    try:
        source_text, _manifest = sbp_ingest.ingest(raw_dir)
    except Exception as exc:
        raise FreshCodingRequestRejected(f"原始資料讀取失敗：{exc}", 422) from exc
    unreadable = _unreadable_source_message(_manifest)
    if unreadable:
        raise FreshCodingRequestRejected(unreadable, 422)
    if not source_text.strip():
        raise FreshCodingRequestRejected("原始資料沒有可讀內容；請檢查檔案後重新上傳。", 422)
    digest = hashlib.sha256(sbp_ingest.comparable_source_text(source_text).encode("utf-8")).hexdigest()
    run_dir = RUNS_DIR / out
    state_path = run_dir / "run.json"
    if sbp_run_lock.content_entries(run_dir) and not state_path.is_file():
        raise FreshCodingRequestRejected("這個產出名稱已有其他研究內容，請換一個名稱。", 409)
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FreshCodingRequestRejected("既有編碼紀錄無法讀取，請換一個產出名稱。", 409) from exc
        if (not isinstance(state, dict) or state.get("case_id") != case_id
                or state.get("source_digest") != digest):
            raise FreshCodingRequestRejected("案例或原始資料已變更，請建立新的編碼產出。", 409)
    else:
        state = {}
    completed = state.get("coding_state") == "awaiting_narrative_jtbd" and all(
        (run_dir / name).is_file() for name in
        ("evidence.yaml", "open_coding.yaml", "axial_coding.yaml", "narrative_job_candidates.yaml")
    )
    return {"case": case_ref, "case_path": case_path, "raw_dir": raw_dir,
            "out": out, "run_dir": run_dir, "completed": completed}


def _run_fresh_coding_cli(context: dict, execution: sbp_execution.ExecutionConfig) -> None:
    cmd = [sys.executable, "-m", "sbp_gen", "coding-candidates",
           "--case", str(context["case_path"]), "--input", str(context["raw_dir"]),
           "--out", str(context["run_dir"])]
    try:
        proc = child_processes.spawn(
            cmd, cwd=REPO / "tools", env=_require_execution(execution).child_environment(),
            run_lock=_current_job_lease(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        with _job_lock:
            _job["proc"] = proc
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), execution))
        code = proc.wait()
        with _job_lock:
            _job["exit_code"] = code
            cancelled = _job.get("state") == "cancelled"
            if cancelled:
                _job["proc"] = None
        if cancelled:
            # Written after the worker exited and outside the job-state lock.
            state_path = context["run_dir"] / "run.json"
            if state_path.is_file():
                try:
                    with sbp_run_files.transaction(state_path) as state:
                        state.update(coding_state="cancelled", active_stage=None,
                                     coding_failure="研究者已取消這一輪編碼。")
                except (OSError, sbp_run_files.RunStateUnreadable):
                    pass
            return
        with _job_lock:
            _job["state"] = "done" if code == 0 else "failed"
            _job["phase"] = "等待研究者選擇顧客任務" if code == 0 else "編碼未完成"
            _job["proc"] = None
            if code != 0:
                failure = _generation_failure_cause(_job["log"])
                if failure["cause"] == "unknown":
                    state_path = context["run_dir"] / "run.json"
                    try:
                        reason = json.loads(state_path.read_text(encoding="utf-8")).get("coding_failure")
                    except (OSError, json.JSONDecodeError):
                        reason = None
                    if reason:
                        failure = {"cause": "coding_runtime_failure",
                                   "what_happened": _redact_execution_text(reason, execution),
                                   "why_it_matters": "已完成的編碼產物已保留，尚未進入研究者選擇。",
                                   "next_action": "排除原因後，重試這一輪編碼。"}
                _job["failure"] = failure
    except OSError as exc:
        with _job_lock:
            _job.update(state="failed", phase="編碼沒有啟動", proc=None,
                        failure={"cause": "runner_invocation_failed",
                                 "what_happened": f"編碼沒有啟動：{exc}",
                                 "why_it_matters": "原始資料尚未交給模型處理。",
                                 "next_action": "檢查執行設定後重試。"})
    except Exception as exc:
        with _job_lock:
            _job.update(state="failed", phase="編碼未完成", proc=None,
                        failure={"cause": "coding_runtime_failure",
                                 "what_happened": _redact_execution_text(exc, execution),
                                 "why_it_matters": "研究者選擇尚未產生，已完成的編碼產物仍保留。",
                                 "next_action": "排除原因後，重試這一輪編碼。"})


def _run_cli(
    case_path: str,
    from_evidence: str | None,
    out_name: str,
    effort: str,
    researcher_note: str | None = None,
    confirmed_journey: dict | None = None,
    input_path: str | None = None,
    discover_journey: bool = False,
    confirmation_context: dict | None = None,
    expected_routing_policy: dict | None = None,
    execution: sbp_execution.ExecutionConfig | None = None,
):
    journey_path = None
    journey_digest = None
    journey_projection_digest = None
    if confirmed_journey:
        run_dir = RUNS_DIR / out_name
        run_dir.mkdir(parents=True, exist_ok=True)
        journey_path = run_dir / "journey_confirmed_input.yaml"
        journey_path.write_text(yaml.dump(confirmed_journey, allow_unicode=True, sort_keys=False), encoding="utf-8")
        journey_digest = _journey_digest(confirmed_journey)
        journey_projection_digest = sbp_stages.as_is_projection.as_is_input_projection_digest(confirmed_journey)
    cmd = _build_generate_cmd(case_path, from_evidence, out_name, effort, researcher_note, journey_path, input_path, discover_journey)

    with _job_lock:
        if not (_job.get("state") == "running" and _job.get("kind") == AS_IS_GENERATION_OPERATION_KIND and _job.get("out") == out_name):
            _replace_job(
                kind=AS_IS_GENERATION_OPERATION_KIND,
                state="running",
                phase="處理中",
                log=[],
                out=out_name,
                output_identity=out_name,
            )
        _job["log"] = [f"$ {' '.join(cmd[2:])}"]
    try:
        proc = child_processes.spawn(
            cmd,
            run_lock=_current_job_lease(),
            cwd=REPO / "tools",
            env=_require_execution(execution).child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with _job_lock:
            _job["proc"] = proc
        for line in proc.stdout:
            with _job_lock:
                _job["log"].append(_redact_execution_text(line.rstrip(), execution))
        code = proc.wait()
        route_mismatch = None
        if code == 0 and expected_routing_policy:
            # restore-browser-generation-workflow design.md 「Failure modes」：
            # 授權案例的實際路由跟開跑前鎖定的 policy 不一致時，要讓 run 失敗，
            # 不能悄悄接受——跟 CLI 的 require_route_match 用同一個函式。
            run_json_path = RUNS_DIR / out_name / "run.json"
            if run_json_path.exists():
                actual_state = json.loads(run_json_path.read_text(encoding="utf-8"))
                for stage in expected_routing_policy:
                    provider = (actual_state.get("providers") or {}).get(stage)
                    model = sbp_llm.model_name((actual_state.get("models") or {}).get(stage))
                    try:
                        real_case.require_route_match(
                            stage, provider=provider, model=model, policy=expected_routing_policy
                        )
                    except real_case.RealCaseGateError as exc:
                        route_mismatch = str(exc)
                        break
        with _job_lock:
            if _job.get("state") == "cancelled":
                # 研究者已經按過取消；子行程收尾的結果不能覆寫掉那個決定。
                pass
            elif route_mismatch:
                _job["state"] = "failed"
                _job["phase"] = "產出失敗"
                _job["log"].append(f"[route-mismatch] {route_mismatch}")
            else:
                _job["state"] = "done" if code == 0 else "failed"
                _job["phase"] = "待確認" if code == 0 else "產出失敗"
            _job["proc"] = None
        # sbp-gen owns its run manifest while it executes.  Add the review
        # boundary metadata only after it has written its final state so a
        # confirmed journey can never be replaced by a stale UI summary.
        run_json = RUNS_DIR / out_name / "run.json"
        if journey_digest and run_json.exists():
            with sbp_run_files.transaction(run_json) as state:
                state["confirmed_journey_digest"] = journey_digest
                state["as_is_input_projection_digest"] = journey_projection_digest
                state["confirmed_journey_file"] = "journey_confirmed_input.yaml"
                state["selected_case"] = case_path
                if confirmation_context:
                    state["journey_confirmation_source"] = confirmation_context.get("source")
                    state["journey_baseline_run"] = confirmation_context.get("baseline_run")
                    if confirmation_context.get("recheck_result"):
                        state["recheck_result"] = confirmation_context["recheck_result"]
                        state["recheck_result_digest"] = confirmation_context.get("recheck_result_digest")
    except Exception as e:  # pragma: no cover
        with _job_lock:
            _job["log"].append(f"[例外] {e}")
            _job["state"] = "failed"
            _job["phase"] = "產出失敗"
            _job["proc"] = None


def _delivery_stage_runtime(run_name: str) -> dict[str, dict]:
    payload = _bootstrap_payload(run_name)
    if not payload:
        return {}
    runtime = ((payload.get("workbench") or {}).get("runtime") or {})
    return {
        str(item.get("id")): item
        for item in runtime.get("stages") or []
        if isinstance(item, dict) and item.get("id")
    }


def _delivery_artifact_state(stage: dict, stage_id: str | None = None) -> tuple[str, str]:
    """Return artifact state and human-attention copy separately."""
    availability = str(stage.get("availability") or "")
    status = str(stage.get("status") or "")
    if status in {"processing", "generating"} or availability == "processing":
        return "產生中", "請稍候產生完成"
    if status in {"generation-failed", "failed"}:
        return "產生失敗", "請檢查失敗原因後重新產生"
    if status in {"update-required", "stale"} or availability == "stale":
        return "舊版本", "上游已變更，需要重新檢查"
    if status in {"pending-confirmation", "draft"}:
        return "AI 草稿", "等待你確認"
    if availability in {"blocked", "unavailable"}:
        return "尚未產生", "需要先完成上游確認"
    if status in {"confirmed", "accepted", "decided", "complete"} or availability in {"complete", "current", "available"}:
        labels = {
            "selective-coding": "核心類別已選定",
            "primary-hmw": "主要 HMW 已選定",
            "persona-target-selection": "目標對象已選定",
            "journey-map": "歷程版本已確認",
            "review-as-is": "服務藍圖已確認",
        }
        if stage_id == "review-to-be" and status in {
            "accepted", "accepted-pending-validation", "accepted_pending_validation", "to_be_accepted"
        }:
            return "方案已採納，待驗證", "目前不需額外確認"
        return labels.get(stage_id, "已確認"), "目前不需額外確認"
    return "尚未產生", "需要先檢視上游狀態"


def _delivery_readiness(view_status: str, stage: dict) -> tuple[str, str | None]:
    if str(stage.get("availability") or "") == "blocked":
        return "blocked", "尚未完成必要的研究者確認。"
    if str(stage.get("status") or "") in {"update-required", "stale"}:
        return "stale", "上游內容已更新，目前的輸出尚未反映最新版本。"
    if view_status == "ready":
        return "ready", None
    if view_status == "error":
        return "generation_failed", "研究內容未受影響，可重新產生交付檔案。"
    if view_status in {"insufficient_evidence", "unsupported", "unavailable", "not_applicable"}:
        return "unavailable", "目前資料不足或不支援此交付格式。"
    return "generation_failed", "輸出目前無法安全產生。"


def _delivery_projection(run_name: object) -> dict:
    safe_name = _safe_component(run_name, "")
    if not safe_name:
        raise ValueError("run 不是合法的路徑片段。")
    run_dir = RUNS_DIR / safe_name
    if not run_dir.is_dir():
        raise FileNotFoundError(f"找不到 run: {safe_name}")
    stages = _delivery_stage_runtime(safe_name)
    sbp_workflow = _journey_uses_solution_selection(run_dir)
    entries = []
    for spec in WORKBENCH_DELIVERIES:
        if spec["stage"] in PUBLIC_PREVIEW_DEFERRED_STAGES:
            continue
        stage = stages.get(spec["stage"], {})
        artifact_state, attention = _delivery_artifact_state(stage, spec["stage"])
        view_status = "unavailable"
        source_refs: list[str] = []
        reason = None
        try:
            view = spec["builder"](run_dir)
            payload = view.to_dict()
            view_status = str(payload.get("status") or view.status)
            source_refs = [
                str(item.get("source_ref"))
                for item in payload.get("source_snapshot") or []
                if isinstance(item, dict) and item.get("source_ref")
            ][:6]
        except Exception as exc:
            # The UI receives a safe, researcher-facing reason; adapter details
            # remain in server logs/tests and never become a second state model.
            reason = getattr(exc, "code", None) or type(exc).__name__
        readiness, readiness_reason = _delivery_readiness(view_status, stage)
        if reason and readiness == "ready":
            readiness, readiness_reason = "generation_failed", "交付產生失敗；研究內容未受影響，可重新產生交付檔案。"
        entries.append({
            "id": spec["id"],
            "label": (WORKBENCH_SBP_WORKFLOW_DELIVERY_LABELS.get(spec["id"], spec["label"])
                      if sbp_workflow else spec["label"]),
            "stage": spec["stage"],
            "formats": [WORKBENCH_DELIVERY_FORMAT_LABELS[item] for item in spec["formats"]],
            "artifact": {"state": artifact_state, "attention": attention},
            "delivery": {"state": readiness, "label": {
                "ready": "可交付", "stale": "需要重新產生", "blocked": "尚無法交付",
                "unavailable": "目前無法提供", "generation_failed": "交付產生失敗",
            }[readiness], "reason": readiness_reason},
            "view_status": view_status,
            "source_refs": source_refs,
        })
    return {"run": safe_name, "deliveries": entries}


def _delivery_is_deferred(delivery_id: object) -> bool:
    """A deliverable of a Public Preview deferred station (400, not a server error)."""
    spec = WORKBENCH_DELIVERY_BY_ID.get(str(delivery_id or ""))
    return bool(spec and spec["stage"] in PUBLIC_PREVIEW_DEFERRED_STAGES)


def _delivery_spec(delivery_id: object) -> dict:
    spec = WORKBENCH_DELIVERY_BY_ID.get(str(delivery_id or ""))
    if not spec:
        raise ValueError("找不到交付類型。")
    if spec["stage"] in PUBLIC_PREVIEW_DEFERRED_STAGES:
        raise ValueError(DEFERRED_DELIVERY_MESSAGE)
    return spec


def _delivery_view_for(run_name: object, delivery_id: object):
    safe_name = _safe_component(run_name, "")
    if not safe_name:
        raise ValueError("run 不是合法的路徑片段。")
    run_dir = RUNS_DIR / safe_name
    if not run_dir.is_dir():
        raise FileNotFoundError(f"找不到 run: {safe_name}")
    spec = _delivery_spec(delivery_id)
    return spec, spec["builder"](run_dir)


AI_COLLABORATION_DELIVERY_ID = "ai_collaboration"
_SELECTIVE_DELIVERY_LABELS = {
    "coding_synthesis": "Narrative / JTBD",
    "jtbd_persona": "Persona",
    "customer_journey": "顧客旅程",
    "as_is_blueprint": "服務藍圖",
    "to_be_report": "改善方案",
}
_SELECTIVE_DELIVERY_FILENAMES = {
    "coding_synthesis": "narrative-jtbd",
    "jtbd_persona": "persona",
    "customer_journey": "customer-journey",
    "as_is_blueprint": "as-is-service-blueprint",
    "to_be_report": "improvement-plan",
}
_SBP_WORKFLOW_SELECTIVE_LABELS = {"to_be_report": "SBP 評估報告"}
_SBP_WORKFLOW_SELECTIVE_FILENAMES = {
    "as_is_blueprint": "service-blueprint",
    "to_be_report": "sbp-evaluation-report",
}


# 2026-10-02 研究者裁定：輸出選單給客戶看的交付物，就是畫面上的那五塊
# （client_deliverables，服務藍圖也是印畫面上的那張圖，add-chart-guides）。舊的證據
# 追溯報告（Narrative／JTBD、Persona、顧客旅程、證據附錄）、SBP 評估報告與舊的現況
# 藍圖文字報告都不在選單裡；它們的交付投影（/api/delivery）不變。
_MENU_REPORT_DELIVERIES: tuple[str, ...] = ()


def _research_deliverables(run_name: object) -> dict:
    """Project-facing export menu: client screens first, then the blueprint reports."""
    projection = _delivery_projection(run_name)
    sbp_workflow = _journey_uses_solution_selection(RUNS_DIR / projection["run"])
    labels = {**_SELECTIVE_DELIVERY_LABELS, **(_SBP_WORKFLOW_SELECTIVE_LABELS if sbp_workflow else {})}
    run_dir = RUNS_DIR / _safe_component(str(projection["run"] or ""), "")
    available = client_deliverables.available(run_dir) if run_dir.is_dir() else []
    for row in projection["deliveries"]:
        spec = WORKBENCH_DELIVERY_BY_ID.get(row["id"])
        if not spec or row["id"] not in _MENU_REPORT_DELIVERIES or row["delivery"]["state"] != "ready":
            continue
        formats = []
        if "pdf" in spec.get("renderers", {}):
            formats.append("pdf")
        if spec.get("markdown") is not None:
            formats.append("markdown")
        if not formats:
            continue
        available.append({
            "id": row["id"],
            "label": labels.get(row["id"], row["label"]),
            "formats": formats,
        })
    # ai-collaboration-export: a supplemental, opt-in item beside -- never inside --
    # the research deliverables. Always Markdown, whatever format the others use.
    if run_dir.is_dir() and _ai_usage(run_dir)["stages"]:
        available.append({"id": AI_COLLABORATION_DELIVERY_ID, "label": "AI 協作紀錄",
                          "formats": ["pdf", "markdown"], "supplemental": True,
                          "note": "執行資訊，不是研究發現；一律輸出為 Markdown。"})
    return {"run": projection["run"], "deliverables": available}


def _render_research_deliverables(run_name: object, ids: list[str], output_format: str, *,
                                  page_base: str | None = None, cookie: str | None = None) -> tuple[bytes, str, str]:
    available = {item["id"]: item for item in _research_deliverables(run_name)["deliverables"]}
    if not ids or any(item not in available for item in ids):
        raise ValueError("選取的研究產物目前無法輸出。")
    if output_format not in {"pdf", "markdown"}:
        raise ValueError("不支援這個輸出格式。")
    if any(output_format not in available[item]["formats"] for item in ids):
        raise ValueError("所選研究產物不支援這個格式。")
    import zipfile
    rendered: list[tuple[str, bytes]] = []
    filenames = {**_SELECTIVE_DELIVERY_FILENAMES,
                 **(_SBP_WORKFLOW_SELECTIVE_FILENAMES
                    if _journey_uses_solution_selection(RUNS_DIR / _safe_component(str(run_name or ""), "")) else {})}
    screens = [item for item in ids if item in client_deliverables.CLIENT_DELIVERABLE_BY_ID]
    if screens:
        if not page_base:
            raise ValueError("畫面交付物要從工作台輸出。")
        run_dir = RUNS_DIR / _safe_component(str(run_name or ""), "")
        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").is_file() else {}
        case_id = str(state.get("case_id") or "")
        try:
            rendered.extend(client_deliverables.render_pdfs(page_base, case_id, run_dir.name, screens, cookie=cookie))
        except client_deliverables.ClientDeliverableError as exc:
            raise ValueError(str(exc)) from exc
        # coding-deliverable-three-layers: the coding deliverable is PDF and
        # XLSX together, both from the same export model.
        if "client_coding" in screens:
            with tempfile.TemporaryDirectory(prefix="sbp-coding-xlsx-") as tmp:
                try:
                    target = sbp_coding_deliverable.render_xlsx(sbp_coding_deliverable.build(run_dir),
                                                                Path(tmp) / "coding.xlsx")
                except sbp_coding_deliverable.CodingDeliverableError as exc:
                    raise ValueError(str(exc)) from exc
                rendered.append((target.name, target.read_bytes()))
    with tempfile.TemporaryDirectory(prefix="sbp-research-deliverables-") as tmp:
        for delivery_id in ids:
            if delivery_id in client_deliverables.CLIENT_DELIVERABLE_BY_ID:
                continue
            if delivery_id == AI_COLLABORATION_DELIVERY_ID:
                run_dir = RUNS_DIR / _safe_component(str(run_name or ""), "")
                rendered.append(("ai-collaboration-record.md",
                                 _ai_collaboration_markdown(_ai_collaboration_record(run_dir)).encode("utf-8")))
                continue
            spec, view = _delivery_view_for(run_name, delivery_id)
            basename = filenames.get(delivery_id, delivery_id)
            if output_format == "pdf":
                target = Path(tmp) / f"{basename}.pdf"
                spec["renderers"]["pdf"](view, target, data_root=_active_data_root())
                rendered.append((target.name, target.read_bytes()))
            else:
                rendered.append((f"{basename}.md", spec["markdown"](view).encode("utf-8")))
    if len(rendered) == 1:
        name, body = rendered[0]
        return body, "application/pdf" if name.endswith(".pdf") else "text/markdown; charset=utf-8", name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in rendered:
            archive.writestr(name, body)
    return buffer.getvalue(), "application/zip", "research-deliverables.zip"


# cross-project-state-isolation: research-decision writes and the artifact whose
# identity their `basis` must match. Item ids (R-2, HMW-1, step-1-1) repeat across
# projects, so these writes are applied only when the basis names this project's
# current content; checked under the project lock before the handler runs.
_IDENTITY_ROUTES = {
    "/api/inference-review": "inference-reviews",
    "/api/cell-coverage": "blueprint",
    "/api/sbp-basis/retain": "blueprint",
    "/api/primary-hmw-picker/select": "primary-hmw",
    "/api/axial-coding-review/pair-selection": "jtbd-selection",
    "/api/selective-themes/edit": "selective-themes",
    "/api/persona-content/field": "persona",
    "/api/persona/hmw-basis/retain": "persona",
    "/api/persona-matrix/segmentation-axes-picker/select": "segmentation-axes",
    "/api/persona-matrix/target-segmentation-picker/select": "target-segmentation",
    "/api/journey-candidate/field": "journey",
    # Moving, deleting or inserting a journey step is an edit of the journey on screen.
    "/api/journey-operation": "journey",
    "/api/journey-annotations": "journey-annotations",
    "/api/journey/persona-basis/retain": "journey",
    "/api/chart-guide/edit": "chart-guides",
    # The research station is hidden in this release, but this write stays reachable.
    "/api/research-review/save": "research-review",
    # No current screen calls these, but they stay reachable research-decision writes.
    "/api/axial-coding-review/assign": "axial-coding",
    "/api/axial-coding-review/exclude": "axial-coding",
    "/api/axial-coding-review/create-category": "axial-coding",
    "/api/axial-coding-review/narrative": "axial-coding",
    "/api/axial-coding/core-category-picker/select": "core-category",
    "/api/journey-map/stage": "journey-map",
    "/api/journey-map/apply": "journey-map",
    "/api/review": "blueprint",
    "/api/improvement-goal/confirm": "improvement-goal",
    # Dispatched by prefix in _dispatch_post; listed here by exact path so they take the
    # project lock and the identity check like every other decision. `start` is a job start.
    "/api/implementation-research/select": "implementation-research",
    "/api/implementation-research/skip": "implementation-research",
    "/api/implementation-research/review": "implementation-research",
}
# The implementation-research actions, matched exactly: any other path under the prefix
# (`/x/select`, `//select`) is not found, never dispatched by its last segment.
_IMPLEMENTATION_RESEARCH_ROUTES = frozenset({
    "/api/implementation-research/select",
    "/api/implementation-research/skip",
    "/api/implementation-research/review",
    "/api/implementation-research/start",
})
# Retired research-decision writes: refused whatever the body, no file changes.
_RETIRED_ROUTES = {
    "/api/mute-inference-flag": "這個端點已退役，推斷的判斷請改用「合理／需要調整／證據不足」。",
    "/api/mute-inference-relation": "這個端點已退役，推斷的判斷請改用「合理／需要調整／證據不足」。",
    # 2026-09-27 walkthrough #11 retired 起草情緒與解決方式 from the journey station; its
    # endpoint rewrote journey_annotations.yaml outside the project lock.
    "/api/journey-annotations/draft": "這個端點已退役，旅程註記請在顧客旅程上直接編輯。",
}
# A confirmation (fingerprint-checked) that changes journey files: its reply
# carries the new identities so the next legitimate edit is not refused.
_IDENTITY_REFRESH_ROUTES = {
    "/api/journey-candidate/confirm": ("journey", "journey-annotations"),
}


# The reads that put decision-bearing content on screen, with the query field that
# names the run. The identity is computed before the handler reads the content, so
# a write landing in between makes the identity older than the content, never
# newer: the next decision is refused rather than applied to unseen content.
_IDENTITY_READS = {
    "/api/run": ("name", ("blueprint", "inference-reviews", "journey-map")),
    "/api/primary-hmw-picker": ("run", ("primary-hmw",)),
    "/api/axial-coding-review": ("run", ("selective-themes", "jtbd-selection", "axial-coding", "core-category")),
    "/api/persona-content": ("run", ("persona",)),
    "/api/persona-matrix/segmentation-axes-picker": ("run", ("segmentation-axes",)),
    "/api/persona-matrix/target-segmentation-picker": ("run", ("target-segmentation",)),
    "/api/journey-channel-map": ("run", ("journey", "journey-annotations", "journey-map")),
    "/api/journey-proposal": ("run", ("journey", "journey-annotations")),
    "/api/chart-guide": ("run", ("chart-guides",)),
    "/api/as-is-research-detail": ("run", ("research-review",)),
    "/api/improvement-goal": ("run", ("improvement-goal",)),
    "/api/as-is-build": ("run", ("implementation-research",)),
}


def _identity_for_read(path: str, query: dict) -> dict | None:
    if path == "/api/research-purpose":  # case-level: the identity is computed on the case folder
        folder = _research_purpose_case_folder((query.get("case_id") or [""])[0])
        return {"research-purpose": artifact_identity.compute(folder, "research-purpose")} if folder else None
    entry = _IDENTITY_READS.get(path)
    if entry is None:
        return None
    field, artifacts = entry
    safe_name = _safe_component(str((query.get(field) or [""])[0]), "")
    if not safe_name or not (RUNS_DIR / safe_name).is_dir():
        return None
    identities = artifact_identity.compute_many(RUNS_DIR / safe_name, artifacts)
    if path == "/api/axial-coding-review":  # this screen also edits the case's research purpose
        folder = _run_case_folder(RUNS_DIR / safe_name)
        identities["research-purpose"] = artifact_identity.compute(folder or RUNS_DIR / safe_name, "research-purpose")
    return identities


# run-state-write-integrity: the POST routes that change a project's run.json
# or journey_proposal.yaml. A route missing here still cannot overwrite silently:
# the shared writer refuses any state write whose project lock is not owned.
_STATE_WRITE_ROUTES = frozenset({
    "/api/review",
    "/api/as-is/confirm",
    "/api/as-is-research",
    "/api/save-version",
    "/api/journey-map/apply",
    "/api/journey-operation",
    "/api/journey-candidate/field",
    "/api/journey-candidate/confirm",
    "/api/journey/solution-reconfirm",
    "/api/sbp-basis/retain",
    "/api/persona/hmw-basis/retain",
    "/api/journey/persona-basis/retain",
    "/api/improvement-goal/confirm",
    "/api/axial-coding-review/pair-selection",
    "/api/primary-hmw-picker/select",
    "/api/axial-coding/core-category-picker/select",
    "/api/persona-matrix/segmentation-axes-picker/select",
    "/api/persona-matrix/target-segmentation-picker/select",
    "/api/integrated-test/checkpoint",
    *_IDENTITY_ROUTES,
})


def _state_write_run_dir(path: str, raw: bytes) -> Path | None:
    if path == "/api/integrated-test/checkpoint":
        return FROZEN_CORPUS.run_dir if FROZEN_CORPUS is not None else None
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return _axial_coding_run_dir(payload.get("run"))
    except ReviewRunNotFound:
        return None


def _project_busy_message(run_dir: Path) -> str:
    """Name the job only when this server knows it is running for this
    project; otherwise the neutral sentence (it never claims an orphan)."""
    with _job_lock:
        known = _job.get("state") == "running" and _job.get("out") == Path(run_dir).name
        kind = _job.get("kind")
    if known:
        return f"目前無法儲存。此專案正在進行{_job_kind_active_phrase(kind)}，為避免覆蓋你的研究決定，請等它完成後再試。"
    return sbp_run_lock.BUSY_MESSAGE


def _state_files_unreadable(run_dir: Path) -> str | None:
    for name in sorted(sbp_run_files.STATE_FILE_NAMES):
        try:
            sbp_run_files.read_state(Path(run_dir) / name)
        except sbp_run_files.RunStateUnreadable as exc:
            return str(exc)
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安靜
        pass

    def _send(self, obj, status=200, *, headers=None):
        # run-state-write-integrity: a handler's generic failure branch answering
        # a held project lock (e.g. a job start) is a conflict, not a bad request.
        if status >= 400 and isinstance(sys.exc_info()[1], sbp_run_lock.RunLockBusy):
            status = 409
            if isinstance(obj, dict):
                obj = {**obj, "reason": "project_busy"}
        # cross-project-state-isolation: a decision write that succeeded answers with
        # the artifact's identity after the write, still under the project lock.
        identity_after = getattr(self, "_identity_after", None)
        if identity_after is not None and status < 300 and isinstance(obj, dict):
            obj = {**obj, "identities": artifact_identity.compute_many(*identity_after)}
        identity_read = getattr(self, "_identity_read", None)
        if identity_read is not None and status < 300 and isinstance(obj, dict):
            obj = {**obj, "identities": identity_read}
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location: str, *, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send_file(self, body: bytes, content_type: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        extension=Path(filename).suffix or ".bin"
        self.send_header("Content-Disposition", f"attachment; filename=service-blueprint{extension}; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str = "text/plain; charset=utf-8"):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _healthz(self):
        checks = {
            "process": "ok",
            "data_root": "ok" if all(
                path.is_dir() and os.access(path, os.R_OK | os.W_OK)
                for path in (_active_data_root(), CASES_DIR, RUNS_DIR)
            ) else "unready",
            "postgres": "not_required",
        }
        try:
            config = sbp_auth.AuthConfig.from_env()
            if config.mode == "email":
                service = sbp_auth.AuthService(config)
                service.check_database()
                checks["postgres"] = "ok"
        except (sbp_auth.AuthConfigurationError, sbp_auth.AuthServiceError):
            checks["postgres"] = "unready"
        ready = all(value in {"ok", "not_required"} for value in checks.values())
        self._send(
            {"status": "ok" if ready else "unready", "checks": checks},
            200 if ready else 503,
        )

    @staticmethod
    def _is_public_api(path: str) -> bool:
        return path == "/api/version" or path.startswith("/api/auth/")

    def _begin_request(self):
        service = _auth_service()
        raw_session = sbp_auth.session_from_cookie_header(self.headers.get("Cookie"))
        user = service.user_for_session(raw_session) if service.enabled else None
        actor = (
            {
                "actor_id": user.user_id,
                "actor_type": "researcher",
                "display_name": user.display_name,
            }
            if user is not None
            else AUTHENTICATED_VALIDATION_ACTOR
        )
        actor_token = _VALIDATION_ACTOR.set(actor)
        return service, user, raw_session, actor_token

    def _authorize_get(self, path: str, query: dict[str, list[str]], service, user) -> bool:
        if not service.enabled or not path.startswith("/api/") or self._is_public_api(path):
            return True
        if user is None:
            self._send({"error": "authentication required"}, 401)
            return False
        if path == "/api/projects":
            _ensure_legacy_projects(user)
        if path == "/api/run":
            run_names = query.get("name", [])
        elif path == "/api/run-status":
            run_names = [name for value in query.get("names", []) for name in value.split(",") if name]
        else:
            run_names = query.get("run", [])
        for run_name in run_names:
            if not _user_owns_run(user, run_name):
                self._send({"error": "not found"}, 404)
                return False
        for case in query.get("case", []):
            if not _user_owns_case(user, _case_id_for(case)):
                self._send({"error": "not found"}, 404)
                return False
        return True

    def _authorize_post(self, path: str, service, user) -> bool:
        if not service.enabled or not path.startswith("/api/") or path.startswith("/api/auth/"):
            return True
        if user is None:
            self._send({"error": "authentication required"}, 401)
            return False
        # Multipart upload parsing belongs to the route. All other JSON
        # routes are read once here and rewound for their existing handlers.
        if path != "/api/upload":
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            except (TypeError, ValueError):
                body = b""
            self.rfile = io.BytesIO(body)
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                if payload.get("run") is not None and not _user_owns_run(user, payload.get("run")):
                    self._send({"error": "not found"}, 404)
                    return False
                if payload.get("case") is not None:
                    case_id = _case_id_for(payload.get("case"))
                    if not _user_owns_case(user, case_id):
                        self._send({"error": "not found"}, 404)
                        return False
        return True

    def do_GET(self):
        actor_token = None
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                return self._healthz()
            service, user, _raw_session, actor_token = self._begin_request()
            self._request_user = user
            if not self._authorize_get(parsed.path, parse_qs(parsed.query), service, user):
                return
            self._identity_read = _identity_for_read(parsed.path, parse_qs(parsed.query))
            return self._do_GET()
        except (sbp_auth.AuthConfigurationError, sbp_auth.AuthServiceError) as exc:
            self._send({"error": str(exc)}, 503)
        finally:
            self._identity_read = None
            if actor_token is not None:
                _VALIDATION_ACTOR.reset(actor_token)
            if hasattr(self, "_request_user"):
                del self._request_user

    def _do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            asset = _static_asset(u.path)
            if asset:
                body, content_type, headers = asset
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/auth/consume":
                try:
                    _user, raw_session = _auth_service().consume(q.get("token", [""])[0])
                    return self._send_redirect(
                        "/",
                        headers={"Set-Cookie": _auth_service().session_cookie(raw_session)},
                    )
                except sbp_auth.AuthServiceError as exc:
                    return self._send({"error": str(exc)}, 400)
            elif u.path == "/api/auth/me":
                service = _auth_service()
                user = getattr(self, "_request_user", None)
                if not service.enabled:
                    self._send({"mode": "disabled", "authenticated": False})
                elif user is None:
                    self._send({"mode": "email", "authenticated": False}, 401)
                else:
                    self._send({
                        "mode": "email",
                        "authenticated": True,
                        "user": {
                            "user_id": user.user_id,
                            "email": user.email,
                            "display_name": user.display_name,
                        },
                    })
            elif u.path == "/api/bootstrap":
                requested_run = q.get("run", [None])[0]
                payload = _bootstrap_payload(requested_run, getattr(self, "_request_user", None))
                if payload is None:
                    self._send({"error": f"找不到 run: {requested_run}"}, 404)
                else:
                    self._send(payload)
            elif u.path == "/api/version":
                self._send(_ui_status())
            elif u.path == "/api/run":
                name = q.get("name", [""])[0]
                d = RUNS_DIR / name
                if not name or not (d / "sbp_draft.yaml").exists():
                    self._send({"error": f"找不到 run: {name}"}, 404)
                else:
                    self._send(_analyse(d))
            elif u.path == "/api/inventory":
                # 不花錢的盤點。paths 用逗號分隔的 repo 相對路徑。
                paths = [p for p in (q.get("paths", [""])[0]).split(",") if p]
                self._send(_inventory(paths))
            elif u.path == "/api/job":
                if not _job_belongs_to(getattr(self, "_request_user", None)):
                    self._send({"error": "not found"}, 404)
                else:
                    self._send(_project_job_status())
            elif u.path == "/api/run-status":
                names = [n for n in (q.get("names", [""])[0]).split(",") if n]
                self._send(_run_status_payload(names))
            elif u.path == "/api/journey-channel-map":
                name = q.get("run", [""])[0]
                safe_name = _safe_component(name, "")
                if not safe_name:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    self._send(_journey_channel_map_response(RUNS_DIR / safe_name))
            elif u.path == "/api/axial-coding-review":
                try:
                    run_dir = _axial_coding_run_dir(q.get("run", [""])[0])
                    self._send(_axial_coding_review_response(run_dir))
                except ReviewRunNotFound as e:
                    self._send({"error": str(e)}, 404)
            elif u.path == "/api/ai-usage":
                try:
                    self._send(_ai_usage(_axial_coding_run_dir(q.get("run", [""])[0])))
                except ReviewRunNotFound as e:
                    self._send({"error": str(e)}, 404)
            elif u.path == "/api/provider-selection":
                self._send({"provider": sbp_provider_setup.load_selection(
                    _active_data_root(), _current_validation_actor()["actor_id"])})
            elif u.path == "/api/provider-status":
                try:
                    provider = q.get("provider", [""])[0]
                    self._send(sbp_provider_setup.status_for(
                        provider, probe=_runner_status,
                        recent_test=sbp_provider_setup.recent_tests.get(
                            _current_validation_actor()["actor_id"], provider,
                            q.get("model", [""])[0], q.get("effort", [""])[0])))
                except sbp_provider_setup.ProviderSelectionError as exc:
                    self._send({"error": str(exc)}, 400)
            elif u.path == "/api/improvement-goal":
                name = q.get("run", [""])[0]
                if not name or pathnames.reject(name):
                    self._send({"error": "run 不是合法的路徑片段。"}, 400)
                else:
                    self._send({"goal": _improvement_goal_gate(RUNS_DIR / name)})
            elif u.path == "/api/as-is-build":
                name = q.get("run", [""])[0]
                if not name or pathnames.reject(name):
                    self._send({"error": "run 不是合法的路徑片段。"}, 400)
                else:
                    self._send({"build": _as_is_build_state(RUNS_DIR / name),
                                "confirmation": _as_is_confirmation(RUNS_DIR / name),
                                "freshness": sbp_basis.sbp_freshness(RUNS_DIR / name),
                                "implementation_research": _implementation_research_view(RUNS_DIR / name),
                                **_external_research_status_view(RUNS_DIR / name)})
            elif u.path == "/api/persona-content":
                name = q.get("run", [""])[0]
                if not name or pathnames.reject(name):
                    self._send({"error": "run 不是合法的路徑片段。"}, 400)
                else:
                    self._send(_persona_content_response(RUNS_DIR / name))
            elif u.path == "/api/persona-matrix/projection":
                body, status = _persona_matrix_projection(q.get("run", [""])[0])
                self._send(body, status)
            elif u.path == "/api/projects":
                self._send({"projects": _projects(getattr(self, "_request_user", None))})
            elif u.path == "/api/delivery":
                names = q.get("run", [])
                if len(names) != 1 or not names[0]:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    self._send(_delivery_projection(names[0]))
            elif u.path == "/api/research-purpose":
                case_id = q.get("case_id", [""])[0]
                case = next((c for c in _discover_cases() if c.get("case_id") == case_id), None)
                folder = (_active_data_root() / case["path"]).parent if case and case.get("path") else None
                if folder is None:
                    self._send({"error": "找不到這個案例。"}, 404)
                else:
                    self._send({"research_purpose": {"text": sbp_selective.read_purpose(folder)}})
            elif u.path == "/api/chart-guide":
                try:
                    run_dir = _axial_coding_run_dir(q.get("run", [""])[0])
                    kind = _chart_guide_kind(q.get("kind", [""])[0])
                except ReviewRunNotFound as exc:
                    self._send({"error": str(exc)}, 404)
                except ValueError as exc:
                    self._send({"error": str(exc)}, 400)
                else:
                    self._send(_chart_guide_payload(run_dir, kind))
            elif u.path == "/api/coding-deliverable":
                # coding-deliverable-three-layers: the one export model the
                # printed PDF reads; the XLSX is written from the same build.
                try:
                    run_dir = _axial_coding_run_dir(q.get("run", [""])[0])
                except ReviewRunNotFound as exc:
                    self._send({"error": str(exc)}, 404)
                else:
                    self._send(sbp_coding_deliverable.build(run_dir))
            elif u.path == "/api/research-deliverables":
                names = q.get("run", [])
                if len(names) != 1 or not names[0]:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    self._send(_research_deliverables(names[0]))
            elif u.path == "/api/research-deliverables/export":
                names = q.get("run", [])
                ids = [item for item in (q.get("ids", [""])[0]).split(",") if item]
                output_format = str(q.get("format", [""])[0]).lower()
                if len(names) != 1 or not names[0] or not ids or not output_format:
                    self._send({"error": "run、ids 與 format query 參數不可省略。"}, 400)
                else:
                    try:
                        body, content_type, filename = _render_research_deliverables(
                            names[0], ids, output_format,
                            page_base=f"http://127.0.0.1:{self.server.server_address[1]}",
                            cookie=self.headers.get("Cookie"))
                    except (FileNotFoundError, ValueError) as exc:
                        self._send({"error": str(exc)}, 409)
                    else:
                        self._send_file(body, content_type, filename)
            elif u.path == "/api/delivery/preview":
                names = q.get("run", [])
                delivery_id = q.get("delivery", [""])[0]
                if len(names) != 1 or not names[0] or not delivery_id:
                    self._send({"error": "run 與 delivery query 參數不可省略。"}, 400)
                elif _delivery_is_deferred(delivery_id):
                    self._send({"error": DEFERRED_DELIVERY_MESSAGE}, 400)
                else:
                    spec, view = _delivery_view_for(names[0], delivery_id)
                    row = next(item for item in _delivery_projection(names[0])["deliveries"] if item["id"] == spec["id"])
                    if row["delivery"]["state"] != "ready" or spec.get("markdown") is None:
                        self._send({"error": "目前無法提供此交付預覽。", "status": row["delivery"]["state"]}, 409)
                    else:
                        self._send_text(spec["markdown"](view), "text/markdown; charset=utf-8")
            elif u.path == "/api/project/export":
                try:
                    body, name = _export_project_zip(q.get("case", [""])[0])
                except (ProjectActionRejected, ImprovementGoalRejected) as e:
                    self._send({"error": str(e)}, 400)
                else:
                    self._send_file(body, "application/zip", f"{name}.zip")
            elif u.path == "/api/delivery/export":
                names = q.get("run", [])
                delivery_id = q.get("delivery", [""])[0]
                output_format = str(q.get("format", [""])[0]).lower()
                if len(names) != 1 or not names[0] or not delivery_id or not output_format:
                    self._send({"error": "run、delivery 與 format query 參數不可省略。"}, 400)
                elif _delivery_is_deferred(delivery_id):
                    self._send({"error": DEFERRED_DELIVERY_MESSAGE}, 400)
                else:
                    spec, view = _delivery_view_for(names[0], delivery_id)
                    row = next(item for item in _delivery_projection(names[0])["deliveries"] if item["id"] == spec["id"])
                    if output_format not in spec["renderers"]:
                        self._send({"error": "此交付不支援指定格式。"}, 400)
                    elif row["delivery"]["state"] != "ready":
                        self._send({"error": "目前無法提供此交付檔案。", "status": row["delivery"]["state"]}, 409)
                    else:
                        suffix = ".pdf" if output_format == "pdf" else ".xlsx"
                        media = "application/pdf" if output_format == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        with tempfile.TemporaryDirectory(prefix="sbp-delivery-") as tmp:
                            target = Path(tmp) / f"{delivery_id}{suffix}"
                            spec["renderers"][output_format](
                                view, target, data_root=_active_data_root()
                            )
                            self._send_file(target.read_bytes(), media, f"{delivery_id}{suffix}")
            elif u.path == "/api/primary-hmw-picker":
                run_name = q.get("run", [""])[0]
                body, status = _framework_point_get("primary-hmw", run_name)
                self._send(body, status)
            elif u.path == "/api/axial-coding/core-category-picker":
                run_name = q.get("run", [""])[0]
                if not run_name:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    body, status = _framework_point_get("selective-coding", run_name)
                    self._send(body, status)
            elif u.path == "/api/persona-matrix/segmentation-axes-picker":
                run_name = q.get("run", [""])[0]
                if not run_name:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    body, status = _framework_point_get("segmentation-axes", run_name)
                    self._send(body, status)
            elif u.path == "/api/persona-matrix/target-segmentation-picker":
                run_name = q.get("run", [""])[0]
                if not run_name:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    body, status = _framework_point_get("target-segmentation", run_name)
                    self._send(body, status)
            elif u.path == "/api/journey-proposal":
                name = q.get("run", [""])[0]
                proposal = RUNS_DIR / name / "journey_proposal.yaml"
                if not name or not proposal.exists():
                    self._send({"error": "找不到 journey proposal"}, 404)
                else:
                    self._send(_load(proposal))
            elif u.path == "/api/journey-recheck":
                name = q.get("run", [""])[0]
                context = _context_for_stored_recheck(name)
                result = _validated_recheck_result(context)
                if result is None:
                    self._send({"error": "歷程重查結果尚未完成"}, 404)
                else:
                    self._send(result)
            elif u.path == "/api/as-is-research":
                names = q.get("run", [])
                if len(names) != 1 or not names[0]:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    self._send(_as_is_research_projection(names[0]))
            elif u.path == "/api/as-is-research-detail":
                names = q.get("run", [])
                if len(names) != 1 or not names[0]:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    self._send(_as_is_research_detail(names[0]))
            elif u.path == "/api/as-is-research/preview":
                # require-explicit-authorization-for-live-model-calls: what
                # confirming this run's As-is baseline would do, without
                # doing any of it -- no write, no operation preparation.
                # Takes `run` for the same calling convention as the other
                # research endpoints above, even though the disclosure
                # itself is not currently run-specific.
                names = q.get("run", [])
                if len(names) != 1 or not names[0]:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    mode = q.get("mode", [None])[0]
                    provider = q.get("provider", [None])[0]
                    if mode is not None and mode not in {"cli", "api"}:
                        self._send({"error": "不支援的執行方式。"}, 400)
                    elif mode == "api" and provider not in sbp_execution.SUPPORTED_API_PROVIDERS:
                        self._send({"error": "不支援的 API 提供者。"}, 400)
                    else:
                        self._send(sbp_as_is_research.research_provider_disclosure(
                            execution_mode=mode, api_provider=provider,
                        ))
            elif u.path == "/api/to-be":
                names = q.get("run", [])
                if len(names) != 1 or not names[0]:
                    self._send({"error": "run query 參數不可省略。"}, 400)
                else:
                    self._send(_to_be_detail(names[0]))
            elif u.path == "/api/versions":
                self._send({"versions": _discover_versions(q.get("run", [""])[0])})
            elif u.path == "/api/saved-version":
                self._send(_analyse_saved_version(q.get("run", [""])[0], q.get("id", [""])[0]))
            elif u.path == "/api/saved-export":
                body, content_type, filename = _saved_version_export(
                    q.get("run", [""])[0], q.get("id", [""])[0], q.get("format", [""])[0]
                )
                self._send_file(body, content_type, filename)
            else:
                self._send({"error": "not found"}, 404)
        except SavedVersionNotFound as e:
            self._send({"error": str(e)}, 404)
        except JourneyRecheckConflict as e:
            self._send({"error": str(e)}, 409)
        except ToBeConflict as e:
            self._send({"error": str(e)}, 409)
        except ToBeRequestRejected as e:
            self._send({"error": str(e)}, 422)
        except JourneyRecheckNotFound as e:
            self._send({"error": str(e)}, 404)
        except JourneyRecheckSourceUnavailable as e:
            self._send({"error": str(e)}, 422)
        except JourneyRecheckRequestRejected as e:
            self._send({"error": str(e)}, 400)
        except AsIsResearchRequestRejected as e:
            self._send({"error": str(e)}, 400)
        except AsIsResearchPrerequisiteRejected as e:
            self._send({"error": str(e)}, 422)
        except sbp_auth.AuthServiceError as e:
            self._send({"error": str(e)}, 503)
        except sbp_validation.ValidationOperationError as e:
            self._send(_validation_error(e), _validation_error_status(e))
        except Exception as e:
            self._send({"error": _exception_text(e)}, 500)

    def do_POST(self):
        actor_token = None
        try:
            service, user, _raw_session, actor_token = self._begin_request()
            self._request_user = user
            path = urlparse(self.path).path
            if not self._authorize_post(path, service, user):
                return
            return self._do_POST()
        except (sbp_auth.AuthConfigurationError, sbp_auth.AuthServiceError) as exc:
            self._send({"error": str(exc)}, 503)
        finally:
            if actor_token is not None:
                _VALIDATION_ACTOR.reset(actor_token)
            if hasattr(self, "_request_user"):
                del self._request_user

    def _do_POST(self):
        path = urlparse(self.path).path
        if path in _RETIRED_ROUTES:
            try:  # keep a keep-alive connection in sync; the body is never parsed
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            except (TypeError, ValueError):
                pass
            return self._send({"error": _RETIRED_ROUTES[path], "reason": "retired"}, 410)
        try:
            if path in _STATE_WRITE_ROUTES:
                return self._locked_state_write(path)
            return self._dispatch_post()
        except sbp_run_lock.RunLockBusy as busy:
            # A job start refused by a held project lock in a handler that has no
            # failure branch of its own: still a plain, truthful answer.
            return self._send({"error": str(busy), "reason": "project_busy"}, 409)

    def _locked_state_write(self, path: str):
        """run-state-write-integrity: a researcher write that changes a
        project's run.json / journey_proposal.yaml runs under that project's
        lock, taken here before the handler reads anything and held until it
        has responded. No waiting: a held lock is a 409 with a plain reason."""
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n)
        self.rfile = io.BytesIO(raw)
        run_dir = _state_write_run_dir(path, raw)
        if run_dir is None:
            if path in _IDENTITY_ROUTES:  # never apply a decision without its identity check
                return self._send({"error": "找不到這份產出。"}, 404)
            return self._dispatch_post()  # the handler reports its own not-found / bad request
        try:
            lease = sbp_run_lock.acquire(run_dir)
        except sbp_run_lock.RunLockBusy:
            return self._send({"error": _project_busy_message(run_dir), "reason": "project_busy"}, 409)
        try:
            unreadable = _state_files_unreadable(run_dir)
            if unreadable:
                return self._send({"error": unreadable, "reason": "state_unreadable"}, 409)
            artifact = _IDENTITY_ROUTES.get(path)
            if artifact is not None:
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = None
                basis = payload.get("basis") if isinstance(payload, dict) else None
                try:
                    artifact_identity.check(run_dir, artifact, basis)
                except artifact_identity.StaleBasis:
                    body, status = artifact_identity.refusal()
                    return self._send(body, status)
                self._identity_after = (run_dir, artifact_identity.sharing(run_dir, artifact))
            elif path in _IDENTITY_REFRESH_ROUTES:
                self._identity_after = (run_dir, _IDENTITY_REFRESH_ROUTES[path])
            return self._dispatch_post()
        finally:
            self._identity_after = None
            lease.release()

    def _dispatch_post(self):
        path = urlparse(self.path).path
        if path == "/api/auth/request-link":
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict) or not isinstance(payload.get("email"), str):
                    return self._send({"error": "email is required"}, 400)
                _auth_service().request_link(payload["email"])
                return self._send({"ok": True, "message": "如果地址符合 staging 設定，登入連結將寄出。"}, 202)
            except json.JSONDecodeError as exc:
                return self._send({"error": str(exc)}, 400)
            except sbp_auth.AuthServiceError as exc:
                return self._send({"error": str(exc)}, 503)
        if path == "/api/auth/logout":
            raw_session = sbp_auth.session_from_cookie_header(self.headers.get("Cookie"))
            try:
                service = _auth_service()
                service.logout(raw_session)
                return self._send(
                    {"ok": True},
                    headers={"Set-Cookie": service.session_cookie("", clear=True)},
                )
            except sbp_auth.AuthServiceError as exc:
                return self._send({"error": str(exc)}, 503)
        if path == "/api/integrated-test/checkpoint":
            if FROZEN_CORPUS is None:
                return self._send({"error": "not found"}, 404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict) or set(payload) != {
                    "checkpoint_id", "expected_previous_fingerprint"
                }:
                    raise FrozenCorpusPublicationError(
                        "frozen checkpoint request fields are not canonical"
                    )
                return self._send(FROZEN_CORPUS.publish_checkpoint(
                    payload.get("checkpoint_id"),
                    expected_previous_fingerprint=payload.get(
                        "expected_previous_fingerprint"
                    ),
                ))
            except json.JSONDecodeError:
                return self._send({"error": "request body must be valid JSON"}, 400)
            except FrozenCorpusConflict as exc:
                return self._send({"error": str(exc)}, 409)
            except FrozenCorpusPublicationError as exc:
                return self._send({"error": str(exc)}, 422)
        if path == "/api/feedback":
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("request body 必須是 JSON object。")
                return self._send(_persist_feedback(payload), 201)
            except (json.JSONDecodeError, ValueError) as exc:
                return self._send({"error": str(exc)}, 400)
            except OSError as exc:
                return self._send({"error": f"回報沒有保存：{_exception_text(exc)}"}, 503)
        if path == "/api/upload":
            upload_root = None
            upload_root_existed = False
            project = None
            project_created = False
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                original_names: list[str] = []
                fields: dict = {}
                files = _parse_multipart(self.headers.get("Content-Type", ""), body, original_names, fields)
                if not files:
                    return self._send({"error": "沒有收到檔案"}, 400)
                # 研究者可以先替專案命名；留白就照第一個檔名，不用 AI 取名。
                chosen = " ".join(str(fields.get("name") or "").split())[:80]
                intake = _unused_intake_name(chosen or _intake_name_from_files(original_names))
                intake_id = _safe_component(intake, "case-intake")
                upload_root = UPLOADS_DIR / intake_id
                upload_root_existed = upload_root.exists()
                user = getattr(self, "_request_user", None)
                service = _auth_service() if user is not None else None
                case_id = f"upload-{intake_id}"
                existing_project = service.project_for_case(case_id) if service is not None else None
                if existing_project is not None and existing_project.owner_user_id != user.user_id:
                    return self._send({"error": "not found"}, 404)
                saved = _save_upload(intake, files)
                saved["case"] = _write_intake_case(saved, label=intake)
                saved["label"] = intake
                if service is not None:
                    case_path = str((upload_root / "case.yaml").relative_to(_active_data_root()))
                    project = service.register_project(
                        case_id=case_id,
                        owner_user_id=user.user_id,
                        data_path=case_path,
                    )
                    project_created = existing_project is None
                return self._send(saved)
            except sbp_auth.AuthServiceError as e:
                if project_created and project is not None:
                    try:
                        _auth_service().unregister_project(
                            project_id=project.project_id,
                            owner_user_id=project.owner_user_id,
                        )
                    except sbp_auth.AuthServiceError:
                        pass
                if upload_root is not None and not upload_root_existed:
                    shutil.rmtree(upload_root, ignore_errors=True)
                return self._send({"error": str(e)}, 503)
            except Exception as e:
                if project_created and project is not None:
                    try:
                        _auth_service().unregister_project(
                            project_id=project.project_id,
                            owner_user_id=project.owner_user_id,
                        )
                    except sbp_auth.AuthServiceError:
                        pass
                if upload_root is not None and not upload_root_existed:
                    shutil.rmtree(upload_root, ignore_errors=True)
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/project/version-archive":
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
                if not isinstance(payload, dict):
                    raise ProjectActionRejected("請求格式不正確。")
                return self._send(_set_version_archived(payload.get("run"), payload.get("archived"), payload.get("active_run")))
            except (ProjectActionRejected, json.JSONDecodeError) as e:
                return self._send({"error": str(e)}, 400)
        if path in ("/api/project/pin", "/api/project/rename", "/api/project/delete"):
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
                if not isinstance(payload, dict):
                    raise ProjectActionRejected("請求格式不正確。")
                case = payload.get("case")
                if path == "/api/project/pin":
                    return self._send(_set_project_pin(case, bool(payload.get("pinned"))))
                if path == "/api/project/rename":
                    return self._send(_rename_project(case, payload.get("name")))
                user = getattr(self, "_request_user", None)
                project = None
                if user is not None:
                    _case_path, case_id = _project_case(case)
                    project = _auth_service().project_for_case(case_id)
                result = _delete_project(case)
                if project is not None:
                    _auth_service().unregister_project(project_id=project.project_id, owner_user_id=project.owner_user_id)
                return self._send(result)
            except (ProjectActionRejected, ImprovementGoalRejected, json.JSONDecodeError) as e:
                return self._send({"error": str(e)}, 400)
        if path == "/api/case-improvement-goal":
            # fix-improvement-goal-source-of-truth: refused unconditionally,
            # before the body is interpreted and before any case path is
            # resolved, so no request shape can rewrite the retired field --
            # not a valid case, not an absent one, not an escaping path. The
            # route stays rather than 404ing: a stale client or a script
            # deserves a sentence saying why, and where to go instead.
            # Draining the body first keeps a keep-alive connection in sync;
            # its content is never parsed.
            try:
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            except (TypeError, ValueError):
                pass
            return self._send(
                {"error": RETIRED_CASE_GOAL_REFUSAL, "error_source": "local"}, 400)
        if path == "/api/review":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise ReviewStateRejected("request body 必須是 JSON object")
                target_state = payload.get("review_state")
                run_name = payload.get("run")
                execution = None
                # Lifecycle-enabled runs must prove their current baseline before
                # the review marker is persisted. Legacy runs retain the existing
                # review-only behavior.
                safe_name = _safe_component(run_name or "", "") if isinstance(run_name, str) else ""
                _run_dir = RUNS_DIR / safe_name if safe_name else RUNS_DIR / "_missing"
                state_path = _run_dir / "run.json"
                current_state = (
                    json.loads(state_path.read_text(encoding="utf-8"))
                    if state_path.is_file()
                    else {}
                )
                # A run adopted from outside this pipeline gets registered here,
                # at the moment its baseline is actually needed — not earlier
                # (nothing should publish to the registry just from being
                # viewed) and not silently (see _adopt_run_into_registry). A
                # no-op if this run already has a registry, or if its content
                # can't be adopted — either way _proposal_eligibility() below
                # sees whatever is true on disk after this.
                # require-explicit-authorization-for-live-model-calls: the
                # authorization boundary. This must run before any of the
                # writes below (the improvement-goal sync, registry
                # adoption, the review marker itself) — the requirement is
                # "no confirmation and no data transmitted", not merely "no
                # research operation created", and those writes happen
                # whether or not research actually starts.
                # confirm-as-is-save-only: a fresh-path run confirms As-is
                # through 確認現況 only; this older path would synchronize a goal
                # and queue research, so it is refused for such runs.
                if target_state == "reviewed" and safe_name and _journey_is_candidate_path(_run_dir):
                    return self._send({"error": "這份產出請用「確認服務藍圖」確認；確認只會保存，不會開始調研。"}, 409)
                as_is_research_disclosure = None
                if target_state == "reviewed":
                    execution = _execution_for_payload(payload)
                    as_is_research_disclosure = sbp_as_is_research.research_provider_disclosure(
                        execution_mode=None if execution.mode == "legacy" else execution.mode,
                        api_provider=execution.provider,
                    )
                    if (
                        as_is_research_disclosure["spend_capable"]
                        and payload.get("live_research_acknowledged") is not True
                    ):
                        raise AsIsResearchPrerequisiteRejected(
                            "這次確認會把資料送到外部服務（"
                            + "、".join(as_is_research_disclosure["providers"])
                            + "），需要先明確同意才能繼續；同意之前不會標記確認，也不會送出任何資料。",
                            source="local",
                        )
                if target_state == "reviewed":
                    # Before anything reads the baseline: make the run's frozen
                    # case snapshot carry the improvement goal the gate is
                    # displaying, so the screen and the check agree.
                    _sync_run_improvement_goal(_run_dir, current_state)
                    _adopt_run_into_registry(_run_dir, current_state)
                lifecycle_enabled = _proposal_eligibility(_run_dir)[0]
                if target_state == "reviewed" and lifecycle_enabled:
                    _research_baseline(run_name, require_reviewed=False)
                result = _mark_run_reviewed(payload.get("run"), payload.get("review_state"), reviewer=payload.get("reviewer", ""))
                if target_state == "reviewed" and lifecycle_enabled:
                    _status, projection = _enqueue_as_is_research(run_name, execution=execution)
                    result["as_is_research"] = projection
                if target_state == "reviewed" and as_is_research_disclosure is not None:
                    # Transport mode never required the acknowledgement above
                    # -- say so explicitly rather than leaving the interface
                    # to infer it from the absence of a rejection.
                    result["research_execution_mode"] = as_is_research_disclosure["execution_mode"]
                    if not as_is_research_disclosure["spend_capable"]:
                        result["research_no_live_fallback"] = True
                if target_state == "unreviewed":
                    operation = sbp_as_is_research.load_current_operation(_run_dir)
                    if operation is not None:
                        result["as_is_research"] = _as_is_research_projection(run_name)
                return self._send(result)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except ReviewStateRejected as e:
                return self._send({"error": str(e)}, 400)
            except AsIsResearchPrerequisiteRejected as e:
                return self._send({"error": str(e), "error_source": getattr(e, "source", "local")}, 422)
            except AsIsResearchConflict as e:
                return self._send({"error": str(e), "error_source": "local"}, 409)
            except AsIsResearchRequestRejected as e:
                return self._send({"error": str(e), "error_source": "local"}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/as-is-research":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict) or set(payload) - {"run", "execution"}:
                    raise AsIsResearchRequestRejected(
                        'request body 必須包含 run 與 execution。'
                    )
                execution = _execution_for_payload(payload)
                _prepare_fresh_research_start(payload.get("run"))
                status, projection = _enqueue_as_is_research(payload.get("run"), execution=execution)
                return self._send(projection, status)
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except AsIsResearchRequestRejected as e:
                return self._send({"error": str(e)}, 400)
            except AsIsResearchPrerequisiteRejected as e:
                return self._send({"error": str(e)}, 422)
            except AsIsResearchConflict as e:
                return self._send({"error": str(e)}, 409)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/to-be-generate":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict) or set(payload) - {"run", "execution"}:
                    raise ToBeRequestRejected("request body 必須包含 run 與 execution。")
                return self._send(_enqueue_fresh_to_be(
                    payload.get("run"), execution=_execution_for_payload(payload)), 202)
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except sbp_execution.ExecutionConfigError as e:
                return self._send({"reason": "runner_unavailable", "error": str(e)}, 400)
            except ToBeRequestRejected as e:
                return self._send({"error": str(e)}, 409)
            except ToBeConflict as e:
                return self._send({"error": str(e)}, 409)
            except AsIsResearchPrerequisiteRejected as e:
                return self._send({"error": str(e)}, 409)
        if path == "/api/to-be-retry":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict) or set(payload) - {"run", "execution"}:
                    raise ToBeRequestRejected(
                        'request body 必須包含 run 與 execution。'
                    )
                return self._send(_enqueue_to_be_retry(
                    payload.get("run"), execution=_execution_for_payload(payload)
                ), 202)
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ToBeConflict as exc:
                return self._send({"error": str(exc)}, 409)
            except ToBeRequestRejected as exc:
                return self._send({"error": str(exc)}, 422)
            except Exception as exc:
                return self._send({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/to-be-review":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise ToBeRequestRejected("request body 必須是 JSON object。")
                allowed = {"run", "fingerprint", "decision", "impact_confirmed"}
                if set(payload) - allowed:
                    raise ToBeRequestRejected("request body 含有不受支援的欄位。")
                result = _decide_to_be(
                    payload.get("run"),
                    fingerprint=payload.get("fingerprint"),
                    decision=payload.get("decision"),
                    impact_confirmed=payload.get("impact_confirmed", False),
                )
                return self._send(result)
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ToBeConflict as exc:
                return self._send({"error": str(exc)}, 409)
            except ToBeRequestRejected as exc:
                return self._send({"error": str(exc)}, 422)
            except Exception as exc:
                return self._send({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/to-be-revision":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict) or set(payload) - {
                    "run", "fingerprint", "instruction", "execution"
                }:
                    raise ToBeRequestRejected(
                        "request body 必須包含 run、fingerprint 與 instruction。"
                    )
                return self._send(_enqueue_to_be_revision(
                    payload.get("run"),
                    fingerprint=payload.get("fingerprint"),
                    instruction=payload.get("instruction"),
                    execution=_execution_for_payload(payload),
                ), 202)
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ToBeConflict as exc:
                return self._send({"error": str(exc)}, 409)
            except ToBeRequestRejected as exc:
                return self._send({"error": str(exc)}, 422)
            except Exception as exc:
                return self._send({"error": f"{type(exc).__name__}: {exc}"}, 400)
        if path == "/api/cell-coverage":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                coverage_name = _safe_component(str(payload.get("run") or ""), "")
                if coverage_name and (RUNS_DIR / coverage_name / "sbp_draft.yaml").is_file():
                    blocked = _sbp_upstream_block_reason(RUNS_DIR / coverage_name)
                    if blocked:  # solution-reconfirmation: the earlier blueprint is read-only
                        return self._send({"error": blocked}, 409)
                result = _set_cell_coverage(payload.get("run"), payload.get("step"), payload.get("layer"),
                                             payload.get("coverage_state"), payload.get("absence_evidence_ref"))
                return self._send(result)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except CellCoverageRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/mute-inference-flag":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                result = _set_muted_inference_flag(payload.get("run"), payload.get("node_id"), payload.get("muted"))
                return self._send(result)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except MuteInferenceFlagRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/mute-inference-relation":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                result = _set_muted_inference_relation(payload.get("run"), payload.get("relation_id"), payload.get("muted"))
                return self._send(result)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except MuteInferenceFlagRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/inference-review":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                safe_name = _safe_component(str(payload.get("run") or ""), "")
                run_dir = RUNS_DIR / safe_name if safe_name else None
                if run_dir is None or not (run_dir / "sbp_draft.yaml").is_file():
                    return self._send({"error": "找不到這份產出。"}, 404)
                blocked = _sbp_upstream_block_reason(run_dir)
                if blocked:  # solution-reconfirmation: the earlier blueprint is read-only
                    return self._send({"error": blocked}, 409)
                reviews = sbp_inference_reviews.record(
                    run_dir, payload.get("target"), payload.get("id"), payload.get("decision"), payload.get("note"))
                return self._send({"ok": True, "decision": sbp_inference_reviews.decision(
                    reviews, payload.get("target"), payload.get("id"))})
            except sbp_inference_reviews.InferenceReviewRejected as e:
                return self._send({"error": str(e)}, 400)
        if path == "/api/save-version":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                result = _save_reviewed_version(payload.get("run"), payload.get("name"), notes=payload.get("notes", ""), reviewer=payload.get("reviewer", ""), svg_data=payload.get("svg"), png_data=payload.get("png"))
                return self._send(result)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except VersionSaveRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/journey-annotations/draft":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _journey_map_run_dir(payload.get("run"))
                execution = _execution_for_payload(payload)
                return self._send({"ok": True, "annotations": _draft_journey_annotations(run_dir, execution)})
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except (JourneyAnnotationRejected, sbp_execution.ExecutionConfigError) as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/journey-annotations":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                try:
                    run_dir = _journey_map_run_dir(payload.get("run"))
                except ReviewRunNotFound:
                    # A fresh-path Journey candidate has no service graph yet;
                    # its steps take annotations all the same.
                    run_dir = _axial_coding_run_dir(payload.get("run"))
                    if not _journey_is_candidate_path(run_dir):
                        raise
                document = _write_journey_annotation(run_dir, payload.get("step_id"), payload.get("field"), payload.get("value"))
                return self._send({"ok": True, "annotations": document})
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JourneyAnnotationRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona-content/confirm":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                return self._send(_confirm_persona(run_dir, payload.get("persona_digest")))
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except PersonaConfirmationRejected as e:
                return self._send({"error": str(e)}, 409)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona-content/field":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                _write_persona_field(run_dir, payload.get("persona_id"), payload.get("field"), payload.get("value"))
                return self._send(_persona_content_response(run_dir))
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except PersonaEditRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/journey-map/stage":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _journey_map_run_dir(payload.get("run"))
                judgement = {key: value for key, value in payload.items() if key != "run"}
                staged = _stage_judgement(run_dir, judgement)
                return self._send({"ok": True, "staged_count": len(staged)})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/journey-operation":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                return self._send(_journey_operation_payload(payload))
            except json.JSONDecodeError:
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 409)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/journey-map/apply":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _journey_map_run_dir(payload.get("run"))
                _apply_staged_judgements(run_dir)
                return self._send({"ok": True, "map": _journey_channel_map_response(run_dir)})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/axial-coding-review/assign":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                result = _apply_axial_coding_assign(
                    run_dir, str(payload.get("category_id")), str(payload.get("evidence_ref")))
                return self._send({"ok": True, **result})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/axial-coding-review/create-category":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                result = _apply_axial_coding_create_category(
                    run_dir, str(payload.get("label") or ""), str(payload.get("rationale") or ""),
                    str(payload.get("evidence_ref")))
                return self._send({"ok": True, **result})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/axial-coding-review/exclude":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                result = _apply_axial_coding_exclude(run_dir, str(payload.get("evidence_ref")))
                return self._send({"ok": True, **result})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/axial-coding-review/pair-selection":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                answer = {k: v for k, v in payload.items() if k != "run"}
                result = _record_narrative_job_selection(run_dir, answer)
                # The command's own words, not this endpoint's: it is the
                # validation path, and restating its refusal here would be a
                # second implementation of the same three checks.
                return self._send(result, 200 if result["ok"] else 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/axial-coding-review/narrative":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                result = _apply_selective_coding_narrative(run_dir, str(payload.get("narrative") or ""))
                return self._send({"ok": True, **result})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JudgementRejected as e:
                return self._send({"error": str(e)}, 400)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/primary-hmw-picker/select":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                body, status = _framework_point_select("primary-hmw", payload)
                return self._send(body, status)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/axial-coding/core-category-picker/select":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                body, status = _framework_point_select("selective-coding", payload)
                return self._send(body, status)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona-matrix/segmentation-axes-picker/select":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                body, status = _framework_point_select("segmentation-axes", payload)
                return self._send(body, status)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona-matrix/target-segmentation-picker/select":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                body, status = _framework_point_select("target-segmentation", payload)
                return self._send(body, status)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona/preview":
            # rebuild-to-next-human-gate: preview is pure -- no writes, no
            # job started. `gate` is "segmentation_axes" or
            # "target_segmentation"; `decision` is the same raw shape the
            # matching --selection-file payload would carry.
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                gate_name = payload.get("gate")
                if gate_name not in sbp_gates.GATES:
                    return self._send({"error": "不認得的關卡。"}, 400)
                decision = _build_gate_decision(run_dir, gate_name, payload.get("decision") or {})
                now = dt.datetime.now().isoformat(timespec="seconds")
                preview = sbp_gates.preview_mutation(run_dir, gate_name, decision, now=now)
                response = _impact_preview_json(preview)
                # The Workbench starts Persona generation right after a target
                # confirmation succeeds; the axes gate still starts nothing.
                follow_up = ("確認後會接著產出這一群的 Persona" if gate_name == "target_segmentation"
                             else "AI 不會自動執行")
                response["plain_summary"] = (
                    f"這次確認會保存選擇；之前的版本會保留，{follow_up}。"
                    if preview.is_mutation else f"確認會保存選擇，{follow_up}。")
                return self._send(response)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona/confirm":
            # Confirms exactly one gate (segmentation_axes or
            # target_segmentation) via gates.apply_mutation -- it never
            # chains into generating candidates or persona content. A
            # mutation requires a current preview_id; a first-ever selection
            # needs none. This Workbench route explicitly disables rebuild
            # scheduling so every confirmation stops after saving.
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                gate_name = payload.get("gate")
                if gate_name not in sbp_gates.GATES:
                    return self._send({"error": "不認得的關卡。"}, 400)
                decision = _build_gate_decision(run_dir, gate_name, payload.get("decision") or {})
                preview_id = str(payload.get("preview_id") or "")
                validator = _gate_validator(run_dir, gate_name)
                now = dt.datetime.now().isoformat(timespec="seconds")
                try:
                    result = sbp_gates.apply_mutation(
                        run_dir,
                        gate_name,
                        decision,
                        preview_id,
                        now=now,
                        validate=validator,
                        actor=_current_validation_actor() if _auth_service().enabled else None,
                        schedule_rebuild=False,
                    )
                except sbp_gates.StalePreview as exc:
                    return self._send({"error": str(exc), "needs_preview": True}, 409)
                except sbp_gates.ValidationFailed as exc:
                    return self._send({"error": "；".join(exc.errors)}, 400)

                response = {
                    "confirmation_id": result.confirmation_id,
                    "operation_id": result.operation_id,
                    "stops_at": result.stops_at,
                }
                return self._send(response)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona/rebuild/retry":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                execution = _execution_for_payload(payload)
                run_dir = _axial_coding_run_dir(payload.get("run"))
                operation_id = payload.get("operation_id")
                if not operation_id:
                    return self._send({"error": "缺少 operation_id。"}, 400)
                with _job_lock:
                    if _job.get("state") == "running":
                        return self._send(
                            {"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
                    _replace_job(
                        kind=PERSONA_CONTENT_GENERATION_OPERATION_KIND, state="running", phase="重試中",
                        log=[], out=run_dir.name, output_identity=run_dir.name,
                    )
                _start_job_thread(_run_persona_rebuild, (run_dir, operation_id, execution))
                return self._send({"started": True})
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/run-stage":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                execution = _execution_for_payload(payload)
                artifact = payload.get("artifact")
                if artifact == "persona_segmentation":
                    run_dir = _axial_coding_run_dir(payload.get("run"))
                    if not _primary_hmw_statement(run_dir):
                        return self._send({"error": "請先確認 HMW，再分析 Persona 分群。"}, 409)
                    if not (run_dir / "evidence.yaml").is_file() or not (run_dir / "axial_coding.yaml").is_file():
                        return self._send({"error": "缺少來源或編碼證據，無法分析 Persona 分群。"}, 409)
                    with _job_lock:
                        if _job.get("state") == "running":
                            return self._send({"error": "目前已有研究工作正在進行，完成後再試。"}, 409)
                        resume = (_job.get("kind") == STAGE_RUN_OPERATION_KIND
                                  and _job.get("artifact") == artifact
                                  and _job.get("out") == run_dir.name
                                  and _job.get("state") in {"waiting_for_model", "failed"})
                        step = _job.get("segmentation_step", "matrix") if resume else "matrix"
                        _replace_job(kind=STAGE_RUN_OPERATION_KIND, state="running",
                                     phase="正在分析 Persona 分群", log=[], out=run_dir.name,
                                     output_identity=run_dir.name, artifact=artifact,
                                     segmentation_step=step)
                    _start_job_thread(_run_persona_segmentation_cli, (run_dir, execution, step))
                    return self._send({"started": True, "step": "Persona 分群分析"}, 202)
                spec = STAGE_COMMANDS.get(artifact)
                if spec is None:
                    return self._send({"error": "這一步不在可以從介面執行的清單裡。"}, 400)
                run_dir = _axial_coding_run_dir(payload.get("run"))
                cli, plain_name = spec
                with _job_lock:
                    if _job.get("state") == "running":
                        return self._send(
                            {"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
                    _replace_job(
                        kind=STAGE_RUN_OPERATION_KIND, state="running",
                        phase=f"正在跑{plain_name}", log=[],
                        out=run_dir.name, output_identity=run_dir.name,
                    )
                _start_job_thread(_run_stage_cli, (cli, plain_name, run_dir, execution))
                return self._send({"started": True, "step": plain_name})
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/persona-matrix/generate-persona":
            # merge-persona-matrix-selection-into-one-screen: the in-screen
            # "生成 persona" trigger. Mirrors _main_persona_content's own
            # primary-quadrant guard so an obviously-doomed request is
            # refused before any subprocess starts, and shares the same
            # _job/_job_lock singleton every other job kind already uses.
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                execution = _execution_for_payload(payload)
                run_dir = _axial_coding_run_dir(payload.get("run"))
                out_name = run_dir.name
                selection_path = run_dir / "target_segmentation_selection.yaml"
                selection_doc = _load(selection_path) if selection_path.is_file() else None
                if not isinstance(selection_doc, dict) or not selection_doc.get("primary_quadrant_id"):
                    return self._send(
                        {"error": "尚未指定主要目標象限，無法生成 persona——請先在上方指定主要目標。"}, 400)
                # persona-generation-confirmation-gate: the same current-target
                # resolver the CLI uses, checked before any subprocess starts.
                if not sbp_gates.is_derivation_runnable(
                        run_dir, "persona_content", now=dt.datetime.now().isoformat(timespec="seconds")):
                    return self._send(
                        {"error": "目標族群還沒確認目前這一版，Persona 暫時不能產出——請先重新確認目標族群。"}, 409)
                with _job_lock:
                    if _job.get("state") == "running":
                        active = _job_kind_active_phrase(_job.get("kind"))
                        return self._send({"error": f"目前已有{active}正在進行，完成後再試。"}, 409)
                    _replace_job(
                        kind=PERSONA_CONTENT_GENERATION_OPERATION_KIND,
                        state="running",
                        phase="處理中",
                        log=[],
                        out=out_name,
                        output_identity=out_name,
                    )
                _start_job_thread(_run_persona_content_cli, (run_dir, out_name, execution))
                return self._send({
                    "ok": True,
                    "kind": PERSONA_CONTENT_GENERATION_OPERATION_KIND,
                    "state": "running",
                    "phase": "處理中",
                }, 202)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/journey-recheck":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                context = _resolve_journey_recheck_request(payload)
                context["execution"] = _execution_for_payload(payload)
                existing = _validated_recheck_result(context)
                if existing is not None:
                    return self._send({
                        "ok": True,
                        "reused": True,
                        "kind": "journey_recheck",
                        "state": "done",
                        "output_identity": context["output_identity"],
                        "result_endpoint": f"/api/journey-recheck?run={quote(context['output_identity'])}",
                        "request_digest": context["request_digest"],
                        "result_digest": existing["result_digest"],
                    })
                with _job_lock:
                    if _job.get("state") == "running":
                        if (
                            _job.get("kind") == "journey_recheck"
                            and _job.get("request_digest") == context["request_digest"]
                        ):
                            return self._send({**{k: v for k, v in _job.items() if k != "run_lock"}, "ok": True, "reused": True}, 202)
                        active = _job_kind_active_phrase(_job.get("kind"))
                        return self._send({"error": f"目前已有{active}正在進行，完成後再試。"}, 409)
                    _replace_job(
                        kind="journey_recheck",
                        state="running",
                        phase="處理中",
                        log=[],
                        out=context["output_identity"],
                        output_identity=context["output_identity"],
                        lock_dir=context["result_dir"],  # the recheck writes its own --out, not runs/<out>
                        result_endpoint=f"/api/journey-recheck?run={quote(context['output_identity'])}",
                        request_digest=context["request_digest"],
                    )
                _start_job_thread(_run_journey_recheck_cli, (context,))
                return self._send({
                    "ok": True,
                    "kind": "journey_recheck",
                    "state": "running",
                    "phase": "處理中",
                    "output_identity": context["output_identity"],
                    "result_endpoint": f"/api/journey-recheck?run={quote(context['output_identity'])}",
                    "request_digest": context["request_digest"],
                }, 202)
            except JourneyRecheckConflict as e:
                return self._send({"error": str(e)}, 409)
            except JourneyRecheckSourceUnavailable as e:
                return self._send({"error": str(e)}, 422)
            except JourneyRecheckRequestRejected as e:
                return self._send({"error": str(e)}, 400)
            except JourneyRecheckNotFound as e:
                return self._send({"error": str(e)}, 422)
            except Exception as e:
                return self._send({"error": _exception_text(e)}, 400)
        if path == "/api/provider-check":
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, TypeError):
                return self._send({"error": "請求內容不是有效的 JSON。"}, 400)
            try:
                config = sbp_execution.ExecutionConfig.from_payload(payload)
            except sbp_execution.ExecutionConfigError as exc:
                return self._send({"error": str(exc)}, 400)
            try:
                with sbp_provider_setup.single_check():
                    if config.mode == "api":
                        result, attempted = sbp_provider_setup.run_openai_check(
                            config.api_key or "", base_url=sbp_openai_llm.BASE_URL), True
                        model = effort = None
                    else:
                        result, attempted = sbp_provider_setup.run_cli_check(
                            config.executor, environ=config.child_environment(), probe=_runner_status,
                            invoke=_connection_check_invoke, classify=_infra_failure_cause)
                        model, effort = config.codex_model, config.codex_effort
                    if attempted:
                        # A refusal for a local layer made no request, so it is not a "recent test".
                        sbp_provider_setup.recent_tests.record(
                            _current_validation_actor()["actor_id"], result["provider"], model, effort, result)
            except sbp_provider_setup.CheckBusy as exc:
                return self._send({"error": str(exc)}, 409)
            return self._send(result)
        if path == "/api/provider-selection":
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, TypeError):
                return self._send({"error": "請求內容不是有效的 JSON。"}, 400)
            if not isinstance(payload, dict):
                return self._send({"error": sbp_provider_setup.INVALID_PROVIDER_MESSAGE}, 400)
            try:
                provider = sbp_provider_setup.save_selection(
                    _active_data_root(), _current_validation_actor()["actor_id"],
                    payload.get("provider"), _write_json_atomic)
            except sbp_provider_setup.ProviderSelectionError as exc:
                return self._send({"error": str(exc)}, 400)
            except OSError:
                return self._send({"error": sbp_provider_setup.SELECTION_WRITE_FAILED_MESSAGE}, 500)
            return self._send({"provider": provider})
        if path == "/api/job-cancel":
            if not _job_belongs_to(getattr(self, "_request_user", None)):
                return self._send({"error": "not found"}, 404)
            return self._send(_cancel_running_job())
        if path in ("/api/journey-candidate/field", "/api/journey-candidate/confirm"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise JourneyCandidateRejected("request body 必須是物件。")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                if path.endswith("/confirm"):
                    return self._send(_confirm_journey_candidate(run_dir, payload.get("journey_digest")))
                return self._send(_edit_journey_candidate(run_dir, payload))
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except JourneyCandidateRejected as e:
                return self._send({"error": str(e)}, 409)
        if path == "/api/research-review/save":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise ResearchReviewRejected("request body 必須是物件。")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                return self._send(_save_research_review(run_dir, payload.get("decisions")))
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except ResearchReviewRejected as e:
                return self._send({"error": str(e)}, 409)
        if path == "/api/improvement-goal/confirm":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise ImprovementGoalRejected("request body 必須是物件。")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                return self._send(_confirm_improvement_goal(run_dir, payload.get("operative_goal")))
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except ImprovementGoalRejected as e:
                return self._send({"error": str(e)}, 409)
        if path == "/api/as-is/confirm":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise AsIsConfirmationRejected("request body 必須是物件。")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                return self._send(_confirm_as_is(run_dir, payload.get("fingerprint")))
            except (ValueError, json.JSONDecodeError):
                return self._send({"error": "request body 必須是有效 JSON。"}, 400)
            except ReviewRunNotFound as e:
                return self._send({"error": str(e)}, 404)
            except AsIsConfirmationRejected as e:
                return self._send({"error": str(e)}, 409)
        if path == "/api/generate-as-is":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return _handle_generate_as_is(self, payload)
        if path in _IMPLEMENTATION_RESEARCH_ROUTES:
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return _handle_implementation_research(self, path.rsplit("/", 1)[1], payload)
        if path == "/api/build-journey":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return _handle_build_journey(self, payload)
        if path == "/api/narrative-job-candidates/generate":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return _handle_generate_jtbd_candidates(self, payload)
        if path in ("/api/research-purpose", "/api/selective-themes/generate", "/api/selective-themes/edit"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return {"/api/research-purpose": _handle_research_purpose,
                    "/api/selective-themes/generate": _handle_generate_selective_themes,
                    "/api/selective-themes/edit": _handle_edit_selective_theme}[path](self, payload)
        if path in ("/api/persona/hmw-basis/retain", "/api/journey/persona-basis/retain"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir((payload or {}).get("run"))
                action = (_retain_persona_against_hmw if path == "/api/persona/hmw-basis/retain"
                          else _retain_journey_against_persona)
                return self._send(action(run_dir))
            except ReviewRunNotFound as exc:
                return self._send({"error": str(exc)}, 404)
            except StaleDecisionRejected as exc:
                return self._send({"error": str(exc)}, 409)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
        if path == "/api/journey/solution-reconfirm":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(payload, dict):
                    raise SolutionReconfirmRejected("request body 必須是物件。")
                run_dir = _axial_coding_run_dir(payload.get("run"))
                return self._send(_reconfirm_solution(run_dir, payload))
            except ReviewRunNotFound as exc:
                return self._send({"error": str(exc)}, 404)
            except SolutionReconfirmRejected as exc:
                return self._send({"error": str(exc)}, 409)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
        if path == "/api/sbp-basis/retain":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                run_dir = _axial_coding_run_dir((payload or {}).get("run"))
                blocked = _sbp_upstream_block_reason(run_dir)
                if blocked:
                    return self._send({"error": blocked}, 409)
                return self._send({"freshness": sbp_basis.retain(run_dir)})
            except ReviewRunNotFound as exc:
                return self._send({"error": str(exc)}, 404)
            except sbp_basis.NotStale as exc:
                return self._send({"error": str(exc)}, 409)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
        if path in ("/api/chart-guide/draft", "/api/chart-guide/edit"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return (_handle_draft_chart_guide if path.endswith("/draft") else _handle_edit_chart_guide)(self, payload)
        if path == "/api/build-journey-solutions":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return _handle_build_journey_solutions(self, payload)
        if path == "/api/form-hmw":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send({"error": str(exc)}, 400)
            return _handle_form_hmw(self, payload)
        if path == "/api/start-coding":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                context = _fresh_coding_context(payload)
                with _job_lock:
                    active = _job.get("state") == "running"
                    same_job = (active and _job.get("kind") == FRESH_CODING_OPERATION_KIND
                                and _job.get("out") == context["out"]
                                and _job.get("case_ref") == context["case"])
                    active_kind = _job.get("kind")
                if same_job and _job_belongs_to(getattr(self, "_request_user", None)):
                    return self._send({"ok": True, "kind": FRESH_CODING_OPERATION_KIND,
                                       "state": "running", "out": context["out"]}, 202)
                if active:
                    return self._send({"error": f"目前已有{_job_kind_active_phrase(active_kind)}正在進行，完成後再試。"}, 409)
                if context["completed"]:
                    return self._send({"ok": True, "kind": FRESH_CODING_OPERATION_KIND,
                                       "state": "done", "out": context["out"]})
                execution = _execution_for_payload(payload, allow_legacy=False)
                with _job_lock:
                    if _job.get("state") == "running":
                        return self._send({"error": f"目前已有{_job_kind_active_phrase(_job.get('kind'))}正在進行，完成後再試。"}, 409)
                    _replace_job(kind=FRESH_CODING_OPERATION_KIND, state="running",
                                 phase="正在準備編碼", log=[], out=context["out"],
                                 output_identity=context["out"], case_ref=context["case"])
                _start_job_thread(_run_fresh_coding_cli, (context, execution))
                return self._send({"ok": True, "kind": FRESH_CODING_OPERATION_KIND,
                                   "state": "running", "out": context["out"]}, 202)
            except FreshCodingRequestRejected as exc:
                return self._send({"error": str(exc)}, exc.status)
            except (json.JSONDecodeError, sbp_execution.ExecutionConfigError, ValueError) as exc:
                return self._send({"error": str(exc)}, 400)
        if path != "/api/generate":
            return self._send({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            execution = _execution_for_payload(payload)
            request_error = _generation_request_error(payload)
            if request_error:
                return self._send({"error": request_error}, 400)
            # None／缺欄位／空字串都視為「沒有」，不傳給 CLI；非字串（畸形
            # 請求）一樣當作沒有，不讓它往下傳壞掉 CLI 呼叫
            # （researcher-note-passthrough spec：伺服器只在有非空字串時才轉送）。
            researcher_note = payload.get("researcher_note")
            if not isinstance(researcher_note, str) or not researcher_note.strip():
                researcher_note = None
            confirmation = None
            if not bool(payload.get("discover_journey")):
                confirmation = _resolve_generation_confirmation(payload)
            expected_routing_policy = (
                real_case.resolve_routing_policy(real_case.REQUIRED_ROUTE_STAGES)
                if _case_id_for(payload.get("case")) == real_case.CANONICAL_CASE_ID
                else None
            )
            with _job_lock:
                if _job.get("state") == "running":
                    active = _job_kind_active_phrase(_job.get("kind"))
                    return self._send({"error": f"目前已有{active}正在進行，完成後再試。"}, 409)
                _replace_job(
                    kind=AS_IS_GENERATION_OPERATION_KIND,
                    state="running",
                    phase="處理中",
                    log=[],
                    out=payload["out"],
                    output_identity=payload["out"],
                )
            _start_job_thread(
                _run_cli,
                (
                    payload["case"],
                    str(confirmation["evidence_path"]) if confirmation else (payload.get("from_evidence") or None),
                    payload["out"],
                    payload.get("effort", "high"),
                    researcher_note,
                    confirmation["journey"] if confirmation else None,
                    _discovery_input_path(payload),
                    bool(payload.get("discover_journey")),
                    confirmation,
                    expected_routing_policy,
                    execution,
                ),
            )
            self._send({"ok": True})
        except Exception as e:
            self._send({"error": _exception_text(e)}, 400)


def resolve_bind_host(explicit_host: str | None) -> str:
    """Resolve the deployable listener address without changing the local default."""
    return explicit_host or os.environ.get("SBP_BIND_HOST", "127.0.0.1")


def _default_cli_runner_command() -> None:
    """A local server started without runner commands uses the shipped Codex
    and Claude Code wrappers instead of failing every CLI call. It picks the wrapper only;
    the wrapper still fails closed when Codex is missing or not signed in."""
    runners = Path(__file__).resolve().parents[1] / "cli_runners"
    # default-claude-runner-on-source-start: Claude Code too, as `ditl-sbp start` does.
    for variable, name in (("SBP_CLI_RUNNER_COMMAND", "codex.py"), ("SBP_CLI_RUNNER_COMMAND_CLAUDE", "claude.py")):
        wrapper = runners / name
        if not (os.environ.get(variable) or "").strip() and wrapper.is_file():
            os.environ[variable] = f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))}"


class _ShutdownRequested(BaseException):
    """SIGTERM arrived: stop serving and end the AI child processes."""


def _raise_shutdown_requested(signum, frame) -> None:
    raise _ShutdownRequested()


def _serve_until_stopped(httpd, grace: float = child_processes.GRACE_SECONDS) -> None:
    """Serve until Ctrl-C or SIGTERM, then end every AI child process the server
    started so none keeps running (and spending quota) in the background."""
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, _ShutdownRequested):
        print()
    finally:
        child_processes.terminate_all(grace)
        httpd.server_close()


class DataRootUnusable(Exception):
    def __init__(self, path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class PortsExhausted(Exception):
    def __init__(self, first: int, last: int) -> None:
        super().__init__(f"ports {first}-{last} are all in use")
        self.first = first
        self.last = last


AlreadyRunning = sbp_instance_lock.AlreadyRunning
EXIT_PORTS_BUSY = 75
EXIT_ALREADY_RUNNING = 76
EXIT_DATA_ROOT_UNUSABLE = 77

_ERRNO_REASONS = {
    errno.EACCES: "沒有寫入權限",
    errno.EPERM: "沒有寫入權限",
    errno.ENOENT: "找不到",
    errno.ENOSPC: "磁碟空間不足",
    errno.EROFS: "唯讀",
    errno.ENOTDIR: "路徑中有一段不是資料夾",
    errno.EEXIST: "路徑中有一段不是資料夾",
}


def _data_root_reason(exc: OSError) -> str:
    return _ERRNO_REASONS.get(exc.errno) or f"系統回報：{exc.strerror or exc}"


def preflight_data_root(path: str | Path) -> Path:
    """The data folder exists (created if needed) and accepts a new file."""
    root = Path(path).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
        fd, probe = tempfile.mkstemp(prefix=".ditl_sbp_write_check_", dir=root)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise DataRootUnusable(root, _data_root_reason(exc)) from exc
    return root


def serve(host: str, ports, data_root: str | Path, *, on_listening=None) -> None:
    """Check the folder, take the one-server-per-folder lock, bind the first free
    port, record it, announce it, then serve until Ctrl-C or SIGTERM."""
    root = preflight_data_root(data_root)
    ports = list(ports)
    try:
        lock = sbp_instance_lock.InstanceLock(root).acquire()
    except AlreadyRunning:
        raise
    except OSError as exc:
        raise DataRootUnusable(root, _data_root_reason(exc)) from exc
    try:
        httpd = None
        for port in ports:
            try:
                httpd = ThreadingHTTPServer((host, port), Handler)
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
        if httpd is None:
            raise PortsExhausted(ports[0], ports[-1])
        try:
            lock.publish_port(httpd.server_address[1])
            signal.signal(signal.SIGTERM, _raise_shutdown_requested)
            if on_listening is not None:
                on_listening(httpd)
        except BaseException:
            httpd.server_close()
            raise
        _serve_until_stopped(httpd)
    finally:
        lock.release()


def _startup_failure(exc: Exception, host: str, requested_port: int, single_port: bool) -> tuple[str, int] | None:
    if isinstance(exc, DataRootUnusable):
        return (f"無法使用研究資料資料夾：{exc.path}（{exc.reason}）。請確認它存在，而且你有寫入權限。",
                EXIT_DATA_ROOT_UNUSABLE)
    if isinstance(exc, AlreadyRunning):
        where = f"http://{host}:{exc.port}，" if exc.port else ""
        who = f"程序 {exc.pid}" if exc.pid else "程序不明"
        return (f"這個資料夾已經有一個 ditl_sbp 在執行（{where}{who}）。", EXIT_ALREADY_RUNNING)
    if isinstance(exc, PortsExhausted):
        if single_port:
            return (f"連接埠 {exc.first} 已被其他程式使用。", EXIT_PORTS_BUSY)
        return (f"{exc.first} 到 {exc.last} 的連接埠都被占用。請關閉其他程式，或用 --port 指定別的連接埠。",
                EXIT_PORTS_BUSY)
    return None


def main():
    _default_cli_runner_command()
    p = argparse.ArgumentParser(prog="sbp-review")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", help="listener address; overrides SBP_BIND_HOST")
    p.add_argument(
        "--data-root", type=Path,
        help="explicit process-local case/run root (default: repository data)",
    )
    p.add_argument(
        "--frozen-corpus-manifest", type=Path,
        help="test-only deterministic corpus; requires --data-root",
    )
    args = p.parse_args()
    if args.frozen_corpus_manifest is not None and args.data_root is None:
        p.error("--frozen-corpus-manifest requires --data-root")
    host = resolve_bind_host(args.host)
    root = DEFAULT_DATA_ROOT if args.data_root is None else args.data_root
    try:
        preflight_data_root(root)
        configure_data_root(
            args.data_root,
            frozen_corpus_manifest=args.frozen_corpus_manifest,
        )

        def banner(httpd):
            print(f"sbp-review → http://{host}:{httpd.server_address[1]}  (Ctrl-C 結束)", flush=True)

        serve(host, [args.port], DATA_ROOT, on_listening=banner)
    except (DataRootUnusable, AlreadyRunning, PortsExhausted) as exc:
        message, code = _startup_failure(exc, host, args.port, single_port=True)
        print(message, file=sys.stderr)
        sys.exit(code)


if __name__ == "__main__":
    main()
