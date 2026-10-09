"""LLM API 呼叫層（Anthropic default，Gemini/Groq/OpenRouter/OpenAI optional）。

三個階段共用。每個階段自己決定 model 與 effort（ADR-0003 決定一：
align／compare 是整理性工作，build 是最高風險的一段，值得多花）。

刻意不用 output_config.format（結構化輸出）：它支援的 JSON Schema 子集不含
if/then，而本專案兩條最重要的約束都是 if/then——「claims 空時 blank 必填」
與「inferred=false 時 evidence_ref 必填」。用結構化輸出只會擋掉淺層錯誤，
卻讓人誤以為深層約束也被擋了。改成「產生 → 用完整 schema 驗證 → 把錯誤餵
回去重試」，驗證的是真正要守的那份契約。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass

DEFAULT_MODELS = {
    # 萃取與比對：整理性工作，風險低於 structure
    "align": "claude-sonnet-5",
    "journey": "claude-sonnet-5",
    "journey_recheck": "claude-sonnet-5",
    # 2026-09-08 補齊——DEFAULT_EFFORT 一直都有這個 stage（跟 journey_recheck
    # 同一個量級，見那邊的註解），DEFAULT_MODELS 漏了，導致
    # default_model()/validated_json_with_fallback() 對這個 stage 一律
    # KeyError，函式從沒被叫成功過（見 as_is_recheck() 的呼叫端；目前也還
    # 沒有任何呼叫者，這個修法讓它至少變得可呼叫）。
    "as_is_recheck": "claude-sonnet-5",
    "issues": "claude-sonnet-5",
    "issue_grounding": "claude-sonnet-5",
    "research_support": "claude-sonnet-5",
    "compare": "claude-sonnet-5",
    "axial_coding": "claude-sonnet-5",
    "open_coding": "claude-sonnet-5",
    "category_properties": "claude-sonnet-5",
    "persona_matrix": "claude-sonnet-5",
    "persona_content": "claude-sonnet-5",
    "persona_dimension_candidates": "claude-sonnet-5",
    "jtbd": "claude-sonnet-5",
    "primary_job_candidates": "claude-sonnet-5",
    "job_map": "claude-sonnet-5",
    "how_might_we": "claude-sonnet-5",
    "primary_hmw_candidates": "claude-sonnet-5",
    "journey_annotations": "claude-sonnet-5",
    "chart_guides": "claude-sonnet-5",
    "selective_themes": "claude-sonnet-5",
    "semantic_segments": "claude-sonnet-5",
    "segmentation_axis_candidates": "claude-sonnet-5",
    "target_segmentation": "claude-sonnet-5",
    "core_category_candidates": "claude-sonnet-5",
    "narrative_job_candidates": "claude-sonnet-5",
    "selective_coding_narrative": "claude-sonnet-5",
    # 建模與推論：假引用發生在這一段，四條句子規則跟 disposition／annotation
    # 判斷（streamline-sbp-generation-pipeline 決定二併進來的）都在這裡
    "structure": "claude-opus-5",
    "reference": "claude-sonnet-5",
    "reconcile": "claude-opus-5",
    # To-be synthesis is a bounded, tool-less construction stage.  Its exact
    # route is versioned by the To-be operation rather than inferred from the
    # lower-risk extraction defaults.
    "to_be": "claude-opus-4-6",
    # 簡報是研究者第一個讀、也最信任的文字（add-research-briefing-layer design.md
    # 決定：跟 structure 同一個量級，不是跟 compare 同一個量級——排錯優先順序的
    # 代價，比排錯一格證據的代價高）。
    "brief": "claude-opus-5",
}

DEFAULT_EFFORT = {
    "align": "medium",
    "journey": "medium",
    "journey_recheck": "medium",
    # 研究者改完藍圖之後的重新檢查。跟 journey_recheck 同一個量級：讀完整份
    # 證據、整張圖看一遍，但只回報、不改東西。
    "as_is_recheck": "medium",
    "issues": "medium",
    "issue_grounding": "medium",
    "research_support": "medium",
    "compare": "medium",
    "axial_coding": "medium",
    "open_coding": "medium",
    "category_properties": "medium",
    # 2026-09-09 補齊——add-open-coding-stage 實作時發現
    # selective_coding_narrative 從加入 DEFAULT_MODELS 那次起就漏掉這裡的
    # 對應項：跟 as_is_recheck 那次一樣的漏法，這裡順手補上，避免
    # default_effort()/validated_json_with_fallback() 在沒有明確 effort
    # 覆寫時對這個 stage KeyError。
    "selective_coding_narrative": "medium",
    "persona_matrix": "medium",
    "persona_content": "medium",
    "persona_dimension_candidates": "medium",
    "jtbd": "medium",
    "primary_job_candidates": "medium",
    "job_map": "medium",
    "how_might_we": "medium",
    "primary_hmw_candidates": "medium",
    "journey_annotations": "medium",
    "chart_guides": "medium",
    "selective_themes": "medium",
    "semantic_segments": "medium",
    "segmentation_axis_candidates": "medium",
    "target_segmentation": "medium",
    "core_category_candidates": "medium",
    "narrative_job_candidates": "medium",
    "structure": "high",
    "reference": "medium",
    "reconcile": "high",
    "to_be": "high",
    "brief": "high",
}

PROVIDERS = ("anthropic", "gemini", "groq", "openrouter", "openai", "cli", "transport")
DEFAULT_PROVIDER = "anthropic"
STAGE_DEFAULT_PROVIDERS = {
    "journey_recheck": "gemini",
    "issues": "gemini",
    "issue_grounding": "gemini",
}

# require-explicit-authorization-for-live-model-calls: the manual-transport
# provider is the only one that guarantees no network call. Every other
# entry in PROVIDERS is "live" for this purpose -- derived from PROVIDERS so
# this can't silently drift out of sync with it if a provider is ever added.
RELAY_PROVIDER = "transport"
LIVE_PROVIDERS = tuple(p for p in PROVIDERS if p != RELAY_PROVIDER)

# Single source of truth for "does this provider's call() actually honour the
# `effort` argument, or silently discard it." Every non-Anthropic transport's
# call() takes `effort` and immediately does `del effort` (each with its own
# comment on why: no trustworthy reasoning-effort mapping for that vendor).
# Anything that records a stage's effort for provenance (run.json's
# "efforts", the dry-run printout) must check this set first, or it ends up
# recording the requested effort as if it were the effort that was used.
PROVIDERS_SUPPORTING_EFFORT = frozenset({"anthropic"})
NOT_SUPPORTED_BY_PROVIDER = "not_supported_by_provider"


def model_name(record: object) -> str:
    """Read both structured provenance and historical model-name strings."""
    if isinstance(record, dict) and isinstance(record.get("model"), str):
        return record["model"]
    return record if isinstance(record, str) else str(record)


def effort_for_record(stage: str, provider: str, requested_effort: str | None) -> str:
    """The effort value to persist for provenance -- what will actually be
    used, not what was asked for. Mirrors default_model()'s job for models."""
    if provider not in PROVIDERS_SUPPORTING_EFFORT:
        return NOT_SUPPORTED_BY_PROVIDER
    return requested_effort or DEFAULT_EFFORT[stage]


