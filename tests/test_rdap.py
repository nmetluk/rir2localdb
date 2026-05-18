"""RDAP fallback — Stage 3-05.

6 сценариев через ``httpx.MockTransport``:
1. cache hit (fresh) → no HTTP request.
2. cache miss / expired → fetch + store.
3. 404 → negative cache.
4. 429 → respects Retry-After.
5. ARIN-IP с RDAP enabled → rpsl блок заполнен.
6. RIPE-IP → RDAP не вызывается даже при rdap_enabled=true.
"""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.api.rdap import lookup_ip_rdap
from rir2localdb.config import Settings

pytestmark = pytest.mark.asyncio


_RDAP_SAMPLE_8888 = {
    "objectClassName": "ip network",
    "startAddress": "8.8.8.0",
    "endAddress": "8.8.8.255",
    "name": "GOGL",
    "type": "DIRECT ALLOCATION",
    "country": "US",
    "entities": [
        {
            "objectClassName": "entity",
            "handle": "GOGL",
            "roles": ["registrant"],
            "vcardArray": [
                "vcard",
                [
                    ["version", {}, "text", "4.0"],
                    ["fn", {}, "text", "Google LLC"],
                    ["org", {}, "text", "Google LLC"],
                ],
            ],
        }
    ],
    "events": [
        {"eventAction": "registration", "eventDate": "2014-03-14T19:59:55-04:00"},
        {"eventAction": "last changed", "eventDate": "2023-12-28T13:00:00-04:00"},
    ],
}


def _settings(rdap_enabled: bool = True) -> Settings:
    return Settings(  # type: ignore[call-arg]
        rdap_fallback_enabled=rdap_enabled,
        rdap_cache_ttl_hours=24,
        rdap_negative_cache_minutes=5,
    )


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 1-4: core lookup logic via db_session + mocked HTTP.
# ---------------------------------------------------------------------------


async def test_ip_lookup_hits_cache_when_fresh(db_session: AsyncSession) -> None:
    """Populate fresh cache → HTTP не вызывается."""
    await db_session.execute(
        text(
            "INSERT INTO rdap_cache "
            "(cache_key, response_raw, normalized, expires_at, http_status) "
            "VALUES ('ip:8.8.8.8', '{}'::jsonb, "
            "CAST(:n AS jsonb), now() + interval '1 hour', 200)"
        ),
        {"n": '{"inetnum": {"rir": "arin", "start": "8.8.8.0", "value": 256}}'},
    )

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP не должен был быть вызван, но был: {req.url}")

    async with _mock_client(handler) as client:
        result = await lookup_ip_rdap(db_session, client, "8.8.8.8", _settings())

    assert result.cached is True
    assert result.found is True
    assert result.normalized is not None
    assert result.normalized["inetnum"]["start"] == "8.8.8.0"


async def test_ip_lookup_fetches_when_cache_expired(db_session: AsyncSession) -> None:
    """``expires_at < now()`` → cache miss → HTTP fetch + store."""
    # Pre-populate expired entry.
    await db_session.execute(
        text(
            "INSERT INTO rdap_cache "
            "(cache_key, response_raw, normalized, expires_at, http_status) "
            "VALUES ('ip:8.8.8.8', '{}'::jsonb, '{}'::jsonb, "
            "now() - interval '1 hour', 200)"
        )
    )

    def handler(req: httpx.Request) -> httpx.Response:
        assert "/ip/8.8.8.8" in str(req.url)
        return httpx.Response(200, json=_RDAP_SAMPLE_8888)

    async with _mock_client(handler) as client:
        result = await lookup_ip_rdap(db_session, client, "8.8.8.8", _settings())

    assert result.cached is False
    assert result.found is True
    assert result.normalized is not None
    assert result.normalized["inetnum"]["netname"] == "GOGL"
    assert result.normalized["inetnum"]["country"] == "US"
    assert result.normalized["organisation"]["org_name"] == "Google LLC"


