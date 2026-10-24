"""Optional OpenAI provider for the sbp-gen pipeline.

OpenAI's Chat Completions API is the origin of the OpenAI-compatible wire
protocol Groq and OpenRouter both mimic, so this reuses the same shared
transport (`_openai_compatible_llm.call()`).
"""
from __future__ import annotations

from . import _openai_compatible_llm
from .llm import CallResult

BASE_URL = "https://api.openai.com/v1"
API_KEY_ENV = "OPENAI_API_KEY"

# 釘死有日期的版本，不用會偷偷換底層版本的別名（跟 gemini_llm.py 同一個理由）——
# 2026-08-27 用這把 key 打 /v1/models 現場確認過這兩個 id 真的存在。
# 名稱會變動，跑之前先查 https://platform.openai.com/docs/models 確認還在。
#
# 旗艦版故意不用 `-pro` 後綴那個變體：2026-08-27/28 兩次現場撞到
# 「This is not a chat model and thus not supported in the v1/chat/
# completions endpoint」，`-pro` 系列走的是不同的 API 介面，這個共用傳輸層
# 目前只走 chat/completions，選了會直接 404。
_OPENAI_MINI = "gpt-5.4-mini-2026-03-17"
_OPENAI_FLAGSHIP = "gpt-5.4-2026-03-05"

DEFAULT_MODELS = {
    "align": _OPENAI_MINI,
    "journey": _OPENAI_MINI,
    "journey_recheck": _OPENAI_MINI,
    "as_is_recheck": _OPENAI_MINI,
    "issues": _OPENAI_MINI,
    "issue_grounding": _OPENAI_MINI,
    "research_support": _OPENAI_MINI,
    "compare": _OPENAI_MINI,
    "reference": _OPENAI_MINI,
    # 詮釋層：跟上面同一個 sonnet 量級對應，維持 mini（2026-09-08 補齊——這批
    # stage 在 llm.py 裡本來就是 sonnet 量級，只是加進 llm.py 的時間晚於這份
    # map，之前落在下面 default_model() 的 align 後備值，數值恰好一樣，但
    # 「恰好一樣」不是保證——新加的 stage 只要量級跟 align 不同就會悄悄錯，
    # 見 test_llm_provider_stage_coverage.py 的跨 provider 核對）。
    "axial_coding": _OPENAI_MINI,
    "open_coding": _OPENAI_MINI,
    "category_properties": _OPENAI_MINI,
    "persona_matrix": _OPENAI_MINI,
    "persona_comparison": _OPENAI_MINI,
    "persona_dimension_synthesis": _OPENAI_MINI,
    "persona_quadrant_names": _OPENAI_MINI,
    "persona_content": _OPENAI_MINI,
    "persona_dimension_candidates": _OPENAI_MINI,
    "jtbd": _OPENAI_MINI,
    "primary_job_candidates": _OPENAI_MINI,
    "job_map": _OPENAI_MINI,
    "how_might_we": _OPENAI_MINI,
    "primary_hmw_candidates": _OPENAI_MINI,
    "journey_annotations": _OPENAI_MINI,
    "chart_guides": _OPENAI_MINI,
    "selective_themes": _OPENAI_MINI,
    "semantic_segments": _OPENAI_MINI,
    "segmentation_axis_candidates": _OPENAI_MINI,
    "target_segmentation": _OPENAI_MINI,
    "core_category_candidates": _OPENAI_MINI,
    "narrative_job_candidates": _OPENAI_MINI,
    "selective_coding_narrative": _OPENAI_MINI,
    "structure": _OPENAI_FLAGSHIP,
    "reconcile": _OPENAI_FLAGSHIP,
    # to_be 在 llm.py 是 opus 量級（最高風險的合成步驟），之前沒被列進這份
    # map，所以透過 default_model() 的 align 後備值落到 mini——風險最高的
    # stage 反而拿到最便宜的模型，還被 run.json 當成拿到了宣告的 effort。
    "to_be": _OPENAI_FLAGSHIP,
    "brief": _OPENAI_FLAGSHIP,
}


def call(
    *,
    stage: str,
    system: str,
    user: str,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 32000,
) -> CallResult:
    del effort  # 跟 Gemini/Groq/OpenRouter 一致：不猜 reasoning_effort 的映射。
    return _openai_compatible_llm.call(
        provider_label="OpenAI",
        base_url=BASE_URL,
        api_key_env=API_KEY_ENV,
        model=model or DEFAULT_MODELS.get(stage, _OPENAI_MINI),
        system=system,
        user=user,
        max_tokens=max_tokens,
        max_tokens_param="max_completion_tokens",
    )