class LLMError(RuntimeError):
    pass


@dataclass
class CallResult:
    """一次 `call()` 的結果——文字內容跟這次呼叫真的花了多少（generation-usage-tracking
    change）。跟「模型產出的內容」分開放在一個具名物件裡，呼叫端不用靠位置記欄位。"""

    text: str
    tokens: int
    seconds: float
    # CLI runner only: the requested/resolved model and effort (configuration,
    # not runtime-confirmed) and the executor's own per-kind token counts.
    model: str | None = None
    effort: str | None = None
    usage: dict | None = None


@dataclass
class ValidatedJSONResult:
    data: object
    tokens: int
    seconds: float
    attempts: list[dict]


class ValidatedJSONError(LLMError):
    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


def _client():
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMError(
            "缺少 anthropic 套件。請先 pip install anthropic"
        ) from e
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get(
        "ANTHROPIC_AUTH_TOKEN"
    ):
        # 不直接失敗：SDK 也接受 `ant auth login` 存下的 profile
        print(
            "[提示] 未設定 ANTHROPIC_API_KEY；SDK 會嘗試改用 ant auth login 的 profile",
            file=sys.stderr,
        )
    return anthropic.Anthropic()


def resolve_provider(provider: str | None = None, *, stage: str | None = None) -> str:
    """Resolve a provider while keeping Anthropic as the historical default."""
    if provider is not None:
        candidates = [provider]
    else:
        candidates = []
        if stage:
            candidates.append(os.environ.get(f"SBP_{stage.upper()}_PROVIDER"))
        candidates.extend((
            os.environ.get("SBP_LLM_PROVIDER"),
            STAGE_DEFAULT_PROVIDERS.get(stage or ""),
            DEFAULT_PROVIDER,
        ))
    selected = next(
        (value.strip().lower() for value in candidates if isinstance(value, str) and value.strip()),
        DEFAULT_PROVIDER,
    )
    if selected not in PROVIDERS:
        raise LLMError(
            f"不支援的 LLM provider：{selected!r}；可用值：{', '.join(PROVIDERS)}"
        )
    return selected


