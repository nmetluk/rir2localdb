# Stage 2, шаг 2-02: миграция `0002_rpsl_tables`

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:** `e92be77` (ADR-0007), `3ac4149` (migration), `d1ebacd`
(docs/03 + smoke tests)

## Q1-Q5 — итог

- **Q1 macro (per object type vs per-RIR):** A — one table per object
  type, `rir` discriminator. ADR-0007 фиксирует.
- **Q2 start_text format:** только start IP в INET, полная RPSL-форма
  в `raw`. Унификация с `ip_allocation` для JOIN'ов.
- **Q3 descr handling:** TEXT (первый), остальное в `raw["descr"]`.
- **Q4 sync_run pool:** общий, `sync_run.tier` дискриминирует.
- **Q5 set of tables:** 8 (inetnum, inet6num, aut_num, organisation,
  route, route6, as_block, role). mntner/person/as-set — Stage 2.5+.
- **Правка к sketch'у:** `aut_num` — single ASN, не range; PK `(rir, asn)`.
  `first_seen_run`/`last_seen_run` — BIGINT (sync_run.id это BIGSERIAL).

## Что сделано

- **ADR-0007** «Один RPSL-объект-тип = одна таблица, не per-(rir, тип)»
  + index entry в `docs/09-decisions.md`.
- **Миграция `0002_rpsl_tables.py`** — 8 таблиц:

  | Таблица | PK | Range / Index |
  |---|---|---|
  | `inetnum` | `(rir, start_text, value)` | `range_v4 int8range` + GiST |
  | `inet6num` | `(rir, start_text, value)` | `range_v6 numrange` + GiST |
  | `aut_num` | `(rir, asn)` | btree(asn) |
  | `organisation` | `(rir, org_handle)` | btree(abuse_c) partial |
  | `route` | `(rir, prefix, origin)` | `prefix cidr` + GiST(inet_ops) |
  | `route6` | `(rir, prefix, origin)` | `prefix cidr` + GiST(inet_ops) |
  | `as_block` | `(rir, as_block_start, as_block_end)` | `asn_range int8range` + GiST |
  | `role` | `(rir, nic_hdl)` | btree(abuse_mailbox) partial |

  Общие колонки: `raw JSONB NOT NULL`, `first_seen_run` /
  `last_seen_run BIGINT REFERENCES sync_run(id)`, `created` /
  `last_modified TIMESTAMPTZ`, `source TEXT`, array-поля `admin_c
  TEXT[]` / `tech_c TEXT[]` / `mnt_by TEXT[]`.

- **`docs/03-database-schema.md` § «RPSL-таблицы (Stage 2)»** —
  переписан под фактическую схему миграции: grid 8 таблиц, скелет
  inetnum с уточнёнными комментариями про start_text/value/descr/org,
  примеры lookup-запросов (`range_v4 @>` для inetnum,
  `prefix >>=` для route).

- **`tests/test_rpsl_migration.py`** — 5 schema-уровень smoke тестов:
  все 8 таблиц существуют, все 15 ожидаемых индексов (5 GiST + 10
  btree-partial), PK natural shapes для inetnum/aut_num/route.

## Проверки

- `alembic upgrade head → downgrade base → upgrade head` — clean
  round-trip, никаких ошибок.
- `pytest tests/` — **96 passed, 1 deselected** (91 prior + 5 migration smoke).
- `ruff check src/ tests/ src/rir2localdb/migrations/` — clean.
- `mypy src/ tests/` — clean (40 source files).
- `psql \dt` показывает 13 таблиц: 4 Stage 1 + 8 RPSL + alembic_version.

## Решения по ходу

- **`value SMALLINT` в `inet6num`** — длина IPv6-префикса (0..128),
  smallint достаточно (vs BIGINT в `inetnum` где это число адресов).
  Экономия места × 5M строк = небольшая, но natural fit.
- **`prefix CIDR` для `route`/`route6`** + GiST `inet_ops` — нативный
  PostgreSQL CIDR-тип хранит и v4, и v6 prefix'ы в одной колонке.
  Lookup `prefix >>= $ip::inet` использует индекс. Альтернатива
  (отдельные таблицы для v4/v6) — пустая чистоплотность.
- **`fax_no TEXT[]`** в organisation — потому что в RPSL атрибут так
  и называется (с подчёркиванием Python пришлось бы делать
  `replace("-","_")`, что мы по контракту парсера не делаем).
  Маппинг RPSL `fax-no:` → SQL `fax_no` — задача ETL.
- **`role TEXT` колонка в таблице `role`** — да, конфликт с именем
  таблицы. RPSL `role:` атрибут хранит «display name» (NOC@RIPE),
  а primary key — `nic_hdl` (handle). Решил оставить как есть;
  если будет путаница в ETL — переименуем в `role_name`.
- **`partial INDEX … WHERE col IS NOT NULL`** на org / country /
  abuse_c / abuse_mailbox. Большинство объектов не заполняют каждое
  опциональное поле; partial держит индексы маленькими.
- **First Write попытка миграции failed** из-за «File has not been
  read yet» — `alembic revision` создал stub-файл, мой Write требовал
  Read предыдущего содержимого. Round-trip с пустым stub'ом успел
  пройти (`alembic upgrade head` на пустом upgrade() работает, но
  ничего не создаёт). Откатил `alembic downgrade 0001`, прочитал
  stub, перезаписал с реальным телом. Безопасно — миграция не
  замешана в production.

## Открытые вопросы для следующих шагов

- **`mntner`, `person`, `as-set`** — отложены. `mntner` нужен для
  Stage 2-05 (ARIN IRR имеет mntner-объекты); добавим миграцией 0003
  или включим в 2-05. `person` дамифицирован в RIPE `.utf8.gz`,
  малополезен; `as-set` — routing-policy расширение, Stage 5+.
- **Cross-RIR ссылки `org`**. В реальных дампах RPSL встречаются
  ссылки `inetnum.org = ORG-CUST1-AP` (APNIC handle) внутри
  legacy-объекта в RIPE-дампе. FK не enforced — orphan-ссылки
  ожидаются. API делает LEFT JOIN, не INNER.
- **Партиционирование `inetnum`** при росте >50M строк. Сейчас не
  актуально (~5-8M суммарно). Stage 3 ops если профилирование
  покажет необходимость.

## Что дальше

- **Stage 2, шаг 2-03: RPSL ETL** (`etl/rpsl_etl.py`).
  - Контракт: ``apply_rpsl_etl(conn, records, rir, run_id) -> EtlStats``.
  - Per-object-type dispatcher: ``next(iter(obj))`` → выбор маппинга
    в один из 8 таблиц.
  - Маппинг: RPSL key (`mnt-by:`) → SQL колонка (`mnt_by`). Через
    static dict-mapping per-table.
  - COPY в staging + ON CONFLICT UPSERT, как в `delegated_etl`.
  - `raw JSONB` = весь `RpslObject` как dict.
  - Streaming: ETL принимает Iterator от parse_rpsl без буферизации
    всего файла.
  - Парсинг `inetnum: 193.0.0.0 - 193.0.0.255` → start_ip + count +
    range_v4. Парсинг `inet6num: 2001:db8::/32` → start + prefix_len + range_v6.
  - Парсинг `aut-num: AS3333` → 3333 (BIGINT).
  - Парсинг `created: 2003-03-17T12:15:57Z` → datetime.
- См. `docs/04-sync-pipeline.md` § ETL-слой.
