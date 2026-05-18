# Stage 2, шаг 2-03a: RPSL ETL skeleton + Q-вопросы

**Дата:** 2026-05-18
**Статус:** ⚠️ частично (skeleton; ждём Q-ответы перед 02-03b)
**Коммиты:** `feat(etl): RPSL ETL skeleton — types, dispatcher, 8 mappers`,
`docs(session-log): 02-03a RPSL ETL skeleton`

## Что в skeleton'е

`src/rir2localdb/etl/rpsl_etl.py` — типы, контракты, helper-сигнатуры;
все тела `raise NotImplementedError`.

Поверхность:

- `RpslEtlStats` (mutable slots dataclass: `objects_seen`,
  `objects_by_type`, `objects_skipped_unknown_type`,
  `objects_skipped_malformed`, `upsert_inserted{}`, `upsert_updated{}`).
- `apply_rpsl_etl(conn, objects, rir, run_id) -> RpslEtlStats` — public.
- `_OBJECT_TYPE_TO_TABLE` — 8 entries, dispatch по первому ключу.
- `_UPSERT_ORDER` — canonical порядок (organisation → role → inetnum →
  inet6num → aut_num → route → route6 → as_block).
- 8 `_<TABLE>_COLUMNS` tuples + `_TABLE_COLUMNS` агрегат.
- 8 мапперов `_to_<table>_row` (6 возвращают `tuple | None`, 2 для
  route/route6 — `list[tuple]` для multi-origin).
- `_create_staging_tables` + 8 `_upsert_<table>_from_staging`.

`tests/test_rpsl_etl.py` — 17 сценариев в `xfail(strict=True,
raises=NotImplementedError)` маркером. При реальной реализации XPASS
сразу зажжёт CI-фейл и заставит заменить тела на содержательные
assertions.

`src/rir2localdb/etl/__init__.py` — re-export `RpslEtlStats` /
`apply_rpsl_etl` рядом с delegated'ом.

`docs/04-sync-pipeline.md` — добавлена секция «RPSL ETL» с описанием
streaming/batch модели.

## Q1–Q11 — ответы (все default'ы)

### Q1. `inetnum.start_text` / `value` — edge cases

**A:** Формат «IP - IP», split по ` - `. Skip+warning если:
(a) одна сторона не парсится через `ipaddress.IPv4Address`;
(b) `end < start`;
(c) формат не «IP - IP» (один IP или CIDR).

`start_text = canonical(start)`, `value = end_int - start_int + 1`,
`range_v4 = [start_int, start_int + value)`.

### Q2. `inet6num` — CIDR-only

**A:** Принимаем только `IP/prefix`. Любая «IP - IP» форма (legacy
ARIN, очень редко в современных дампах) → skip+warning. Это
матчит `value SMALLINT` (хранит prefix_length) в миграции 0002.

### Q3. `aut_num.asn` — int parsing

**A:** `s = obj["aut-num"][0]; n = int(s.removeprefix("AS"))`. Skip+warning
если `ValueError` или `n > 2**32 - 1` (Postgres BIGINT вмещает, но
4-byte ASN max = 2^32-1). Negative ASN — невозможно в RPSL, не
обрабатываем явно (попадёт в ValueError или станет ASN с минусом —
который не превысит лимит и пройдёт; примем риск, в реальности не
встречается).

### Q4. `as-block` — `AS<n> - AS<m>` формат

**A:** Split по ` - `, обе части через `removeprefix("AS")` + `int()`,
validate `start <= end`, `asn_range = [start, end+1)`. Skip+warning
на любой формат-фейл.

### Q5. `route` multi-origin → N строк

**A:** PK включает `origin` (PK = `(rir, prefix, origin)`). Если в
объекте N `origin:` атрибутов — N строк. Один `raw` JSONB шарится
между всеми N (~200 байт × N, приемлемо). Stats: 1 в `objects_seen`,
N в `upsert_inserted["route"]` — sanity-check тестов это учитывает.