def default_model(stage: str, provider: str | None = None) -> str:
    """Return a provider-specific default model for CLI display and run state."""
    selected = resolve_provider(provider, stage=stage)
    if selected == "gemini":
        from .gemini_llm import DEFAULT_MODELS as GEMINI_MODELS

        return GEMINI_MODELS.get(stage, GEMINI_MODELS["align"])
    if selected == "groq":
        from .groq_llm import DEFAULT_MODELS as GROQ_MODELS

        return GROQ_MODELS.get(stage, GROQ_MODELS["align"])
    if selected == "openrouter":
        from .openrouter_llm import DEFAULT_MODELS as OPENROUTER_MODELS

        return OPENROUTER_MODELS.get(stage, OPENROUTER_MODELS["align"])
    if selected == "openai":
        from .openai_llm import DEFAULT_MODELS as OPENAI_MODELS

        return OPENAI_MODELS.get(stage, OPENAI_MODELS["align"])
    if selected == "transport":
        # 手動傳輸沒有預設模型——回答的是操作者找的那個代理，只有他知道是誰。
        # 宣告值從 SBP_TRANSPORT_MODEL 來，沒宣告就照實記成未宣告，不猜。
        from .transport_llm import declared_model

        return declared_model()
    if selected == "cli":
        return os.environ.get("SBP_CLI_MODEL", "authenticated-cli")
    return DEFAULT_MODELS[stage]


def call(
    *,
    stage: str,
    system: str,
    user: str,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 32000,
    provider: str | None = None,
) -> CallResult:
    """Call the selected provider; omitted provider means the legacy default."""
    selected = resolve_provider(provider, stage=stage)
    if selected == "gemini":
        from . import gemini_llm

        return gemini_llm.call(
            stage=stage,
            system=system,
            user=user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
        )
    if selected == "groq":
        from . import groq_llm

        return groq_llm.call(
            stage=stage,
            system=system,
            user=user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
        )
    if selected == "openrouter":
        from . import openrouter_llm

        return openrouter_llm.call(
            stage=stage,
            system=system,
            user=user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
        )
    if selected == "transport":
        # 手動傳輸：不打網路，把提示詞交出去、等人把回覆搬回來
        # （make-manual-transport-repeatable）。
        from . import transport_llm

        return transport_llm.call(
            stage=stage,
            system=system,
            user=user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
        )
    if selected == "cli":
        from . import cli_runner

        return cli_runner.call(
            stage=stage,
            system=system,
            user=user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
        )
    if selected == "openai":
        from . import openai_llm

        return openai_llm.call(
            stage=stage,
            system=system,
            user=user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
        )
    return _call_anthropic(
        stage=stage,
        system=system,
        user=user,
        model=model,
        effort=effort,
        max_tokens=max_tokens,
    )


# fix-retry-baseline-selection: a reply whose root has the wrong shape (e.g. a
# JSON Patch array where a graph object is expected) gets only one root error,
# so it must not compete with complete artifacts on error count.
_ROOT_SHAPE_ERROR = re.compile(r"^\[結構\] \(root\): .*(?:is not of type|is a required property)")


def is_repair_baseline(errors: list[str]) -> bool:
    """Whether an output may anchor a repair: no root-level schema error."""
    return not any(_ROOT_SHAPE_ERROR.search(str(error)) for error in errors)


class RepairBaseline:
    """The output a retry corrects. Among complete outputs (see
    `is_repair_baseline`) the one with the fewest errors wins; until one
    exists, the fewest-errors output of any shape is used, as before."""

    def __init__(self):
        self.complete = None  # (data, errors)
        self.any = None

    def offer(self, data, errors: list[str]) -> None:
        if self.any is None or len(errors) < len(self.any[1]):
            self.any = (data, errors)
        if is_repair_baseline(errors) and (self.complete is None or len(errors) < len(self.complete[1])):
            self.complete = (data, errors)

    def anchor(self):
        return self.complete or self.any


