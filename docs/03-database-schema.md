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

По одной семье таблиц на каждый RIR с богатым дампом
(`ripe_*`, `apnic_*`, `afrinic_*`). Имена единообразные:

- `<rir>_inetnum`, `<rir>_inet6num`
- `<rir>_aut_num`
- `<rir>_organisation`
- `<rir>_route`, `<rir>_route6`
- `<rir>_as_block`
- `<rir>_role`, `<rir>_mntner`, `<rir>_irt` (опционально)

Скелет inetnum-таблицы:

```sql
CREATE TABLE ripe_inetnum (
    primary_key     TEXT PRIMARY KEY,       -- значение поля 'inetnum:'
    range_v4        INT8RANGE NOT NULL,
    netname         TEXT,
    country         TEXT,
    descr           TEXT,
    status          TEXT,
    org_handle      TEXT,                   -- ссылка на ripe_organisation.primary_key
    mnt_by          TEXT[],
    admin_c         TEXT[],
    tech_c          TEXT[],
    abuse_c         TEXT,
    created         TIMESTAMPTZ,
    last_modified   TIMESTAMPTZ,
    source          TEXT,
    raw             JSONB NOT NULL,         -- все атрибуты как массивы строк
    first_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
    last_seen_run   BIGINT NOT NULL REFERENCES sync_run(id)
);

CREATE INDEX ripe_inetnum_range_gist ON ripe_inetnum USING gist (range_v4);
CREATE INDEX ripe_inetnum_org        ON ripe_inetnum (org_handle);
```

`raw` хранит **полный** объект как JSONB
(`{"inetnum": [...], "netname": [...], "remarks": [...]}`), чтобы
ничего не терять и не страдать от расширений формата.

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
