"""Shared LLM helpers for workflow construction and replay decisions."""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from gpa.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CLIENT_MAX_RETRIES,
    LLM_MODEL,
    LLM_REQUEST_TIMEOUT_SECONDS,
    LLM_TEXT_FALLBACK_MODEL,
    LLM_TEXT_MODEL,
    LLM_VISION_FALLBACK_MODEL,
    LLM_VISION_MODEL,
)

logger = logging.getLogger(__name__)

_client: Any = None
_provider: "JSONLLMProvider | None" = None
_thread_state = threading.local()


@dataclass
class LLMCallMetric:
    model: str
    modality: str
    image_detail: str
    duration_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "modality": self.modality,
            "image_detail": self.image_detail,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "success": self.success,
            "error": self.error,
        }


def _metrics_buffer() -> list[LLMCallMetric]:
    buffer = getattr(_thread_state, "metrics", None)
    if buffer is None:
        buffer = []
        _thread_state.metrics = buffer
    return buffer


def clear_llm_metrics() -> None:
    _thread_state.metrics = []


def consume_llm_metrics() -> list[dict[str, Any]]:
    metrics = [item.to_dict() for item in _metrics_buffer()]
    _thread_state.metrics = []
    return metrics


def _set_provider_usage(usage: dict[str, int] | None) -> None:
    _thread_state.provider_usage = dict(usage or {})


def _take_provider_usage() -> dict[str, int]:
    usage = dict(getattr(_thread_state, "provider_usage", {}) or {})
    _thread_state.provider_usage = {}
    return usage


def _attribute_int(value: Any, name: str) -> int:
    try:
        return int(getattr(value, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _response_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": _attribute_int(usage, "prompt_tokens"),
        "completion_tokens": _attribute_int(usage, "completion_tokens"),
        "total_tokens": _attribute_int(usage, "total_tokens"),
        "cached_tokens": _attribute_int(prompt_details, "cached_tokens"),
        "reasoning_tokens": _attribute_int(completion_details, "reasoning_tokens"),
    }


class JSONLLMProvider(Protocol):
    """Provider contract for one structured model request.

    Retries remain in :func:`call_json_llm` so every provider gets identical
    failure handling and callers retain the original public API.
    """

    def call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image: Any = None,
        image_detail: str = "high",
        temperature: float = 0.2,
    ) -> dict[str, Any]: ...


@dataclass
class OpenAIChatCompletionsProvider:
    """OpenAI-compatible Chat Completions implementation."""

    model: str
    client_factory: Callable[[], Any]

    def call_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image: Any = None,
        image_detail: str = "high",
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        if image is None:
            user_content: Any = user_prompt
        else:
            user_content = [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _image_data_url(image),
                        "detail": image_detail,
                    },
                },
            ]
        request_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        if _model_accepts_temperature(self.model):
            request_kwargs["temperature"] = temperature
        response = self.client_factory().chat.completions.create(**request_kwargs)
        _set_provider_usage(_response_usage(response))
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("LLM response must be a JSON object.")
        return data


def set_llm_provider(provider: JSONLLMProvider | None) -> None:
    """Install a process-local provider, or restore configured defaults."""
    global _provider
    _provider = provider


def _configured_model(*, image: Any = None, fallback: bool = False) -> str:
    """Choose the configured text or vision model with legacy fallback."""
    if fallback:
        override = (
            LLM_VISION_FALLBACK_MODEL if image is not None
            else LLM_TEXT_FALLBACK_MODEL
        )
        if override:
            return str(override)
    override = LLM_VISION_MODEL if image is not None else LLM_TEXT_MODEL
    return str(override or LLM_MODEL)


def _current_provider(*, image: Any = None, fallback: bool = False) -> JSONLLMProvider:
    if _provider is not None:
        return _provider
    # Build the default lazily so tests and runtime configuration can replace
    # the model or client factory without stale cached state.
    return OpenAIChatCompletionsProvider(
        model=_configured_model(image=image, fallback=fallback),
        client_factory=_get_client,
    )


def require_llm_config() -> None:
    if not LLM_API_KEY:
        raise RuntimeError("GPA_LLM_API_KEY is required for LLM-assisted build and replay.")


def _get_client() -> Any:
    global _client
    require_llm_config()
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            timeout=LLM_REQUEST_TIMEOUT_SECONDS,
            max_retries=LLM_CLIENT_MAX_RETRIES,
        )
    return _client


def call_json_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    image: Any = None,
    image_detail: str = "high",
    temperature: float = 0.2,
    attempts: int = 3,
    retry_sleep: float = 1.0,
) -> dict:
    """Call the configured LLM and parse a JSON object response."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        provider = _current_provider(
            image=image,
            fallback=bool(attempt > 0 and _provider is None),
        )
        model = str(getattr(provider, "model", "") or _configured_model(image=image))
        _set_provider_usage({})
        started = time.perf_counter()
        try:
            data = provider.call_json(
                system_prompt,
                user_prompt,
                image=image,
                image_detail=image_detail,
                temperature=temperature,
            )
            usage = _take_provider_usage()
            _metrics_buffer().append(LLMCallMetric(
                model=model,
                modality="vision" if image is not None else "text",
                image_detail=image_detail if image is not None else "none",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                cached_tokens=int(usage.get("cached_tokens", 0)),
                reasoning_tokens=int(usage.get("reasoning_tokens", 0)),
            ))
            return data
        except Exception as exc:
            usage = _take_provider_usage()
            _metrics_buffer().append(LLMCallMetric(
                model=model,
                modality="vision" if image is not None else "text",
                image_detail=image_detail if image is not None else "none",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
                cached_tokens=int(usage.get("cached_tokens", 0)),
                reasoning_tokens=int(usage.get("reasoning_tokens", 0)),
                success=False,
                error=str(exc)[:300],
            ))
            last_error = exc
            logger.warning("LLM JSON call attempt %s failed: %s", attempt + 1, exc)
            if attempt < attempts - 1:
                time.sleep(retry_sleep)
    raise RuntimeError(f"LLM JSON call failed: {last_error}") from last_error


def _image_data_url(image: Any) -> str:
    """Encode a PIL-like screenshot as an inline PNG data URL for vision models."""
    if isinstance(image, str) and image.startswith("data:image/"):
        return image
    buffer = io.BytesIO()
    if hasattr(image, "save"):
        image.save(buffer, format="PNG")
    elif isinstance(image, bytes):
        buffer.write(image)
    else:
        raise TypeError("image must be a PIL Image, bytes, or data URL string.")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _model_accepts_temperature(model: str) -> bool:
    """Some newer reasoning/frontier models only accept their default temperature."""
    name = str(model or "").casefold()
    return not name.startswith(("gpt-5", "o1", "o3", "o4"))


__all__ = [
    "JSONLLMProvider",
    "LLMCallMetric",
    "OpenAIChatCompletionsProvider",
    "call_json_llm",
    "clear_llm_metrics",
    "consume_llm_metrics",
    "require_llm_config",
    "set_llm_provider",
]
