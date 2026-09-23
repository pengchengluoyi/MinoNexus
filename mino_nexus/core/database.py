"""SQLite + SQLAlchemy。库文件 mino.db。路径在 paths.py，不和 engine 焊死。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from mino_nexus.core.paths import data_dir

DB_PATH = data_dir() / "mino.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"


def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _build_engine():
    url = f"sqlite:///{data_dir() / 'mino.db'}"
    eng = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    event.listen(eng, "connect", _set_sqlite_pragma)
    return eng


engine = _build_engine()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_ready = False
_booting = False


def get_db() -> Iterator[Session]:
    ensure_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    ensure_db()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_db() -> None:
    global _ready, _booting
    if _ready or _booting:
        return
    _booting = True
    try:
        from mino_nexus import models  # noqa: F401 — 注册表

        Base.metadata.create_all(bind=engine)
        from mino_nexus.core.migration import run_auto_migration

        run_auto_migration()
        from mino_nexus.core.bootstrap import bootstrap

        bootstrap()
        _ready = True
    finally:
        _booting = False


def rebind_sqlite_to_data_dir() -> None:
    """测试在 monkeypatch MINO_NEXUS_DATA_DIR 后调用，避免写入真实 mino.db。"""
    global engine, SessionLocal, DB_PATH, SQLALCHEMY_DATABASE_URL, _ready, _booting
    try:
        engine.dispose()
    except Exception:
        pass
    DB_PATH = data_dir() / "mino.db"
    SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"
    engine = _build_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _ready = False
    _booting = False
