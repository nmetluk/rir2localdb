# 03 · Схема БД

## Дизайн-принципы

1. **Быстрый lookup по IP.** Главный сценарий — «по адресу найти
   охватывающий блок». Поэтому центральная таблица `ip_allocation`
   хранит диапазон как **range-тип PostgreSQL** с GiST-индексом
   и оператором включения `@>`.
2. **Раздельные таблицы для v4 и v6.** IPv4 диапазоны помещаются в
   `int8range` (32 бита значений), IPv6 — в `numrange` (нужно 128 бит).
   Это удобнее, чем универсальные подходы поверх `cidr`, потому что
   реальные блоки в delegated stats — не всегда выравненные CIDR
   (например, `value=65536` для IPv4 это `/16`, но `value=20480`
   это объединение нескольких CIDR).
3. **Идемпотентность через `last_seen_run_id`.** Не удаляем записи,
   а помечаем «не видели с такого-то прогона». Очистка — отдельной
   командой и/или по порогу.
4. **Сырые RPSL объекты хранятся как структурированный JSONB**
   плюс выделенные колонки для горячих полей и индексов. Это
   защищает от прыжков схемы RIR'ов (они расширяют RPSL,
   а мы не хотим миграции на каждое расширение).
5. **Слои источников не сливаются автоматически.** Запись из
   delegated stats и запись из inetnum — два разных факта; API
   собирает финальный ответ из обоих, в БД они не превращаются
   в один Frankenstein-row.

## Таблицы

### `sync_run` — журнал прогонов

```sql
CREATE TABLE sync_run (
    id            BIGSERIAL PRIMARY KEY,
    tier          TEXT NOT NULL,            -- 'core' | 'rich' | 'arin-rr' | 'arin-bulk'
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT NOT NULL,            -- 'running' | 'success' | 'failed'
    stats         JSONB NOT NULL DEFAULT '{}'::jsonb,
    error         TEXT
);
-- status: 3 значения. Партиальный индекс sync_run_status_partial_idx
-- (см. migration 0001) опирается на 'running' и 'failed'. CHECK не
-- ставим — расширение словаря возможно отдельной миграцией Stage 1.5.

CREATE INDEX ON sync_run (started_at DESC);
CREATE INDEX ON sync_run (status) WHERE status IN ('running', 'failed');
```

### `sync_file` — состояние каждого файла

```sql
CREATE TABLE sync_file (
    url             TEXT PRIMARY KEY,
    rir             TEXT NOT NULL,          -- 'afrinic' | 'apnic' | 'arin' | 'lacnic' | 'ripe'
    tier            TEXT NOT NULL,          -- 'core' | 'rich' | 'arin-rr' | 'arin-bulk'
    kind            TEXT NOT NULL,          -- значение `Source.format.value` из sources.py:
                                            -- 'delegated' | 'rpsl' | 'rpsl-gz' | 'rpsl-split-gz' | 'md5'
    last_run_id     BIGINT REFERENCES sync_run(id),
    last_status     TEXT NOT NULL,          -- значение `FetchStatus.value` из sync/fetcher.py:
                                            -- 'new' | 'updated' | 'unchanged' | 'error'
    last_etag       TEXT,
    last_modified   TIMESTAMPTZ,
    last_md5        TEXT,
    last_sha256     TEXT,
    last_size       BIGINT,
    last_fetched_at TIMESTAMPTZ,
    last_parsed_at  TIMESTAMPTZ
);
-- Источник правды для словарей `kind` и `last_status` — соответствующие
-- enum'ы в коде (`Format` и `FetchStatus`). CHECK constraint не ставим:
-- расширение словаря (новый формат / новый статус) делается в коде, а
-- БД накатывается следующей миграцией если нужно.
-- Маппинг FetchResult → колонки sync_file (правила UPSERT по статусу
-- и tier'у) — в src/rir2localdb/sync/state.py, docstring модуля.
```

`url` как PK — потому что каноничный ключ источника. Если URL у
RIR-а сменится, это эффективно создаёт новую строку и старая
постепенно «протухает» — это правильное поведение.

### `ip_allocation` — выделенные диапазоны (delegated tier)

