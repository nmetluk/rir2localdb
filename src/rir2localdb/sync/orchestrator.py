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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from rir2localdb.config import Settings
from rir2localdb.etl.delegated_etl import EtlStats, apply_delegated_etl
from rir2localdb.etl.rpsl_etl import RpslEtlStats, apply_rpsl_etl
from rir2localdb.parsers.delegated import parse_delegated
from rir2localdb.parsers.rpsl import parse_rpsl
from rir2localdb.sources import Format, Source, Tier, sources_for_tiers
from rir2localdb.sync.fetcher import FetchStatus, fetch, make_http_client
from rir2localdb.sync.gc import GcStats, run_gc
from rir2localdb.sync.state import mark_parsed, read_previous_state, write_result

# Форматы, обрабатываемые RPSL парсером + ETL. Все три имеют один и
# тот же текстовый формат внутри (RFC 2622 RPSL), различаются только
# упаковкой / split'ингом по типам объектов.
_RPSL_FORMATS: frozenset[Format] = frozenset({Format.RPSL, Format.RPSL_GZ, Format.RPSL_SPLIT_GZ})

if TYPE_CHECKING:
    import asyncpg
    import httpx

logger = logging.getLogger(__name__)


# Ключ для pg_advisory_xact_lock. Фиксированное int8 значение,
# уникальное для нашего приложения. Если будет нужен ещё один независимый
# lock, выделим отдельный ключ и задокументируем в реестре.
ADVISORY_LOCK_KEY: Final[int] = 0x7269723263616C64  # ASCII "rir2cald"


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncRunSummary:
    """Итог одного sync-run'а.

    Собирается в ``run_sync``, возвращается CLI, в сериализованном
    виде кладётся в ``sync_run.stats`` (JSONB). Поля ``files_*``
    относятся к источникам; ``etl_*`` — к строкам в ip/asn-таблицах
    и RPSL-таблицах суммарно за run.

    ``kw_only=True`` — чтобы свободно добавлять опциональные поля с
    дефолтами в любом порядке (default-after-non-default ordering
    больше не валит __init__).
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
    """Сумма ``EtlStats.records_seen`` + ``RpslEtlStats.objects_seen``
    по всем источникам run'а. Один общий счётчик для обзора."""
    etl_ip_inserted: int
    etl_ip_updated: int
    etl_asn_inserted: int
    etl_asn_updated: int
    duration_ms: int
    # RPSL ETL (Stage 2). Дефолты — 0/пустой dict; если в run'е не
    # было rpsl-источников, поля остаются нулевыми.
    etl_rpsl_records_total: int = 0
    etl_rpsl_unknown_type_skipped: int = 0
    etl_rpsl_malformed_skipped: int = 0
    etl_rpsl_by_type: dict[str, dict[str, int]] = field(default_factory=dict)
    """``{"inetnum": {"inserted": N, "updated": M}, ...}``. Включает только
    таблицы, в которые был хоть один upsert в этом run'е."""
    # GC (Stage 3-03). Заполняется только при ``status='success'`` —
    # GC бежит после успешного sync'а в той же транзакции.
    gc_threshold_run_id: int | None = None
    """Id N-ого с конца successful sync_run'а, использованный как
    threshold для is_stale. ``None`` если bootstrap (success-runs <
    ``gc_grace_runs``) или sync завершился ``failed``."""
    gc_marked_stale: dict[str, int] = field(default_factory=dict)
    """Per-table count записей помеченных ``is_stale=TRUE`` этим run'ом."""
    gc_cleared_stale: dict[str, int] = field(default_factory=dict)
    """Per-table count записей у которых ``is_stale=TRUE`` → ``FALSE`` —
    вернувшиеся в активные после того как sync снова их увидел."""
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
    rpsl_etl: RpslEtlStats | None = None


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
                # Session over the same AsyncConnection — все SQLAlchemy
                # операции в одной транзакции. INSERT sync_run / advisory
                # lock / UPDATE sync_run идут через session, чтобы попасть
                # в SQLAlchemy-tracked транзакцию. Raw asyncpg conn (для
                # ETL COPY) добывается только когда нужен — после первой
                # SQLAlchemy-операции, чтобы BEGIN уже был отправлен на
                # сервер и asyncpg видел себя внутри транзакции.
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    insert_result = await session.execute(
                        text(
                            "INSERT INTO sync_run (tier, status) "
                            "VALUES (:tier, 'running') RETURNING id"
                        ),
                        {"tier": tier_label},
                    )
                    run_id = int(insert_result.scalar_one())

                    lock_result = await session.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"),
                        {"k": ADVISORY_LOCK_KEY},
                    )
                    got_lock = bool(lock_result.scalar_one())
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
                        await _finalize_sync_run(session, run_id, summary)
                        await txn.commit()
                        return summary

                    # SQLAlchemy уже отправил BEGIN на сервер. Теперь
                    # raw asyncpg conn видит активную транзакцию.
                    raw_dbapi = await connection.get_raw_connection()
                    raw_conn: asyncpg.Connection = raw_dbapi.driver_connection
                    assert raw_conn is not None, "asyncpg driver_connection missing"

                    sources = sources_for_tiers(set(tier_list))
                    counters = _Counters()
                    async with make_http_client(settings) as client:
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
                    if (
                        counters.total_sources > 0
                        and counters.files_errored == counters.total_sources
                    ):
                        status = "failed"
                        final_error = f"all {counters.files_errored} source(s) failed"
                    else:
                        status = "success"
                        final_error = None

                    # GC бежит ТОЛЬКО на успешном sync'е и в этой же
                    # транзакции — атомарно с UPDATE sync_run.status.
                    # Сначала UPDATE до 'success' (чтобы текущий run
                    # уже считался в success-окне threshold), потом GC,
                    # потом final UPDATE с gc-счётчиками в stats.
                    gc_stats = GcStats(
                        grace_runs=settings.gc_grace_runs,
                        threshold_run_id=None,
                    )
                    if status == "success":
                        await session.execute(
                            text("UPDATE sync_run SET status = 'success' WHERE id = :rid"),
                            {"rid": run_id},
                        )
                        await session.flush()
                        gc_stats = await run_gc(session, settings)

                    summary = _build_summary(
                        run_id=run_id,
                        tier_label=tier_label,
                        status=status,
                        counters=counters,
                        error=final_error,
                        started=started,
                        gc_stats=gc_stats,
                    )
                    await _finalize_sync_run(session, run_id, summary)

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
    etl_rpsl_records_total: int = 0
    etl_rpsl_unknown_type_skipped: int = 0
    etl_rpsl_malformed_skipped: int = 0
    etl_rpsl_by_type: dict[str, dict[str, int]] = field(default_factory=dict)

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
        if outcome.rpsl_etl is not None:
            r = outcome.rpsl_etl
            self.etl_rpsl_records_total += r.objects_seen
            self.etl_rpsl_unknown_type_skipped += r.objects_skipped_unknown_type
            self.etl_rpsl_malformed_skipped += r.objects_skipped_malformed
            for tbl, n in r.upsert_inserted.items():
                self.etl_rpsl_by_type.setdefault(tbl, {"inserted": 0, "updated": 0})[
                    "inserted"
                ] += n
            for tbl, n in r.upsert_updated.items():
                self.etl_rpsl_by_type.setdefault(tbl, {"inserted": 0, "updated": 0})["updated"] += n


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

    if result.local_path is None:
        # NEW/UPDATED без local_path — невозможно по контракту fetcher'а;
        # но проверка как safety net.
        return _SourceOutcome(fetch_status=result.status)

    if source.format == Format.DELEGATED:
        records = list(parse_delegated(result.local_path))
        etl_stats = await apply_delegated_etl(raw_conn, records, run_id)
        await mark_parsed(session, source.url, datetime.now(tz=UTC))
        await session.flush()
        return _SourceOutcome(
            fetch_status=result.status,
            parser_records=len(records),
            etl=etl_stats,
        )

    if source.format in _RPSL_FORMATS:
        objects = parse_rpsl(result.local_path)
        rpsl_stats = await apply_rpsl_etl(
            raw_conn,
            objects,
            rir=source.rir.value,
            run_id=run_id,
        )
        await mark_parsed(session, source.url, datetime.now(tz=UTC))
        await session.flush()
        return _SourceOutcome(
            fetch_status=result.status,
            parser_records=rpsl_stats.objects_seen,
            rpsl_etl=rpsl_stats,
        )

    logger.warning(
        "orchestrator: unsupported format %s for %s — skipping ETL",
        source.format,
        source.url,
    )
    return _SourceOutcome(fetch_status=result.status)