def build_retry_message(user: str, anchor_output: str, errors: list[str], closing: str | None = None) -> str:
    """Build a retry prompt that corrects a concrete prior draft instead of
    regenerating blind (fix-structure-retry-anchoring design decision: one
    shared retry-prompt builder used by both retry loops).

    Embedding the anchor draft gives the model something concrete to edit;
    without it, a shrinking error list is the model's only signal about what
    the rest of the output should look like, which is what let it drop a
    passing field like `claims` on a later attempt (see the change's
    proposal for the observed 11→1→2→3→6 error sequence).

    `closing` (experiment-open-coding-partial-repair): a retry that asks for
    only part of the output states its own last paragraph; the carried draft
    and the error list stay identical in form. Left out, nothing changes.
    """
    shown = "\n".join(f"- {e}" for e in errors[:40])
    more = f"\n（另有 {len(errors) - 40} 筆同類錯誤）" if len(errors) > 40 else ""
    ask = closing if closing is not None else (
        "請只修正上面點出的問題，其餘部分維持你上一次的內容不變，"
        "回傳修正後的完整 JSON。"
    )
    return (
        f"{user}\n\n---\n\n這是你上一次的完整輸出：\n\n{anchor_output}\n\n"
        f"這次驗證沒通過，問題如下：\n\n{shown}{more}\n\n"
        f"{ask}"
    )


def validated_json_with_fallback(
    *,
    stage: str,
    system: str,
    user: str,
    validate,
    canonicalize,
    primary_provider: str,
    fallback_provider: str | None = "anthropic",
    models: dict[str, str | None] | None = None,
    efforts: dict[str, str | None] | None = None,
    max_tokens: int = 32000,
    max_schema_retries: int = 2,
) -> ValidatedJSONResult:
    """Generate one validated JSON result with a one-way provider fallback.

    Provider and parse failures move directly to fallback. Schema failures use
    the configured repair budget first. Every attempt is retained even when no
    provider succeeds, allowing the orchestrator to persist a diagnostic trace
    without trusting model-authored metadata.

    Retries anchor on the best-scoring attempt so far, and that anchor
    persists across a provider switch instead of resetting (fix-structure-
    retry-anchoring design decisions: anchor on the best attempt, and the
    anchor persists across provider fallback). A provider or parse failure
    yields no candidate and leaves the existing anchor untouched.

    require-explicit-authorization-for-live-model-calls: when the primary
    provider is the manual-transport relay, no fallback is ever appended,
    regardless of what the caller passes -- transport exhausting its retry
    budget or failing to parse a pasted response fails through transport
    itself, never through a live provider. This is the one place that rule
    is enforced; the four production call sites keep passing
    fallback_provider="anthropic" unconditionally and it is simply ignored
    under transport.
    """
    providers = [primary_provider]
    # research-execution-adapters: a Workbench API job runs only on the
    # researcher-selected provider with their own key; it never falls back.
    api_execution = (os.environ.get("SBP_EXECUTION_MODE") or "").strip().lower() == "api"
    if (
        primary_provider not in {RELAY_PROVIDER, "cli"}
        and not api_execution
        and fallback_provider
        and fallback_provider != primary_provider
    ):
        providers.append(fallback_provider)
    # Defence in depth: if a future change to the block above ever lets a
    # live provider sit alongside transport in this list, fail loudly here
    # rather than silently placing a live call.
    if RELAY_PROVIDER in providers and any(p in LIVE_PROVIDERS for p in providers):
        raise LLMError("手動傳輸不得退回任何線上 provider")
    attempts: list[dict] = []
    total_tokens = 0
    total_seconds = 0.0
    fallback_reason = None
    baseline = RepairBaseline()
    for provider_index, provider in enumerate(providers):
        anchor = baseline.anchor()
        convo = (
            user
            if anchor is None
            else build_retry_message(user, json.dumps(anchor[0], indent=2, ensure_ascii=False), anchor[1])
        )
        model = (models or {}).get(provider) or default_model(stage, provider)
        effort = (efforts or {}).get(provider) or DEFAULT_EFFORT[stage]
        for repair_index in range(max_schema_retries + 1):
            attempt_number = len(attempts) + 1
            trace_base = {
                "provider": provider,
                "model": model,
                "attempt": attempt_number,
                "validation_errors": [],
            }
            if provider_index and fallback_reason:
                trace_base["fallback_reason"] = fallback_reason
            try:
                response = call(
                    stage=stage,
                    system=system,
                    user=convo,
                    model=model,
                    effort=effort,
                    max_tokens=max_tokens,
                    provider=provider,
                )
                total_tokens += response.tokens
                total_seconds += response.seconds
            except LLMError as exc:
                outcome = "empty_output" if "空" in str(exc) else "provider_error"
                attempts.append({**trace_base, "outcome": outcome, "validation_errors": [str(exc)]})
                fallback_reason = f"{provider}:{outcome}"
                break
            try:
                data = extract_json(response.text)
            except LLMError as exc:
                attempts.append({**trace_base, "outcome": "parse_error", "validation_errors": [str(exc)]})
                fallback_reason = f"{provider}:parse_error"
                break
            accepted_trace = [*attempts, {**trace_base, "outcome": "accepted"}]
            canonicalize(data, accepted_trace)
            validation_errors = validate(data)
            if not validation_errors:
                return ValidatedJSONResult(
                    data=data,
                    tokens=total_tokens,
                    seconds=total_seconds,
                    attempts=accepted_trace,
                )
            attempts.append({
                **trace_base,
                "outcome": "validation_error",
                "validation_errors": validation_errors[:40],
            })
            baseline.offer(data, validation_errors)
            if repair_index == max_schema_retries:
                fallback_reason = f"{provider}:validation_error"
                break
            anchor_data, anchor_errors = baseline.anchor()
            convo = build_retry_message(user, json.dumps(anchor_data, indent=2, ensure_ascii=False), anchor_errors)
    raise ValidatedJSONError(
        f"{stage} 的 primary 與 fallback 都未產生有效結果",
        attempts,
    )


