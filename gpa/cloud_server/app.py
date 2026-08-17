"""Versioned public API surface for GPA Cloud."""
from __future__ import annotations

import asyncio
import re
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        supplied = str(request.headers.get("x-request-id") or "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else config.request_id()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
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
