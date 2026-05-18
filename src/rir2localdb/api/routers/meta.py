"""``/v1/healthz``, ``/v1/readyz``, ``/v1/status``."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from rir2localdb.api.schemas import HealthzResponse, StatusResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthzResponse)
async def healthz() -> HealthzResponse:
    """Liveness — не трогает БД. Для readiness см. ``/v1/readyz``."""
    return HealthzResponse()


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    """Readiness — пингует БД через ``SELECT 1``.

    Отделено от ``/healthz`` (liveness) сознательно — стандарт
    k8s probes: liveness не должен зависеть от внешних состояний.
    """
    sessionmaker = request.app.state.sessionmaker
    try:
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db not ready: {exc}") from exc
    return {"status": "ready"}


@router.get("/status", response_model=StatusResponse)
async def status(request: Request) -> StatusResponse:
    """Сводка состояния: последний sync_run + список sync_file + db_alive."""
    sessionmaker = request.app.state.sessionmaker
    try:
        async with sessionmaker() as session:
            latest_run = (
                (
                    await session.execute(
                        text(
                            "SELECT id, tier, started_at, finished_at, "
                            "       status, stats, error "
                            "FROM sync_run ORDER BY started_at DESC LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
            sources_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT url, rir, kind, last_status, last_fetched_at, "
                            "       last_parsed_at, last_size "
                            "FROM sync_file "
                            "ORDER BY last_fetched_at DESC NULLS LAST"
                        )
                    )
                )
                .mappings()
                .all()
            )
        db_alive = True
    except Exception:
        latest_run = None
        sources_rows = []
        db_alive = False

    return StatusResponse(
        latest_sync_run=dict(latest_run) if latest_run else None,
        sources=[dict(r) for r in sources_rows],
        db_alive=db_alive,
    )
