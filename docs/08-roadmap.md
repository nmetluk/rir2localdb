# 08 · Roadmap (подробно)

Краткая версия — `ROADMAP.md` в корне. Здесь — этап с конкретными
тасками и Definition of Done.

## Stage 0 · Bootstrap & planning ✅

**Цель:** иметь репозиторий, в котором следующий рабочий день
(человеческий или агентский) начинается сразу с кода, не с
«а с чего тут вообще начать».

Деливередлы:
- [x] `README.md`, `ROADMAP.md`, `CONTEXT.md`, `CONTRIBUTING.md`,
  `LICENSE`, `.gitignore`, `.env.example`.
- [x] Скелет src-layout, пустые пакеты с `__init__.py`.
- [x] `docker-compose.yml` с одним сервисом — Postgres 16.
- [x] `pyproject.toml` со списком зависимостей.
- [x] `docs/00..09` — все разделы.
- [x] `docs/adr/0001..0005` — ключевые архитектурные решения.
- [x] `src/rir2localdb/sources.py` — каталог URL/метаданных
  (boots Stage 1, поэтому пишется в Stage 0).

DoD: новый человек/агент, прочитав README → CONTEXT → docs/08 за
30 минут, понимает что делать дальше и кратко зачем.

---

## Stage 1 · Core sync + minimal API

**Цель:** end-to-end путь от FTP до `curl /v1/ip/8.8.8.8`,
только `delegated-extended`, без RPSL.

Деливередлы:
- [x] `alembic init migrations` и интеграция с асинхронным engine.
- [x] Первая миграция `0001_initial`:
  - `sync_run`, `sync_file`, `ip_allocation`, `asn_allocation`
    (см. `docs/03-database-schema.md`).
- [x] `src/rir2localdb/config.py` — pydantic-settings, читает env.
- [x] `src/rir2localdb/db/engine.py` — фабрика async engine'а и
  sessionmaker'а.
- [x] `src/rir2localdb/db/models.py` — SQLAlchemy декларативные модели
  (`SyncRun` / `SyncFile`).
- [x] `src/rir2localdb/sync/fetcher.py` — `fetch(source) -> FetchResult`.
  Three-tier change detection, retries, BSD-формат md5 (RIPE).
- [x] `src/rir2localdb/sync/state.py` — CRUD над `sync_file`.
- [x] `src/rir2localdb/sync/orchestrator.py` — `run_sync(tiers, settings)` —
  открывает `sync_run`, advisory xact lock, итерирует источники под
  savepoint'ами, диспатчит на парсер+ETL, закрывает run.
- [x] `src/rir2localdb/parsers/delegated.py` — итератор записей.
- [x] `src/rir2localdb/etl/delegated_etl.py` — `COPY` в staging,
  `INSERT ON CONFLICT` в основные таблицы, обновление
  `last_seen_run`.
- [x] `src/rir2localdb/cli.py` — typer-команды: `sync`, `status`,
  `migrate`, `gc`.
- [x] `src/rir2localdb/api/app.py` — FastAPI приложение, lifespan
  для async engine.
- [x] `src/rir2localdb/api/routers/ip.py` — `GET /v1/ip/{addr}`.
- [x] `src/rir2localdb/api/routers/asn.py` — `GET /v1/asn/{num}`.
- [x] `src/rir2localdb/api/routers/meta.py` — `/v1/status`, `/v1/healthz`.
- [x] `tests/test_delegated_parser.py` — на фрагментах от каждого RIR.
- [x] `tests/test_orchestrator.py` — 5 сценариев (happy / unchanged /
  partial errors / all-fail / dry-run).
- [x] `tests/integration/test_live_ripe.py` — smoke против ftp.ripe.net.
- [x] `tests/test_api_smoke.py` — поднимает app и дёргает endpoints через ASGITransport.
- [ ] CI на GitHub Actions: ruff + pytest + alembic check (Stage 1
  достаточно одного workflow). **(после-Stage 1 follow-up)**

DoD: на чистой машине шаги quick-start из `README.md` проходят без
ручных правок; `curl http://localhost:8000/v1/ip/8.8.8.8` → JSON
с записью ARIN; `rir2localdb status` показывает успешный run.

**Stage 1 закрыт 2026-05-18** (`.claude/session-log/01-99-stage-1-closed.md`):
- 67 unit-тестов + 1 integration smoke, ruff/mypy чисты.
- Live sync прогнался: 5 RIR'ов, 760k записей, ~647k IP allocation +
  ~113k ASN, длительность ~100 секунд.
- DoD проверен curl'ом: 8.8.8.8 → arin/US, 2001:4860:4860::8888 →
  arin/US, AS15169 → arin/US.

---

## Stage 2 · Rich whois (RPSL dumps)

**Цель:** для адресов RIPE/APNIC/AFRINIC возвращать богатые данные
(netname, org, mnt-by, abuse-c).

Деливередлы:
- [ ] Миграция `0002_rpsl_tables`: `ripe_inetnum`, `ripe_inet6num`,
  `ripe_aut_num`, `ripe_organisation`, `ripe_route`, `ripe_route6`,
  `ripe_as_block` и аналогичные для APNIC/AFRINIC.
- [ ] `parsers/rpsl.py` — потоковый парсер RPSL.
- [ ] `etl/rpsl_etl.py` — диспатчер по типу объекта, для каждого
  типа свой загрузчик.
- [ ] Расширение API: поле `rpsl` в ответах `/ip` и `/asn`.
- [ ] Опциональный коннектор ARIN Bulk Whois:
  - `RIR2LOCALDB_ARIN_BULK_KEY` env;
  - `arin-bulk` tier;
  - парсер ARIN XML;
  - таблицы `arin_*`.
- [ ] Опциональный LACNIC RDAP-fallback при запросе IP в зоне LACNIC.
- [ ] Тесты на RPSL парсер, включая edge-cases из `docs/05-parsers.md`.

DoD: `curl /v1/ip/193.0.6.139` возвращает `rpsl.inetnum.netname`
и `rpsl.organisation.org_name`.

---

## Stage 3 · Operations hardening

Деливередлы:
- [ ] systemd-юниты в `deploy/systemd/`.
- [ ] Dockerfile multi-stage, image публикуется через GHCR (или
  локально — by choice владельца).
- [ ] Структурированные логи везде, без plain print.
- [ ] `/metrics` Prometheus endpoint.
- [ ] Графана-дашборд JSON в `deploy/grafana/dashboards/`.
- [ ] Алерты (Prometheus rules) в `deploy/prometheus/rules.yaml`.
- [ ] `docs/runbook.md` — типичные инциденты и их разрешение.
- [ ] `pg_dump` бэкап-скрипт в `scripts/backup.sh`.

DoD: 7 дней без вмешательства, метрики и алерты работают.

---

## Stage 4 · Quality & coverage

Деливередлы:
- [ ] Покрытие тестами ≥ 80% на парсерах и ETL.
- [ ] Property-based тесты на RPSL парсере (`hypothesis`).
- [ ] Бенчмарк lookup'а IP/ASN: p95 < 10 мс на 100 RPS, отчёт в
  `docs/benchmarks.md`.
- [ ] OpenAPI-схема стабилизирована, опубликован клиентский
  Python SDK (генерируется из FastAPI, лежит в `clients/python`).

---

## Stage 5 · Bonus / nice-to-have

- BGP-данные (RIPE RIS).
- Историческое хранение через `time-partitioned` таблицы.
- GUI на FastAPI + HTMX.
- whois-сервер по 43 порту (тонкая обёртка над теми же запросами).
