"""Сценарии для FastAPI ``/v1/*`` через ``httpx.ASGITransport``.

Реальный uvicorn не поднимаем — ASGITransport вызывает app
in-process. Lifespan триггерится вручную через
``app.router.lifespan_context(app)``: иначе lifespan-startup,
устанавливающий ``app.state.sessionmaker``, не сработает.

Pre-populate БД через ``clean_db`` (raw asyncpg, autocommit,
TRUNCATE-before/after). API-приложение получает свой engine
через ``test_settings.test_database_url`` → читает закоммиченные
данные.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rir2localdb.api.app import make_app
from rir2localdb.config import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_settings(test_database_url: str, tmp_path: Path) -> Settings:
    """``Settings`` для API-тестов — ``database_url`` смотрит на тестовую БД."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=test_database_url,
        data_dir=tmp_path,
        http_timeout=5.0,
        http_max_connections=2,
        http_retries=2,
    )


@pytest_asyncio.fixture
async def api_client(api_settings: Settings) -> AsyncIterator[AsyncClient]:
    """ASGI-клиент с поднятым lifespan'ом (startup → yield → shutdown)."""
    app = make_app(api_settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


# ---------------------------------------------------------------------------
# Helpers — pre-populate БД для lookup-тестов.
# ---------------------------------------------------------------------------


async def _seed_sync_run(conn: asyncpg.Connection) -> int:
    rid = await conn.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'success') RETURNING id"
    )
    return int(rid)


async def _seed_ipv4(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "arin",
    cc: str = "US",
    start: str = "8.0.0.0",
    value: int = 16777216,
) -> None:
    start_int = int.from_bytes(bytes(int(p) for p in start.split(".")), "big")
    await conn.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v4, start_text, value, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ($1, $2, 4, $3, $4, $5, 'allocated',
                $6, $7, $7)
        """,
        rir,
        cc,
        asyncpg.Range(start_int, start_int + value),
        start,
        value,
        date(1992, 12, 1),
        run_id,
    )


async def _seed_ipv6(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "arin",
    cc: str = "US",
    start: str = "2001:db8::",
    prefix: int = 32,
) -> None:
    import ipaddress

    start_int = int(ipaddress.IPv6Address(start))
    end_int = start_int + (1 << (128 - prefix))
    await conn.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v6, prefix_length, start_text, value, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ($1, $2, 6, $3, $4, $5, $6, 'allocated',
                $7, $8, $8)
        """,
        rir,
        cc,
        asyncpg.Range(Decimal(start_int), Decimal(end_int)),
        prefix,  # prefix_length (smallint)
        start,
        prefix,  # value (bigint) — для v6 семантически тот же prefix length
        date(1999, 7, 1),
        run_id,
    )


async def _seed_asn(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "arin",
    cc: str = "US",
    start_asn: int = 15169,
    count: int = 1,
) -> None:
    await conn.execute(
        """
        INSERT INTO asn_allocation
            (rir, cc, asn_range, start_asn, count, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ($1, $2, $3, $4, $5, 'allocated',
                $6, $7, $7)
        """,
        rir,
        cc,
        asyncpg.Range(start_asn, start_asn + count),
        start_asn,
        count,
        date(2000, 3, 8),
        run_id,
    )


# ---------------------------------------------------------------------------
# Helpers — pre-populate RPSL-таблиц для enrichment-тестов.
#
# JSONB колонки записываются через JSON-строку (asyncpg по умолчанию
# принимает str → JSONB). raw — минимальный {"<type>": [...]} dict для
# валидной JSONB-строки; реальные тесты ETL'а проверяют content roundtrip.
# ---------------------------------------------------------------------------


async def _seed_inetnum(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "ripe",
    start: str = "193.0.0.0",
    value: int = 256,
    netname: str | None = "RIPE-NCC",
    country: str | None = "NL",
    org: str | None = "ORG-RIEN1-RIPE",
    descr: str | None = "RIPE NCC",
    status: str | None = "ASSIGNED PA",
    admin_c: list[str] | None = None,
    tech_c: list[str] | None = None,
    mnt_by: list[str] | None = None,
    source: str | None = "RIPE",
) -> None:
    start_int = int.from_bytes(bytes(int(p) for p in start.split(".")), "big")
    await conn.execute(
        """
        INSERT INTO inetnum
            (rir, start_text, value, range_v4, netname, country, descr, org,
             admin_c, tech_c, status, mnt_by, source, raw,
             first_seen_run, last_seen_run)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                $14::jsonb, $15, $15)
        """,
        rir,
        start,
        value,
        asyncpg.Range(start_int, start_int + value),
        netname,
        country,
        descr,
        org,
        admin_c,
        tech_c,
        status,
        mnt_by,
        source,
        f'{{"inetnum": ["{start} - seed"]}}',
        run_id,
    )