def _build_summary(
    *,
    run_id: int,
    tier_label: str,
    status: Literal["success", "failed"],
    counters: _Counters,
    error: str | None,
    started: float,
    gc_stats: GcStats | None = None,
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
        etl_rpsl_records_total=counters.etl_rpsl_records_total,
        etl_rpsl_unknown_type_skipped=counters.etl_rpsl_unknown_type_skipped,
        etl_rpsl_malformed_skipped=counters.etl_rpsl_malformed_skipped,
        etl_rpsl_by_type=dict(counters.etl_rpsl_by_type),
        gc_threshold_run_id=gc_stats.threshold_run_id if gc_stats else None,
        gc_marked_stale=dict(gc_stats.marked_stale) if gc_stats else {},
        gc_cleared_stale=dict(gc_stats.cleared_stale) if gc_stats else {},
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=error,
    )


async def _finalize_sync_run(session: AsyncSession, run_id: int, summary: SyncRunSummary) -> None:
    """UPDATE sync_run в финальное состояние, кладёт summary в stats jsonb.

    Идёт через SQLAlchemy session (а не raw asyncpg), чтобы UPDATE
    был частью SQLAlchemy-tracked транзакции — иначе на dry_run
    rollback может оставить эту строку закоммиченной.
    """
    stats_payload: dict[str, Any] = {
        k: v for k, v in asdict(summary).items() if k not in ("run_id", "tier", "status", "error")
    }
    # ``clock_timestamp()``, а не ``now()``: внутри одной транзакции
    # ``now()`` ≡ ``statement_timestamp()`` и возвращает время старта
    # транзакции; после многоминутного sync'а finished_at оказался бы
    # равен started_at. ``clock_timestamp()`` даёт реальный wall-clock.
    await session.execute(
        text(
            "UPDATE sync_run "
            "SET status=:status, finished_at=clock_timestamp(), "
            "    stats=CAST(:stats AS jsonb), error=:err "
            "WHERE id=:rid"
        ),
        {
            "status": summary.status,
            "stats": json.dumps(stats_payload),
            "err": summary.error,
            "rid": run_id,
        },
    )
    await session.flush()
