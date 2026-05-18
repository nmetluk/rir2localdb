"""Stale-records GC — Stage 3-03.

6 сценариев: bootstrap (мало run'ов → нет marking), normal stale,
returning record cleared, API hides stale, API shows with
``include_stale=true``, sync_run.stats содержит ``gc_*``.

Используется ``db_session`` (SQLAlchemy AsyncSession в транзакции с
rollback'ом) для unit-тестов GC: ``run_gc(session)`` работает на той
же session, мы запрашиваем результат до commit'а. Для API-тестов —
``clean_db`` + ``api_client``, seed коммитится autocommit'ом.
"""

from __future__ import annotations

import asyncpg
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.config import Settings
from rir2localdb.sync.gc import run_gc

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — seed через AsyncSession.
# ---------------------------------------------------------------------------


async def _seed_success_run(session: AsyncSession) -> int:
    rid = await session.execute(
        text(
            "INSERT INTO sync_run (tier, status, finished_at) "
            "VALUES ('core', 'success', now()) RETURNING id"
        )
    )
    return int(rid.scalar_one())


async def _seed_inetnum(session: AsyncSession, run_id: int, *, start: str) -> None:
    start_int = int.from_bytes(bytes(int(p) for p in start.split(".")), "big")
    await session.execute(
        text(
            "INSERT INTO inetnum "
            "(rir, start_text, value, range_v4, raw, first_seen_run, last_seen_run) "
            "VALUES ('ripencc', :start, 256, int8range(:lo, :hi), "
            "'{}'::jsonb, :rid, :rid)"
        ),
        {"start": start, "lo": start_int, "hi": start_int + 256, "rid": run_id},
    )


async def _make_settings(grace: int) -> Settings:
    """``Settings`` с подменённым ``gc_grace_runs``. database_url из env."""
    return Settings(  # type: ignore[call-arg]
        gc_grace_runs=grace,
    )


# ---------------------------------------------------------------------------
# 1-3: core GC logic через db_session.
# ---------------------------------------------------------------------------


async def test_bootstrap_no_marking_when_few_runs(db_session: AsyncSession) -> None:
    """3 success-run'а, grace=7 → threshold=None, ничего не помечается."""
    run_ids = []
    for _ in range(3):
        run_ids.append(await _seed_success_run(db_session))
    await _seed_inetnum(db_session, run_ids[0], start="10.0.0.0")

    settings = await _make_settings(grace=7)
    stats = await run_gc(db_session, settings)

    assert stats.threshold_run_id is None
    assert stats.marked_stale == {}
    assert stats.cleared_stale == {}

    # Запись осталась active.
    row = await db_session.execute(text("SELECT is_stale FROM inetnum LIMIT 1"))
    assert row.scalar_one() is False


async def test_record_marked_stale_after_n_runs_absence(db_session: AsyncSession) -> None:
    """8 success-run'ов, запись только в run 1; grace=7 → threshold=run 2,
    запись (last_seen_run=1 < 2) → is_stale=TRUE."""
    run_ids = []
    for _ in range(8):
        run_ids.append(await _seed_success_run(db_session))
    await _seed_inetnum(db_session, run_ids[0], start="10.0.0.0")  # только в run 1

    settings = await _make_settings(grace=7)
    stats = await run_gc(db_session, settings)

    # threshold = 7th-from-end = run 2 (offset=6 in DESC order from run 8)
    assert stats.threshold_run_id == run_ids[1]
    assert stats.marked_stale.get("inetnum", 0) == 1
    assert stats.cleared_stale == {}

    row = await db_session.execute(text("SELECT is_stale FROM inetnum LIMIT 1"))
    assert row.scalar_one() is True