async def _seed_inet6num(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "ripe",
    start: str = "2001:db8::",
    prefix: int = 32,
    netname: str | None = "TEST-NET-V6",
    country: str | None = "NL",
    org: str | None = "ORG-RIEN1-RIPE",
    source: str | None = "RIPE",
) -> None:
    import ipaddress

    start_int = int(ipaddress.IPv6Address(start))
    end_int = start_int + (1 << (128 - prefix))
    await conn.execute(
        """
        INSERT INTO inet6num
            (rir, start_text, value, range_v6, netname, country, org, source, raw,
             first_seen_run, last_seen_run)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $10)
        """,
        rir,
        start,
        prefix,
        asyncpg.Range(Decimal(start_int), Decimal(end_int)),
        netname,
        country,
        org,
        source,
        f'{{"inet6num": ["{start}/{prefix}"]}}',
        run_id,
    )


async def _seed_aut_num(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "ripe",
    asn: int = 3333,
    as_name: str | None = "RIPE-NCC-AS",
    descr: str | None = "RIPE NCC",
    org: str | None = "ORG-RIEN1-RIPE",
    status: str | None = None,
    source: str | None = "RIPE",
) -> None:
    await conn.execute(
        """
        INSERT INTO aut_num
            (rir, asn, as_name, descr, org, status, source, raw,
             first_seen_run, last_seen_run)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $9)
        """,
        rir,
        asn,
        as_name,
        descr,
        org,
        status,
        source,
        f'{{"aut-num": ["AS{asn}"]}}',
        run_id,
    )


