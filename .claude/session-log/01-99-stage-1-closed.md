# Stage 1 — закрыт

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:** `985d483..8cbaa90` (51 коммит после Stage 0 bootstrap)

Stage 1 цель: end-to-end путь от FTP-зеркала RIR до
`curl /v1/ip/8.8.8.8`, только `delegated-extended` tier, без RPSL.
**Цель достигнута.**

## Что построено

| Слой | Модули | Тесты |
|---|---|---|
| Storage | `db/models.py` (SyncRun/SyncFile), `migrations/0001_initial` (4 таблицы + GiST + natural keys + CHECK) | round-trip upgrade/downgrade проверен вручную |
| Sync HTTP | `sync/fetcher.py` (3-tier change detection, retry policy, BSD md5) | 10 unit + 1 live RIPE smoke |
| Sync state | `sync/state.py` (SyncFile CRUD, status-aware UPSERT, HTTP-date round-trip) | 10 unit (включая HTTP-date helpers) |
| Parser | `parsers/delegated.py` (NRO pipe-format, version "2"/"2.3", iana/unknown-type skip) | 18 unit (5 RIR-фрагментов + edge cases) |
| ETL | `etl/delegated_etl.py` (COPY в staging + ON CONFLICT UPSERT, `RETURNING (xmax=0)`) | 13 unit на live `rir2localdb_test` |
| Orchestrator | `sync/orchestrator.py` (advisory xact lock, per-source savepoints, dry-run) | 5 unit + 1 live integration |
| CLI | `cli.py` (sync / status / migrate / serve / gc) | косвенно через orchestrator-тесты |
| API | `api/app.py` (lifespan), `api/routers/{ip,asn,meta}.py`, `api/schemas.py` | 12 unit через ASGITransport |
| Config | `config.py` (pydantic-settings, env + .env), `logging_setup.py` | — |
| Methodology | `.claude/WORKFLOW.md`, ADR-0006 | — |

**Источники истины:**
- Каталог 5 RIR — `src/rir2localdb/sources.py`, 30 источников (1 core + 11 RPSL split RIPE + 11 RPSL split APNIC + 1 RPSL AFRINIC + 1 ARIN IRR; в Stage 1 используется только tier `core`).
- Схема БД — `docs/03-database-schema.md`, миграция `0001_initial`.
- Контракты слоёв — `docs/02-architecture.md`.
- Сводный roadmap — `docs/08-roadmap.md`.

## Цифры

