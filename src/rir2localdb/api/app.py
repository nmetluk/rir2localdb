"""FastAPI app factory + lifespan.

Контракт endpoint'ов и поведение — ``docs/06-api.md``.

Фабричный паттерн ``make_app(settings=None)`` — для тестируемости:
тесты подсовывают свой ``Settings`` с тестовой БД через
``ASGITransport(app=make_app(test_settings))``. Production-режим
вызывается без аргумента, читает ``get_settings()``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from rir2localdb import __version__
from rir2localdb.api import metrics as metrics_module
from rir2localdb.api.routers import asn as asn_router
from rir2localdb.api.routers import ip as ip_router
from rir2localdb.api.routers import meta as meta_router
from rir2localdb.config import Settings, get_settings
from rir2localdb.db.engine import make_engine

_METRICS_PATH = "/v1/metrics"


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

    @app.middleware("http")
    async def prometheus_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Inc ``http_requests_total`` + observe ``http_request_duration_seconds``.

        ``/v1/metrics`` сам исключён, иначе на каждый Prometheus scrape
        счётчик растёт — это не наблюдаемая нагрузка, это сам мониторинг.

        ``endpoint`` label — route template (например ``/v1/ip/{addr}``),
        не concrete path. Иначе cardinality explosion на каждый
        уникальный IP / ASN. Если FastAPI не смог зарезолвить route
        (404 на неизвестный path), fall-back на ``request.url.path``
        — таких events немного.
        """
        if request.url.path == _METRICS_PATH:
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        route = request.scope.get("route")
        endpoint = getattr(route, "path", None) or request.url.path

        metrics_module.http_requests_total.labels(
            method=request.method,
            endpoint=endpoint,
            status=str(response.status_code),
        ).inc()
        metrics_module.http_request_duration_seconds.labels(
            method=request.method,
            endpoint=endpoint,
        ).observe(duration)

        return response

    app.include_router(ip_router.router, prefix="/v1")
    app.include_router(asn_router.router, prefix="/v1")
    app.include_router(meta_router.router, prefix="/v1")
    app.include_router(metrics_module.router, prefix="/v1")

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "name": "rir2localdb",
            "version": __version__,
            "docs": "/docs",
            "v1": [
                "/v1/ip/{addr}",
                "/v1/asn/{num}",
                "/v1/status",
                "/v1/healthz",
                "/v1/metrics",
            ],
        }

    return app
