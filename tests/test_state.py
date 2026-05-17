"""Тесты ``sync.state``: 8 сценариев на БД + 2 unit'а на HTTP-date.

DB-фикстуры см. в ``conftest.py``. БД-зависимые тесты используют
``db_session`` (per-test rollback) и ``sync_run_id`` (FK-источник).
Unit-тесты HTTP-date — без БД, без фикстур.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.db.models import SyncFile
from rir2localdb.sources import Format, Rir, Source, Tier
from rir2localdb.sync.fetcher import FetchResult, FetchStatus
from rir2localdb.sync.state import (
    _datetime_to_http_date,
    _http_date_to_datetime,
    mark_parsed,
    read_previous_state,
    write_result,
)

# ---------------------------------------------------------------------------
# Helpers / common objects.
# ---------------------------------------------------------------------------

URL = "https://example.test/delegated"
MD5_OLD = "a" * 32
MD5_NEW = "b" * 32
SHA_OLD = "c" * 64
SHA_NEW = "d" * 64
ETAG_OLD = '"old-etag"'
ETAG_NEW = '"new-etag"'
LM_OLD = "Wed, 01 Jan 2025 00:00:00 GMT"
LM_NEW = "Thu, 02 Jan 2025 00:00:00 GMT"


def _source(*, tier: Tier = Tier.CORE) -> Source:
    return Source(
        rir=Rir.RIPE,
        tier=tier,
        format=Format.DELEGATED,
        url=URL,
        md5_url=URL + ".md5",
        description="test source",
    )


def _result(
    status: FetchStatus,
    *,
    tier_used: int | None,
    md5_sidecar: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    content_sha256: str | None = None,
    size_bytes: int | None = None,
    error: str | None = None,
) -> FetchResult:
    return FetchResult(
        status=status,
        url=URL,
        md5_sidecar=md5_sidecar,
        etag=etag,
        last_modified=last_modified,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        error=error,
        tier_used=tier_used,
    )


async def _row(session: AsyncSession) -> SyncFile:
    """Поднять единственную строку sync_file (или ассертить наличие)."""
    rows = (await session.execute(select(SyncFile))).scalars().all()
    assert len(rows) == 1, f"expected one sync_file row, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# Сценарии.
# ---------------------------------------------------------------------------


async def test_read_previous_returns_none_when_empty(db_session: AsyncSession) -> None:
    """Для URL, которого нет в sync_file, read_previous_state возвращает None."""
    result = await read_previous_state(db_session, URL)
    assert result is None


async def test_write_new_then_read_returns_all_fields(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """write(NEW) → read возвращает все валидаторы; HTTP-date round-trip корректен."""
    result = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_NEW,
        etag=ETAG_NEW,
        last_modified=LM_NEW,
        content_sha256=SHA_NEW,
        size_bytes=12345,
    )
    await write_result(db_session, _source(), result, sync_run_id)

    previous = await read_previous_state(db_session, URL)
    assert previous is not None
    assert previous.last_md5 == MD5_NEW
    assert previous.last_etag == ETAG_NEW
    assert previous.last_modified == LM_NEW  # HTTP-date round-tripped
    assert previous.last_sha256 == SHA_NEW


async def test_write_updated_overwrites_existing_row(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """Повторный write по тому же URL — UPDATE, не дубль. Заодно Q4: tier перекатегоризуется."""
    first = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_OLD,
        etag=ETAG_OLD,
        last_modified=LM_OLD,
        content_sha256=SHA_OLD,
        size_bytes=100,
    )
    await write_result(db_session, _source(tier=Tier.CORE), first, sync_run_id)

    second = _result(
        FetchStatus.UPDATED,
        tier_used=3,
        md5_sidecar=MD5_NEW,
        etag=ETAG_NEW,
        last_modified=LM_NEW,
        content_sha256=SHA_NEW,
        size_bytes=200,
    )
    # Q4: тот же URL, но Source теперь в другом tier'е.
    await write_result(db_session, _source(tier=Tier.RICH), second, sync_run_id)

    row = await _row(db_session)
    assert row.last_status == "updated"
    assert row.last_md5 == MD5_NEW
    assert row.last_etag == ETAG_NEW
    assert row.last_sha256 == SHA_NEW
    assert row.last_size == 200
    assert row.tier == Tier.RICH.value, "Q4: tier должен перезаписаться из source"


async def test_write_unchanged_via_tier1_updates_only_md5_and_meta(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """Tier 1 UNCHANGED: пишем last_md5, остальные validators — старое."""
    seed = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_OLD,
        etag=ETAG_OLD,
        last_modified=LM_OLD,
        content_sha256=SHA_OLD,
        size_bytes=100,
    )
    await write_result(db_session, _source(), seed, sync_run_id)

    # Свежий md5_sidecar совпадает с прошлым → tier 1 UNCHANGED.
    unchanged = _result(FetchStatus.UNCHANGED, tier_used=1, md5_sidecar=MD5_OLD)
    await write_result(db_session, _source(), unchanged, sync_run_id)

    row = await _row(db_session)
    assert row.last_status == "unchanged"
    assert row.last_md5 == MD5_OLD
    assert row.last_etag == ETAG_OLD
    assert row.last_sha256 == SHA_OLD
    assert row.last_size == 100


async def test_write_unchanged_via_tier2_updates_only_etag_lm_and_meta(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """Tier 2 UNCHANGED: last_md5 сохраняется СТАРЫМ даже если md5_sidecar свежий."""
    seed = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_OLD,
        etag=ETAG_OLD,
        last_modified=LM_OLD,
        content_sha256=SHA_OLD,
        size_bytes=100,
    )
    await write_result(db_session, _source(), seed, sync_run_id)

    # Свежий md5 разошёлся со старым → fetcher пошёл в tier 2 → 304.
    # Сервер прислал новые ETag/Last-Modified в 304-ответе.
    unchanged = _result(
        FetchStatus.UNCHANGED,
        tier_used=2,
        md5_sidecar=MD5_NEW,
        etag=ETAG_NEW,
        last_modified=LM_NEW,
    )
    await write_result(db_session, _source(), unchanged, sync_run_id)

    row = await _row(db_session)
    assert row.last_status == "unchanged"
    # Ключевая семантика: last_md5 НЕ перезаписан, чтобы recheck-next-run.
    assert row.last_md5 == MD5_OLD
    assert row.last_etag == ETAG_NEW
    assert row.last_sha256 == SHA_OLD
    assert row.last_size == 100


async def test_write_unchanged_via_tier3_updates_all_payload_fields(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """Tier 3 UNCHANGED: тело скачано, sha совпал; обновляем все validators."""
    seed = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_OLD,
        etag=ETAG_OLD,
        last_modified=LM_OLD,
        content_sha256=SHA_OLD,
        size_bytes=100,
    )
    await write_result(db_session, _source(), seed, sync_run_id)

    # Tier 3 UNCHANGED: качали тело, sha == старому, но etag/lm могли сменить.
    unchanged = _result(
        FetchStatus.UNCHANGED,
        tier_used=3,
        md5_sidecar=MD5_NEW,
        etag=ETAG_NEW,
        last_modified=LM_NEW,
        content_sha256=SHA_OLD,
        size_bytes=100,
    )
    await write_result(db_session, _source(), unchanged, sync_run_id)

    row = await _row(db_session)
    assert row.last_status == "unchanged"
    assert row.last_md5 == MD5_NEW
    assert row.last_etag == ETAG_NEW
    assert row.last_sha256 == SHA_OLD
    assert row.last_size == 100


async def test_write_error_preserves_payload_keeps_only_status_and_meta(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """ERROR: payload (md5/etag/lm/sha/size) сохраняется, обновляются только status + meta."""
    seed = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_OLD,
        etag=ETAG_OLD,
        last_modified=LM_OLD,
        content_sha256=SHA_OLD,
        size_bytes=100,
    )
    await write_result(db_session, _source(), seed, sync_run_id)

    # Допустим, fetcher получил свежий md5_sidecar, но затем главный GET упал.
    err = _result(
        FetchStatus.ERROR,
        tier_used=None,
        md5_sidecar=MD5_NEW,
        error="main file https://...: HTTP 500",
    )
    await write_result(db_session, _source(), err, sync_run_id)

    row = await _row(db_session)
    assert row.last_status == "error"
    # Все payload — preserved, в т.ч. md5 даже если sidecar был свежим.
    assert row.last_md5 == MD5_OLD
    assert row.last_etag == ETAG_OLD
    assert row.last_sha256 == SHA_OLD
    assert row.last_size == 100


async def test_mark_parsed_updates_only_last_parsed_at(
    db_session: AsyncSession, sync_run_id: int
) -> None:
    """mark_parsed обновляет last_parsed_at, ничего больше не трогает."""
    seed = _result(
        FetchStatus.NEW,
        tier_used=3,
        md5_sidecar=MD5_NEW,
        etag=ETAG_NEW,
        last_modified=LM_NEW,
        content_sha256=SHA_NEW,
        size_bytes=999,
    )
    await write_result(db_session, _source(), seed, sync_run_id)
    before = await _row(db_session)
    assert before.last_parsed_at is None  # write_result его не трогает.

    parsed_at = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    await mark_parsed(db_session, URL, parsed_at)

    after = await _row(db_session)
    assert after.last_parsed_at == parsed_at
    assert after.last_md5 == before.last_md5
    assert after.last_etag == before.last_etag
    assert after.last_status == before.last_status
    assert after.last_fetched_at == before.last_fetched_at


# ---------------------------------------------------------------------------
# Unit-тесты HTTP-date helpers (без БД).
# ---------------------------------------------------------------------------


def test_http_date_round_trip() -> None:
    """str → datetime → str даёт тот же RFC 7231 IMF-fixdate."""
    # 1 Jan 2026 — действительно четверг; иначе format_datetime
    # пересчитает имя дня и round-trip разойдётся (что само по себе
    # корректное поведение, но в тесте нужна стабильность).
    original = "Thu, 01 Jan 2026 12:00:00 GMT"
    dt = _http_date_to_datetime(original)
    assert dt is not None
    assert dt.tzinfo is not None
    assert _datetime_to_http_date(dt) == original


def test_http_date_malformed_returns_none() -> None:
    """Кривые/пустые входы → None, без исключений."""
    assert _http_date_to_datetime(None) is None
    assert _http_date_to_datetime("") is None
    assert _http_date_to_datetime("not-a-date") is None
    assert _datetime_to_http_date(None) is None
