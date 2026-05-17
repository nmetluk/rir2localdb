# ADR-0005 · SQLAlchemy 2.x async + asyncpg, сырой SQL на горячих путях

Дата: 2026-05-17  
Статус: принято

## Контекст

Нужно совмещать удобство (модели, миграции, lookup-запросы для API)
со скоростью (`COPY` для ETL, без накладных расходов ORM на
миллионах строк).

## Решение

- SQLAlchemy 2.x в async-режиме поверх asyncpg.
- ORM-модели (`Mapped[...]`) — для миграций (Alembic autogenerate)
  и для read-запросов в API.
- На горячих путях ETL — `asyncpg.connection.copy_records_to_table()`
  напрямую, минуя ORM.
- Lookup в API — через `session.execute(select(...))` или
  через `conn.fetchrow(text(...))` для запросов с GiST-оператором
  `@>` (на нём SQLAlchemy 2 умеет, но текстовый SQL короче и
  читаемее).

## Следствия

+ Один источник истины для схемы (модели → миграции).
+ Скорость bulk-load'а — на уровне сырого asyncpg.
+ Удобство read-запросов через ORM.
− Двойной API: `AsyncSession` для одних мест, `Connection` для
  других. Документируется и оформляется как явные слои:
  - `db/repositories/*.py` — Session-based, для API.
  - `etl/*` — Connection-based, для bulk-операций.

## Альтернативы

1. **Только asyncpg, без ORM.** Быстрее всего, но миграции придётся
   вести руками или через отдельный инструмент. Минус.
2. **Только SQLAlchemy с unit_of_work.** Чисто, но `COPY` через
   ORM — это «нет, спасибо», на миллионах строк ORM-инсерты
   будут на порядок медленнее.
3. **Psycopg 3 (sync + async).** Зрелый, поддерживает `COPY` и
   server-side cursor. Жизнеспособная альтернатива asyncpg;
   выбираем asyncpg по привычке и широте поддержки в FastAPI-стеке.