async def _seed_organisation(
    conn: asyncpg.Connection,
    run_id: int,
    *,
    rir: str = "ripe",
    org_handle: str = "ORG-RIEN1-RIPE",
    org_name: str | None = "Reseaux IP Europeens Network Coordination Centre",
    org_type: str | None = "RIR",
    abuse_c: str | None = "AR17615-RIPE",
    address: list[str] | None = None,
    phone: list[str] | None = None,
    email: list[str] | None = None,
    source: str | None = "RIPE",
) -> None:
    await conn.execute(
        """
        INSERT INTO organisation
            (rir, org_handle, org_name, org_type, abuse_c,
             address, phone, email, source, raw,
             first_seen_run, last_seen_run)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $11)
        """,
        rir,
        org_handle,
        org_name,
        org_type,
        abuse_c,
        address,
        phone,
        email,
        source,
        f'{{"organisation": ["{org_handle}"]}}',
        run_id,
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_healthz(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    response = await api_client.get("/v1/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    response = await api_client.get("/v1/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readyz_db_down(api_settings: Settings) -> None:
    """Если sessionmaker сломан / БД недоступна — 503."""

    class _BrokenSessionmaker:
        def __call__(self) -> object:
            raise RuntimeError("simulated DB down")

    app = make_app(api_settings)
    async with app.router.lifespan_context(app):
        # Подменяем sessionmaker после startup'а на сломанный.
        app.state.sessionmaker = _BrokenSessionmaker()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/readyz")
    assert response.status_code == 503
    assert "simulated DB down" in response.json()["detail"]


async def test_lookup_ipv4_hit(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="arin", cc="US", start="8.0.0.0", value=16777216)

    response = await api_client.get("/v1/ip/8.0.0.5")
    assert response.status_code == 200
    data = response.json()
    assert data["address"] == "8.0.0.5"
    assert data["family"] == 4
    assert data["rir"] == "arin"
    assert data["cc"] == "US"
    assert data["start"] == "8.0.0.0"
    assert data["value"] == 16777216
    assert data["prefix_length"] is None
    assert data["status"] == "allocated"
    assert data["allocated_on"] == "1992-12-01"
    assert data["first_seen_run"] == run_id
    assert data["last_seen_run"] == run_id


async def test_lookup_ipv4_miss(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    response = await api_client.get("/v1/ip/8.8.8.8")
    assert response.status_code == 404
    assert "no allocation found" in response.json()["detail"]


async def test_lookup_ipv4_invalid_address(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    response = await api_client.get("/v1/ip/not-an-ip")
    assert response.status_code == 400
    assert "invalid IP address" in response.json()["detail"]


async def test_lookup_ipv6_hit(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv6(clean_db, run_id, rir="ripencc", cc="NL", start="2001:db8::", prefix=32)

    response = await api_client.get("/v1/ip/2001:db8::1")
    assert response.status_code == 200
    data = response.json()
    assert data["family"] == 6
    assert data["rir"] == "ripencc"
    assert data["cc"] == "NL"
    assert data["start"] == "2001:db8::"
    assert data["value"] == 32
    assert data["prefix_length"] == 32


async def test_lookup_ipv6_miss(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    response = await api_client.get("/v1/ip/2001:db8::1")
    assert response.status_code == 404


async def test_lookup_asn_hit(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    run_id = await _seed_sync_run(clean_db)
    await _seed_asn(clean_db, run_id, rir="arin", cc="US", start_asn=15169, count=1)

    response = await api_client.get("/v1/asn/15169")
    assert response.status_code == 200
    data = response.json()
    assert data["asn"] == 15169
    assert data["rir"] == "arin"
    assert data["start_asn"] == 15169
    assert data["count"] == 1


async def test_lookup_asn_miss(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    response = await api_client.get("/v1/asn/15169")
    assert response.status_code == 404


async def test_lookup_asn_invalid_negative(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    response = await api_client.get("/v1/asn/-1")
    # FastAPI/Pydantic вернёт 422 на отрицательное число; либо наш explicit
    # check вернёт 400. Принимаем оба варианта.
    assert response.status_code in (400, 422)


async def test_status_with_data(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    run_id = await _seed_sync_run(clean_db)
    await clean_db.execute(
        """
        INSERT INTO sync_file
            (url, rir, tier, kind, last_run_id, last_status, last_fetched_at)
        VALUES ($1, 'ripencc', 'core', 'delegated', $2, 'updated', now())
        """,
        "https://example.test/file",
        run_id,
    )

    response = await api_client.get("/v1/status")
    assert response.status_code == 200
    data = response.json()
    assert data["db_alive"] is True
    assert data["latest_sync_run"] is not None
    assert data["latest_sync_run"]["id"] == run_id
    assert data["latest_sync_run"]["tier"] == "core"
    assert len(data["sources"]) == 1
    assert data["sources"][0]["rir"] == "ripencc"


async def test_status_empty_db(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    response = await api_client.get("/v1/status")
    assert response.status_code == 200
    data = response.json()
    assert data["db_alive"] is True
    assert data["latest_sync_run"] is None
    assert data["sources"] == []
    assert data["summary_by_rir"] == []


async def test_status_includes_per_rir_summary(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """После populate с двух разных RIR summary_by_rir содержит две записи."""
    run_id = await _seed_sync_run(clean_db)
    # ARIN: 1 ipv4 + 1 asn.
    await _seed_ipv4(clean_db, run_id, rir="arin", start="8.0.0.0", value=16777216)
    await _seed_asn(clean_db, run_id, rir="arin", start_asn=15169, count=1)
    # RIPE: 1 ipv4.
    await _seed_ipv4(clean_db, run_id, rir="ripencc", start="2.0.0.0", value=65536)
    # sync_file для двух RIR'ов.
    await clean_db.execute(
        """
        INSERT INTO sync_file
            (url, rir, tier, kind, last_run_id, last_status, last_fetched_at)
        VALUES
            ('https://t.test/arin', 'arin', 'core', 'delegated', $1, 'updated', now()),
            ('https://t.test/ripe', 'ripencc', 'core', 'delegated', $1, 'new', now())
        """,
        run_id,
    )

    response = await api_client.get("/v1/status")
    assert response.status_code == 200
    summary = {r["rir"]: r for r in response.json()["summary_by_rir"]}
    assert set(summary) == {"arin", "ripencc"}
    assert summary["arin"]["ip_allocations"] == 1
    assert summary["arin"]["asn_allocations"] == 1
    assert summary["ripencc"]["ip_allocations"] == 1
    assert summary["ripencc"]["asn_allocations"] == 0
    assert summary["arin"]["last_fetched_at"] is not None
    assert summary["ripencc"]["last_fetched_at"] is not None


async def test_ip_overlap_returns_most_specific(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """Две overlapping записи (/8 и /16) → query 10.1.2.3 находит /16."""
    run_id = await _seed_sync_run(clean_db)
    # 10.0.0.0/8 — широкая запись
    await _seed_ipv4(clean_db, run_id, rir="apnic", cc="JP", start="10.0.0.0", value=16777216)
    # 10.1.0.0/16 — узкая, внутри /8
    await _seed_ipv4(clean_db, run_id, rir="ripencc", cc="DE", start="10.1.0.0", value=65536)

    response = await api_client.get("/v1/ip/10.1.2.3")
    assert response.status_code == 200
    data = response.json()
    assert data["start"] == "10.1.0.0"
    assert data["value"] == 65536  # /16, не /8
    assert data["rir"] == "ripencc"


# ---------------------------------------------------------------------------
# Stage 2: RPSL enrichment.
# ---------------------------------------------------------------------------


async def test_ip_lookup_with_rpsl_inetnum_only(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """inetnum есть, organisation отсутствует → rpsl.inetnum != None,
    rpsl.organisation == None."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    await _seed_inetnum(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    # без organisation

    response = await api_client.get("/v1/ip/193.0.0.5")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"] is not None
    assert data["rpsl"]["inetnum"] is not None
    assert data["rpsl"]["inetnum"]["netname"] == "RIPE-NCC"
    assert data["rpsl"]["organisation"] is None  # orphan org-handle


async def test_ip_lookup_with_rpsl_full(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """inetnum + organisation → оба заполнены."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    await _seed_inetnum(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    await _seed_organisation(clean_db, run_id, rir="ripe", org_handle="ORG-RIEN1-RIPE")

    response = await api_client.get("/v1/ip/193.0.0.5")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"]["inetnum"]["netname"] == "RIPE-NCC"
    assert data["rpsl"]["organisation"] is not None
    assert data["rpsl"]["organisation"]["org_handle"] == "ORG-RIEN1-RIPE"
    assert "Reseaux" in data["rpsl"]["organisation"]["org_name"]
    assert data["rpsl"]["organisation"]["abuse_c"] == "AR17615-RIPE"


async def test_ip_lookup_inet6num(api_client: AsyncClient, clean_db: asyncpg.Connection) -> None:
    """IPv6 lookup попадает в inet6num, value=prefix_length."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv6(clean_db, run_id, rir="ripe", start="2001:db8::", prefix=32)
    await _seed_inet6num(clean_db, run_id, rir="ripe", start="2001:db8::", prefix=32)

    response = await api_client.get("/v1/ip/2001:db8::1")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"]["inetnum"] is not None  # union включает inet6num
    assert data["rpsl"]["inetnum"]["start"] == "2001:db8::"
    assert data["rpsl"]["inetnum"]["value"] == 32
    assert data["rpsl"]["inetnum"]["netname"] == "TEST-NET-V6"


async def test_ip_lookup_no_rpsl_data(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """ip_allocation есть, RPSL-таблицы пустые → rpsl != None, но оба поля None."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="arin", start="8.0.0.0", value=16777216)
    # никаких inetnum/organisation

    response = await api_client.get("/v1/ip/8.0.0.5")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"] is not None
    assert data["rpsl"]["inetnum"] is None
    assert data["rpsl"]["organisation"] is None


async def test_ip_lookup_include_rpsl_false(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """?include_rpsl=false → rpsl целиком null, даже когда данные есть."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    await _seed_inetnum(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    await _seed_organisation(clean_db, run_id, rir="ripe", org_handle="ORG-RIEN1-RIPE")

    response = await api_client.get("/v1/ip/193.0.0.5?include_rpsl=false")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"] is None


async def test_asn_lookup_with_rpsl_aut_num_and_org(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """AS3333 → aut_num + organisation в rpsl."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_asn(clean_db, run_id, rir="ripe", start_asn=3333, count=1)
    await _seed_aut_num(clean_db, run_id, rir="ripe", asn=3333)
    await _seed_organisation(clean_db, run_id, rir="ripe", org_handle="ORG-RIEN1-RIPE")

    response = await api_client.get("/v1/asn/3333")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"]["aut_num"] is not None
    assert data["rpsl"]["aut_num"]["as_name"] == "RIPE-NCC-AS"
    assert data["rpsl"]["organisation"] is not None
    assert "Reseaux" in data["rpsl"]["organisation"]["org_name"]


async def test_ip_lookup_picks_most_specific_inetnum(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """Два overlapping inetnum: /16 и /24 — отвечаем /24."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="ripe", start="193.0.0.0", value=65536)  # /16
    await _seed_inetnum(
        clean_db, run_id, rir="ripe", start="193.0.0.0", value=65536, netname="RIPE-WIDE"
    )
    await _seed_inetnum(
        clean_db, run_id, rir="ripe", start="193.0.0.0", value=256, netname="RIPE-NARROW"
    )

    response = await api_client.get("/v1/ip/193.0.0.5")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"]["inetnum"]["netname"] == "RIPE-NARROW"
    assert data["rpsl"]["inetnum"]["value"] == 256


async def test_ip_lookup_orphan_org_handle(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """inetnum.org указывает на org_handle, которого нет в organisation."""
    run_id = await _seed_sync_run(clean_db)
    await _seed_ipv4(clean_db, run_id, rir="ripe", start="193.0.0.0", value=256)
    await _seed_inetnum(
        clean_db, run_id, rir="ripe", start="193.0.0.0", value=256, org="ORG-NONEXISTENT-RIPE"
    )
    # никакой organisation

    response = await api_client.get("/v1/ip/193.0.0.5")
    assert response.status_code == 200
    data = response.json()
    assert data["rpsl"]["inetnum"] is not None
    assert data["rpsl"]["inetnum"]["org"] == "ORG-NONEXISTENT-RIPE"
    assert data["rpsl"]["organisation"] is None