- **27 Python-модулей в `src/rir2localdb/`** + **2771 строка** (с docstring'ами и type hints).
- **7 тест-файлов** в `tests/`: 67 unit-сценариев + 1 integration smoke (skipped by default).
- **6 ADR** в `docs/adr/`: 0001 Python, 0002 PostgreSQL, 0003 ranges, 0004 HTTPS, 0005 SQLAlchemy + asyncpg, 0006 cooperative Claude workflow.
- **10 session-log файлов** в `.claude/session-log/` (включая retro и followup'ы).
- **51 коммит** после Stage 0 bootstrap, средний размер ~50 строк, мелкие порции по Conventional Commits.

## Live DoD прогон (2026-05-18, 10:28 UTC)

```
rir2localdb migrate                                 # OK
rir2localdb sync --tier core                        # 102239 ms
  files: total=5 new=5 updated=0 unchanged=0 errored=0
  parser: records_total=760029
  etl ip:  inserted=646765 updated=0
  etl asn: inserted=113264 updated=0

rir2localdb serve &
curl /v1/ip/8.8.8.8              → arin, US, 8.8.8.0,    256 addrs, 2023-12-28
curl /v1/ip/2001:4860:4860::8888 → arin, US, 2001:4860::, /32,       2005-03-14
curl /v1/asn/15169               → arin, US, AS15169 (assigned),    2000-03-30
curl /v1/status                  → latest run + 5 sources, db_alive=true
curl /v1/healthz                 → {"status": "ok"}
```

ARIN на всех трёх Google-ресурсах — реальное состояние delegated stats.
`8.8.8.8` находит `8.8.8.0/256` (а не широкий ARIN-блок) — узкая запись
поверх широкой работает через `ORDER BY upper-lower ASC LIMIT 1`.

## Архитектурные ключевые решения (для быстрой ревью)

1. **ADR-0003 range storage**: `int8range` для IPv4, `numrange` для
   IPv6 (128 бит). GiST-индексы.
2. **ADR-0005 SQLAlchemy 2 + asyncpg**: ORM для OLTP, raw asyncpg
   на hot-path (ETL `copy_records_to_table`).
3. **ADR-0006 cooperative Claude workflow**: планирующий Claude в
   браузерном чате читает GitHub, исполняющий в Claude Code пишет код
   и session-log; обмен через публичный git + Telegram-уведомления.
4. **`FetchResult.tier_used: int | None`**: explicit tier disambiguation
   для state.py UPSERT-правил (см. 01-04, Q1).
5. **`session.begin_nested()` savepoints per-source в orchestrator'е**:
   одна ошибка одного источника не валит весь run.
6. **`pg_try_advisory_xact_lock(0x7269723263616C64)`**: один активный
   sync_run за раз, авто-release на commit/rollback.
7. **Версионирование API через префикс `/v1/`**: с первого дня.

## Сюрпризы / неочевидные баги, всплывшие по дороге

1. **RIPE отдаёт md5 в BSD-формате** `MD5 (file) = <hash>`, не GNU.
   Fix: regex `\b[0-9a-fA-F]{32}\b` вместо first-token (01-07b, commit `ad596e0`).
2. **APNIC/ARIN/LACNIC перешли на NRO формат «2.3»** (dotted).
   `str.isdigit("2.3")` → False, version-line попадала в основной путь
   и шла warning'ом. Fix: regex `^\d+(\.\d+)*$` (01-08, commit `44fb33c`).
3. **TX-binding bug при смешивании SQLAlchemy + raw asyncpg.** Если
   raw asyncpg query идёт ДО первой SQLAlchemy-операции, asyncpg видит
   autocommit-режим и коммитит мимо SQLAlchemy txn.rollback().
   Fix: в orchestrator INSERT sync_run + advisory lock + UPDATE
   sync_run через `session.execute(text(...))`, raw asyncpg только для
   ETL hot-path (01-07b, commit `f9458c5`).
4. **`asyncpg.AmbiguousParameterError`** на одинаковом `$N`, привязанном
   к колонкам разного типа (smallint + bigint). Лечится дублированием
   параметра в вызове.

## Открытые вопросы / follow-ups

Зафиксированы в `CONTEXT.md` § «Открытые вопросы»:

1. **ARIN rich-данные** — гибрид IRR + RDAP (Stage 2).
2. **LACNIC RDAP-enrichment** — опционально, rate-limit.
3. **«whois-ответ» в API** — минимум сейчас, RPSL расширение в Stage 2.
4. **`prefix_length` для CIDR-aligned IPv4** — ETL может вычислять.
5. **Wheel-packaging миграций** — `importlib.resources` + hatch config.
6. **`data_dir` validation** — Stage 3 ops.

Дополнительно отложено в Stage 1.5 / Stage 3:

- **CI workflow** (`.github/workflows/ci.yml`): ruff + mypy + pytest +
  alembic round-trip. Простой single-workflow, ~10 минут работы.
- **`/v1/readyz`** — отдельный readiness endpoint (сейчас читается из
  `/v1/status.db_alive`).
- **Per-RIR agg в `/v1/status`** — сейчас плоский список sync_file'ов.

## Что дальше — Stage 2

См. `docs/08-roadmap.md` § Stage 2. Кратко:

- Парсер RPSL (общий для RIPE/APNIC/AFRINIC).
- Per-RIR таблицы для `inetnum` / `inet6num` / `aut-num` /
  `organisation` / `route(6)` / `as-block`.
- ETL для split-дампов, gzip-стриминг.
- Расширенный API: объединение delegated stats с RPSL-данными в ответе.
- Опциональный коннектор ARIN Bulk Whois (под ключ).
- LACNIC RDAP-fallback.

DoD Stage 2: `curl /v1/ip/193.0.6.139` возвращает `rpsl.inetnum.netname`
и `rpsl.organisation.org_name`.

Перед стартом Stage 2 — CI workflow коротким follow-up.
