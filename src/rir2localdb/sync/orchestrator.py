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
5. **Per-source loop.** Последовательно по ``sources_for_tiers(tiers)``:
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
   даже взять lock или критичная ошибка инфраструктуры; единичные
   per-source ошибки идут как ``success`` с ``files_errored>0``.
7. **Commit или rollback** в зависимости от ``dry_run``.

См. также: ``docs/02-architecture.md`` § «Sync layer»,
``docs/04-sync-pipeline.md``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from rir2localdb.config import Settings
from rir2localdb.sources import Tier

# Ключ для pg_advisory_xact_lock. Фиксированное int8 значение,
# уникальное для нашего приложения. Используется только в orchestrator —
# если будет нужен ещё один независимый lock, выделим отдельный ключ
# и задокументируем в реестре.
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
    raise NotImplementedError("run_sync — stage 1 step 7 phase B")
