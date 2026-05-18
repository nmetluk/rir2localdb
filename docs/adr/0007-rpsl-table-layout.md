# ADR-0007 · Один RPSL-объект-тип = одна таблица, не per-(rir, тип)

Дата: 2026-05-18
Статус: принято

## Контекст

В Stage 2 зеркалим RPSL split-дампы пяти RIR'ов: 11 типов объектов
(``inetnum``, ``inet6num``, ``aut-num``, ``organisation``, ``route``,
``route6``, ``as-block``, ``role``, ``person``, ``mntner``, ``as-set``).
RIPE / APNIC / AFRINIC публикуют полный whois; ARIN — только IRR
(``route``/``route6``/``as-set``/``mntner``).

Структура хранения — открытый вопрос:

- **Per object type** (A): одна таблица ``inetnum`` с колонкой ``rir`` как
  дискриминатор. 8-10 таблиц всего.
- **Per (rir, type)** (B): ``ripe_inetnum``, ``apnic_inetnum``,
  ``afrinic_inetnum``, и так для каждого типа × RIR. ~30 таблиц.

## Решение

**Variant A. Одна таблица на тип объекта**, ``rir`` как дискриминатор
в составном PK. Партиционирование (если когда-нибудь понадобится) —
через нативный Postgres ``PARTITION BY LIST (rir)`` без поломки
публичного API.

## Следствия

**Плюсы:**
- **API lookup тривиален**: ``SELECT … FROM inetnum WHERE range_v4 @>
  $ip``. Без ``UNION ALL`` по N RIR-таблицам.
- **Объём управляем**: суммарно ~7-10M строк ``inetnum`` со всех RIR.
  GiST-индекс ~500 MB — приемлемо.
- **Миграции дешевле**: добавление колонки = один ALTER TABLE вместо
  N (по числу RIR).
- **Per-RIR различия** покрываются NULL-allowed optional columns +
  ``raw JSONB`` для полного RPSL-объекта. Фактические различия
  между RIPE/APNIC/AFRINIC схемами — единицы опциональных атрибутов,
  одной общей таблицей решаются без потери данных.
- **Партиционирование как future work**: ``PARTITION BY LIST (rir)``
  в Stage 3 ops, если профилирование покажет горячие точки. Это
  изменение transparent для API.

**Минусы:**
- На очень большом объёме (>100M строк ``inetnum``) GiST может
  деградировать — но мы туда не доедем без RPSL-аналогов от других
  registry. Если доедем — партиционируем.
- ``rir`` в каждой строке = ~10 байт overhead × N строк. На 10M
  строк это 100 MB; неважно.

## Альтернативы

**B — per (rir, type) таблицы.** Аргументы за:
- ETL пишет в свою таблицу — изоляция между RIR'ами.
- Theoretically faster per-RIR scans без discriminator-фильтра.

Контр:
- API ``UNION ALL`` по 3-5 таблицам на каждый lookup. Optimizer'у
  сложнее построить план.
- Schema migrations — N×N (типов × RIR'ов).
- Никакой реальной изоляции — все таблицы в одной БД, общий buffer
  pool, общий WAL.

ETL-аргумент слабый: COPY в staging + INSERT-from-staging работает
одинаково в обоих вариантах.

## Ссылки

- ``docs/03-database-schema.md`` § «RPSL-таблицы (Stage 2)» — точная
  схема таблиц.
- Миграция ``0002_rpsl_tables``.
- ``.claude/session-log/02-02-rpsl-tables-migration.md``.
