"""Stale-records garbage collection — Stage 3-03 § D.

Closes ADR-0001 / opens ADR-0008 (soft-delete via ``is_stale`` +
N-run grace period).

**Policy:** запись помечается ``is_stale=TRUE`` если её ``last_seen_run``
строго меньше id N-ого с конца successful sync_run'а (default N=7,
настраивается ``Settings.gc_grace_runs``). Это означает: запись не
появилась ни в одном из последних N успешных sync'ов.

**Symmetric clear:** при том же запуске GC, если запись с
``is_stale=TRUE`` была touched текущим (или recent) sync'ом и теперь
``last_seen_run >= threshold`` — снимаем флаг.

**Hard delete не делаем.** Stale-навсегда, пока кто-то не запустит
отдельную ``purge-stale`` команду (не в этой версии).

**Bootstrap.** Если успешных run'ов < ``gc_grace_runs``, threshold
вернётся NULL — GC не делает ничего. Соответствует «нет ещё истории
чтобы судить».

**Транзакционность.** ``run_gc`` принимает ``AsyncSession`` и работает
в текущей транзакции вызывающего (orchestrator commit'ит вместе с
sync_run UPDATE'ом).

**Public API:**

    GcStats — frozen dataclass со счётчиками.
    run_gc(session, settings) -> GcStats
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.config import Settings

# Таблицы с ``last_seen_run`` колонкой — субъекты GC.
# Порядок фиксирован для предсказуемых логов.
_GC_TABLES: Final[tuple[str, ...]] = (
    "ip_allocation",
    "asn_allocation",
    "inetnum",
    "inet6num",
    "aut_num",
    "organisation",
    "role",
    "route",
    "route6",
    "as_block",
    "mntner",
    "person",
    "as_set",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class GcStats:
    """Результат одного ``run_gc`` запуска.

    ``threshold_run_id is None`` означает bootstrap-mode: успешных
    sync_run'ов меньше чем ``grace_runs``, GC ничего не сделал.
    """

    grace_runs: int
    threshold_run_id: int | None
    marked_stale: dict[str, int] = field(default_factory=dict)
    cleared_stale: dict[str, int] = field(default_factory=dict)
    rdap_cache_deleted: int = 0
    """Сколько expired-RDAP entries удалено (см. Stage 3-05). 0 если
    rdap_cache пуст или ничего не expired более чем 7 дней назад."""


async def run_gc(session: AsyncSession, settings: Settings) -> GcStats:
    """Apply stale-records GC policy на 13 таблицах.

    Шаги:
        1. ``threshold_run_id = SELECT id FROM sync_run WHERE
           status='success' ORDER BY id DESC OFFSET (grace_runs-1)
           LIMIT 1``. Если строки нет — bootstrap, возврат без
           изменений.
        2. Для каждой таблицы:
           a. ``UPDATE ... SET is_stale=TRUE WHERE last_seen_run <
              threshold AND is_stale=FALSE`` — count в ``marked_stale``.
           b. ``UPDATE ... SET is_stale=FALSE WHERE last_seen_run >=
              threshold AND is_stale=TRUE`` — count в ``cleared_stale``
              (вернувшиеся в активные).

    Использует ``rowcount`` от ``CursorResult`` — это efficient,
    PostgreSQL возвращает count без отдельного SELECT.
    """
    grace_runs = settings.gc_grace_runs

    # OFFSET grace_runs-1 — позиция N-ого с конца. Если success-runs
    # меньше N, OFFSET выкидывает все → пусто → bootstrap.
    threshold_row = (
        await session.execute(
            text(
                "SELECT id FROM sync_run WHERE status = 'success' "
                "ORDER BY id DESC OFFSET :offset LIMIT 1"
            ),
            {"offset": grace_runs - 1},
        )
    ).first()

    if threshold_row is None:
        return GcStats(grace_runs=grace_runs, threshold_run_id=None)

    threshold_id = int(threshold_row[0])

    marked: dict[str, int] = {}
    cleared: dict[str, int] = {}

    for table in _GC_TABLES:
        mark_result = await session.execute(
            text(
                f"UPDATE {table} SET is_stale = TRUE "
                "WHERE last_seen_run < :threshold AND is_stale = FALSE"
            ),
            {"threshold": threshold_id},
        )
        assert isinstance(mark_result, CursorResult)
        if mark_result.rowcount > 0:
            marked[table] = mark_result.rowcount

        clear_result = await session.execute(
            text(
                f"UPDATE {table} SET is_stale = FALSE "
                "WHERE last_seen_run >= :threshold AND is_stale = TRUE"
            ),
            {"threshold": threshold_id},
        )
        assert isinstance(clear_result, CursorResult)
        if clear_result.rowcount > 0:
            cleared[table] = clear_result.rowcount

    # Cleanup expired RDAP cache entries — старее 7 дней.
    # Recently-expired (within 7d) сохраняются для diagnostic /
    # observability через ``rir2localdb_rdap_cache_entries{status=expired}``.
    rdap_result = await session.execute(
        text("DELETE FROM rdap_cache WHERE expires_at < now() - INTERVAL '7 days'")
    )
    assert isinstance(rdap_result, CursorResult)
    rdap_deleted = rdap_result.rowcount if rdap_result.rowcount > 0 else 0

    return GcStats(
        grace_runs=grace_runs,
        threshold_run_id=threshold_id,
        marked_stale=marked,
        cleared_stale=cleared,
        rdap_cache_deleted=rdap_deleted,
    )