```sql
CREATE TABLE ip_allocation (
    id               BIGSERIAL PRIMARY KEY,
    rir              TEXT NOT NULL,
    cc               TEXT,                  -- ISO-3166 alpha-2 или NULL
    family           SMALLINT NOT NULL,     -- 4 или 6
    range_v4         INT8RANGE,             -- заполнено для family=4
    range_v6         NUMRANGE,              -- заполнено для family=6
    prefix_length    SMALLINT,              -- для v6 (исходное из источника); для v4 NULL (хранится размер)
    start_text       INET NOT NULL,         -- удобство: исходное представление
    value            BIGINT NOT NULL,       -- исходное (адресов для v4, длина префикса для v6)
    status           TEXT NOT NULL,         -- 'allocated' | 'assigned' | 'available' | 'reserved' | ...
    allocated_on     DATE,
    opaque_id        TEXT,
    extensions       TEXT,                  -- остаток строки после opaque-id
    first_seen_run   BIGINT NOT NULL REFERENCES sync_run(id),
    last_seen_run    BIGINT NOT NULL REFERENCES sync_run(id),
    CHECK (
        (family = 4 AND range_v4 IS NOT NULL AND range_v6 IS NULL) OR
        (family = 6 AND range_v6 IS NOT NULL AND range_v4 IS NULL)
    )
);

CREATE INDEX ip_allocation_v4_gist ON ip_allocation USING gist (range_v4)
    WHERE family = 4;
CREATE INDEX ip_allocation_v6_gist ON ip_allocation USING gist (range_v6)
    WHERE family = 6;
CREATE INDEX ip_allocation_cc       ON ip_allocation (cc);
CREATE INDEX ip_allocation_rir      ON ip_allocation (rir);
```

**Lookup IPv4:**
```sql
SELECT * FROM ip_allocation
 WHERE family = 4
   AND range_v4 @> CAST(<ip-as-int8> AS int8)
 ORDER BY upper(range_v4) - lower(range_v4) ASC  -- самый специфичный
 LIMIT 1;
```

**Lookup IPv6** — то же самое, но с `range_v6` и numeric-представлением.

### `asn_allocation` — выделенные ASN (delegated tier)

```sql
CREATE TABLE asn_allocation (
    id              BIGSERIAL PRIMARY KEY,
    rir             TEXT NOT NULL,
    cc              TEXT,
    asn_range       INT8RANGE NOT NULL,    -- [start, start+value)
    start_asn       BIGINT NOT NULL,
    count           INTEGER NOT NULL,
    status          TEXT NOT NULL,
    allocated_on    DATE,
    opaque_id       TEXT,
    extensions      TEXT,
    first_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
    last_seen_run   BIGINT NOT NULL REFERENCES sync_run(id)
);

CREATE INDEX asn_allocation_range_gist ON asn_allocation USING gist (asn_range);
CREATE INDEX asn_allocation_start      ON asn_allocation (start_asn);
```

### RPSL-таблицы (Stage 2)

**Одна таблица на тип объекта**, ``rir`` колонка как discriminator
(см. ADR-0007). Партиционирование через ``PARTITION BY LIST (rir)`` —
future work, прозрачно для API. Миграция `0002_rpsl_tables`.

Восемь таблиц в Stage 2 § 2-02:

| Таблица | Primary key | Range column (GiST) | Назначение |
|---|---|---|---|
| `inetnum` | `(rir, start_text, value)` | `range_v4 int8range` | IPv4-блоки |
| `inet6num` | `(rir, start_text, value)` | `range_v6 numrange` | IPv6-префиксы |
| `aut_num` | `(rir, asn)` | — | rich data на single ASN |
| `organisation` | `(rir, org_handle)` | — | юр.лицо |
| `route` | `(rir, prefix, origin)` | `prefix cidr` | IPv4 IRR-маршруты |
| `route6` | `(rir, prefix, origin)` | `prefix cidr` | IPv6 IRR-маршруты |
| `as_block` | `(rir, as_block_start, as_block_end)` | `asn_range int8range` | RPSL ASN-блок |
| `role` | `(rir, nic_hdl)` | — | контактная роль |

Скелет ``inetnum``-таблицы:

