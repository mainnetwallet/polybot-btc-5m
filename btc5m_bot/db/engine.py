"""SQLite engine, WAL configuration, integrity and backup helpers."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _is_url(target: Path | str) -> bool:
    return isinstance(target, str) and "://" in target


def make_engine(target: Path | str, *, echo: bool = False) -> Engine:
    """Engine for the trading database.

    ``target`` is either a local SQLite file path (Path) or a database URL (str) such as the
    Postgres URL in POLYBOT_DATABASE_URL. Use ``Settings.database_target`` to get the right one.
    """
    if _is_url(target):
        from ..config import normalize_database_url

        url = normalize_database_url(str(target))
        if url.startswith("sqlite"):
            return _sqlite_engine_from_url(url, echo=echo)
        # Free-tier Postgres sleeps and drops idle connections: pre-ping + short recycle + a
        # connect timeout make the pool survive that instead of failing a trade mid-flight.
        return create_engine(
            url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            pool_recycle=240,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 15},
        )
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _sqlite_engine_from_url(f"sqlite+pysqlite:///{path.as_posix()}", echo=echo)


def _sqlite_engine_from_url(url: str, *, echo: bool) -> Engine:
    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create schema when absent. Production setup uses Alembic; this is the dev/test fallback."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextlib.contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def integrity_check(engine: Engine) -> str:
    if engine.dialect.name != "sqlite":
        # A hosted database checks its own storage; here we only prove it is reachable and writable.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).scalar_one()
        return "ok"
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA integrity_check")).scalar_one()
    if str(result).lower() != "ok":
        raise RuntimeError(f"database integrity check failed: {result}")
    return str(result)


class BackupNotSupported(RuntimeError):
    """The database is hosted: backups are the provider's job (Render/Neon/Supabase snapshots)."""


def backup_database(engine: Engine, dest: Path) -> Path:
    if engine.dialect.name != "sqlite":
        raise BackupNotSupported("hosted database: use the provider's backups/point-in-time restore")
    dest = Path(dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(dest)
    safe = str(dest).replace("'", "''")
    with engine.connect() as conn:
        conn.execute(text(f"VACUUM INTO '{safe}'"))
    return dest


@contextlib.contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    """Cross-platform advisory lock preventing two engines from trading at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"another POLYBOT instance holds {path}") from exc
        else:  # pragma: no cover - deployment target is Windows
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"another POLYBOT instance holds {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
