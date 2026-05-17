# Stage 1, шаг 6a: delegated_etl skeleton — Q1–Q5 подтверждены

**Дата:** 2026-05-18
**Статус:** ⚠️ частично (skeleton)
**Коммит:** `2a058db`

## Q1–Q5 — итог

- **Q1:** raw `asyncpg.Connection` (ADR-0005 hot path).
- **Q2:** `TEMP TABLE … ON COMMIT DROP` (с `DROP IF EXISTS` префиксом
  на случай повторных вызовов в одной outer-транзакции).
- **Q3:** одна функция `apply_delegated_etl(conn, records, run_id)`,
  партиционирование ipv4/ipv6 vs asn внутри.
- **Q4:** маппинг `value → range` на стороне Python (`asyncpg.Range`),
  staging хранит готовые значения.
- **Q5:** stale-records не трогаем, GC — Stage 3 ops.

## Скелет

`src/rir2localdb/etl/delegated_etl.py` — типы, контракты,
helper-сигнатуры; все тела `raise NotImplementedError`. Подробности
в docstring модуля.

`tests/conftest.py` — добавлены `pg_conn` (raw asyncpg в своей
транзакции с rollback'ом) и `pg_sync_run_id` параллельно к
SQLAlchemy-фикстурам для state-тестов.

`tests/test_delegated_etl.py` — 10 stub-сценариев, фикстуры
резолвятся чисто, тела `NotImplementedError`.

## Что дальше

01-06b — реализация: тела `_create_staging_tables`,
`_record_to_*_row`, `_upsert_*_from_staging`, `apply_delegated_etl`;
unit-тесты на `pg_conn` (~13 сценариев).
