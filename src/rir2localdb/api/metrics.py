"""Prometheus ``/v1/metrics`` endpoint.

Stage 3-02. Экспонирует metrics в Prometheus exposition format
(`text/plain; version=0.0.4`):

- ``rir2localdb_last_sync_run_*`` — gauges о последнем sync_run.
- ``rir2localdb_table_rows`` — approximate row counts через
  ``pg_class.reltuples`` (sub-millisecond, обновляется ``ANALYZE``'ом).
  Альтернатива ``SELECT COUNT(*)`` дала бы точные числа, но на 5M+
  rows стоила бы пары секунд на каждом scrape.
- ``rir2localdb_source_last_fetched_timestamp_seconds`` — freshness
  per-source (rir/kind/url labels). Cardinality ограничена 29
  источниками (см. ``sources.py``), explosion не случится.
- ``rir2localdb_http_requests_total`` / ``..._duration_seconds`` —
  HTTP middleware считает (в ``api/app.py``).

``collect_db_metrics`` вызывается **на каждом scrape**. Prometheus
обычно scrape'ит каждые 15-60 сек, и pg_class-запрос — single
sub-ms.

Контракт CONTENT_TYPE = ``CONTENT_TYPE_LATEST`` (``text/plain;
version=0.0.4; charset=utf-8``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


# Module-level registry. ``prometheus_client.REGISTRY`` (default global)
# не используем — он не дружит с тестами (state leak между test cases).
# Свой registry даёт явный reset point.
registry = CollectorRegistry()


# ---------------------------------------------------------------------------
# Sync-run metrics.
# ---------------------------------------------------------------------------

last_sync_run_finished_timestamp = Gauge(
    "rir2localdb_last_sync_run_finished_timestamp_seconds",
    "Unix timestamp of the last finished sync_run (success or failed)",
    registry=registry,
)

last_sync_run_duration_seconds = Gauge(
    "rir2localdb_last_sync_run_duration_seconds",
    "Duration of the last finished sync_run in seconds",
    registry=registry,
)

last_sync_run_status = Gauge(
    "rir2localdb_last_sync_run_status",
    "Status of last sync_run: 1=success, 0=failed, -1=running/unknown",
    registry=registry,
)


# ---------------------------------------------------------------------------
# Per-table row counts (approximation via pg_class.reltuples).
# ---------------------------------------------------------------------------

table_rows = Gauge(
    "rir2localdb_table_rows",
    "Approximate number of rows in each rir2localdb table (from pg_class.reltuples)",
    ["table"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# Source freshness — last_fetched_at per (rir, kind, url).
# ---------------------------------------------------------------------------

source_last_fetched_timestamp = Gauge(
    "rir2localdb_source_last_fetched_timestamp_seconds",
    "Unix timestamp of last fetched_at per source",
    ["rir", "kind", "url"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# GC / stale records (Stage 3-03).
# ---------------------------------------------------------------------------

stale_records = Gauge(
    "rir2localdb_stale_records",
    "Number of records currently marked as stale by GC (per table)",
    ["table"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# RDAP fallback (Stage 3-05).
# ---------------------------------------------------------------------------

rdap_lookups_total = Counter(
    "rir2localdb_rdap_lookups_total",
    "RDAP lookup attempts: ip|asn × hit|miss × found|notfound",
    ["kind", "cached", "found"],
    registry=registry,
)

rdap_cache_entries = Gauge(
    "rir2localdb_rdap_cache_entries",
    "Number of entries in rdap_cache, split by expiration status",
    ["status"],  # active / expired
    registry=registry,
)


# ---------------------------------------------------------------------------
# HTTP API metrics — incremented by middleware in api/app.py.
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "rir2localdb_http_requests_total",
    "Total HTTP requests to API",
    ["method", "endpoint", "status"],
    registry=registry,
)

http_request_duration_seconds = Histogram(
    "rir2localdb_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=registry,
)


# Целевые таблицы для row-count gauge. Жёстко зашиты, чтобы не делать
# pg_catalog scan по всему schema'е и не ловить ANALYZE'ом юзерские
# таблицы (если кто-то добавит).
_TRACKED_TABLES: tuple[str, ...] = (
    "sync_run",
    "sync_file",
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


async def collect_db_metrics(session: AsyncSession) -> None:
    """Заполнить все DB-derived gauge'ы из текущего состояния БД.

    На каждом ``/v1/metrics`` scrape — 3 SQL-запроса:

    1. ``SELECT * FROM sync_run ORDER BY id DESC LIMIT 1`` —
       last finished/running run, заполняет ``last_sync_run_*`` gauges.
    2. ``SELECT relname, reltuples FROM pg_class WHERE relkind='r'
       AND relname = ANY(...)`` — approximate row counts.
    3. ``SELECT rir, kind, url, last_fetched_at FROM sync_file`` —
       source freshness.

    Все запросы быстрые (single pg_class scan, sync_file ≤29 строк,
    sync_run LIMIT 1). Не блокирует другие транзакции.
    """
    # 1. last sync_run
    last_run = (
        (
            await session.execute(
                text(
                    "SELECT status, started_at, finished_at FROM sync_run ORDER BY id DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .first()
    )
    if last_run is not None:
        status = last_run["status"]
        if status == "success":
            last_sync_run_status.set(1)
        elif status == "failed":
            last_sync_run_status.set(0)
        else:
            last_sync_run_status.set(-1)
        finished = last_run["finished_at"]
        started = last_run["started_at"]
        if finished is not None:
            last_sync_run_finished_timestamp.set(finished.timestamp())
        if finished is not None and started is not None:
            last_sync_run_duration_seconds.set((finished - started).total_seconds())

    # 2. table rows (approximate via pg_class.reltuples)
    rows = (
        await session.execute(
            text(
                "SELECT relname, reltuples::bigint AS rowcount "
                "FROM pg_class WHERE relkind = 'r' AND relname = ANY(:tables)"
            ),
            {"tables": list(_TRACKED_TABLES)},
        )
    ).mappings()
    for row in rows:
        table_rows.labels(table=row["relname"]).set(max(row["rowcount"], 0))

    # 3. source last_fetched_at
    sources = (
        await session.execute(
            text(
                "SELECT rir, kind, url, last_fetched_at FROM sync_file "
                "WHERE last_fetched_at IS NOT NULL"
            )
        )
    ).mappings()
    for src in sources:
        source_last_fetched_timestamp.labels(
            rir=src["rir"],
            kind=src["kind"],
            url=src["url"],
        ).set(src["last_fetched_at"].timestamp())

    # 4a. rdap_cache size (active vs expired) — single query.
    rdap_counts = (
        await session.execute(
            text(
                "SELECT "
                "  count(*) FILTER (WHERE expires_at > now()) AS active, "
                "  count(*) FILTER (WHERE expires_at <= now()) AS expired "
                "FROM rdap_cache"
            )
        )
    ).first()
    if rdap_counts is not None:
        rdap_cache_entries.labels(status="active").set(rdap_counts[0] or 0)
        rdap_cache_entries.labels(status="expired").set(rdap_counts[1] or 0)

    # 4. stale records per table — точный COUNT по is_stale=TRUE.
    # На больших таблицах COUNT дёшев когда WHERE-фильтр узкий (stale
    # rows обычно <1%). Если станет slow на проде — switch to
    # approximation после добавления отдельного index ON is_stale=TRUE.
    # sync_run / sync_file не subject GC — пропускаем.
    for table in _TRACKED_TABLES:
        if table in ("sync_run", "sync_file"):
            continue
        count = (
            await session.execute(text(f"SELECT count(*) FROM {table} WHERE is_stale = TRUE"))
        ).scalar_one()
        stale_records.labels(table=table).set(count or 0)


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    """Prometheus scrape endpoint.

    Best-effort: при недоступности БД возвращаем 200 с теми метриками,
    что уже есть (HTTP-counter не зависит от БД). Это полезно для
    alerting'а — если /metrics сам падает, Prometheus поднимает
    ``up{job="rir2localdb"} = 0``.
    """
    sessionmaker = request.app.state.sessionmaker
    try:
        async with sessionmaker() as session:
            await collect_db_metrics(session)
    except Exception:
        # DB-derived gauges остаются с last-known значениями.
        pass

    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
