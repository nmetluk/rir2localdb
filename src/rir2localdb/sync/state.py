"""CRUD над ``sync_file`` — связь fetcher'а с БД.

Чистый персистентный слой: никаких HTTP-вызовов, файловых операций или
запуска парсеров. Все функции принимают ``AsyncSession``; commit — на
вызывающем коде (обычно — оркестраторе из ``sync/orchestrator.py``).

Публичный API:

    read_previous_state(session, url) -> PreviousFetchState | None
    write_result(session, source, result, run_id) -> None
    mark_parsed(session, url, parsed_at) -> None

Правила UPSERT'а в ``write_result`` зависят от ``FetchResult.status`` и
``FetchResult.tier_used`` (см. docstring fetcher'а):

| status      | tier | last_md5      | last_etag / lm  | last_sha256 / size |
|-------------|------|---------------|-----------------|--------------------|
| NEW         | 3    | sidecar→old   | fresh           | fresh              |
| UPDATED     | 3    | sidecar→old   | fresh           | fresh              |
| UNCHANGED   | 1    | sidecar (==old) | old           | old                |
| UNCHANGED   | 2    | **OLD**       | fresh→old (304) | old                |
| UNCHANGED   | 3    | sidecar→old   | fresh           | fresh (== old)     |
| ERROR       | None | old (preserve)| old             | old                |

«sidecar→old» = COALESCE: пишем свежий md5 если есть, иначе сохраняем
старый. «fresh→old» аналогично — пишем значение из ответа сервера если
есть, иначе сохраняем старое. **Исключение для tier 2**: даже если
``md5_sidecar`` свежий и отличается от старого, мы НЕ перезаписываем
``last_md5`` — это даёт fetcher'у возможность повторно обнаружить
расхождение на следующем прогоне (см. edge-case в docstring
``sync/fetcher.py``).

``last_fetched_at`` обновляется на каждом вызове ``write_result``
(включая ERROR) — это «когда последний раз пробовали». Историческая
запись успешного fetch'а (валидаторы) остаётся валидной для следующего
conditional GET.

``last_parsed_at`` тут НЕ трогается. Только ``mark_parsed()`` его
обновляет — вызывается из ``etl/*`` после успешного завершения парсинга.

``rir`` / ``tier`` / ``kind`` колонки в UPDATE-ветке UPSERT'а всегда
перезаписываются из текущего ``Source``: self-healing при
переклассификации каталога в ``sources.py``.
"""

from __future__ import annotations

import email.utils
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.db.models import SyncFile
from rir2localdb.sources import Source
from rir2localdb.sync.fetcher import FetchResult, FetchStatus, PreviousFetchState

logger = logging.getLogger(__name__)


async def read_previous_state(
    session: AsyncSession, url: str
) -> PreviousFetchState | None:
    """Прочитать строку ``sync_file`` по ``url`` и собрать ``PreviousFetchState``.

    Returns:
        ``PreviousFetchState`` с заполненными ``last_md5``/``last_etag``/
        ``last_modified``/``last_sha256``, либо ``None``, если строки нет.
        ``last_modified`` конвертируется обратно в HTTP-date строку,
        потому что fetcher работает с этим форматом для conditional GET.
    """
    row = (
        await session.execute(select(SyncFile).where(SyncFile.url == url))
    ).scalar_one_or_none()
    if row is None:
        return None
    return PreviousFetchState(
        last_md5=row.last_md5,
        last_etag=row.last_etag,
        last_modified=_datetime_to_http_date(row.last_modified),
        last_sha256=row.last_sha256,
    )


async def write_result(
    session: AsyncSession,
    source: Source,
    result: FetchResult,
    run_id: int,
) -> None:
    """UPSERT строки ``sync_file`` по правилам из docstring модуля."""
    existing = (
        await session.execute(select(SyncFile).where(SyncFile.url == result.url))
    ).scalar_one_or_none()

    values = _row_values(source, result, run_id, existing)

    stmt = (
        pg_insert(SyncFile)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[SyncFile.url],
            set_={k: v for k, v in values.items() if k != "url"},
        )
    )
    await session.execute(stmt)


