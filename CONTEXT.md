# CONTEXT — точка возобновления работы

> Этот файл всегда отвечает на вопрос: «что было сделано, на чём остановились,
> что делать дальше?» **Обновляется в конце каждой рабочей сессии.**
> Если открываешь репозиторий впервые после паузы — читай этот файл первым.

---

## TL;DR проекта

`rir2localdb` качает публичные FTP/HTTPS-выгрузки пяти RIR
(AFRINIC, APNIC, ARIN, LACNIC, RIPE), кладёт их в PostgreSQL,
отдаёт по REST API whois-подобную информацию об IP-адресах и ASN.

Запускается один раз в сутки (cron), внутри ходит по каталогам RIR,
скачивает только изменившиеся файлы, парсит их, идемпотентно
обновляет таблицы.

---

## Где мы сейчас

**Stage 0: Bootstrap & planning — завершён.**

- [x] Спецификации, ADR, скелет репозитория, каталог источников
  `src/rir2localdb/sources.py`, документация (`docs/`, 10 файлов + 5 ADR).

**Stage 1: Core sync + minimal API — ЗАКРЫТ ✅ (2026-05-18).**
**Stage 1.50: Стабилизация — ЗАКРЫТА ✅ (2026-05-18).**
**Stage 2 ЗАКРЫТ ✅ (2026-05-18).**
**Stage 1: Core sync + minimal API — ЗАКРЫТ ✅ (2026-05-18).**
**Stage 1.50: Стабилизация — ЗАКРЫТА ✅ (2026-05-18).**

Stage 2 (6 шагов):
- 2-01 ✅ RPSL parser (`parsers/rpsl.py`, 18 тестов).
- 2-02 ✅ миграция `0002_rpsl_tables` (8 таблиц, ADR-0007).
- 2-03 ✅ RPSL ETL (`etl/rpsl_etl.py`, 17 тестов; batched COPY +
  per-table staging + JSONB binary codec).
- 2-04 ✅ API RPSL enrichment (`rpsl` блок в `/v1/ip` и `/v1/asn`,
  8 тестов; `?include_rpsl=false`; LEFT JOIN organisation).
- 2-05 ✅ orchestrator rich-tier coverage (format-based dispatch
  DELEGATED↔RPSL, `sources_for_tiers({RICH})` авто-тянет ARIN_RR,
  CLI status показывает RPSL records, 4 теста). **Live DoD ✅**:
  9.6M RPSL records загружено за 24 минуты; `curl /v1/ip/193.0.6.139`
  возвращает `rpsl.inetnum.netname="RIPE-NCC"` и
  `rpsl.organisation.org_name="Reseaux IP Europeens Network Coordination Centre (RIPE NCC)"`.
- 2-06 ⏳ RDAP fallback — опционально, отложено в Stage 3+.

Подробный итог — `.claude/session-log/02-99-stage-2-closed.md`.

**Цифры:** 33 модуля в `src/rir2localdb/`, 125 unit-тестов + 1
integration smoke, 7 ADR, ~70 коммитов после bootstrap.

Live full sync (core+rich) на сервере: **646k IP allocation + 113k
ASN + 9.65M RPSL objects** (5.5M inetnum, 1.5M route, 1.1M inet6num,
791k route6, 185k role, 118k organisation, 72k aut_num, 1.7k as_block);
**duration ~24 минуты**.

**Открытые follow-ups (см. 02-99):**
- ARIN IRR zero-padded prefix (92 skipped, нерешённый): regex
  pre-normalize в `_to_route_rows`. Stage 3.
- `rir` divergence: `ip_allocation.rir="ripencc"` vs
  `inetnum.rir="ripe"`. Нормализация в Stage 3.
- Stage 2-06 RDAP fallback (опционально, для ARIN ownership).
- Stale-records GC (см. ADR-0001), Stage 3 ops.

