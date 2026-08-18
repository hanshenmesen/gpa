"""Versioned public API surface for GPA Cloud."""
from __future__ import annotations

import asyncio
import hmac
import re
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from gpa import __version__
from gpa.cloud.agent_protocol import AGENT_PROTOCOL_VERSION
from gpa.cloud_server.auth import (
    IdentityTokenError,
    IdentityVerifier,
    OIDCIdentityVerifier,
)
from gpa.cloud_server.config import CloudServerSettings
from gpa.cloud_server.database import CloudDatabase
from gpa.cloud_server.operations import (
    OperationalTelemetry,
    SlidingWindowLimiter,
    client_fingerprint,
    structured_access_log,
)

_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{8,96}$")


def create_app(
    settings: CloudServerSettings | None = None,
    database: CloudDatabase | None = None,
    identity_verifier: IdentityVerifier | None = None,
) -> FastAPI:
    config = settings or CloudServerSettings()
    db = database or CloudDatabase(config.database_url.get_secret_value())
    verifier = identity_verifier or OIDCIdentityVerifier(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if db.configured:
            await asyncio.to_thread(db.open)
        try:
            yield
        finally:
            await asyncio.to_thread(db.close)

    app = FastAPI(
        title="GPA Cloud API",
        version=__version__,
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.database = db
    app.state.identity_verifier = verifier
    limiter = SlidingWindowLimiter()
    telemetry = OperationalTelemetry()
    app.state.telemetry = telemetry

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        started = time.monotonic()
        supplied = str(request.headers.get("x-request-id") or "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else config.request_id()
        request.state.request_id = request_id
        client_address = request.client.host if request.client else "unknown"
        fingerprint = client_fingerprint(
            client_address,
            salt=config.session_signing_key.get_secret_value() or config.service_name,
        )
        content_length = str(request.headers.get("content-length") or "").strip()
        payload_rejected = False
        rate_limited = False
        if content_length:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = -1
            if declared_bytes < 0 or declared_bytes > config.max_request_bytes:
                payload_rejected = True
                response = JSONResponse(
                    {"detail": "request body is too large"},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
            else:
                response = None
        else:
            response = None
        if response is None and request.url.path not in {"/health/live", "/health/ready"}:
            bucket = "auth" if request.url.path.startswith("/v1/auth/") else "api"
            limit = (
                config.auth_rate_limit_per_minute
                if bucket == "auth"
                else config.rate_limit_per_minute
            )
            decision = limiter.check(fingerprint, bucket, limit=limit)
            if not decision.allowed:
                rate_limited = True
                response = JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                response = JSONResponse(
                    {"detail": "internal server error", "request_id": request_id},
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        if config.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        latency_ms = (time.monotonic() - started) * 1000
        telemetry.observe(
            status_code=response.status_code,
            latency_ms=latency_ms,
            rate_limited=rate_limited,
            payload_rejected=payload_rejected,
        )
        structured_access_log(
            request_id=request_id,
            method=request.method,
            path=request.url.path[:300],
            status=response.status_code,
            latency_ms=round(latency_ms, 3),
            client=fingerprint,
        )
        return response

    @app.get("/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        return {
            "status": "ok",
            "service": config.service_name,
            "version": __version__,
        }

    @app.get("/health/ready", tags=["health"])
    async def health_ready() -> Response:
        ready, database_status = await asyncio.to_thread(db.check)
        payload = {
            "status": "ready" if ready else "not_ready",
            "database": database_status,
        }
        return JSONResponse(
            payload,
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/internal/metrics", include_in_schema=False)
    async def internal_metrics(request: Request) -> Response:
        expected = config.metrics_token.get_secret_value()
        supplied = str(request.headers.get("x-gpa-metrics-token") or "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return Response(status_code=status.HTTP_404_NOT_FOUND)
        return Response(
            telemetry.prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/meta/capabilities", tags=["meta"])
    async def capabilities() -> dict[str, object]:
        return {
            "api_version": "v1",
            "agent_protocol_version": AGENT_PROTOCOL_VERSION,
            "desktop_authority": "local_only",
            "cloud_commands": "allowlisted",
            "arbitrary_shell": False,
            "identity_provider_configured": verifier.configured,
        }

    @app.get("/v1/auth/identity", tags=["auth"])
    async def authenticated_identity(request: Request) -> Response:
        if not verifier.configured:
            return JSONResponse(
                {"detail": "identity provider is not configured"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            claims = await asyncio.to_thread(
                verifier.verify_authorization,
                request.headers.get("authorization", ""),
            )
        except IdentityTokenError:
            return JSONResponse(
                {"detail": "authentication required"},
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return JSONResponse(
            {
                "subject": claims.subject,
                "email": claims.email,
                "display_name": claims.display_name,
                "email_verified": claims.email_verified,
            }
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = CloudServerSettings()
    uvicorn.run(
        "gpa.cloud_server.app:app",
        host=settings.bind_host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
