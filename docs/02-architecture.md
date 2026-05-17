# 02 · Архитектура

## Высокоуровневая картина

```
                    ┌────────────────────────────────────┐
                    │ Sources (sources.py)               │
                    │ AFRINIC · APNIC · ARIN · LACNIC ·  │
                    │ RIPE — HTTPS endpoints + tier      │
                    └────────────┬───────────────────────┘
                                 │
                ┌────────────────▼────────────────────┐
                │ Sync layer  (src/rir2localdb/sync)  │
                │ ┌──────────┐ ┌─────────┐ ┌────────┐ │
                │ │ catalog  │→│ fetcher │→│ state  │ │
                │ │ resolve  │ │ HTTPS+  │ │ db log │ │
                │ │  URLs    │ │  md5    │ │ files  │ │
                │ └──────────┘ └────┬────┘ └────────┘ │
                └─────────────────┬─┴──────────────────┘
                                  │ raw bytes (gz/plain)
                ┌─────────────────▼───────────────────┐
                │ Parsers (src/rir2localdb/parsers)   │
                │ ┌──────────────┐ ┌────────────────┐ │
                │ │ delegated.py │ │   rpsl.py      │ │
                │ │ pipe-format  │ │ object stream  │ │
                │ └──────┬───────┘ └────────┬───────┘ │
                └────────┼──────────────────┼─────────┘
                         │ records          │ records
                ┌────────▼──────────────────▼─────────┐
                │ ETL (src/rir2localdb/etl)           │
                │ COPY → staging tables → swap        │
                └─────────────────┬───────────────────┘
                                  │ SQL
                       ┌──────────▼─────────┐
                       │ PostgreSQL 16+     │
                       │ - ip_allocation    │
                       │ - asn_allocation   │
                       │ - ripe_*, apnic_*… │
                       │ - sync_files/runs  │
                       └──────────┬─────────┘
                                  │
                       ┌──────────▼─────────┐
                       │ API (FastAPI)      │
                       │ /ip/{addr}         │
                       │ /asn/{num}         │
                       │ /status            │
                       └────────────────────┘
```

## Технологический стек

| Слой | Выбор | Альтернативы и почему отверг |
|------|-------|------------------------------|
| Язык | Python 3.12 | Go быстрее, но экосистема под RPSL/whois беднее, и I/O-bound задача от него выиграет несильно. См. ADR-0001. |
| Async I/O | `httpx.AsyncClient` | `aiohttp` — норм, но `httpx` встаёт ближе к FastAPI, унифицирует код. |
| БД | PostgreSQL 16+ | См. ADR-0002. |
| Драйвер | `asyncpg` | `psycopg3` — тоже отлично, но `asyncpg` быстрее в `COPY`. |
| ORM | SQLAlchemy 2.x async | Только для моделей и схем. Горячие пути (`COPY`, lookup) — сырой SQL через `asyncpg`. |
| Миграции | Alembic | — |
| API | FastAPI | — |
| CLI | Typer | — |
| Логи | structlog (JSON в проде, color в dev) | — |
| Конфиг | pydantic-settings (env + .env) | — |
| Тесты | pytest, pytest-asyncio | — |
| Линт/формат | ruff (включая format) | — |
| Контейнер | Docker, multi-stage | — |
| Расписание | systemd timer / cron (prod), APScheduler (dev) | См. `docs/07-operations.md`. |

## Слои и контракты между ними

### Sync layer

**Вход:** `Source` объекты из `sources.py`.

**Выход:** локальные файлы на диске (или поток bytes) + строка в
таблице `sync_files` с актуальным состоянием.

**Контракт:** функция `fetch(source) -> FetchResult`, где
`FetchResult` либо `Downloaded(path, sha256, size, fetched_at)`,
либо `NotModified(last_known_sha256)`, либо `Failed(reason)`.

### Parser layer

**Вход:** путь к локальному файлу + тип источника.

**Выход:** итератор записей нормализованного типа. Для delegated —
`DelegatedRecord` dataclass; для RPSL — `RpslObject` dict с
сохранением порядка атрибутов.

**Контракт:** парсер **никогда** не пишет в БД. Это позволяет
тестировать парсеры на in-memory фрагментах и переиспользовать
для CLI-инспекторов.

### ETL layer

**Вход:** итератор записей + контекст текущего `sync_run`.

**Выход:** изменения в основных таблицах, плюс счётчики в
`sync_runs.stats_json`.

**Контракт:**
- Все операции одного файла — в одной транзакции.
- Идемпотентность: повторный запуск на тех же данных не меняет
  результат (используется `INSERT ... ON CONFLICT DO UPDATE` или
  truncate-staging-then-swap).
- Срок жизни записи помечается `last_seen_run_id`. Записи, которые
  не появились в последнем успешном run'е, не удаляются автоматически —
  отмечаются. Удаление staled записей — отдельной командой
  (или порогом, например, «не видели больше 14 run'ов подряд»).

### API layer

Тонкий. Логика lookup'а — в `db/queries.py` (Stage 1) или
`services/*.py` (Stage 2 при усложнении).

## Потоки данных в типичный sync run

1. CLI/cron вызывает `rir2localdb sync --tier core`.
2. `orchestrator` создаёт строку в `sync_runs` со статусом `running`.
3. Для каждого `Source` из `sources.py` с подходящим tier:
   a. `fetcher` идёт за `.md5` (если есть) и сравнивает с тем, что
      записано в `sync_files` для этого URL;
   b. если хэш не изменился — пишем `NotModified` и пропускаем;
   c. иначе — скачиваем основной файл, проверяем md5,
      сохраняем во временный путь, считаем sha256, обновляем
      `sync_files`.
4. Для каждого свежескачанного файла:
   a. `parser` стримит записи;
   b. `etl` пакетами по 10 000 шлёт в staging-таблицу через `COPY`;
   c. по завершении файла — atomic swap (RENAME) либо `INSERT ON
      CONFLICT` для дельты.
5. `orchestrator` закрывает run со статусом `success` или `failed`,
   пишет stats.

## Что НЕ async

Парсинг и ETL — синхронный код, потому что узкое место не сеть,
а CPU и БД. Async — только сетевой fetch и API.

Запускаем парсинг и ETL в отдельных процессах (через
`anyio.to_thread.run_sync` / `loop.run_in_executor`), если нужно
параллелить. По умолчанию sync run — последовательный, потому что
RIR'ам не нужны 5 параллельных подключений ради красоты, и
PostgreSQL пишет лучше одним толстым `COPY`, чем пятью тонкими.