### Q6. `created` / `last-modified` parsing

**A:** `datetime.fromisoformat(value)` (Python 3.11+ принимает `Z`-суффикс).
Try/except ValueError → `None` + warning, **объект не skip'ается**
(метаданные опциональны).

### Q7. Пустые array-поля → NULL не `[]`

**A:** `obj.get(key)` напрямую. Если ключа нет — `None` → NULL в БД.
Если есть — `list[str]` (всегда ≥1, т.к. парсер гарантирует
`list[str]` с минимум одним элементом). Пустого `[]` не бывает по
контракту парсера. NULL экономит partial-индексы (`WHERE col IS NOT
NULL`).

### Q8. `raw JSONB` — прямая сериализация dict

**A:** Передаём `obj` (=`dict[str, list[str]]`) напрямую в
asyncpg-кортеж. asyncpg сериализует Python dict в JSONB через
встроенный codec; ручной `json.dumps` не нужен. Test sanity:
`SELECT raw FROM inetnum WHERE …` возвращает обратно идентичный dict.

### Q9. Streaming + batched COPY

**A:** Обязательно. `_BATCH_SIZE = 10_000` per-table буфер.
При `len(buffer[table]) >= 10_000` — COPY → `staging_<table>`,
очистка буфера. Финальный flush после исчерпания итератора. RAM
footprint: 8 × 10_000 × ~200 байт ≈ 16 МБ max.

Альтернатива (буферизовать всё в RAM, как `delegated_etl`) для RIPE
inetnum (~5M объектов, ~1 ГБ Python-tuples) — нерабочая.

### Q10. Stale records — не трогаем

**A:** Идентично 1-06: записи, не вошедшие в текущий run, остаются
с устаревшим `last_seen_run`. GC отложен в Stage 3 ops (ADR-0001).

### Q11. `RpslEtlStats` mutable, не frozen

**A:** Без `frozen=True` (отличие от `EtlStats` в delegated_etl).
Причина: RPSL ETL **стримит** и инкрементирует счётчики по мере
итерации; нет момента «всё собрано в Python-памяти» как у delegated'а.
Идентично `RpslParseStats` (см. 02-01b).

## Test plan для 02-03b (17 сценариев)

1. `empty_input_no_changes` — пустой Iterable.
2. `inetnum_single_object` — happy path.
3. `inet6num_cidr`.
4. `aut_num_basic`.
5. `mixed_object_types` — все 8 типов за один проход.
6. `route_multi_origin_yields_multiple_rows` — N origin → N rows.
7. `route6_multi_origin`.
8. `unknown_type_counted_and_skipped` — mntner/person/as-set.
9. `malformed_inetnum_skipped` — битый IP, end<start, one-IP.
10. `malformed_aut_num_too_large` — ASN > 2^32-1.
11. `inet6num_non_cidr_skipped`.
12. `datetime_parse_success_and_failure`.
13. `empty_array_fields_stored_as_null`.
14. `raw_jsonb_preserves_full_object`.
15. `rerun_updates_last_seen_keeps_first_seen`.
16. `streaming_flush_at_batch_boundary` — 1.5×_BATCH_SIZE объектов.
17. `stats_counts_seen_unknown_malformed_separately`.

## Что дальше

02-03b — реализация:

- Тела 8 мапперов (с edge-case handling по Q1-Q4).
- `_create_staging_tables` — 8 `CREATE TEMP TABLE … ON COMMIT DROP`.
- 8 `_upsert_<table>_from_staging` — `INSERT … SELECT FROM staging_<t>
  … ON CONFLICT (PK) DO UPDATE … RETURNING (xmax = 0)`.
- `apply_rpsl_etl` — dispatch + batched flush + finalize stats.
- Удалить `xfail` маркеры в `test_rpsl_etl.py`, добавить содержательные
  assertions (counts, row contents, raw JSONB roundtrip).
