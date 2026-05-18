# Stage 3 — closed

**Дата:** 2026-05-18
**Статус:** ✅ Stage 3 ops closed целиком (6 шагов).

## TL;DR

Stage 3 закрыл операционный цикл: систематика запуска (systemd /
Docker), observability (metrics / logs / dashboards / alerts),
data hygiene (stale GC), и optional fallback (RDAP).

Все 6 шагов:

- **3-01** ✅ systemd timer + service (daily sync 03:00 UTC,
  hardened sandbox, live install на сервере). `03-01-systemd-timer.md`.
- **3-02** ✅ Prometheus `/v1/metrics` + structured JSON logs через
  structlog. Заодно поймал и пофиксил `now()` → `clock_timestamp()`
  для `sync_run.finished_at`. `03-02-metrics-and-structured-logs.md`.
- **3-03** ✅ Stale-records GC (ADR-0008). Soft-delete через
  `is_stale` + 7-run grace; auto после успешного sync; API hide
  by default + `?include_stale=true`. `03-03-stale-records-gc.md`.
- **3-04** ✅ Dockerfile + compose. Multi-stage build,
  python:3.12-slim, non-root user, один image / три CMD
  (serve/migrate/sync), profile'ы для oneshot. `03-04-docker.md`.
- **3-05** ✅ RDAP fallback для ARIN (ADR-0009). On-demand GET +
  DB cache 24h, transparent. Known limitation: APNIC catch-all
  IANA-NETBLOCK маскирует bulk-miss → fallback не активируется на
  большинстве ARIN-IP. `03-05-rdap-fallback.md`.
- **3-06** ✅ Grafana dashboard + Prometheus alerts.
  4 группы alert rules (9 правил), 5-section dashboard,
  Alertmanager→Telegram example. `03-06-grafana-and-alerts.md`.

## Текущие цифры

- **~39 модулей** в `src/rir2localdb/`.
- **151 unit-тест** + 1 integration smoke (pytest -m integration).
- **9 ADR**.
- **6 миграций** (0001-0006).
- **13 RPSL таблиц** + 2 delegated + rdap_cache.
- **~120 коммитов** после bootstrap.

Production-ready stack:
- systemd service + timer для daily sync.
- Docker / docker-compose для contained deployment.
- `/v1/metrics` endpoint + 9 alert rules + Grafana dashboard.
- Structured JSON logs в production (через `RIR2LOCALDB_LOG_FORMAT=json`).
- Stale-records GC + opt-in RDAP fallback для ARIN.

## Live state на сервере (по итогу Stage 3)

- systemd timer `rir2localdb-sync.timer` active, next run 03:04:40 UTC.
- БД: 1 первый full sync (24 мин) + 2 incremental (8.5 min + 4.6 sec):
  - 9.65M RPSL objects (5.5M inetnum, 1.5M route, ...).
  - 646k IP allocation + 113k ASN allocation.
- alembic at head (0006).

## Открытые follow-ups (опциональны)

1. **IANA-NETBLOCK suppression** для RDAP fallback. APNIC bulk RPSL
   содержит catch-all `IANA-NETBLOCK-<X>` placeholder'ы (descr «not
   allocated to APNIC»). Они маскируют bulk-miss → RDAP не
   активируется. Refine: либо suppress pattern в trigger
   condition, либо `?rdap_force=true` query param. Stage 3-05
   session-log.

2. **LACNIC RDAP fallback**. Аналогично ARIN — отдельный module
   с `rdap.lacnic.net` base URL. Добавляется одним PR'ом по
   образцу ARIN. Не приоритет, можно при реальном спросе.

3. **ARIN Bulk Whois API**. Альтернатива RDAP для тех, кто
   готов пройти ToU + получить API-key. Tier `arin-bulk` уже
   объявлен в `sources.py`, но не реализован.

4. **API расширение под mntner/person/as_set**. Данные в БД
   (Stage 2.50), REST endpoint'ов нет. Можно добавить
   `/v1/mntner/{handle}`, `/v1/asset/{name}` при спросе.

5. **GH Container Registry push**. Docker image сейчас build'ится
   в CI ephemerally. Для распространения нужен push в
   `ghcr.io/nmetluk/rir2localdb` с tags по SHA / releases.
   Требует setup permissions / credentials.

6. **Stage 4+** опционально: Kubernetes Helm chart, distroless
   image, BGP-таблицы (RIPE RIS), partitioning при >50M строк,
   benchmark suite с SLO.

## Архитектура целиком (post-Stage 3)

```
                     ┌────────────────┐
                     │ systemd timer  │
                     │  daily 03:00 UTC│
                     └────┬───────────┘
                          ↓
┌──────────────┐   ┌──────▼─────────┐   ┌──────────────────┐
│ RIR mirrors  │←──┤ rir2localdb    ├──→│  PostgreSQL 16   │
│ (5 sources)  │   │ sync (oneshot) │   │  (Stage 1 + 2)   │
│ + ARIN IRR   │   └────────────────┘   └────────┬─────────┘
└──────────────┘                                 │
                                                 ↓
                  ┌────────────────┐   ┌─────────▼─────────┐
                  │ Prometheus     │←──┤ rir2localdb api   │
                  │ + Grafana      │   │ FastAPI + RDAP    │
                  │ + Alertmanager │   │ /v1/{ip,asn,...}  │
                  └────┬───────────┘   └───────────────────┘
                       ↓                       ↑
                  ┌────▼───────────┐            │
                  │ Telegram alerts│            │
                  └────────────────┘   ┌────────┴──────────┐
                                       │ rdap.arin.net     │
                                       │ (opt-in fallback) │
                                       └───────────────────┘
```

## Финал

Утилита готова к long-running deployment. Все обещанные функции есть,
infra для prod есть, observability есть. Pause или новые features по
запросу.