async def test_ip_lookup_handles_404(db_session: AsyncSession) -> None:
    """404 → negative cache с http_status=404, найден=False."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorCode": 404})

    async with _mock_client(handler) as client:
        result = await lookup_ip_rdap(db_session, client, "1.2.3.4", _settings())

    assert result.cached is False
    assert result.found is False
    assert result.http_status == 404

    # Проверим что закэшировалось.
    row = (
        await db_session.execute(
            text(
                "SELECT http_status, expires_at > now() AS fresh FROM rdap_cache WHERE cache_key='ip:1.2.3.4'"
            )
        )
    ).first()
    assert row is not None
    assert row.http_status == 404
    assert row.fresh is True


async def test_ip_lookup_handles_429_with_retry_after(db_session: AsyncSession) -> None:
    """429 + Retry-After: 600 → TTL >= 600s. Negative cache."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "600"}, content=b"slow down")

    async with _mock_client(handler) as client:
        result = await lookup_ip_rdap(db_session, client, "5.5.5.5", _settings())

    assert result.found is False
    assert result.http_status == 429

    expires_seconds = (
        await db_session.execute(
            text(
                "SELECT EXTRACT(epoch FROM (expires_at - now())) AS secs "
                "FROM rdap_cache WHERE cache_key='ip:5.5.5.5'"
            )
        )
    ).scalar_one()
    # default neg-cache = 5 min = 300s; Retry-After=600 → используем 600.
    assert float(expires_seconds) >= 300.0


# ---------------------------------------------------------------------------
# 5-6: full pipeline через api_client.
# ---------------------------------------------------------------------------


async def test_arin_ip_with_rdap_enabled_enriches_response(
    api_settings: Settings,
    clean_db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ARIN allocation + пустой inetnum + RDAP enabled →
    `/v1/ip/8.8.8.8` response.rpsl.inetnum заполнен из RDAP."""
    from httpx import ASGITransport, AsyncClient

    from rir2localdb.api.app import make_app

    # Seed только ip_allocation (без inetnum) для ARIN-блока.
    run_id = await clean_db.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'success') RETURNING id"
    )
    start_int = int.from_bytes(bytes(int(p) for p in ["8", "8", "8", "0"]), "big")
    await clean_db.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v4, start_text, value, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ('arin', 'US', 4, int8range($1, $2), '8.8.8.0', 256, 'allocated',
                NULL, $3, $3)
        """,
        start_int,
        start_int + 256,
        run_id,
    )

    settings = api_settings.model_copy(update={"rdap_fallback_enabled": True})
    app = make_app(settings)

    # Replace http_client с MockTransport — после lifespan startup'а.
    def mock_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_RDAP_SAMPLE_8888)

    async with app.router.lifespan_context(app):
        # Override http_client сразу после startup'а.
        await app.state.http_client.aclose()
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/ip/8.8.8.8")

        await app.state.http_client.aclose()

    assert response.status_code == 200
    data = response.json()
    assert data["rir"] == "arin"
    # RDAP fallback должен был заполнить inetnum.
    assert data["rpsl"]["inetnum"] is not None
    assert data["rpsl"]["inetnum"]["netname"] == "GOGL"
    assert data["rpsl"]["inetnum"]["source"] == "ARIN-RDAP"
    assert data["rpsl"]["organisation"] is not None
    assert data["rpsl"]["organisation"]["org_name"] == "Google LLC"


async def test_non_arin_ip_skips_rdap(
    api_settings: Settings,
    clean_db: asyncpg.Connection,
) -> None:
    """RIPE allocation + RDAP enabled → RDAP не вызывается (не наш RIR)."""
    from httpx import ASGITransport, AsyncClient

    from rir2localdb.api.app import make_app

    run_id = await clean_db.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'success') RETURNING id"
    )
    start_int = int.from_bytes(bytes(int(p) for p in ["193", "0", "0", "0"]), "big")
    await clean_db.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v4, start_text, value, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ('ripencc', 'NL', 4, int8range($1, $2), '193.0.0.0', 256, 'allocated',
                NULL, $3, $3)
        """,
        start_int,
        start_int + 256,
        run_id,
    )

    settings = api_settings.model_copy(update={"rdap_fallback_enabled": True})
    app = make_app(settings)

    rdap_called = False

    def mock_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal rdap_called
        rdap_called = True
        return httpx.Response(200, json=_RDAP_SAMPLE_8888)

    async with app.router.lifespan_context(app):
        await app.state.http_client.aclose()
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/v1/ip/193.0.0.5")

        await app.state.http_client.aclose()

    assert response.status_code == 200
    # RIPE-блок без bulk inetnum + RDAP не вызывался → rpsl.inetnum=None.
    assert response.json()["rpsl"]["inetnum"] is None
    assert rdap_called is False
