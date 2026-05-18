"""Сценарии ``sync.orchestrator.run_sync`` — 5 кейсов.

Идиома: orchestrator открывает СВОЙ engine + транзакцию и
**коммитит** их. Поэтому здесь не используется ``pg_conn``-фикстура
(она с rollback-в-teardown и не увидит коммиченных данных).

Вместо неё — фикстура ``clean_db``: отдельный asyncpg-conn для
verification, который ``TRUNCATE … RESTART IDENTITY CASCADE`` все
state-таблицы перед и после теста. Получаем чистую изоляцию ценой
двух TRUNCATE-вызовов.

HTTP-слой мокается через ``httpx.MockTransport`` (как в test_fetcher.py),
``sources_for_tiers`` патчится monkeypatch'ем — каждый тест задаёт
свой набор Source'ов, не итерирует реальные 5 RIR из ``CORE_SOURCES``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest

from rir2localdb.config import Settings
from rir2localdb.sources import Format, Rir, Source, Tier
from rir2localdb.sync.orchestrator import run_sync

# ---------------------------------------------------------------------------
# Common fixtures / helpers
# ---------------------------------------------------------------------------

_BODY_ONE_IPV4 = b"""\
2|ripencc|20260518|1|19920901|20260517|+0200
ripencc|*|asn|*|1|summary
ripencc|*|ipv4|*|1|summary
ripencc|*|ipv6|*|1|summary
ripencc|DE|ipv4|2.0.0.0|65536|20100712|allocated|A91|e-stats
"""


def _make_source(
    *,
    url: str,
    rir: Rir = Rir.RIPE,
    with_md5: bool = True,
) -> Source:
    return Source(
        rir=rir,
        tier=Tier.CORE,
        format=Format.DELEGATED,
        url=url,
        md5_url=url + ".md5" if with_md5 else None,
        description="test source",
    )


@pytest.fixture
def orchestrator_settings(test_database_url: str, tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=test_database_url,
        data_dir=tmp_path,
        http_timeout=5.0,
        http_max_connections=2,
        http_retries=2,
    )


@pytest.fixture
def patch_http(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Callable[[httpx.Request], httpx.Response]], None]]:
    """Подменить ``make_http_client`` orchestrator'а на MockTransport-обёрнутый клиент."""

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def fake_factory(settings: Settings) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr("rir2localdb.sync.orchestrator.make_http_client", fake_factory)

    yield install


@pytest.fixture
def patch_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[list[Source]], None]]:
    """Подменить ``sources_for_tiers`` orchestrator'а на возврат фиксированного списка."""

    def install(sources: list[Source]) -> None:
        def fake_for_tiers(_tiers: object) -> tuple[Source, ...]:
            return tuple(sources)

        monkeypatch.setattr("rir2localdb.sync.orchestrator.sources_for_tiers", fake_for_tiers)

    yield install


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_happy_path_one_source_new(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    source = _make_source(url="https://test.example.invalid/ripencc/delegated")
    patch_sources([source])

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith(".md5"):
            return httpx.Response(404)
        return httpx.Response(200, content=_BODY_ONE_IPV4)

    patch_http(handler)

    summary = await run_sync([Tier.CORE], orchestrator_settings)

    assert summary.status == "success"
    assert summary.files_total == 1
    assert summary.files_fetched_new == 1
    assert summary.files_errored == 0
    assert summary.parser_records_total == 1
    assert summary.etl_ip_inserted == 1
    assert summary.error is None

    rows = await clean_db.fetch("SELECT rir, family, value FROM ip_allocation")
    assert len(rows) == 1
    assert rows[0]["rir"] == "ripencc"
    assert rows[0]["family"] == 4
    assert rows[0]["value"] == 65536

    sync_run_status = await clean_db.fetchval(
        "SELECT status FROM sync_run WHERE id=$1", summary.run_id
    )
    assert sync_run_status == "success"


async def test_unchanged_skips_etl(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    source = _make_source(url="https://test.example.invalid/ripencc/delegated", with_md5=False)
    patch_sources([source])

    # Pre-populate sync_file так, чтобы fetch получил previous.last_etag
    # и отправил If-None-Match. На 304 → UNCHANGED → ETL не вызывается.
    seed_run_id = await clean_db.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'success') RETURNING id"
    )
    await clean_db.execute(
        """
        INSERT INTO sync_file
            (url, rir, tier, kind, last_run_id, last_status,
             last_etag, last_fetched_at)
        VALUES ($1, 'ripencc', 'core', 'delegated', $2, 'updated',
                '"old-etag"', now())
        """,
        source.url,
        seed_run_id,
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    patch_http(handler)

    summary = await run_sync([Tier.CORE], orchestrator_settings)

    assert summary.status == "success"
    assert summary.files_unchanged == 1
    assert summary.files_fetched_new == 0
    assert summary.files_fetched_updated == 0
    assert summary.etl_ip_inserted == 0
    assert summary.parser_records_total == 0

    ip_count = await clean_db.fetchval("SELECT COUNT(*) FROM ip_allocation")
    assert ip_count == 0


async def test_one_source_error_others_succeed(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    s_fail = _make_source(url="https://test.example.invalid/fail/delegated", rir=Rir.RIPE)
    s_ok = _make_source(url="https://test.example.invalid/ok/delegated", rir=Rir.APNIC)
    patch_sources([s_fail, s_ok])

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "fail" in url:
            return httpx.Response(500)
        if url.endswith(".md5"):
            return httpx.Response(404)
        return httpx.Response(200, content=_BODY_ONE_IPV4)

    patch_http(handler)

    summary = await run_sync([Tier.CORE], orchestrator_settings)

    assert summary.status == "success"  # not 'failed' — 1 из 2 успешен
    assert summary.files_total == 2
    assert summary.files_errored == 1
    assert summary.files_fetched_new == 1
    assert summary.etl_ip_inserted == 1
    assert summary.error is None


async def test_all_sources_error_marks_run_failed(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    s1 = _make_source(url="https://test.example.invalid/t1/d", rir=Rir.RIPE)
    s2 = _make_source(url="https://test.example.invalid/t2/d", rir=Rir.APNIC)
    patch_sources([s1, s2])

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    patch_http(handler)

    summary = await run_sync([Tier.CORE], orchestrator_settings)

    assert summary.status == "failed"
    assert summary.files_total == 2
    assert summary.files_errored == 2
    assert summary.error is not None
    assert "2" in summary.error

    sync_run_row = await clean_db.fetchrow(
        "SELECT status, error FROM sync_run WHERE id=$1", summary.run_id
    )
    assert sync_run_row["status"] == "failed"
    assert sync_run_row["error"] is not None


async def test_dry_run_no_db_writes(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    source = _make_source(url="https://test.example.invalid/t/d")
    patch_sources([source])

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith(".md5"):
            return httpx.Response(404)
        return httpx.Response(200, content=_BODY_ONE_IPV4)

    patch_http(handler)

    summary = await run_sync([Tier.CORE], orchestrator_settings, dry_run=True)

    # Summary отражает что было бы при «не dry-run».
    assert summary.files_fetched_new == 1
    assert summary.etl_ip_inserted == 1

    # …но БД пустая (rollback вернул всё, включая sync_run).
    ip_count = await clean_db.fetchval("SELECT COUNT(*) FROM ip_allocation")
    sync_run_count = await clean_db.fetchval("SELECT COUNT(*) FROM sync_run")
    sync_file_count = await clean_db.fetchval("SELECT COUNT(*) FROM sync_file")
    assert ip_count == 0
    assert sync_run_count == 0
    assert sync_file_count == 0
