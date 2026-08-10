"""Shared LLM helpers for workflow construction and replay decisions."""
from __future__ import annotations

import base64
import io
import json
import logging
import time
from typing import Any

from gpa.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

_client: Any = None


def require_llm_config() -> None:
    if not LLM_API_KEY:
        raise RuntimeError("GPA_LLM_API_KEY is required for LLM-assisted build and replay.")


def _get_client() -> Any:
    global _client
    require_llm_config()
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
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
        try:
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
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {"type": "json_object"},
            }
            if _model_accepts_temperature(LLM_MODEL):
                request_kwargs["temperature"] = temperature
            response = _get_client().chat.completions.create(**request_kwargs)
            content = response.choices[0].message.content or "{}"
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("LLM response must be a JSON object.")
            return data
        except Exception as exc:
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
