import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from storage.models import Base

_engine = None
_session_factory = None


async def init_db(db_path: str = "data/trades.db") -> None:
    global _engine, _session_factory

    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    abs_path = os.path.abspath(db_path)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{abs_path}", echo=False)
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory()
