"""``/v1/healthz``, ``/v1/readyz``, ``/v1/status``."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from rir2localdb.api.schemas import HealthzResponse, RirSummary, StatusResponse

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
    """Сводка состояния: последний sync_run + sync_file + per-RIR агрегаты."""
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
            ip_counts = {
                row["rir"]: row["count"]
                for row in (
                    await session.execute(
                        text("SELECT rir, COUNT(*) AS count FROM ip_allocation GROUP BY rir")
                    )
                ).mappings()
            }
            asn_counts = {
                row["rir"]: row["count"]
                for row in (
                    await session.execute(
                        text("SELECT rir, COUNT(*) AS count FROM asn_allocation GROUP BY rir")
                    )
                ).mappings()
            }
            fetched_at = {
                row["rir"]: row["last_fetched_at"]
                for row in (
                    await session.execute(
                        text(
                            "SELECT rir, MAX(last_fetched_at) AS last_fetched_at "
                            "FROM sync_file GROUP BY rir"
                        )
                    )
                ).mappings()
            }
        db_alive = True
    except Exception:
        latest_run = None
        sources_rows = []
        ip_counts = {}
        asn_counts = {}
        fetched_at = {}
        db_alive = False

    all_rirs = sorted(set(ip_counts) | set(asn_counts) | set(fetched_at))
    summary_by_rir = [
        RirSummary(
            rir=r,
            ip_allocations=ip_counts.get(r, 0),
            asn_allocations=asn_counts.get(r, 0),
            last_fetched_at=fetched_at.get(r),
        )
        for r in all_rirs
    ]

    return StatusResponse(
        latest_sync_run=dict(latest_run) if latest_run else None,
        sources=[dict(r) for r in sources_rows],
        summary_by_rir=summary_by_rir,
        db_alive=db_alive,
    )