После Stage 1 прошёл короткий блок «стабилизация перед Stage 2»
(6 задач, `01-50-stabilization.md`):
- `/v1/readyz` отделён от `healthz` (k8s probe split).
- Per-RIR summary в `/v1/status` (`summary_by_rir: list[RirSummary]`).
- ETL вычисляет `prefix_length` для CIDR-aligned IPv4 (open Q#5 closed).
- Миграции перенесены в `src/rir2localdb/migrations/`,
  `importlib.resources` в `_alembic_config` — wheel-ready.
- GitHub Actions: `ci.yml` (push/PR, postgres service, ruff+mypy+alembic+pytest)
  + `integration.yml` (schedule daily + dispatch, continue-on-error).
- CI badge в README.

DoD достигнут: `curl /v1/ip/8.8.8.8` → ARIN/US, `2001:4860:4860::8888`
→ ARIN/US, `AS15169` → ARIN/US после `rir2localdb sync --tier core`.
Подробный итог — `.claude/session-log/01-99-stage-1-closed.md`.

**Цифры:** 27 модулей в `src/rir2localdb/`, 2771 строка кода,
67 unit + 1 integration тестов, 6 ADR, 51 коммит после bootstrap.
Live sync: 760k записей, 647k IP allocation + 113k ASN, ~102 секунды.

**Stage 1 — детали по шагам (для исторической справки):**

Сделано в текущей сессии (2026-05-17):

- [x] `alembic init -t async migrations` + интеграция с пакетом:
  `migrations/env.py` читает URL БД из `rir2localdb.config.Settings`
  (а не из `alembic.ini`), `prepend_sys_path = src`.
- [x] `src/rir2localdb/config.py` — минимальный `Settings`
  (pydantic-settings, пока только `database_url`), фабрика
  `get_settings()` с `lru_cache`.
- [x] `src/rir2localdb/db/engine.py` — `make_engine` /
  `make_sessionmaker` для async SQLAlchemy 2 + asyncpg.
- [x] `src/rir2localdb/db/models.py` — пустой `Base(DeclarativeBase)`
  (модели добавляем по мере появления ORM-потребителей).
- [x] Миграция `migrations/versions/0001_initial_schema.py` —
  таблицы `sync_run`, `sync_file`, `ip_allocation`, `asn_allocation`
  с `int8range`/`numrange`, партиальными GiST-индексами, CHECK по
  `family`, FK на `sync_run`, и уникальными индексами под натуральные
  ключи (`(rir, family, start_text, value)` и `(rir, start_asn, count)`).
- [x] Round-trip `upgrade head → downgrade base → upgrade head`
  проходит на локальной БД без ошибок.
- [x] `src/rir2localdb/cli.py` — `app = typer.Typer(...)`-плейсхолдер,
  чтобы entry-point из `pyproject.toml` и `python -m rir2localdb`
  резолвились в реальный объект. Команды наполняются в шаге §8.
- [x] Docs hygiene: единая запись имён таблиц (ед. число), пример
  UPSERT'а в `docs/03` использует полный натуральный ключ, `/v1/`
  префикс везде где упоминаются эндпоинты.
- [x] `ruff format`, `ruff check`, `mypy src/` — все зелёные
  (RUF001/002/003 — омоглифы — отключены в `pyproject.toml`,
  это русскоязычные docstring'и by design).
- [x] **Скелет шага 3** — `sync/fetcher.py` (типы, контракты, retry
  policy) + 9 сценариев тестов с `raise NotImplementedError`.
  Детали: `.claude/session-log/01-03-fetcher-skeleton.md`.
- [x] **Реализация шага 3** — `sync/fetcher.py` целиком + тесты
  9/9 зелёные. `_retry_request` обслуживает буферизованные запросы
  (md5 sidecar), `_conditional_get` имеет собственный retry-loop для
  стриминга тела (sha256 на лету, `.tmp` → `os.replace`). 304 несёт
  обновлённые cache validators. Edge-case «md5 mismatch + 304» —
  warning + trust server. `fetch()` никогда не raise'ит из-за
  сети/HTTP — всё через `FetchResult.error`. Детали:
  `.claude/session-log/01-03-fetcher-impl.md`.
- [x] `config.Settings` расширен `data_dir` / `http_timeout` /
  `http_max_connections` / `http_retries`.
- [x] Docs: переписана секция ARIN в `docs/01-data-sources.md` —
  IRR-only / Bulk Whois / hybrid plan на Stage 2 (см. «Открытые
  вопросы #1»).
- [x] `.claude/session-log/` — директория с правилами формата,
  template'ом и двумя файлами под шаг 3 (skeleton + impl).
- [x] **Методология (отдельный коммит)** — `.claude/WORKFLOW.md`
  (операционный README), ADR-0006 «Cooperative Claude workflow»,
  ссылка в `docs/09-decisions.md`, секция «Development workflow»
  в `README.md`.
- [x] **Шаг 5 — `parsers/delegated.py`**: чистый stream, не трогает
  БД/сеть. `DelegatedRecord` (frozen dataclass) +
  `parse_delegated(path)`. Skip-правила: comments / version / summary
  / iana / unknown-type (warning). `ValueError` на битую дату;
  `"00000000"` → `None`. 17 тестов: 5 параметризованных RIR-фрагментов
  + 12 edge cases. Детали:
  `.claude/session-log/01-05-parsers-delegated.md`.
- [x] **Шаг 8 — FastAPI `/v1/*`** (commits `3a296e9`..`8cbaa90`):
  - `api/app.py` — `make_app(settings)` фабрика с lifespan'ом
    (создаёт engine + sessionmaker на startup, dispose на shutdown).
    Корень `/` отдаёт JSON-манифест.
  - `api/schemas.py` — 4 Pydantic-модели (плоские, без обёрток).
  - `api/routers/ip.py` — `/v1/ip/{addr}` через `ipaddress.ip_address`,
    IPv6 как `Decimal(int(ip))` → numeric, `ORDER BY upper-lower ASC
    LIMIT 1` для самой узкой записи.
  - `api/routers/asn.py` — `/v1/asn/{num}`, валидация 0..2^32-1.
  - `api/routers/meta.py` — `/v1/healthz` (без БД) + `/v1/status`
    (последний sync_run + sync_file + db_alive).
  - `cli.serve --host --port` — uvicorn-runner с lazy import.
  - 12 ASGI-smoke-тестов через `httpx.ASGITransport` + `clean_db`.
  - **По дороге**: fix парсера для NRO «2.3» version-header
    (APNIC/ARIN/LACNIC перешли с «2»). `clean_db` фикстура перенесена
    в conftest.py для shared usage.
  - Детали: `.claude/session-log/01-08-fastapi.md`.
- [x] **Шаг 7 — `sync/orchestrator.py` + CLI** (двухходовой):
  - 7a (skeleton, `dd49def`+`6ebab2a`+`874a815`+`8eb418a`):
    `SyncRunSummary` + `run_sync` контракт, 4 typer-команды
    (sync/status/migrate/gc), `logging_setup.configure_logging`,
    `ADVISORY_LOCK_KEY = 0x7269723263616C64`. Q1-Q5 согласованы.
    Детали: `01-07a-orchestrator-skeleton.md`.
  - 7b (impl, `7f8df23`..`0774176`): `run_sync` целиком —
    SQLAlchemy session для управляющих операций (INSERT sync_run /
    advisory lock / UPDATE), raw asyncpg для ETL hot-path. Per-source
    isolation через `session.begin_nested()` savepoint'ы. CLI
    команды реализованы (alembic.config программно, без alembic.ini).
    5 unit-сценариев через `clean_db` фикстуру (TRUNCATE + asyncpg).
    Integration smoke против ftp.ripe.net (опциональный `pytest -m
    integration`). По дороге: bug fix md5 BSD-формата для RIPE
    (regex `\b[0-9a-fA-F]{32}\b`); bug fix tx-binding (raw asyncpg
    autocommit, если SQLAlchemy ещё не отправил BEGIN). Детали:
    `01-07b-orchestrator-impl.md`.
- [x] **Шаг 6 — `etl/delegated_etl.py`** (двухходовой: skeleton + impl):
  - 6a (skeleton, `2a058db`): типы, контракты, helper-сигнатуры с
    `raise NotImplementedError`; Q1–Q5 согласованы (raw
    `asyncpg.Connection`, TEMP-staging с ON COMMIT DROP, одна
    публичная функция, маппинг в Python, без stale-GC).
    Детали: `01-06a-etl-skeleton-confirmed.md`.
  - 6b (impl, `cb98034` + `a37fb14`): тела `_record_to_*_row`,
    `_create_staging_tables`, `_upsert_*_from_staging`,
    `apply_delegated_etl`. SQL в `Final[str]`-константах.
    `first_seen_run` preserve через отсутствие в SET. Подсчёт
    inserted-vs-updated через `RETURNING (xmax = 0)`. 13 тестов
    на `pg_conn`/`pg_sync_run_id` (raw asyncpg-фикстуры).
    Детали: `01-06b-etl-delegated-impl.md`.
  - Workflow rule: в `.claude/WORKFLOW.md` зафиксирован паттерн
    «двухходовой шаг = два session-log» (`NN-MMa` skeleton +
    `NN-MMb` impl).
- [x] **Шаг 4 — `sync/state.py`**:
  - `FetchResult.tier_used: int | None` (Q1) — explicit tier signal
    для state.py; обновил fetcher + 9 тестов + docs/04.
  - ORM-модели `SyncFile` / `SyncRun` в `db/models.py`.
  - `Settings.test_database_url` + `.env.example` + `.env` локально.
  - `migrations/env.py` теперь уважает программный
    `cfg.set_main_option("sqlalchemy.url", ...)`.
  - `tests/conftest.py` — session-engine + per-test `db_session` с
    rollback + `sync_run_id` factory. Тестовая БД `rir2localdb_test`
    создана через `sudo -u postgres psql -c "CREATE DATABASE ..."`.
  - `sync/state.py` — `read_previous_state` / `write_result` /
    `mark_parsed` + HTTP-date helpers. Правила UPSERT'а по
    `(status, tier_used)` в docstring модуля таблицей.
  - 10 тестов state.py (8 на БД + 2 unit на HTTP-date), включая
    Q4-проверку перекатегоризации tier'а.
  - Docs: glossary в `docs/03` для `kind` / `last_status` /
    `sync_run.status` (Q2/Q3); cross-ref в `docs/02` и `docs/04`.
  - YAML fix в notify-session-log: `grep -v _template`.
  - Детали: `.claude/session-log/01-04-sync-state.md`.

Не сделано (Stage 2):

- [ ] Stage 2: RPSL rich-tier (см. `docs/08-roadmap.md` § Stage 2 и
  «Что делать дальше» ниже).

---

## Что делать дальше (Stage 1)

**Stage 2 шаг 2-03 — RPSL ETL.** Полный план Stage 2 — `docs/08-roadmap.md`. Ключевые блоки:

1. ~~**Парсер RPSL** (`parsers/rpsl.py`) — общий потоковый,
   gzip-stream.~~ ✅ Шаг 2-01 закрыт. 18 unit-тестов; continuation
   через space; lowercase keys; first-key — primary attribute.
   Детали: `.claude/session-log/02-01b-rpsl-parser-impl.md`.
2. ~~**Per-RIR таблицы** — миграция `0002_rpsl_tables`.~~ ✅ Шаг 2-02
   закрыт. ADR-0007 утвердил вариант «one table per object type»;
   8 таблиц + GiST на range-колонках; partial-индексы на
   org/country/abuse_c. Round-trip clean. Детали:
   `.claude/session-log/02-02-rpsl-tables-migration.md`.
2b. (изначальный пункт 2 был про другую структуру — закрыт)
   `ripe_inetnum` уже в `docs/03`. Аналогично для apnic/afrinic;
   ARIN — отдельная история (см. ниже).
3. **ETL для split-дампов** (`etl/rpsl_etl.py`). RIPE inet6num
   ~36 МБ gzip, ~500MB развёрнутый — потоковая обработка обязательна.
   Per-RIR диспатчер по `Source.rir`, маппинг RPSL объектов в
   per-RIR таблицы.
4. **Расширение API**: поле `rpsl` в ответах `/v1/ip/{addr}` и
   `/v1/asn/{num}` с `netname` / `org_name` / `mnt-by` / `abuse-c`.
5. **ARIN специально**: гибрид по плану из open-question #1.
   - (a) Mirror `pub/rr/arin.db.gz` (только IRR: route/route6/as-set/
     mntner) как tier `arin-rr`.
   - (b) On-demand RDAP к `rdap.arin.net` для обогащения ownership
     на lookup-time, с кэшированием в БД.
   - (c) Bulk Whois API — опциональный fallback под env-ключ.
6. **LACNIC RDAP-fallback** — опционально, с rate-limit.

**Первый шаг Stage 2:** парсер RPSL (можно начать без БД-миграции,
on-disk тестировать). Затем миграция, потом ETL, потом API.

**DoD Stage 2:** `curl /v1/ip/193.0.6.139` возвращает
`rpsl.inetnum.netname` (RIPE-NCC) и `rpsl.organisation.org_name`.

**Definition of Done для Stage 1:** на чистой машине проходит сценарий
быстрого старта из `README.md`, `curl http://localhost:8000/v1/ip/8.8.8.8`
возвращает корректный JSON с реестром, страной и статусом.

---

## Открытые вопросы (нужно решить до Stage 2)

1. **ARIN rich-данные — гибрид IRR + RDAP.** ARIN не публикует
   полный whois-дамп; в `pub/rr/arin.db.gz` лежит только IRR
   (`route`, `route6`, `as-set`, `mntner`), без `inetnum`,
   `organisation`, `role`, `person`. Полный whois доступен только
   через Bulk Whois API под ToU и ключ.
   Рабочий план на Stage 2 (финализируется в начале Stage 2):
   - (a) ежедневный mirror `pub/rr/arin.db.gz` как `arin-rr` tier
     — для маршрутных/mntner-объектов;
   - (b) on-demand RDAP к `rdap.arin.net` на lookup-time для
     обогащения ownership/contacts, с локальным кэшированием и
     уважением rate-limit;
   - (c) Bulk Whois API — опциональный fallback для инсталляций,
     готовых пройти ToU и держать полный whois локально.
   Этим вариант (b) в типичном кейсе закрывает потребность, и
   заявку на Bulk Whois можно не подавать. Детали — в
   `docs/01-data-sources.md`, секция ARIN.
2. **LACNIC rich data.** Полного публичного дампа нет. Варианты:
   а) ограничиться delegated stats; б) подключить RDAP-обогащение
   с rate-limit. По умолчанию — (а), (б) опционально в Stage 2.
3. **Что считать «whois-ответом» в API.** Минимум — поля из delegated
   stats. Максимум — слияние с RPSL inetnum/aut-num/organisation.
   В Stage 1 ограничиваемся минимумом, в Stage 2 расширяем.
4. ~~**`prefix_length` для IPv4 CIDR-aligned записей.**~~ ✅ **Закрыт
   в 1.50** (commit `0ed70fe`): ETL `_record_to_ip_row` вычисляет
   prefix_length для IPv4 если `value` — степень двойки.
5. ~~**Wheel-packaging миграций.**~~ ✅ **Закрыт в 1.50**
   (commit `8c6f3ec`): миграции перенесены в
   `src/rir2localdb/migrations/`, `_alembic_config` использует
   `importlib.resources.files()`.
6. **`data_dir` validation.** `run_sync` создаёт `settings.data_dir`
   через `mkdir(parents=True, exist_ok=True)` — опечатка в `.env`
   создаст каталог в неожиданном месте. Stage 3 ops может добавить
   sanity-check (абсолютный путь, права на запись, не корневая
   система).

---

## Где что лежит (карта репозитория)

```
rir2localdb/
├── README.md, ROADMAP.md, CONTEXT.md   ← навигация (читать сначала)
├── docs/                               ← вся проектная документация
│   ├── 00..09-*.md                     ← разделы по темам
│   └── adr/                            ← architecture decision records
├── src/rir2localdb/
│   ├── sources.py                      ← каталог URL и форматов (готов)
│   ├── config.py                       ← Settings (database_url + http_*/data_dir)
│   ├── cli.py                          ← Typer-app, плейсхолдер
│   ├── db/                             ← engine.py + models.Base (Base пуст)
│   ├── sync/fetcher.py                 ← реализован (шаг 3)
│   ├── sync/state.py                   ← реализован (шаг 4)
│   ├── sync/orchestrator.py, sync/catalog.py ← TODO-стабы
│   ├── parsers/delegated.py            ← реализован (шаг 5)
│   ├── parsers/rpsl.py                 ← TODO-стаб (Stage 2)
│   ├── etl/delegated_etl.py            ← реализован (шаг 6)
│   ├── etl/rpsl_etl.py                 ← TODO-стаб (Stage 2)
│   ├── sync/orchestrator.py            ← реализован (шаг 7)
│   ├── cli.py                          ← реализован (шаги 7+8, 5 команд)
│   ├── logging_setup.py                ← реализован (шаг 7, plain text)
│   ├── api/app.py                      ← реализован (шаг 8)
│   ├── api/schemas.py                  ← реализован (шаг 8)
│   └── api/routers/{ip,asn,meta}.py    ← реализованы (шаг 8)
├── alembic.ini                         ← конфиг Alembic (URL берётся из env)
├── migrations/                         ← Alembic, async-template
│   ├── env.py                          ← интегрирован с config.Settings
│   └── versions/0001_initial_schema.py ← миграция Stage 1
├── tests/                              ← fetcher + state + parser + etl + orchestrator + conftest
├── tests/integration/                  ← live RIPE smoke (skip by default)
├── .claude/WORKFLOW.md                 ← методология (см. ADR-0006)
├── .claude/session-log/                ← по одному файлу на шаг Stage N
├── .github/workflows/                  ← notify-session-log.yml (Telegram)
├── scripts/                            ← вспомогательные shell-скрипты
├── pyproject.toml                      ← деплой/зависимости + ruff/mypy
├── docker-compose.yml                  ← локальный Postgres
└── .env.example                        ← образец переменных окружения
```

---

## Контакты внешних систем (для справки)

| RIR     | Базовый URL                                | Богатый whois? |
|---------|--------------------------------------------|----------------|
| AFRINIC | https://ftp.afrinic.net/pub/               | да (`dbase/afrinic.db.gz`) |
| APNIC   | https://ftp.apnic.net/                     | да (`pub/apnic/whois/*.gz`) |
| ARIN    | https://ftp.arin.net/pub/                  | только по API-ключу |
| LACNIC  | https://ftp.lacnic.net/pub/                | нет (только delegated) |
| RIPE    | https://ftp.ripe.net/                      | да (`ripe/dbase/split/*.gz`) |

Полный машинно-читаемый каталог — `src/rir2localdb/sources.py`.

---

## Чек-лист обновления этого файла

После каждой значимой рабочей сессии:

1. Перенести готовое из «Что делать дальше» в «Где мы сейчас».
2. Дополнить «Открытые вопросы», если что-то выяснилось.
3. Если изменилась структура — обновить «Где что лежит».
4. Коммит с сообщением `chore(context): update after <stage/topic>`.
