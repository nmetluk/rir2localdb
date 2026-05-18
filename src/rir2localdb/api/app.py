"""FastAPI app factory + lifespan.

Контракт endpoint'ов и поведение — ``docs/06-api.md``.

Фабричный паттерн ``make_app(settings=None)`` — для тестируемости:
тесты подсовывают свой ``Settings`` с тестовой БД через
``ASGITransport(app=make_app(test_settings))``. Production-режим
вызывается без аргумента, читает ``get_settings()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from rir2localdb import __version__
from rir2localdb.api.routers import asn as asn_router
from rir2localdb.api.routers import ip as ip_router
from rir2localdb.api.routers import meta as meta_router
from rir2localdb.config import Settings, get_settings
from rir2localdb.db.engine import make_engine


def make_app(settings: Settings | None = None) -> FastAPI:
    """Собрать ``FastAPI`` приложение.

    Args:
        settings: если ``None``, читается ``get_settings()`` (env / .env).
            Тесты передают свой ``Settings`` с ``database_url``
            смотрящим на тестовую БД.
    """
    resolved_settings: Settings = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(resolved_settings.database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        _app.state.engine = engine
        _app.state.sessionmaker = sessionmaker
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="rir2localdb",
        version=__version__,
        description="Daily mirror of RIR data with whois-like REST API.",
        lifespan=lifespan,
    )
    app.include_router(ip_router.router, prefix="/v1")
    app.include_router(asn_router.router, prefix="/v1")
    app.include_router(meta_router.router, prefix="/v1")

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "name": "rir2localdb",
            "version": __version__,
            "docs": "/docs",
            "v1": ["/v1/ip/{addr}", "/v1/asn/{num}", "/v1/status", "/v1/healthz"],
        }

    return app
