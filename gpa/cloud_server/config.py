"""Fail-closed configuration for the independently deployed GPA cloud API."""
from __future__ import annotations

import secrets
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CloudServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GPA_CLOUD_SERVER_",
        env_file=None,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = "development"
    service_name: str = "gpa-cloud-api"
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1024, le=65535)
    public_origin: str = "http://127.0.0.1:8080"
    database_url: SecretStr = SecretStr("")
    session_signing_key: SecretStr = SecretStr("")
    identity_issuer: str = ""
    identity_audience: str = ""
    identity_jwks_url: str = ""
    object_storage_endpoint: str = ""
    object_storage_bucket: str = ""
    metrics_token: SecretStr = SecretStr("")
    max_request_bytes: int = Field(default=1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024)
    rate_limit_per_minute: int = Field(default=300, ge=10, le=100_000)
    auth_rate_limit_per_minute: int = Field(default=30, ge=5, le=10_000)
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "CloudServerSettings":
        origin = urlsplit(self.public_origin)
        if origin.scheme not in {"http", "https"} or not origin.hostname:
            raise ValueError("public origin must be an absolute HTTP(S) URL")
        if origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError("public origin must not contain credentials, query, or fragment")
        if self.environment in {"staging", "production"}:
            if origin.scheme != "https":
                raise ValueError("staging and production public origins must use HTTPS")
            if not self.database_url.get_secret_value().strip():
                raise ValueError("staging and production require PostgreSQL")
            if len(self.session_signing_key.get_secret_value()) < 32:
                raise ValueError("staging and production require a 32+ character signing key")
            if self.environment == "production" and len(self.metrics_token.get_secret_value()) < 24:
                raise ValueError("production requires a 24+ character metrics token")
            for label, value in (
                ("identity issuer", self.identity_issuer),
                ("identity JWKS URL", self.identity_jwks_url),
                ("object storage endpoint", self.object_storage_endpoint),
            ):
                if value and urlsplit(value).scheme != "https":
                    raise ValueError(f"staging and production {label} must use HTTPS")
        return self

    @property
    def docs_enabled(self) -> bool:
        return self.environment == "development"

    def request_id(self) -> str:
        return f"req_{secrets.token_urlsafe(18)}"

    @property
    def public_release_ready(self) -> bool:
        return bool(
            self.environment == "production"
            and self.identity_issuer.strip()
            and self.identity_audience.strip()
            and self.identity_jwks_url.strip()
            and self.object_storage_endpoint.strip()
            and self.object_storage_bucket.strip()
            and len(self.metrics_token.get_secret_value()) >= 24
        )


__all__ = ["CloudServerSettings"]
