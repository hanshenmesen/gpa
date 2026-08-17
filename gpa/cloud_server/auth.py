"""Strict OIDC bearer-token verification for GPA Cloud."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gpa.cloud_server.config import CloudServerSettings


class IdentityTokenError(RuntimeError):
    """Raised for missing, expired or unverifiable identity tokens."""


@dataclass(frozen=True)
class IdentityClaims:
    subject: str
    email: str | None = None
    display_name: str | None = None
    email_verified: bool = False


class IdentityVerifier(Protocol):
    @property
    def configured(self) -> bool: ...

    def verify_authorization(self, authorization: str) -> IdentityClaims: ...


class OIDCIdentityVerifier:
    """Verify asymmetric OIDC access/ID tokens against a pinned JWKS endpoint."""

    def __init__(self, settings: CloudServerSettings) -> None:
        self.issuer = settings.identity_issuer.strip().rstrip("/")
        self.audience = settings.identity_audience.strip()
        self.jwks_url = settings.identity_jwks_url.strip()
        self._jwks_client = None

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.audience and self.jwks_url)

    def verify_authorization(self, authorization: str) -> IdentityClaims:
        scheme, separator, token = str(authorization or "").partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.strip():
            raise IdentityTokenError("Bearer authentication is required")
        if not self.configured:
            raise IdentityTokenError("identity provider is not configured")
        try:
            import jwt

            if self._jwks_client is None:
                self._jwks_client = jwt.PyJWKClient(
                    self.jwks_url,
                    cache_jwk_set=True,
                    lifespan=300,
                    timeout=5,
                )
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except Exception as exc:
            raise IdentityTokenError("identity token is invalid or expired") from exc

        subject = str(payload.get("sub") or "").strip()
        if not subject:
            raise IdentityTokenError("identity token has no subject")
        email = str(payload.get("email") or "").strip() or None
        display_name = str(payload.get("name") or "").strip() or None
        return IdentityClaims(
            subject=subject,
            email=email,
            display_name=display_name,
            email_verified=payload.get("email_verified") is True,
        )


__all__ = [
    "IdentityClaims",
    "IdentityTokenError",
    "IdentityVerifier",
    "OIDCIdentityVerifier",
]
