"""Which AI provider the researcher chose, and (later in this change) what the Workbench
can say about it.

The selection is an installation preference, not a research artifact: one small file in
the data root, one entry per actor, holding only the provider name. A fresh installation
has no selection and nothing here, or anywhere else, picks a provider on the researcher's
behalf (provider-setup, provider-selection).

This module does not import the server: the server hands in what it owns (the atomic file
writer, the failure classifier, the command resolver).
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

# The three providers, as stored. `codex` and `claude` are CLI executors; `openai` is the API.
PROVIDERS = ("codex", "claude", "openai")

SETTINGS_DIRNAME = "settings"
SELECTION_FILENAME = "provider_selection.json"
SELECTION_VERSION = 1

INVALID_PROVIDER_MESSAGE = "請選擇 Codex、Claude Code 或 OpenAI API。"
UNKNOWN_PROVIDER_MESSAGE = "請指定要查詢的 AI 執行方式（codex、claude 或 openai）。"
SELECTION_WRITE_FAILED_MESSAGE = "無法儲存 AI 執行方式的選擇，請確認資料資料夾可以寫入。"


class ProviderSelectionError(ValueError):
    """The requested provider is not one of the three."""


def selection_path(data_root: Path) -> Path:
    return Path(data_root) / SETTINGS_DIRNAME / SELECTION_FILENAME


def _read_selections(path: Path) -> dict[str, dict[str, str]]:
    """Every well-formed entry in the file; anything unreadable or malformed reads as empty."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    selections = document.get("selections") if isinstance(document, dict) else None
    if not isinstance(selections, dict):
        return {}
    kept: dict[str, dict[str, str]] = {}
    for actor_key, entry in selections.items():
        provider = entry.get("provider") if isinstance(entry, dict) else None
        if isinstance(actor_key, str) and provider in PROVIDERS:
            kept[actor_key] = {"provider": provider}
    return kept


def load_selection(data_root: Path, actor_key: str) -> str | None:
    """The actor's selected provider, or None when there is no (valid) selection."""
    entry = _read_selections(selection_path(data_root)).get(actor_key)
    return entry["provider"] if entry else None


def save_selection(data_root: Path, actor_key: str, provider: Any,
                   write_json: Callable[[Path, dict], None]) -> str:
    """Store `provider` for the actor and return it.

    Raises ProviderSelectionError for a value that is not one of the three providers, and
    lets the writer's OSError through; either way the stored selection is unchanged, because
    `write_json` replaces the file atomically or not at all. Only the provider is stored: no
    key, model or effort ever reaches this file."""
    if provider not in PROVIDERS:
        raise ProviderSelectionError(INVALID_PROVIDER_MESSAGE)
    path = selection_path(data_root)
    selections = _read_selections(path)
    selections[actor_key] = {"provider": provider}
    write_json(path, {"version": SELECTION_VERSION, "selections": selections})
    return provider


