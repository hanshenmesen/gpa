import ast
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from gpa.cloud_server.app import create_app
from gpa.cloud_server.auth import IdentityClaims, IdentityTokenError
from gpa.cloud_server.config import CloudServerSettings


class _Database:
    configured = True

    def __init__(self, ready=True):
        self.ready = ready
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def check(self):
        return self.ready, "ready" if self.ready else "unavailable"


class _IdentityVerifier:
    configured = True

    def verify_authorization(self, authorization):
        if authorization != "Bearer valid-test-token":
            raise IdentityTokenError("invalid")
        return IdentityClaims(
            subject="identity-user-1",
            email="user@example.test",
            display_name="Test User",
            email_verified=True,
        )


class CloudServerTests(unittest.TestCase):
    def settings(self, **updates):
        values = {
            "environment": "development",
            "public_origin": "http://127.0.0.1:8080",
        }
        values.update(updates)
        return CloudServerSettings(**values)

    def test_liveness_and_capabilities_disclose_no_secrets(self):
        app = create_app(self.settings(), _Database())
        with TestClient(app) as client:
            live = client.get("/health/live")
            capabilities = client.get("/v1/meta/capabilities")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["service"], "gpa-cloud-api")
        self.assertEqual(capabilities.json()["desktop_authority"], "local_only")
        self.assertFalse(capabilities.json()["arbitrary_shell"])
        self.assertEqual(live.headers["x-content-type-options"], "nosniff")
        self.assertEqual(live.headers["cache-control"], "no-store")
        self.assertNotIn("database_url", live.text)

    def test_readiness_fails_closed_when_database_is_down(self):
        app = create_app(self.settings(), _Database(ready=False))
        with TestClient(app) as client:
            response = client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready", "database": "unavailable"})

    def test_identity_endpoint_requires_verified_bearer_token(self):
        app = create_app(self.settings(), _Database(), _IdentityVerifier())
        with TestClient(app) as client:
            rejected = client.get("/v1/auth/identity")
            accepted = client.get(
                "/v1/auth/identity",
                headers={"Authorization": "Bearer valid-test-token"},
            )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.headers["www-authenticate"], "Bearer")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["subject"], "identity-user-1")
        self.assertTrue(accepted.json()["email_verified"])

    def test_identity_endpoint_is_unavailable_until_provider_is_configured(self):
        app = create_app(self.settings(), _Database())
        with TestClient(app) as client:
            response = client.get("/v1/auth/identity")
        self.assertEqual(response.status_code, 503)

    def test_production_requires_tls_database_and_signing_key(self):
        with self.assertRaises(ValidationError):
            CloudServerSettings(
                environment="production",
                public_origin="http://gpa.example",
            )
        settings = CloudServerSettings(
            environment="production",
            public_origin="https://gpa.example",
            database_url="postgresql://gpa@db/gpa",
            session_signing_key="x" * 32,
        )
        self.assertFalse(settings.docs_enabled)

    def test_cloud_package_cannot_import_desktop_execution_modules(self):
        root = Path(__file__).resolve().parents[1] / "gpa" / "cloud_server"
        forbidden = (
            "demo_web",
            "gpa.desktop",
            "gpa.execution",
            "gpa.recording",
            "gpa.replay.service",
        )
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            with self.subTest(path=path.name):
                self.assertFalse(
                    any(name == prefix or name.startswith(prefix + ".") for name in imports for prefix in forbidden),
                    imports,
                )


if __name__ == "__main__":
    unittest.main()
