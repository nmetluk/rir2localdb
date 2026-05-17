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

**Stage 1: Core sync + minimal API — в работе. Шаги 1–2 закрыты,
шаг 3 — скелет.**

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
- [x] **Скелет шага 3** — `sync/fetcher.py` с контрактом:
  - `FetchStatus` (`NEW`/`UPDATED`/`UNCHANGED`/`ERROR`),
  - `PreviousFetchState` (что читаем из `sync_file`),
  - `FetchResult` (что отдаём наружу — все поля для записи в
    `sync_file` + `local_path` для парсера),
  - `FetchError` (внутреннее, никогда не наружу из `fetch()`),
  - `fetch()` + помощники `make_user_agent`, `make_http_client`,
    `cache_path_for`, `_fetch_md5_sidecar`, `_conditional_get`,
    `_retry_request` — все ``raise NotImplementedError`` с docstring'ами.
  - Retry-политика зафиксирована: 3 попытки, backoff
    ``min(2**attempt, 60)``, ретраим только 5xx / 429 / network errors,
    4xx — без retry. Без `tenacity`.
  - `config.Settings` расширен `data_dir` / `http_timeout` /
    `http_max_connections` / `http_retries`.
- [x] **Test-plan шага 3** — `tests/test_fetcher.py` со скелетами
  9 сценариев (`raise NotImplementedError`), каждый помечает: какой
  tier должен сработать, какие поля `FetchResult` ожидаются.
- [x] Docs: переписана секция ARIN в `docs/01-data-sources.md`
  (открытое / закрытое / стратегия Stage 2) — см. ниже «Открытые
  вопросы #1».

Не сделано (ждёт следующих шагов Stage 1):

- [ ] **Реализация** `sync/fetcher.py` — тело функций по скелету (шаг 3).
- [ ] `sync/state.py`, `sync/orchestrator.py` (шаг 4 + 7).
- [ ] `parsers/delegated.py` (шаг 5).
- [ ] `etl/delegated_etl.py` (шаг 6).
- [ ] CLI-команды `sync` / `status` / `migrate` / `gc` (шаг 7).
- [ ] FastAPI `/v1/ip` / `/v1/asn` (шаг 8).
- [ ] CI.

---

## Что делать дальше (Stage 1)

Подробный список — `docs/08-roadmap.md` раздел «Stage 1». Кратко
(шаги 1–2 закрыты; шаг 3 — скелет, ждёт реализации):

1. ~~`alembic init` + миграция `0001_initial`.~~ ✅
2. ~~Таблицы `sync_run`, `sync_file`, `ip_allocation`, `asn_allocation`.~~ ✅
3. **`sync/fetcher.py` — реализация по скелету.** Контракты и
   тест-сценарии уже зафиксированы в `src/rir2localdb/sync/fetcher.py`
   и `tests/test_fetcher.py`. Что писать:
   - `make_user_agent` / `make_http_client` / `cache_path_for` —
     тривиально, по docstring'у.
   - `_retry_request` — ручной цикл с `min(2**attempt, 60)` backoff,
     ретраим только сеть / 5xx / 429.
   - `_fetch_md5_sidecar` — get → парсинг 32-hex; 404 → `None`.
   - `_conditional_get` — стрим тела через `client.stream()`,
     `<path>.tmp` → `os.replace`, sha256 на лету.
   - `fetch()` — оркестрирует tier 1 → 2 → 3, ловит `FetchError`
     и превращает в `FetchResult(ERROR, error=...)`.
   - Тесты — 9 сценариев из `tests/test_fetcher.py`, все через
     `httpx.MockTransport`, без реального HTTPS.
4. `sync/state.py` — CRUD над `sync_file`: чтение `PreviousFetchState`
   по URL, запись результата `fetch()` обратно в строку (mapping
   `FetchResult` → колонки + `last_run_id`).
5. `parsers/delegated.py` — итератор `DelegatedRecord` по пайп-формату
   (`docs/05-parsers.md`). Unit-тесты на фрагментах от каждого RIR.
6. `etl/delegated_etl.py` — `COPY` в TEMP staging + UPSERT по
   натуральному ключу `(rir, family, start_text, value)`.
7. `sync/orchestrator.py` + CLI-команды `sync`, `status`, `migrate`, `gc`.
8. Минимальный FastAPI: `GET /v1/ip/{addr}`, `GET /v1/asn/{num}`,
   `GET /v1/status`, `GET /v1/healthz`.

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
4. **`prefix_length` для IPv4 CIDR-aligned записей.** В миграции
   колонка `ip_allocation.prefix_length` оставлена NULL для v4 (как
   описано в `docs/03`). Когда `value` — степень двойки, блок
   фактически выровнен по CIDR, и ETL мог бы вычислять и заполнять
   `prefix_length` — это удобно для API и человеко-читаемого вывода.
   Решение по Stage 2: вычислять в ETL (cheaper) или в API on-the-fly
   (proще). Сейчас не нужно — задача ETL, не миграции.

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
│   ├── config.py                       ← Settings (минимум: database_url)
│   ├── cli.py                          ← Typer-app, плейсхолдер
│   ├── db/                             ← engine.py + models.Base (Base пуст)
│   ├── sync/, parsers/, etl/, api/     ← TODO-стабы, наполняются в Stage 1
├── alembic.ini                         ← конфиг Alembic (URL берётся из env)
├── migrations/                         ← Alembic, async-template
│   ├── env.py                          ← интегрирован с config.Settings
│   └── versions/0001_initial_schema.py ← миграция Stage 1
├── tests/                              ← pytest, пока пусто
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