```sql
CREATE TABLE inetnum (
    rir             TEXT      NOT NULL,
    start_text      INET      NOT NULL,
    value           BIGINT    NOT NULL,        -- кол-во адресов
    range_v4        INT8RANGE NOT NULL,
    netname         TEXT,
    country         TEXT,
    descr           TEXT,                       -- первый descr; полный в raw
    org             TEXT,                       -- handle FK на organisation (не enforced)
    admin_c         TEXT[],
    tech_c          TEXT[],
    status          TEXT,
    mnt_by          TEXT[],
    created         TIMESTAMPTZ,
    last_modified   TIMESTAMPTZ,
    source          TEXT,
    raw             JSONB     NOT NULL,         -- полный объект как dict[str, list[str]]
    first_seen_run  BIGINT    NOT NULL REFERENCES sync_run(id),
    last_seen_run   BIGINT    NOT NULL REFERENCES sync_run(id),
    PRIMARY KEY (rir, start_text, value)
);

CREATE INDEX inetnum_range_v4_gist ON inetnum USING gist (range_v4);
CREATE INDEX inetnum_org_idx       ON inetnum (org)     WHERE org IS NOT NULL;
CREATE INDEX inetnum_country_idx   ON inetnum (country) WHERE country IS NOT NULL;
```

**Ключевые моменты:**

- ``start_text`` — start IP в форме INET (как в ``ip_allocation``), не
  полная RPSL-строка ``"193.0.0.0 - 193.0.0.255"``. Полная — в ``raw``.
- ``value`` — кол-во адресов для v4, длина префикса для v6 (как в
  ``ip_allocation`` Stage 1).
- ``descr TEXT`` — первый descr-атрибут (quick API display). Полный
  список — в ``raw["descr"]: list[str]``.
- ``org TEXT`` — handle organisation-объекта. **FK не enforced**:
  может ссылаться на orphan-объект (legacy), или organisation в
  таблице другого ``rir`` (cross-RIR ссылки в RPSL редки, но
  существуют).
- ``raw JSONB`` — полный объект как ``{key: [val, val, ...]}``, форма
  совпадает с ``RpslObject`` из ``parsers/rpsl.py``. Гарантирует, что
  никакая информация не теряется при добавлении RIR'ом нового
  атрибута.

Lookup IP в RPSL inetnum:

```sql
SELECT * FROM inetnum
 WHERE range_v4 @> CAST($1 AS int8)
 ORDER BY upper(range_v4) - lower(range_v4) ASC  -- самый специфичный
 LIMIT 1;
```

Lookup route'ов покрывающих IP:

```sql
SELECT * FROM route
 WHERE prefix >>= $1::inet
 ORDER BY masklen(prefix) DESC                   -- самый специфичный
 LIMIT 5;
```

**Отложено в Stage 2.5+:** ``mntner``, ``person``, ``as-set``. Они
нужны для FK-расширений и routing-policy-queries; в DoD Stage 2
(``curl /v1/ip/193.0.6.139`` → ``rpsl.inetnum.netname``) не задействованы.

## Стратегия обновления

**Вариант A (по умолчанию для delegated): truncate-and-swap.**
Файл всегда содержит полное состояние, ежедневно. Алгоритм:

```sql
BEGIN;
CREATE TEMP TABLE staging (LIKE ip_allocation INCLUDING ALL) ON COMMIT DROP;
COPY staging FROM STDIN ... ;            -- через asyncpg.copy_records_to_table
-- UPSERT по натуральному ключу (rir, family, start_text, value):
-- смена value (размер блока) трактуется как новая аллокация,
-- старая строка постепенно протухает через last_seen_run.
INSERT INTO ip_allocation (...) SELECT ... FROM staging
ON CONFLICT (rir, family, start_text, value)
DO UPDATE SET ..., last_seen_run = EXCLUDED.last_seen_run;
COMMIT;
```

Натуральный ключ для `ip_allocation` — `(rir, family, start_text, value)`,
для `asn_allocation` — `(rir, start_asn, count)`. Уникальный индекс на
это в Stage 1.

**Вариант B (для RPSL): per-object upsert.**
RPSL-дамп тоже полный, но объектов миллионы. Тот же подход
`COPY → staging → INSERT ON CONFLICT`, ключ — `primary_key` объекта.

## Чего не делаем в Stage 1

- Партиций по времени — не нужны, размеры данных небольшие
  (~миллион строк в `ip_allocation` суммарно).
- Materialized views — не нужны, GiST-индекс справляется.
- Шардинга — не нужен.

## Размеры на одном узле (прикидка)

- `ip_allocation` v4 + v6: ~1.2 млн строк, ~300 МБ с индексами.
- `asn_allocation`: ~150 тыс. строк, ~30 МБ.
- `ripe_inetnum`: ~5 млн строк, ~3 ГБ с индексами и `raw` JSONB.
- `ripe_aut_num`: ~50 тыс. строк, ~150 МБ.
- Итого по Stage 2 — ~5–10 ГБ, легко помещается на любую коробку.
