"""Apply versioned GPA Cloud PostgreSQL migrations exactly once."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Iterable


class MigrationError(RuntimeError):
    """Raised when the migration history is inconsistent."""


def migration_directory() -> Path:
    return Path(__file__).with_name("migrations")


def migration_files(directory: Path | None = None) -> list[Path]:
    root = directory or migration_directory()
    files = sorted(root.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise MigrationError(f"no migrations found in {root}")
    versions = [path.stem for path in files]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version")
    return files


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migrations(database_url: str, files: Iterable[Path] | None = None) -> list[str]:
    """Apply pending migrations while holding a database-wide advisory lock."""
    if not str(database_url or "").strip():
        raise MigrationError("database URL is required")
    selected = list(files or migration_files())
    applied_now: list[str] = []

    import psycopg

    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext('gpa-cloud-migrations'))")
            try:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        checksum TEXT,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cursor.execute(
                    "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT"
                )
                cursor.execute("SELECT version, checksum FROM schema_migrations")
                applied = {str(version): checksum for version, checksum in cursor.fetchall()}

                for path in selected:
                    version = path.stem
                    checksum = migration_checksum(path)
                    previous = applied.get(version)
                    if previous:
                        if previous != checksum:
                            raise MigrationError(
                                f"applied migration {version} no longer matches its checksum"
                            )
                        continue
                    if version in applied and previous is None:
                        cursor.execute(
                            "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                            (checksum, version),
                        )
                        continue

                    cursor.execute(path.read_text(encoding="utf-8"), prepare=False)
                    cursor.execute(
                        """
                        INSERT INTO schema_migrations(version, checksum)
                        VALUES (%s, %s)
                        ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum
                        """,
                        (version, checksum),
                    )
                    applied_now.append(version)
            finally:
                cursor.execute("SELECT pg_advisory_unlock(hashtext('gpa-cloud-migrations'))")
    return applied_now


def main(argv: list[str] | None = None) -> None:
    from gpa.cloud_server.config import CloudServerSettings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="override GPA_CLOUD_SERVER_DATABASE_URL")
    parser.add_argument("--list", action="store_true", help="list packaged migrations")
    args = parser.parse_args(argv)
    files = migration_files()
    if args.list:
        for path in files:
            print(f"{path.stem}  {migration_checksum(path)}")
        return

    database_url = args.database_url
    if not database_url:
        database_url = CloudServerSettings().database_url.get_secret_value()
    applied = apply_migrations(database_url, files)
    print("Database is current." if not applied else f"Applied: {', '.join(applied)}")


if __name__ == "__main__":
    main()