def status_for(provider: Any, *, probe: Callable[[str], Mapping[str, Any]],
               recent_test: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What can be observed about `provider`, as separate facts and never as a verdict.

    Codex and Claude Code: installed, authenticated and configured, taken from `probe`
    (the server's own local checks; credential contents are never read), plus the recent
    connection check. OpenAI API: only the recent connection check. The server holds no API
    key between requests, so whether one is pasted is the page's own state and `probe` is
    not asked about it.

    A missing or unknown provider raises ProviderSelectionError; there is no fallback to
    Codex. The result carries no combined "ready" or "usable" field."""
    if provider not in PROVIDERS:
        raise ProviderSelectionError(UNKNOWN_PROVIDER_MESSAGE)
    status: dict[str, Any] = {"provider": provider}
    if provider != "openai":
        layers = probe(provider)
        for layer in ("installed", "authenticated", "configured"):
            status[layer] = bool(layers.get(layer))
        if isinstance(layers.get("codex_settings"), Mapping):
            status["codex_settings"] = dict(layers["codex_settings"])
    status["recent_test"] = dict(recent_test) if recent_test else None
    return status


# --- connection check ---------------------------------------------------------------------

PROVIDER_LABELS = {"codex": "Codex", "claude": "Claude Code", "openai": "OpenAI API"}

# One minimal completion. The reply is only checked for being non-empty; its content is never
# judged, and nothing from any research project is ever sent.
CHECK_REQUEST = {"kind": "completion", "system": "連線測試", "user": "請只回覆 OK"}
CLI_CHECK_TIMEOUT_SECONDS = 60.0
CHECK_BUSY_MESSAGE = "另一個連線測試還在進行中。"

_LAYER_ORDER = ("installed", "authenticated", "configured")


class CheckBusy(RuntimeError):
    """A connection check is already running."""


_check_lock = threading.Lock()


@contextmanager
def single_check() -> Iterator[None]:
    """At most one connection check at a time; a second one is refused, not queued."""
    if not _check_lock.acquire(blocking=False):
        raise CheckBusy(CHECK_BUSY_MESSAGE)
    try:
        yield
    finally:
        _check_lock.release()


def _generic_failure() -> dict[str, Any]:
    return {
        "cause": "check_failed",
        "what_happened": "連線測試沒有成功。",
        "why_it_matters": "這只代表剛剛這一次請求沒有成功，沒有改動任何研究資料。",
        "next_action": "可以稍後再測一次；若持續失敗，請查看執行器那端的狀態。",
    }


def _layer_failure(provider: str, layer: str) -> dict[str, Any]:
    """Why a check was not even attempted: a local fact the Workbench already knows."""
    label = PROVIDER_LABELS[provider]
    what, next_action = {
        "installed": (f"這台電腦找不到 {label}。", f"請依 README 安裝並登入 {label}，或改用其他 AI 執行方式。"),
        "authenticated": (f"{label} 尚未登入。", f"請先完成 {label} 的登入，或改用其他 AI 執行方式。"),
        "configured": (f"這個工作台還沒設定用 {label} 執行。", "請改用 Codex 或 OpenAI API；設定方式見 README。"),
    }[layer]
    return {"cause": f"layer_{layer}", "layer": layer, "what_happened": what,
            "why_it_matters": "還沒有送出任何測試請求，也沒有使用任何額度。", "next_action": next_action}


def _result(provider: str, state: str, cause: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"provider": provider, "state": state,
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "cause": dict(cause) if cause else None}


def run_cli_check(provider: str, *, environ: Mapping[str, str],
                  probe: Callable[[str], Mapping[str, Any]],
                  invoke: Callable[..., Mapping[str, Any]],
                  classify: Callable[[str], Mapping[str, Any] | None]) -> tuple[dict[str, Any], bool]:
    """One minimal request to Codex or Claude Code through the shipped wrapper.

    Returns (result, attempted). When a local layer is not satisfied nothing is called and
    `attempted` is False: that result names the layer and is not a statement about the
    provider. Otherwise exactly one request is sent -- never retried -- with the
    researcher's own model and effort (they travel in `environ`), and any failure is
    classified by `classify` (the existing CLI failure classification)."""
    layers = probe(provider)
    for layer in _LAYER_ORDER:
        if not layers.get(layer):
            return _result(provider, "failed", _layer_failure(provider, layer)), False
    try:
        response = invoke(CHECK_REQUEST, timeout=CLI_CHECK_TIMEOUT_SECONDS, executor=provider, environ=environ)
        reply = response.get("content", response.get("text")) if isinstance(response, Mapping) else None
        failure = None if isinstance(reply, str) and reply.strip() else "CLI runner 回報這次工作失敗（runner_empty_reply）。"
    except Exception as exc:  # noqa: BLE001 - any failure of the request is a failed check
        failure = str(exc)
    if failure is None:
        return _result(provider, "ok"), True
    return _result(provider, "failed", classify(failure) or _generic_failure()), True


# --- OpenAI API: only the key is queried ------------------------------------------------------

OPENAI_CHECK_TIMEOUT_SECONDS = 10.0


def _openai_failure(status: int | None) -> dict[str, Any]:
    """The cause for an HTTP status (None: no answer at all). Written from the status code
    alone: the response body is never read, because a provider's error text can echo part of
    the key."""
    if status in (401, 403):
        return {"cause": "api_key_rejected", "what_happened": "OpenAI 不接受這把 key，或這把 key 沒有權限。",
                "why_it_matters": "這只代表剛剛這一次查詢沒有通過；沒有改動任何研究資料。",
                "next_action": "請確認貼上的是完整的 key，而且這把 key 有使用 API 的權限，然後再測一次。"}
    if status == 429:
        return {"cause": "rate_limited", "what_happened": "OpenAI 暫時擋下了這次查詢（次數或額度上限）。",
                "why_it_matters": "這只代表剛剛這一次查詢沒有通過；沒有改動任何研究資料。",
                "next_action": "等幾分鐘再測一次。"}
    if status is None:
        return {"cause": "unreachable", "what_happened": "連不上 OpenAI。",
                "why_it_matters": "這只代表剛剛這一次查詢沒有通過；沒有改動任何研究資料。",
                "next_action": "確認這台機器連得上網路，然後再測一次。"}
    return {"cause": "api_check_failed", "what_happened": f"OpenAI 回報這次查詢沒有成功（HTTP {status}）。",
            "why_it_matters": "這只代表剛剛這一次查詢沒有通過；沒有改動任何研究資料。",
            "next_action": "可以稍後再測一次；若持續失敗，請查看 OpenAI 的服務狀態。"}


def run_openai_check(api_key: str, *, base_url: str,
                     opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    """One `GET <base_url>/models` with the pasted key: no model output, no tokens, no retry.

    It shows only that the key, the network and API access worked just now -- not that any
    particular model can finish a job. The key exists in this call's memory and nowhere else:
    it is not stored, logged or returned, and nothing is written while the call runs."""
    request = urllib.request.Request(f"{base_url.rstrip('/')}/models", method="GET",
                                     headers={"Authorization": f"Bearer {api_key}"})
    status: int | None
    try:
        with opener(request, timeout=OPENAI_CHECK_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        status = None
    if status == 200:
        return _result("openai", "ok")
    return _result("openai", "failed", _openai_failure(status))


# --- the most recent check, kept in memory ---------------------------------------------------

MAX_RECENT_TESTS = 50


def _norm(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


class RecentTests:
    """The latest connection check per (actor, provider, model, effort), in memory only.

    A result is never reported for another combination -- changing the model, the effort or
    the provider reads as "not tested" -- and a restart of the server clears everything. At
    most `limit` results are kept; the oldest is dropped first. Nothing is written to disk,
    and nothing here is consulted when work is admitted: a check is decision support, not a
    gate. OpenAI API has no model or effort, so its results are keyed by actor alone."""

    def __init__(self, limit: int = MAX_RECENT_TESTS):
        self._limit = limit
        self._items: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(actor_key: str, provider: str, model: Any, effort: Any) -> tuple:
        if provider == "openai":
            return (actor_key, provider, None, None)
        return (actor_key, provider, _norm(model), _norm(effort))

    def record(self, actor_key: str, provider: str, model: Any, effort: Any, result: Mapping[str, Any]) -> None:
        entry = {"state": result["state"], "checked_at": result["checked_at"], "cause": result.get("cause")}
        key = self._key(actor_key, provider, model, effort)
        with self._lock:
            self._items[key] = entry
            self._items.move_to_end(key)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)

    def get(self, actor_key: str, provider: str, model: Any = None, effort: Any = None) -> dict[str, Any] | None:
        with self._lock:
            entry = self._items.get(self._key(actor_key, provider, model, effort))
            return dict(entry) if entry else None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


recent_tests = RecentTests()
