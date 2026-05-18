"""Sync orchestrator — координирует fetcher / parser / ETL для одного run'а.

Публичный API:

    run_sync(tiers, settings, *, dry_run=False) -> SyncRunSummary

Алгоритм (см. также ``docs/04-sync-pipeline.md`` § «Идемпотентность sync run'а»):

1. **Транзакция-обёртка.** Открыть SQLAlchemy AsyncEngine →
   AsyncConnection → AsyncSession (для ``sync_run`` / ``sync_file``
   через ``sync.state``). Raw ``asyncpg.Connection`` для ETL hot
   path достаём ИЗ той же ``AsyncConnection`` через
   ``await conn.get_raw_connection()`` — все операции одного run'а
   (state-CRUD и ETL) идут в одной транзакции.
2. **INSERT sync_run** (``status='running'``, ``tier='+'.join(tiers)``,
   ``started_at=now()``). ``run_id`` — для FK в ``sync_file.last_run_id``
   и ``ip_allocation.first/last_seen_run``.
3. **Advisory lock.** ``SELECT pg_try_advisory_xact_lock(<key>)`` —
   гарантирует один активный run за раз; lock освобождается на
   commit/rollback автоматически (xact-scoped). Если ``false`` —
   fail fast c ``error="another sync_run is already running"``.
4. **HTTP client.** ``httpx.AsyncClient`` через ``make_http_client(settings)``.
5. **Per-source loop.** Последовательно по ``sources_for_tiers(tiers)``,
   каждый источник — в своём ``session.begin_nested()`` savepoint:
   - ``previous = await read_previous_state(session, source.url)``
   - ``result = await fetch(client, source, previous, settings)``
   - ``await write_result(session, source, result, run_id)``
   - Если ``result.status in {NEW, UPDATED}``:
       - ``records = parse_delegated(result.local_path)`` (для NRO-формата),
       - ``etl_stats = await apply_delegated_etl(raw_conn, records, run_id)``,
       - ``await mark_parsed(session, source.url, now())``.
   - Каждый Source — в свой ``try/except``: одна ошибка одного
     источника инкрементирует ``files_errored``, не валит весь run.
6. **UPDATE sync_run** (``status='success'/'failed'``, ``finished_at=now()``,
   ``stats=jsonb``, ``error=...``). ``failed`` — когда не удалось
   даже взять lock, БД недоступна, или все источники упали; единичные
   per-source ошибки идут как ``success`` с ``files_errored>0``.
7. **Commit или rollback** в зависимости от ``dry_run``.

См. также: ``docs/02-architecture.md`` § «Sync layer»,
``docs/04-sync-pipeline.md``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from rir2localdb.config import Settings
from rir2localdb.etl.delegated_etl import EtlStats, apply_delegated_etl
from rir2localdb.parsers.delegated import parse_delegated
from rir2localdb.sources import Format, Source, Tier, sources_for_tiers
from rir2localdb.sync.fetcher import FetchStatus, fetch, make_http_client
from rir2localdb.sync.state import mark_parsed, read_previous_state, write_result

if TYPE_CHECKING:
    import asyncpg
    import httpx

logger = logging.getLogger(__name__)


# Ключ для pg_advisory_xact_lock. Фиксированное int8 значение,
# уникальное для нашего приложения. Если будет нужен ещё один независимый
# lock, выделим отдельный ключ и задокументируем в реестре.
ADVISORY_LOCK_KEY: Final[int] = 0x7269723263616C64  # ASCII "rir2cald"


@dataclass(frozen=True, slots=True)
class SyncRunSummary:
    """Итог одного sync-run'а.

    Собирается в ``run_sync``, возвращается CLI, в сериализованном
    виде кладётся в ``sync_run.stats`` (JSONB). Поля ``files_*``
    относятся к источникам; ``etl_*`` — к строкам в ip/asn-таблицах
    суммарно за run.
    """

    run_id: int
    tier: str
    """``'+'.join(t.value for t in tiers)`` — например ``"core"`` или ``"core+rich"``."""
    status: Literal["success", "failed"]
    files_total: int
    files_fetched_new: int
    files_fetched_updated: int
    files_unchanged: int
    files_errored: int
    parser_records_total: int
    """Сумма ``EtlStats.records_seen`` по всем источникам в run'е."""
    etl_ip_inserted: int
    etl_ip_updated: int
    etl_asn_inserted: int
    etl_asn_updated: int
    duration_ms: int
    error: str | None = None
    """``None`` при ``status='success'``; человеко-читаемое сообщение
    при ``'failed'`` (lock contention, БД недоступна, и т.п.).
    Per-source ошибки в это поле не попадают — они учтены в
    ``files_errored`` и status остаётся ``'success'``."""


