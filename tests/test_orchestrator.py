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

_BODY_RPSL_INETNUM_AND_ORG = b"""\
inetnum:        193.0.0.0 - 193.0.0.255
netname:        RIPE-NCC
country:        NL
org:            ORG-RIEN1-RIPE
status:         ASSIGNED PA
source:         RIPE

organisation:   ORG-RIEN1-RIPE
org-name:       RIPE Network Coordination Centre
org-type:       RIR
source:         RIPE
"""

_BODY_RPSL_ROUTE = b"""\
route:          193.0.0.0/24
origin:         AS3333
descr:          RIPE NCC
source:         ARIN
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


def _make_rpsl_source(
    *,
    url: str,
    rir: Rir = Rir.RIPE,
    tier: Tier = Tier.RICH,
    fmt: Format = Format.RPSL_SPLIT_GZ,
) -> Source:
    """RPSL source без md5 (RIPE/APNIC RPSL-дампы их не публикуют)."""
    return Source(
        rir=rir,
        tier=tier,
        format=fmt,
        url=url,
        md5_url=None,
        description="test rpsl source",
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


# ---------------------------------------------------------------------------
# Stage 2-05: RPSL routing в orchestrator.
# ---------------------------------------------------------------------------


async def test_rich_tier_routes_to_rpsl_etl(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    """RPSL-источник пропускается через ``parse_rpsl`` + ``apply_rpsl_etl``.

    Body содержит inetnum + organisation → обе таблицы заполнены,
    summary имеет etl_rpsl_records_total=2 и etl_rpsl_by_type для обеих.
    """
    source = _make_rpsl_source(
        url="https://test.example.invalid/ripe.db.inetnum.utf8.gz", rir=Rir.RIPE
    )
    patch_sources([source])

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_BODY_RPSL_INETNUM_AND_ORG)

    patch_http(handler)

    summary = await run_sync([Tier.RICH], orchestrator_settings)

    assert summary.status == "success"
    assert summary.files_fetched_new == 1
    assert summary.etl_rpsl_records_total == 2
    assert summary.etl_rpsl_by_type == {
        "inetnum": {"inserted": 1, "updated": 0},
        "organisation": {"inserted": 1, "updated": 0},
    }
    # delegated-счётчики нулевые
    assert summary.etl_ip_inserted == 0
    assert summary.etl_asn_inserted == 0

    inetnum_rows = await clean_db.fetch("SELECT rir, netname FROM inetnum")
    assert len(inetnum_rows) == 1
    assert inetnum_rows[0]["rir"] == "ripe"
    assert inetnum_rows[0]["netname"] == "RIPE-NCC"

    org_rows = await clean_db.fetch("SELECT org_handle, org_name FROM organisation")
    assert len(org_rows) == 1
    assert org_rows[0]["org_handle"] == "ORG-RIEN1-RIPE"


async def test_mixed_core_and_rich_tiers(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    """Оба ETL-пути в одном run'е."""
    core_source = _make_source(url="https://test.example.invalid/delegated", rir=Rir.RIPE)
    rich_source = _make_rpsl_source(
        url="https://test.example.invalid/ripe.db.inetnum.gz", rir=Rir.RIPE
    )
    patch_sources([core_source, rich_source])

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith(".md5"):
            return httpx.Response(404)
        if "delegated" in url:
            return httpx.Response(200, content=_BODY_ONE_IPV4)
        return httpx.Response(200, content=_BODY_RPSL_INETNUM_AND_ORG)

    patch_http(handler)

    summary = await run_sync([Tier.CORE, Tier.RICH], orchestrator_settings)

    assert summary.status == "success"
    assert summary.files_total == 2
    # delegated path:
    assert summary.etl_ip_inserted == 1
    # rpsl path:
    assert summary.etl_rpsl_records_total == 2
    assert "inetnum" in summary.etl_rpsl_by_type
    assert "organisation" in summary.etl_rpsl_by_type

    ip_count = await clean_db.fetchval("SELECT COUNT(*) FROM ip_allocation")
    inetnum_count = await clean_db.fetchval("SELECT COUNT(*) FROM inetnum")
    assert ip_count == 1
    assert inetnum_count == 1


async def test_arin_irr_routes_to_rpsl_etl(
    clean_db: asyncpg.Connection,
    orchestrator_settings: Settings,
    patch_http: Callable[[Callable[[httpx.Request], httpx.Response]], None],
    patch_sources: Callable[[list[Source]], None],
) -> None:
    """ARIN IRR (``Tier.ARIN_RR``, format ``RPSL_GZ``) → route table."""
    source = _make_rpsl_source(
        url="https://test.example.invalid/arin.db.gz",
        rir=Rir.ARIN,
        tier=Tier.ARIN_RR,
        fmt=Format.RPSL_GZ,
    )
    patch_sources([source])

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_BODY_RPSL_ROUTE)

    patch_http(handler)

    summary = await run_sync([Tier.ARIN_RR], orchestrator_settings)

    assert summary.status == "success"
    assert summary.etl_rpsl_by_type["route"] == {"inserted": 1, "updated": 0}

    route_rows = await clean_db.fetch("SELECT rir, prefix, origin FROM route")
    assert len(route_rows) == 1
    assert route_rows[0]["rir"] == "arin"
    assert str(route_rows[0]["prefix"]) == "193.0.0.0/24"
    assert route_rows[0]["origin"] == "AS3333"


def test_sources_for_tiers_expands_rich_to_include_arin_rr() -> None:
    """``Tier.RICH`` в запросе автоматически тянет ``Tier.ARIN_RR``."""
    from rir2localdb.sources import ARIN_RR_SOURCES, RICH_SOURCES, sources_for_tiers

    result = sources_for_tiers({Tier.RICH})
    result_urls = {s.url for s in result}

    for s in RICH_SOURCES:
        assert s.url in result_urls, f"RICH source missing: {s.url}"
    for s in ARIN_RR_SOURCES:
        assert s.url in result_urls, f"ARIN_RR auto-include failed: {s.url}"

    # ``Tier.ARIN_RR`` сам по себе НЕ тянет RICH — обратная сторона
    # автоэкспанзии.
    arin_only = sources_for_tiers({Tier.ARIN_RR})
    arin_only_urls = {s.url for s in arin_only}
    for s in ARIN_RR_SOURCES:
        assert s.url in arin_only_urls
    for s in RICH_SOURCES:
        assert s.url not in arin_only_urls
