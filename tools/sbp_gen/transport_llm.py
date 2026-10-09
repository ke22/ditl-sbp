"""手動傳輸 provider——把一次模型呼叫換成一次人工接力。

make-manual-transport-repeatable：這個專案跑過兩次手動傳輸（見
`runs/2026-08-21-manual-transport-chongzheng/run.json`），做法是把管線真的
會送出去的提示詞擷取出來、交給一個全新 context 的代理、再把回覆搬回來。
當時用來擷取的 `driver.py` 跟事後修欄位的兩支腳本都沒有進版控，於是那套
做法只存在於一段散文裡。

暫停機制放在 provider 這一層，是因為這裡是階段唯一對外講話的地方：上面的
訊息組裝、驗證閉包、重試錨定、usage 累加全部原封不動地跑。提示詞因此是
「構造上就是真的」，不是靠另一條重建路徑產生的——重建的那條路正是會漂掉
的那條（design.md 決定一）。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .llm import CallResult, NOT_SUPPORTED_BY_PROVIDER

# 工作區放在 run 目錄底下。run 目錄不進版控，而且把「這份產出是怎麼來的」
# 留在產出旁邊正是這支 change 的目的，所以不做自動清理。
WORKSPACE_DIRNAME = "_transport"
MODEL_ENV = "SBP_TRANSPORT_MODEL"
UNDECLARED_MODEL = "未宣告"
# usage 記成「量不到」而不是 0：usage 揭露會把每個階段的量攤給研究者看，
# 0 讀起來是「這一步沒花成本」，不是「這一步的成本不知道」。
UNMEASURED_USAGE = "unmeasured"
# 這份產出不能當評測的對照組——`docs/experiment/pipeline.md` 要求候選與
# 基準的 model／effort／max tokens 一致並保留逐段 usage，手動傳輸三樣都
# 給不出來。崇正那次是靠人手寫一段說明擋住的，這裡改成工具自己寫。
BASELINE_DISQUALIFIED_KEY = "evaluation_baseline_eligible"
ORIGIN = "manual_transport"


class TransportSuspended(BaseException):
    """一次手動接力的暫停點。

    **刻意繼承 `BaseException` 而不是 `Exception`**：十五支單階段指令各自
    有一段 `except Exception` 會把失敗寫進 run 狀態，暫停被它們接到就會留下
    一個根本沒發生的失敗紀錄。繼承 `BaseException` 讓那些 handler 看不到它，
    只有 `__main__.main()` 那一個 handler 接得到（design.md 決定二）。

    改成繼承 `Exception` 會讓這件事悄悄壞掉，所以有一條測試直接盯住基底類別。
    """

    def __init__(self, *, stage: str, attempt: int, prompt_path: Path, response_path: Path) -> None:
        super().__init__(f"{stage} 第 {attempt} 輪等待人工回覆")
        self.stage = stage
        self.attempt = attempt
        self.prompt_path = prompt_path
        self.response_path = response_path

    def operator_instructions(self, *, command: str) -> str:
        return (
            f"[手動傳輸] {self.stage} 第 {self.attempt} 輪：提示詞已存好，等你把模型回覆搬回來。\n"
            f"  1. 打開      {self.prompt_path}\n"
            f"  2. 把它整份交給 Claude Code／Codex，把回覆原樣存成 {self.response_path}\n"
            f"  3. 設定 {MODEL_ENV}=<實際回答的模型名稱>，再跑一次同一道指令：\n"
            f"     {command}"
        )


class TransportStale(RuntimeError):
    """回覆是對著另一版提示詞或另一批輸入準備的。

    普通的 `RuntimeError`，不是 `TransportSuspended`——這是真的失敗，該被
    指令自己的失敗處理接住並記錄。
    """


# 這個 process 裡每個階段已經呼叫過幾次。重試迴圈一輪呼叫一次，所以這個
# 計數就是輪次。不從檔案數推算：檔案會留著當歷史，數檔案會在續跑時把
# 第一輪算成新的一輪。
_calls_this_process: dict[str, int] = {}
_workspace_root: Path | None = None
_consumed_stages: list[str] = []


def configure_workspace(run_dir: Path | str | None) -> None:
    """由 `__main__.main()` 在分派前呼叫一次，指定這次指令的 run 目錄。"""
    global _workspace_root
    _workspace_root = Path(run_dir) if run_dir is not None else None
    _calls_this_process.clear()
    _consumed_stages.clear()


def workspace_root() -> Path | None:
    return _workspace_root


def consumed_stages() -> list[str]:
    """這次執行真的走過手動傳輸的階段。空的代表這道指令沒有呼叫模型。"""
    return list(_consumed_stages)


def _stage_dir(stage: str) -> Path:
    if _workspace_root is None:
        raise TransportStale(
            "手動傳輸沒有拿到 run 目錄——只有帶 --run 的單階段指令支援這個 provider。"
        )
    return _workspace_root / WORKSPACE_DIRNAME / stage


def prompt_path(stage: str, attempt: int) -> Path:
    return _stage_dir(stage) / f"attempt-{attempt:02d}.prompt.md"


def response_path(stage: str, attempt: int) -> Path:
    return _stage_dir(stage) / f"attempt-{attempt:02d}.response.txt"


_SYSTEM_HEADING = "## 系統提示詞"
_USER_HEADING = "## 使用者訊息"


def text_digest(system: str, user: str) -> str:
    """給人對照兩份信封用的短指紋。**不是**判斷過期的依據——過期是拿
    記下來的原文跟這次重建的原文逐字比對決定的（design.md 決定四）。"""
    h = hashlib.sha256()
    h.update(system.encode("utf-8"))
    h.update(b"\x00")
    h.update(user.encode("utf-8"))
    return h.hexdigest()[:12]


def render_envelope(*, stage: str, attempt: int, system: str, user: str) -> str:
    return (
        f"# 手動傳輸信封\n\n"
        f"- 階段：{stage}\n"
        f"- 輪次：{attempt}\n"
        f"- 指紋：{text_digest(system, user)}\n\n"
        f"{_SYSTEM_HEADING}\n\n{system}\n\n{_USER_HEADING}\n\n{user}\n"
    )


def parse_envelope(text: str) -> tuple[str, str]:
    """把信封拆回 `(system, user)`。

    切法跟 `render_envelope()` 寫出來的格式綁在一起，所以兩者要一起改。
    分隔用的是行首的標題，提示詞內文裡出現同樣字串也不會誤切。
    """
    marker = f"\n{_USER_HEADING}\n"
    head, _, tail = text.partition(marker)
    if not _:
        raise TransportStale("信封格式不對：找不到使用者訊息的分段。")
    _, _, system = head.partition(f"\n{_SYSTEM_HEADING}\n")
    return system.strip("\n"), tail.strip("\n")


def call(
    *,
    stage: str,
    system: str,
    user: str,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 32000,
) -> CallResult:
    """不呼叫任何模型服務。

    有這一輪的回覆就把它當成這次呼叫的結果回傳；沒有就把這一輪的提示詞
    寫成信封並暫停。`effort` 跟 `max_tokens` 收下即丟——手動傳輸保證不了
    這兩個值，`PROVIDERS_SUPPORTING_EFFORT` 不含 transport，所以 effort
    的 provenance 會自動記成「這個 provider 不支援」。
    """
    del effort, max_tokens
    attempt = _calls_this_process.get(stage, 0) + 1
    _calls_this_process[stage] = attempt
    envelope_path = prompt_path(stage, attempt)
    reply_path = response_path(stage, attempt)

    if not reply_path.is_file():
        envelope_path.parent.mkdir(parents=True, exist_ok=True)
        envelope_path.write_text(
            render_envelope(stage=stage, attempt=attempt, system=system, user=user),
            encoding="utf-8",
        )
        raise TransportSuspended(
            stage=stage, attempt=attempt, prompt_path=envelope_path, response_path=reply_path
        )

    if not envelope_path.is_file():
        raise TransportStale(
            f"{stage} 第 {attempt} 輪有回覆但沒有對應的信封（{envelope_path}），無法確認它是對著哪一版提示詞寫的。"
        )
    recorded_system, recorded_user = parse_envelope(envelope_path.read_text(encoding="utf-8"))
    if recorded_system != system.strip("\n"):
        raise TransportStale(
            f"{stage} 第 {attempt} 輪的回覆是對著另一版系統提示詞準備的——提示詞或 schema 改過了。"
            f"請刪掉 {_stage_dir(stage)} 重跑這個階段。"
        )
    if recorded_user != user.strip("\n"):
        raise TransportStale(
            f"{stage} 第 {attempt} 輪的回覆是對著另一批輸入準備的——這個 run 的輸入改過了。"
            f"請刪掉 {_stage_dir(stage)} 重跑這個階段。"
        )

    if stage not in _consumed_stages:
        _consumed_stages.append(stage)
    return CallResult(text=reply_path.read_text(encoding="utf-8"), tokens=0, seconds=0.0)


def declared_model() -> str:
    return os.environ.get(MODEL_ENV) or UNDECLARED_MODEL


def record_provenance(run_json_path: Path, stages_done: list[str]) -> dict:
    """把手動傳輸的四件事寫進 run 狀態，不靠人記得補散文。

    `origin` 用的是審閱介面已經認得的值（它會顯示「手動搬進來的產出」），
    所以介面那邊一行都不用改。
    """
    import json

    state = {}
    if run_json_path.is_file():
        state = json.loads(run_json_path.read_text(encoding="utf-8")) or {}
    state["origin"] = ORIGIN
    models = state.setdefault("models", {})
    providers = state.setdefault("providers", {})
    efforts = state.setdefault("efforts", {})
    usage = state.setdefault("usage", {})
    name = declared_model()
    for stage in stages_done:
        models[stage] = {
            "provider": "transport",
            "model": name,
            "effort": NOT_SUPPORTED_BY_PROVIDER,
        }
        providers[stage] = "transport"
        efforts[stage] = NOT_SUPPORTED_BY_PROVIDER
        usage[stage] = UNMEASURED_USAGE
    state[BASELINE_DISQUALIFIED_KEY] = False
    state["evaluation_baseline_note"] = (
        "手動傳輸的產出不能當受控評測的 baseline 或 candidate："
        "傳輸方式保證不了 model／effort／max tokens 與 API 呼叫一致，也量不到 usage"
        "（docs/experiment/pipeline.md 的候選條件）。"
    )
    run_json_path.parent.mkdir(parents=True, exist_ok=True)
    run_json_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return state
