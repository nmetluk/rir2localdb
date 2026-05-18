"""Prometheus ``/v1/metrics`` — exposition format + DB-derived gauges + middleware.

Запросы через ``api_client`` (ASGITransport), seed через ``clean_db``
(autocommit asyncpg — API видит committed данные).
"""

from __future__ import annotations

import asyncpg
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_metrics_endpoint_returns_prometheus_format(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    response = await api_client.get("/v1/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    # Обязательные элементы Prometheus exposition format.
    assert "# HELP rir2localdb_table_rows" in body
    assert "# TYPE rir2localdb_table_rows gauge" in body
    assert "# TYPE rir2localdb_http_requests_total counter" in body
    assert "# TYPE rir2localdb_http_request_duration_seconds histogram" in body


async def test_metrics_includes_table_counts_via_pg_class(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """``rir2localdb_table_rows`` появляется для tracked-таблиц после ANALYZE."""
    # ANALYZE обновляет pg_class.reltuples — без него reltuples=0 для свежей БД.
    await clean_db.execute("ANALYZE inetnum")
    await clean_db.execute("ANALYZE ip_allocation")

    response = await api_client.get("/v1/metrics")
    body = response.text
    # Хотя бы один label для inetnum / ip_allocation должен присутствовать
    # (даже если value=0, gauge сам зарегистрирован).
    assert 'rir2localdb_table_rows{table="inetnum"}' in body
    assert 'rir2localdb_table_rows{table="ip_allocation"}' in body


async def test_metrics_records_http_request_counter(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """Middleware считает запросы (counter инкрементируется на каждый non-/metrics)."""
    await api_client.get("/v1/healthz")
    response = await api_client.get("/v1/metrics")
    body = response.text

    # Hit-counter для /v1/healthz должен быть >=1.
    matches = [
        line
        for line in body.splitlines()
        if line.startswith("rir2localdb_http_requests_total{")
        and "/v1/healthz" in line
        and not line.startswith("#")
    ]
    assert matches, "no http_requests_total{endpoint=/v1/healthz} line"
    # Парсим значение в конце строки.
    value = float(matches[0].rsplit(" ", 1)[1])
    assert value >= 1.0


async def test_metrics_endpoint_itself_not_counted(
    api_client: AsyncClient, clean_db: asyncpg.Connection
) -> None:
    """Scrape ``/v1/metrics`` НЕ инкрементит ``http_requests_total`` —
    иначе на каждый Prometheus scrape счётчик растёт «сам от себя»."""

    def _parse(body: str, labels_match: str) -> float:
        for line in body.splitlines():
            if (
                line.startswith("rir2localdb_http_requests_total{")
                and labels_match in line
                and not line.startswith("#")
            ):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    r1 = await api_client.get("/v1/metrics")
    await api_client.get("/v1/metrics")
    await api_client.get("/v1/metrics")
    r_final = await api_client.get("/v1/metrics")

    initial = _parse(r1.text, 'endpoint="/v1/metrics"')
    final = _parse(r_final.text, 'endpoint="/v1/metrics"')
    assert initial == final, "metrics endpoint должен быть само-исключающимся"
