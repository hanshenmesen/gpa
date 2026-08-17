"""Small PostgreSQL lifecycle adapter for cloud readiness and migrations."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID


class CloudDatabase:
    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        self.pool: Any | None = None

    @property
    def configured(self) -> bool:
        return bool(self.database_url)

    def open(self) -> None:
        if not self.configured or self.pool is not None:
            return
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(
            conninfo=self.database_url,
            min_size=1,
            max_size=8,
            open=True,
            kwargs={"autocommit": True},
        )

    def close(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.close()

    def check(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "unconfigured"
        try:
            self.open()
            with self.pool.connection(timeout=3) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    row = cursor.fetchone()
            return (row == (1,), "ready" if row == (1,) else "unexpected_result")
        except Exception:
            return False, "unavailable"

    @contextmanager
    def tenant_connection(
        self,
        tenant_id: str | UUID,
        user_id: str | UUID,
    ) -> Iterator[Any]:
        """Yield a transaction with PostgreSQL row-level-security context set."""
        if not self.configured:
            raise RuntimeError("PostgreSQL is not configured")
        tenant = str(UUID(str(tenant_id)))
        user = str(UUID(str(user_id)))
        self.open()
        with self.pool.connection(timeout=5) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('gpa.tenant_id', %s, true)", (tenant,))
                    cursor.execute("SELECT set_config('gpa.user_id', %s, true)", (user,))
                yield connection


__all__ = ["CloudDatabase"]
