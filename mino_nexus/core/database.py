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

_model_loader = None


def register_model_loader(fn) -> None:
    """ORM 包在自己的 __init__ 里登记，base 层不反向 import models。"""
    global _model_loader
    _model_loader = fn


def _load_models() -> None:
    if _model_loader is None:
        # 静默跳过会让建表拿不到 ORM 定义，问题在很远的地方才暴露。
        raise RuntimeError("models_not_registered：入口需先 import mino_nexus.models")
    _model_loader()

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


_bootstrap_hook = None


def register_bootstrap(fn) -> None:
    """装配根注册启动种子；`core` 不 import 任何业务模块（V8 §0.8）。

    由 `mino_nexus.startup` 在入口 import 时调用。
    """
    global _bootstrap_hook
    _bootstrap_hook = fn


def ensure_db() -> None:
    global _ready, _booting
    if _ready or _booting:
        return
    _booting = True
    try:
        _load_models()

        from mino_nexus.core.schema_contract import initialize_current_schema
        initialize_current_schema(engine, Base.metadata)
        if _bootstrap_hook is None:
            # 显式失败：静默跳过种子会让能力目录为空，问题在很远的地方才暴露。
            raise RuntimeError("bootstrap_not_registered：入口需先 import mino_nexus.startup")

        _bootstrap_hook()
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
