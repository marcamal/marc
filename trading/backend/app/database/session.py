"""Async database engine, sessions and schema creation.

SQLite is the default because it needs no server and no setup — exactly right
for a local-first MVP on a Windows PC. Everything here is written against the
async SQLAlchemy API, so switching `ATLAS_DATABASE_URL` to PostgreSQL is a
config change rather than a rewrite.

The SQLite PRAGMAs below matter more than they look:

*   `journal_mode=WAL` lets the API read while an agent writes. Without it,
    SQLite's default locking makes concurrent access throw
    "database is locked" under exactly the load ATLAS generates.
*   `synchronous=NORMAL` is the right trade for a journal: a crash could lose
    the last fraction of a second of writes, which is acceptable for records
    we can re-fetch from the broker, in exchange for far fewer fsyncs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import PROJECT_ROOT
from app.core.logging import get_logger
from app.database.models import RETENTION_DAYS, Base

log = get_logger(__name__)


class Database:
    """Owns the engine and the session factory."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = self._normalise_url(url)
        self._engine: AsyncEngine = create_async_engine(
            self.url,
            echo=echo,
            future=True,
            # SQLite does not benefit from connection pooling in-process, and
            # the default pool interacts badly with aiosqlite's threading.
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        if self._is_sqlite:
            self._install_sqlite_pragmas()

    @property
    def _is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @staticmethod
    def _normalise_url(url: str) -> str:
        """Resolve a relative SQLite path against the project root.

        Without this, the database file lands wherever the process happened to
        be started from — so running uvicorn from `backend/` and from the
        project root would silently use two different databases.
        """
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return url

        raw_path = url[len(prefix) :]
        if raw_path.startswith("/") or (len(raw_path) > 1 and raw_path[1] == ":"):
            return url  # already absolute

        resolved = (PROJECT_ROOT / raw_path.lstrip("./")).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return f"{prefix}{resolved.as_posix()}"

    def _install_sqlite_pragmas(self) -> None:
        @event.listens_for(self._engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                # Wait rather than fail when another connection holds a lock.
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def create_schema(self) -> None:
        """Create any missing tables.

        Fine for the MVP. Once the schema starts changing under real data,
        Alembic takes over — the migration environment is already configured.
        """
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        log.info(
            "database ready",
            extra={"url": self._safe_url(), "tables": len(Base.metadata.tables)},
        )

    async def dispose(self) -> None:
        await self._engine.dispose()

    def _safe_url(self) -> str:
        """URL with any password removed, for logging."""
        if "@" in self.url and "://" in self.url:
            scheme, rest = self.url.split("://", 1)
            if "@" in rest:
                return f"{scheme}://***@{rest.split('@', 1)[1]}"
        return self.url

    # ------------------------------------------------------------------ #
    # sessions
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A transactional session. Commits on success, rolls back on error."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    # ------------------------------------------------------------------ #
    # maintenance
    # ------------------------------------------------------------------ #

    async def purge_old_rows(self, policy: dict[str, int] | None = None) -> dict[str, int]:
        """Apply the retention policy.

        Keeps the MVP's SQLite file from growing without bound. Orders, risk
        decisions, proposals and journal entries are never purged: they are
        the record of what ATLAS did.
        """
        retention = policy or RETENTION_DAYS
        deleted: dict[str, int] = {}

        async with self.session() as session:
            for table_name, days in retention.items():
                table = Base.metadata.tables.get(table_name)
                if table is None:
                    continue
                # Explicit None checks, not `a or b`: a SQLAlchemy Column
                # raises TypeError on truth testing ("Boolean value of this
                # clause is not defined"), so `or` would blow up here.
                cutoff_column = table.c.get("created_at")
                if cutoff_column is None:
                    cutoff_column = table.c.get("timestamp")
                if cutoff_column is None:
                    continue

                cutoff = datetime.now(UTC) - timedelta(days=days)
                result = await session.execute(delete(table).where(cutoff_column < cutoff))
                count = result.rowcount or 0
                if count:
                    deleted[table_name] = count

        if deleted:
            log.info("retention purge complete", extra={"deleted": deleted})
        return deleted

    async def health_check(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.error("database health check failed: %s", exc)
            return False

    def file_size_bytes(self) -> int | None:
        """Size of the SQLite file, for the System page."""
        if not self._is_sqlite:
            return None
        path = Path(self.url.replace("sqlite+aiosqlite:///", ""))
        try:
            return path.stat().st_size
        except OSError:
            return None