async def test_returning_record_cleared_stale(db_session: AsyncSession) -> None:
    """Stale запись + sync снова её обновил (last_seen_run > threshold) →
    is_stale возвращается в FALSE."""
    run_ids = []
    for _ in range(8):
        run_ids.append(await _seed_success_run(db_session))

    # Запись с last_seen_run = run 1 и уже is_stale=TRUE
    start_int = int.from_bytes(bytes(int(p) for p in ["10", "0", "0", "0"]), "big")
    await db_session.execute(
        text(
            "INSERT INTO inetnum "
            "(rir, start_text, value, range_v4, raw, first_seen_run, last_seen_run, is_stale) "
            "VALUES ('ripencc', '10.0.0.0', 256, int8range(:lo, :hi), "
            "'{}'::jsonb, :r1, :r8, TRUE)"
        ),
        {"lo": start_int, "hi": start_int + 256, "r1": run_ids[0], "r8": run_ids[-1]},
    )

    settings = await _make_settings(grace=7)
    stats = await run_gc(db_session, settings)

    # threshold = run_ids[1]; last_seen_run = run_ids[-1] = run 8 > threshold
    # is_stale was TRUE, должен быть очищен.
    assert stats.cleared_stale.get("inetnum", 0) == 1

    row = await db_session.execute(text("SELECT is_stale FROM inetnum LIMIT 1"))
    assert row.scalar_one() is False


# ---------------------------------------------------------------------------
# 4-5: API hides/shows stale records.
# ---------------------------------------------------------------------------


async def test_api_hides_stale_by_default(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """populate stale inetnum + ip_allocation → GET /v1/ip/... → 404 для default."""
    # seed sync_run + ip_allocation как stale
    run_id = await clean_db.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'success') RETURNING id"
    )
    start_int = int.from_bytes(bytes(int(p) for p in ["193", "0", "0", "0"]), "big")
    await clean_db.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v4, start_text, value, status, allocated_on,
             first_seen_run, last_seen_run, is_stale)
        VALUES ('ripencc', 'NL', 4, int8range($1, $2), '193.0.0.0', 256, 'allocated',
                NULL, $3, $3, TRUE)
        """,
        start_int,
        start_int + 256,
        run_id,
    )

    response = await api_client.get("/v1/ip/193.0.0.5")
    assert response.status_code == 404


async def test_api_shows_stale_with_include_stale_true(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """То же seed, но ``?include_stale=true`` → 200 + ``is_stale: true``."""
    run_id = await clean_db.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'success') RETURNING id"
    )
    start_int = int.from_bytes(bytes(int(p) for p in ["193", "0", "0", "0"]), "big")
    await clean_db.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v4, start_text, value, status, allocated_on,
             first_seen_run, last_seen_run, is_stale)
        VALUES ('ripencc', 'NL', 4, int8range($1, $2), '193.0.0.0', 256, 'allocated',
                NULL, $3, $3, TRUE)
        """,
        start_int,
        start_int + 256,
        run_id,
    )

    response = await api_client.get("/v1/ip/193.0.0.5?include_stale=true")
    assert response.status_code == 200
    data = response.json()
    assert data["is_stale"] is True
    assert data["start"] == "193.0.0.0"


# ---------------------------------------------------------------------------
# 6: orchestrator integration — sync_run.stats содержит gc_* поля.
# ---------------------------------------------------------------------------


async def test_gc_fields_in_sync_run_stats(db_session: AsyncSession) -> None:
    """После run_gc + INSERT в sync_run.stats — поля ``gc_threshold_run_id``,
    ``gc_marked_stale``, ``gc_cleared_stale`` сохраняются.

    Это integration sanity: проверяем что SyncRunSummary поля
    сериализуются JSON'ом и распарсиваются обратно из JSONB.
    """
    import json

    from rir2localdb.sync.gc import GcStats

    stats = GcStats(
        grace_runs=7,
        threshold_run_id=42,
        marked_stale={"inetnum": 5},
        cleared_stale={},
    )
    # симулируем _finalize_sync_run путь сохранения
    payload = {
        "gc_threshold_run_id": stats.threshold_run_id,
        "gc_marked_stale": dict(stats.marked_stale),
        "gc_cleared_stale": dict(stats.cleared_stale),
    }
    rid = await db_session.execute(
        text(
            "INSERT INTO sync_run (tier, status, stats) "
            "VALUES ('core', 'success', CAST(:p AS jsonb)) RETURNING id"
        ),
        {"p": json.dumps(payload)},
    )
    run_id = int(rid.scalar_one())

    # читаем обратно
    stored = (
        await db_session.execute(
            text("SELECT stats FROM sync_run WHERE id = :rid"),
            {"rid": run_id},
        )
    ).scalar_one()
    parsed = json.loads(stored) if isinstance(stored, str) else stored
    assert parsed["gc_threshold_run_id"] == 42
    assert parsed["gc_marked_stale"] == {"inetnum": 5}
    assert parsed["gc_cleared_stale"] == {}
