import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gpa.cloud_server.database import CloudDatabase
from gpa.cloud_server.migrations import migration_checksum, migration_files
from gpa.cloud_server.pairing import (
    PairingError,
    create_pairing_challenge,
    hash_pairing_secret,
    normalize_user_code,
    pairing_secret_matches,
)
from gpa.cloud_server.preflight import cloud_findings, desktop_findings


class _Cursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params=None):
        self.calls.append((query, params))


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def transaction(self):
        return _Transaction()

    def cursor(self):
        return self.cursor_instance


class _Pool:
    def __init__(self):
        self.connection_instance = _Connection()

    def connection(self, timeout=0):
        self.timeout = timeout
        return self.connection_instance


class CloudOperationsTests(unittest.TestCase):
    def test_packaged_migrations_are_ordered_and_checksummed(self):
        files = migration_files()
        self.assertEqual(
            [path.name for path in files],
            ["0001_initial.sql", "0002_security_and_operations.sql"],
        )
        self.assertTrue(all(len(migration_checksum(path)) == 64 for path in files))
        security_sql = files[1].read_text(encoding="utf-8")
        self.assertIn("FORCE ROW LEVEL SECURITY", security_sql)
        self.assertIn("device_pairing_requests", security_sql)
        self.assertIn("outbox_events", security_sql)

    def test_migration_discovery_fails_closed_when_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "no migrations"):
                migration_files(Path(directory))

    def test_tenant_connection_sets_local_rls_context(self):
        database = CloudDatabase("postgresql://example")
        database.pool = _Pool()
        tenant_id, user_id = uuid4(), uuid4()
        with database.tenant_connection(tenant_id, user_id) as connection:
            self.assertIs(connection, database.pool.connection_instance)
        calls = database.pool.connection_instance.cursor_instance.calls
        self.assertEqual(calls[0][1], (str(tenant_id),))
        self.assertEqual(calls[1][1], (str(user_id),))

    def test_tenant_connection_rejects_non_uuid_scope(self):
        database = CloudDatabase("postgresql://example")
        database.pool = _Pool()
        with self.assertRaises(ValueError):
            with database.tenant_connection("not-a-tenant", uuid4()):
                pass

    def test_release_preflight_reports_external_account_blockers(self):
        desktop = desktop_findings(
            environ={},
            which=lambda _name: "/usr/bin/tool",
            module_available=lambda _name: True,
            system="Darwin",
        )
        blocked_names = {item.name for item in desktop if item.blocking}
        self.assertEqual(
            blocked_names,
            {"GPA_MACOS_SIGNING_IDENTITY", "GPA_MACOS_NOTARY_PROFILE"},
        )

        cloud = cloud_findings(environ={}, which=lambda _name: "/usr/bin/tool")
        self.assertTrue(any(item.name == "GPA_CLOUD_SERVER_DATABASE_URL" for item in cloud))
        self.assertTrue(all(item.blocking for item in cloud if item.name.startswith("GPA_")))

    def test_release_preflight_accepts_complete_cloud_configuration(self):
        env = {
            "GPA_CLOUD_SERVER_PUBLIC_ORIGIN": "https://api.gpa.example",
            "GPA_CLOUD_SERVER_DATABASE_URL": "postgresql://gpa@example/gpa",
            "GPA_CLOUD_SERVER_SESSION_SIGNING_KEY": "x" * 32,
            "GPA_CLOUD_SERVER_IDENTITY_ISSUER": "https://identity.example/",
            "GPA_CLOUD_SERVER_IDENTITY_AUDIENCE": "gpa-cloud-api",
            "GPA_CLOUD_SERVER_IDENTITY_JWKS_URL": "https://identity.example/jwks.json",
            "GPA_CLOUD_SERVER_OBJECT_STORAGE_ENDPOINT": "https://objects.example",
            "GPA_CLOUD_SERVER_OBJECT_STORAGE_BUCKET": "gpa",
        }
        findings = cloud_findings(environ=env, which=lambda _name: "/usr/bin/tool")
        self.assertFalse(any(item.blocking for item in findings))

    def test_macos_packaging_sources_exist(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "packaging/macos/GPA.spec").is_file())
        self.assertTrue((root / "packaging/macos/GPA.entitlements").is_file())
        self.assertTrue((root / "scripts/build_macos_app.sh").is_file())

    def test_pairing_challenge_is_short_lived_and_secrets_are_only_hashed(self):
        key = "k" * 32
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        challenge = create_pairing_challenge(key, now=now)
        self.assertTrue(challenge.device_code.startswith("gpa_pair_"))
        self.assertEqual(len(normalize_user_code(challenge.user_code)), 8)
        self.assertEqual((challenge.expires_at - now).total_seconds(), 600)
        device_hash = hash_pairing_secret(challenge.device_code, key, purpose="device")
        user_hash = hash_pairing_secret(challenge.user_code, key, purpose="user")
        self.assertEqual(len(device_hash), 32)
        self.assertNotEqual(device_hash, user_hash)
        self.assertTrue(
            pairing_secret_matches(challenge.user_code.lower(), user_hash, key, purpose="user")
        )
        self.assertFalse(pairing_secret_matches("AAAA-AAAA", user_hash, key, purpose="user"))

    def test_pairing_rejects_weak_keys_and_long_lifetimes(self):
        with self.assertRaises(PairingError):
            create_pairing_challenge("weak")
        with self.assertRaises(PairingError):
            create_pairing_challenge("k" * 32, ttl_seconds=3600)


if __name__ == "__main__":
    unittest.main()