async def mark_parsed(
    session: AsyncSession, url: str, parsed_at: datetime
) -> None:
    """Обновить ``last_parsed_at`` для строки ``sync_file``.

    Никаких других колонок не трогает. Если строки нет — UPDATE
    молча ничего не сделает (это нормально: parser не должен
    запускаться до fetcher'а, поэтому строка обязана быть).
    """
    await session.execute(
        update(SyncFile).where(SyncFile.url == url).values(last_parsed_at=parsed_at)
    )


# ---------------------------------------------------------------------------
# Внутренние помощники.
# ---------------------------------------------------------------------------


def _row_values(
    source: Source,
    result: FetchResult,
    run_id: int,
    existing: SyncFile | None,
) -> dict[str, Any]:
    """Собрать словарь колонок для INSERT/UPDATE.

    Поля ``rir``/``tier``/``kind`` всегда из ``source`` (self-healing).
    ``last_status``/``last_run_id``/``last_fetched_at`` всегда обновляются.
    Payload-поля (``last_md5``/etag/lm/sha256/size) — по правилам из
    docstring модуля.
    """
    payload = _payload_for_status(result, existing)
    return {
        "url": result.url,
        "rir": source.rir.value,
        "tier": source.tier.value,
        "kind": source.format.value,
        "last_run_id": run_id,
        "last_status": result.status.value,
        "last_fetched_at": datetime.now(tz=UTC),
        **payload,
    }


def _payload_for_status(
    result: FetchResult, existing: SyncFile | None
) -> dict[str, Any]:
    """Decide, что писать в payload-колонки на основе ``status``/``tier_used``."""

    def old(field: str) -> Any:
        return getattr(existing, field, None) if existing else None

    if result.status == FetchStatus.ERROR:
        # ERROR: preserve all payload — нам нечем обновлять.
        return {
            "last_md5": old("last_md5"),
            "last_etag": old("last_etag"),
            "last_modified": old("last_modified"),
            "last_sha256": old("last_sha256"),
            "last_size": old("last_size"),
        }

    # Для UNCHANGED-via-tier-2 — единственный спец-кейс: keep old md5.
    if result.status == FetchStatus.UNCHANGED and result.tier_used == 2:
        return {
            "last_md5": old("last_md5"),
            "last_etag": _coalesce(result.etag, old("last_etag")),
            "last_modified": _coalesce(
                _http_date_to_datetime(result.last_modified), old("last_modified")
            ),
            "last_sha256": old("last_sha256"),
            "last_size": old("last_size"),
        }

    # Все остальные исходы (NEW / UPDATED / UNCHANGED tier 1 / UNCHANGED tier 3):
    # COALESCE — пишем свежее если есть, иначе сохраняем старое.
    return {
        "last_md5": _coalesce(result.md5_sidecar, old("last_md5")),
        "last_etag": _coalesce(result.etag, old("last_etag")),
        "last_modified": _coalesce(
            _http_date_to_datetime(result.last_modified), old("last_modified")
        ),
        "last_sha256": _coalesce(result.content_sha256, old("last_sha256")),
        "last_size": _coalesce(result.size_bytes, old("last_size")),
    }


def _coalesce(fresh: Any, old: Any) -> Any:
    """``fresh`` если не ``None``, иначе ``old``."""
    return fresh if fresh is not None else old


def _http_date_to_datetime(s: str | None) -> datetime | None:
    """Распарсить HTTP-date (RFC 7231 IMF-fixdate) в TZ-aware ``datetime``.

    Возвращает ``None`` если строка пустая, ``None`` или невалидная.
    Не бросает — кривой ``Last-Modified`` от RIR-зеркала не должен
    валить fetcher.
    """
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt


def _datetime_to_http_date(dt: datetime | None) -> str | None:
    """Отформатировать TZ-aware ``datetime`` обратно в HTTP-date строку.

    Используется при восстановлении ``PreviousFetchState`` для fetcher'а,
    которому нужно передать значение прошлого ``Last-Modified`` в
    ``If-Modified-Since``-заголовке conditional GET.
    """
    if dt is None:
        return None
    return email.utils.format_datetime(dt, usegmt=True)