def _call_anthropic(
    *,
    stage: str,
    system: str,
    user: str,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 32000,
) -> CallResult:
    """送一次請求，回傳文字內容連同這次呼叫真的花了多少 token／秒數。

    一律用 streaming——build 階段的輸出動輒兩萬 token 以上，非串流會撞到
    SDK 的 HTTP timeout。

    秒數只量這段 API 等待時間，不含下面的文字擷取／檢查——那些是毫秒級，
    比 API 等待小很多（design.md 風險段落）。
    """
    model = model or DEFAULT_MODELS[stage]
    effort = effort or DEFAULT_EFFORT[stage]

    client = _client()
    started = time.perf_counter()
    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            message = stream.get_final_message()
    except TypeError as exc:
        # The SDK raises a bare TypeError, at request time (not client
        # construction), for "Could not resolve authentication method" when
        # neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set and no
        # `ant auth login` profile applies. That text reads like a data or
        # code problem -- two real runs (see runs/瀏覽器測試案例-2026-09-06-*)
        # failed exactly this way because the server was started without
        # tools/with-keys, which this machine needs (see that script's own
        # comment: it has no Anthropic credential at all). Only this one
        # known message is rewritten; any other TypeError propagates as-is.
        if "Could not resolve authentication method" in str(exc):
            raise LLMError(
                "Anthropic 呼叫失敗：沒有可用的認證（沒有 ANTHROPIC_API_KEY／"
                "ANTHROPIC_AUTH_TOKEN，也沒有 `ant auth login` 設定檔）。"
                "這台機器本來就沒有 Anthropic 憑證——用 `tools/with-keys` 啟動"
                "伺服器／CLI，或改用其他 provider。"
            ) from exc
        raise
    seconds = time.perf_counter() - started

    if message.stop_reason == "refusal":
        raise LLMError(f"模型拒絕回應（{getattr(message, 'stop_details', None)}）")
    if message.stop_reason == "max_tokens":
        raise LLMError(
            f"輸出被 max_tokens={max_tokens} 截斷。調高 --max-tokens 後重跑，"
            "不要拿截斷的結果去驗證。"
        )

    text = "".join(b.text for b in message.content if b.type == "text")
    if not text.strip():
        raise LLMError("模型回了空字串")

    # 防禦性處理：usage 理論上一定有，但用量統計不精確不該擋住生成本身。
    usage = getattr(message, "usage", None)
    tokens = (getattr(usage, "input_tokens", 0) or 0) + (
        getattr(usage, "output_tokens", 0) or 0
    )

    return CallResult(text=text, tokens=tokens, seconds=seconds)


def extract_json(text: str):
    """從回覆裡挖出 JSON。

    容忍模型在 JSON 前後多寫幾句話或包 ```json 圍籬——這是造句層的雜訊，
    不值得為它多跑一次 API。
    """
    s = text.strip()
    if "```" in s:
        # 取第一個圍籬區塊的內容
        parts = s.split("```")
        for part in parts[1:]:
            body = part.split("\n", 1)[-1] if "\n" in part else part
            body = body.strip()
            if body.startswith(("{", "[")):
                s = body
                break
    start = min(
        (i for i in (s.find("{"), s.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        raise LLMError(f"回覆裡找不到 JSON:\n{text[:500]}")
    end = max(s.rfind("}"), s.rfind("]"))
    s = s[start : end + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失敗：{e}\n---\n{s[:800]}") from e
