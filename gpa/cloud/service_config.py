"""Validated endpoints for GPA's user-owned cloud control plane."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from gpa.runtime_config import env_float

_ENVIRONMENTS = frozenset({"development", "staging", "production"})
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class CloudServiceConfigurationError(ValueError):
    """Raised when a configured cloud endpoint weakens the transport boundary."""


@dataclass(frozen=True)
class CloudServiceConfig:
    api_base_url: str = ""
    web_base_url: str = "https://gpa-replay-online.hanshenmesenai.chatgpt.site"
    environment: str = "development"
    connect_timeout_seconds: float = 10.0

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "CloudServiceConfig":
        values = os.environ if environ is None else environ
        environment = str(values.get("GPA_CLOUD_ENV") or "development").strip().casefold()
        if environment not in _ENVIRONMENTS:
            raise CloudServiceConfigurationError(
                "GPA_CLOUD_ENV must be development, staging, or production."
            )
        api_url = _validated_base_url(
            "GPA_CLOUD_API_URL",
            str(values.get("GPA_CLOUD_API_URL") or "").strip(),
            allow_empty=True,
        )
        web_url = _validated_base_url(
            "GPA_CLOUD_WEB_URL",
            str(
                values.get("GPA_CLOUD_WEB_URL")
                or "https://gpa-replay-online.hanshenmesenai.chatgpt.site"
            ).strip(),
        )
        timeout = env_float(
            "GPA_CLOUD_CONNECT_TIMEOUT_SECONDS",
            10.0,
            minimum=1.0,
            maximum=60.0,
            environ=values,
        )
        if environment == "production" and api_url.startswith("http://"):
            raise CloudServiceConfigurationError("Production cloud API must use HTTPS.")
        return cls(
            api_base_url=api_url,
            web_base_url=web_url,
            environment=environment,
            connect_timeout_seconds=timeout,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_base_url)

    def api_url(self, path: str) -> str:
        if not self.enabled:
            raise CloudServiceConfigurationError("GPA cloud API is not configured.")
        normalized = "/" + str(path or "").lstrip("/")
        return f"{self.api_base_url}{normalized}"

    @property
    def agent_gateway_url(self) -> str:
        endpoint = self.api_url("/v1/agent/connect")
        parts = urlsplit(endpoint)
        websocket_scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((websocket_scheme, parts.netloc, parts.path, "", ""))


def _validated_base_url(name: str, value: str, *, allow_empty: bool = False) -> str:
    if not value:
        if allow_empty:
            return ""
        raise CloudServiceConfigurationError(f"{name} is required.")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise CloudServiceConfigurationError(f"{name} must be an absolute HTTP(S) URL.")
    if parts.username or parts.password:
        raise CloudServiceConfigurationError(f"{name} must not contain credentials.")
    if parts.query or parts.fragment:
        raise CloudServiceConfigurationError(f"{name} must not contain a query or fragment.")
    if parts.scheme == "http" and parts.hostname.casefold() not in _LOCAL_HOSTS:
        raise CloudServiceConfigurationError(f"{name} allows HTTP only for loopback development.")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


__all__ = [
    "CloudServiceConfig",
    "CloudServiceConfigurationError",
]
