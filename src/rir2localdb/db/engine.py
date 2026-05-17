"""Фабрики async-движка и сессионника SQLAlchemy 2.x.

См. ``docs/02-architecture.md`` и ADR-0005. Stage 1: минимум — engine
+ ``async_sessionmaker``. FastAPI lifespan и async-контекст для CLI
прикручиваются позже, когда появятся первые потребители (sync, api).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rir2localdb.config import get_settings


def make_engine(database_url: str | None = None) -> AsyncEngine:
    """Создать async engine.

    Args:
        database_url: Полный SQLAlchemy URL вида
            ``postgresql+asyncpg://user:pass@host:port/db``. Если ``None`` —
            берём из ``Settings.database_url``.
    """
    url = database_url or get_settings().database_url
    return create_async_engine(url, future=True, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Создать async_sessionmaker, привязанный к engine'у."""
    return async_sessionmaker(engine, expire_on_commit=False)
