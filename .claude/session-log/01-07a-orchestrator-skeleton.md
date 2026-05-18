# Stage 1, шаг 7a: orchestrator + CLI skeleton

**Дата:** 2026-05-18
**Статус:** ⚠️ частично (skeleton, ждёт ответа на Q1–Q5)
**Коммиты:** `dd49def` (orchestrator skeleton), `6ebab2a` (cli skeleton),
`874a815` (logging_setup impl)

## Что сделано

- **`src/rir2localdb/sync/orchestrator.py`** — публичный контракт:
  - `SyncRunSummary` (frozen dataclass) с полным набором счётчиков
    `files_*` (per-source) и `etl_*` (per-row); `error: str | None`
    non-None только при `status='failed'`.
  - `run_sync(tiers, settings, *, dry_run=False) -> SyncRunSummary`
    — `raise NotImplementedError`, тело в фазе B.
  - `ADVISORY_LOCK_KEY: Final[int]` — фиксированный int8 ключ для
    `pg_try_advisory_xact_lock`.
  - Алгоритм в docstring модуля (8 шагов): транзакция-обёртка с
    общим asyncpg conn из SQLAlchemy AsyncConnection → INSERT
    sync_run → advisory xact lock → http client → per-source loop
    с try/except → UPDATE sync_run → commit-или-rollback.
- **`src/rir2localdb/cli.py`** (был placeholder с одним
  `app = typer.Typer()`):
  - 4 команды: `sync` (с `--tier` multi + `--dry-run`), `status`,
    `migrate` (`--revision`), `gc`. Все тела `raise NotImplementedError`.
  - `--tier` принимает `list[Tier] | None = None` (не mutable default).
    Резолв `None → [Tier.CORE]` — в фазе B.
  - `rir2localdb --help` рендерит все 4 команды.
- **`src/rir2localdb/logging_setup.py`** — реализован сразу
  (тривиально, не deferred): `configure_logging(level, json)` поверх
  `logging.basicConfig`. `json`-параметр — placeholder для Stage 3,
  call-sites уже могут его передавать.

## Проверки

- `ruff check src/` — clean.
- `mypy src/` — clean (26 source files).
- `rir2localdb --help` — рендерит 4 команды + описания.
- pytest не гоняли — фаза A не трогает тестов.

## Вопросы к согласованию (для планировщика)

### Q1. `pg_advisory_lock` — какой тип?

**Варианты:** (a) `pg_try_advisory_lock(<key>)` — session-scoped,
освобождать вручную в finally; (b) `pg_try_advisory_xact_lock(<key>)` —
xact-scoped, освобождается на commit/rollback автоматически.

**Мой выбор: (b)** — одна транзакция = одна точка решения; нельзя
забыть release при exception'ах. Ключ зашит в `ADVISORY_LOCK_KEY`
(`0x7269723263616C64` — ASCII bytes `"rir2cald"`, читаемо в `pg_locks`).

### Q2. Raw asyncpg для ETL — откуда брать?

**Варианты:** (a) отдельный `asyncpg.connect(dsn)` — две независимых
транзакции, нужно 2PC для атомарности; (b) raw asyncpg ИЗ
`AsyncConnection.get_raw_connection()` — одно физ.соединение, одна
транзакция и для state-CRUD, и для ETL.

**Мой выбор: (b)** — даёт атомарность: ETL и UPSERT в `sync_file`
коммитятся вместе. Это меняет идиому всего sync-run'а; пишу так
именно потому что атомарность — главное архитектурное свойство run'а.

### Q3. Поведение `--dry-run`

**Варианты:** (a) полный прогон, но финальный rollback всей транзакции;
(b) bypass всех write-операций (без INSERT sync_run, без ETL, без
write_result).

**Мой выбор: (a)** — одна точка решения (`commit if not dry_run else
rollback`); вся логика идёт через тот же путь, дублирования нет.
Дёшевле для PG: TEMP-таблицы, COPY, INSERT ON CONFLICT — все
выполняются, просто откатываются.

### Q4. Параллелизм по источникам в этом шаге

**Мой выбор: нет, sequential.** Stage 1 = простая последовательная
итерация по `sources_for_tiers(tiers)`. Параллельно по RIR (как
обсуждалось в `docs/04-sync-pipeline.md` § «Параллельность внутри
run'а») — Stage 3 ops, когда появятся реальные показатели длительности.

### Q5. Содержимое команды `status`

**Минимальный полезный вывод:**
- Последние 5 sync_run: id, started_at, finished_at, status,
  одна сводная строка (`files_total=N, errored=M, …`).
- `sync_file`: rir, kind, last_status, last_fetched_at,
  отсортирован по `last_fetched_at DESC`.

**Мой выбор: `rich.Table` для обоих блоков.** `rich` уже в зависимостях
через `typer[all]` (typer 0.12+ его таскает). Без rich пришлось бы
форматировать вручную; вывод будет хуже читаться в терминале.

## Открытые вопросы для следующих шагов (не блокеры)

- **`alembic.ini` location для `migrate` команды.** CLI устанавливается
  через `pip install -e .` и может вызываться из любого cwd.
  Зашить относительный путь от пакета (`Path(__file__).parents[3]`)
  или ожидать запуск из repo-root? Решу в фазе B.
- **Что делать если `data_dir` не существует?** Создавать
  автоматически в `make_http_client`/`cache_path_for`, или fail
  с понятной ошибкой? Сейчас `cache_path_for` возвращает Path без
  проверки. Решу в фазе B.

## Что дальше

- Жду ответы на Q1–Q5 (или подтверждение всех defaults).
- 01-07b — реализация: тела `run_sync` и всех CLI команд + 5 unit-тестов
  оркестратора + integration smoke против `ftp.ripe.net`. Превью
  объёма — в исходном промпте шага 7 фазы B.