@dataclass(frozen=True, slots=True)
class _SourceOutcome:
    """Промежуточный результат для одного источника. Не публичный."""

    fetch_status: FetchStatus
    parser_records: int = 0
    etl: EtlStats = field(default_factory=EtlStats)


async def run_sync(
    tiers: Iterable[Tier],
    settings: Settings,
    *,
    dry_run: bool = False,
) -> SyncRunSummary:
    """Запустить один sync-run и собрать сводку.

    Args:
        tiers: какие ``Tier``'ы обрабатывать. Источники
            фильтруются через ``sources_for_tiers()`` из
            ``sources.py``.
        settings: конфиг (``database_url``, ``data_dir``, ``http_*``).
        dry_run: если ``True`` — финальная транзакция rollback'ается,
            БД остаётся нетронутой. Полезно для smoke-теста.

    Returns:
        ``SyncRunSummary``. На критичной ошибке (не удалось взять
        lock, БД недоступна, и т.п.) — ``status='failed'``,
        ``error`` не-``None``, run_id может быть 0 если INSERT
        sync_run сам не сработал.
    """
    started = time.perf_counter()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    tier_list = list(tiers)
    tier_label = "+".join(t.value for t in tier_list) if tier_list else ""

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            txn = await connection.begin()
            try:
                raw_dbapi = await connection.get_raw_connection()
                raw_conn: asyncpg.Connection = raw_dbapi.driver_connection
                assert raw_conn is not None, "asyncpg driver_connection missing"

                run_id = int(
                    await raw_conn.fetchval(
                        "INSERT INTO sync_run (tier, status) "
                        "VALUES ($1, 'running') RETURNING id",
                        tier_label,
                    )
                )

                got_lock = await raw_conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock($1)", ADVISORY_LOCK_KEY
                )
                if not got_lock:
                    lock_error = "another sync_run is already running"
                    summary = _build_summary(
                        run_id=run_id,
                        tier_label=tier_label,
                        status="failed",
                        counters=_Counters(),
                        error=lock_error,
                        started=started,
                    )
                    await _finalize_sync_run(raw_conn, run_id, summary)
                    await txn.commit()
                    return summary

                sources = sources_for_tiers(set(tier_list))
                counters = _Counters()
                async with (
                    make_http_client(settings) as client,
                    AsyncSession(bind=connection, expire_on_commit=False) as session,
                ):
                    for source in sources:
                        try:
                            async with session.begin_nested():
                                outcome = await _run_one_source(
                                    source=source,
                                    session=session,
                                    raw_conn=raw_conn,
                                    client=client,
                                    settings=settings,
                                    run_id=run_id,
                                )
                            counters.add(outcome)
                        except Exception as exc:
                            logger.exception("source %s failed: %s", source.url, exc)
                            counters.files_errored += 1

                status: Literal["success", "failed"]
                final_error: str | None
                if counters.total_sources > 0 and counters.files_errored == counters.total_sources:
                    status = "failed"
                    final_error = f"all {counters.files_errored} source(s) failed"
                else:
                    status = "success"
                    final_error = None

                summary = _build_summary(
                    run_id=run_id,
                    tier_label=tier_label,
                    status=status,
                    counters=counters,
                    error=final_error,
                    started=started,
                )
                await _finalize_sync_run(raw_conn, run_id, summary)

                if dry_run:
                    await txn.rollback()
                else:
                    await txn.commit()
                return summary

            except Exception as exc:
                logger.exception("run_sync infrastructure error: %s", exc)
                with contextlib.suppress(Exception):
                    await txn.rollback()
                return _build_summary(
                    run_id=0,
                    tier_label=tier_label,
                    status="failed",
                    counters=_Counters(),
                    error=f"{type(exc).__name__}: {exc}",
                    started=started,
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Внутренние помощники.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Counters:
    """Накопитель счётчиков по ходу run'а. Не публичный."""

    total_sources: int = 0
    files_fetched_new: int = 0
    files_fetched_updated: int = 0
    files_unchanged: int = 0
    files_errored: int = 0
    parser_records_total: int = 0
    etl_ip_inserted: int = 0
    etl_ip_updated: int = 0
    etl_asn_inserted: int = 0
    etl_asn_updated: int = 0

    def add(self, outcome: _SourceOutcome) -> None:
        self.total_sources += 1
        match outcome.fetch_status:
            case FetchStatus.NEW:
                self.files_fetched_new += 1
            case FetchStatus.UPDATED:
                self.files_fetched_updated += 1
            case FetchStatus.UNCHANGED:
                self.files_unchanged += 1
            case FetchStatus.ERROR:
                self.files_errored += 1
        self.parser_records_total += outcome.parser_records
        self.etl_ip_inserted += outcome.etl.ip_inserted
        self.etl_ip_updated += outcome.etl.ip_updated
        self.etl_asn_inserted += outcome.etl.asn_inserted
        self.etl_asn_updated += outcome.etl.asn_updated


async def _run_one_source(
    *,
    source: Source,
    session: AsyncSession,
    raw_conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    settings: Settings,
    run_id: int,
) -> _SourceOutcome:
    """Обработать один Source: fetch → write_result → (parse+ETL+mark_parsed).

    Выполняется внутри session.begin_nested() — savepoint, который
    откатится на исключении и оставит outer transaction живой.
    """
    previous = await read_previous_state(session, source.url)
    result = await fetch(client, source, previous, settings)
    await write_result(session, source, result, run_id)
    await session.flush()

    if result.status not in (FetchStatus.NEW, FetchStatus.UPDATED):
        return _SourceOutcome(fetch_status=result.status)

    if result.local_path is None or source.format != Format.DELEGATED:
        # NEW/UPDATED без local_path — невозможно по контракту fetcher'а;
        # форматы !=DELEGATED — Stage 2, сейчас не парсим.
        return _SourceOutcome(fetch_status=result.status)

    records = list(parse_delegated(result.local_path))
    etl_stats = await apply_delegated_etl(raw_conn, records, run_id)
    await mark_parsed(session, source.url, datetime.now(tz=UTC))
    await session.flush()

    return _SourceOutcome(
        fetch_status=result.status,
        parser_records=len(records),
        etl=etl_stats,
    )


def _build_summary(
    *,
    run_id: int,
    tier_label: str,
    status: Literal["success", "failed"],
    counters: _Counters,
    error: str | None,
    started: float,
) -> SyncRunSummary:
    return SyncRunSummary(
        run_id=run_id,
        tier=tier_label,
        status=status,
        files_total=counters.total_sources,
        files_fetched_new=counters.files_fetched_new,
        files_fetched_updated=counters.files_fetched_updated,
        files_unchanged=counters.files_unchanged,
        files_errored=counters.files_errored,
        parser_records_total=counters.parser_records_total,
        etl_ip_inserted=counters.etl_ip_inserted,
        etl_ip_updated=counters.etl_ip_updated,
        etl_asn_inserted=counters.etl_asn_inserted,
        etl_asn_updated=counters.etl_asn_updated,
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=error,
    )


async def _finalize_sync_run(
    raw_conn: asyncpg.Connection, run_id: int, summary: SyncRunSummary
) -> None:
    """UPDATE sync_run в финальное состояние, кладёт summary в stats jsonb."""
    stats_payload: dict[str, Any] = {
        k: v
        for k, v in asdict(summary).items()
        if k not in ("run_id", "tier", "status", "error")
    }
    await raw_conn.execute(
        "UPDATE sync_run "
        "SET status=$1, finished_at=now(), stats=$2::jsonb, error=$3 "
        "WHERE id=$4",
        summary.status,
        json.dumps(stats_payload),
        summary.error,
        run_id,
    )
