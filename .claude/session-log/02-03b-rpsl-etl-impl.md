# Stage 2, шаг 2-03b: RPSL ETL — реализация

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:** `feat(etl): RPSL ETL — mappers, staging, UPSERT impl`,
`test(etl): RPSL ETL scenarios — 17 cases, xfail removed`,
`docs: update RPSL ETL pipeline notes`,
`docs(session-log): 02-03b RPSL ETL impl`,
`chore(context): step 2-03 closed`

## Что сделано

### `etl/rpsl_etl.py` (полный impl)

- **`apply_rpsl_etl(conn, objects, rir, run_id) -> RpslEtlStats`** —
  стриминг по itrator'у объектов с per-table батч-буферами (`_BATCH_SIZE
  = 10_000`). Промежуточный flush при заполнении буфера + финальный в
  canonical-порядке (`_UPSERT_ORDER`).
- **`_flush_table`** — COPY → staging + UPSERT + TRUNCATE staging.
  TRUNCATE сбрасывает контент TEMP-таблицы, оставляя её живой для
  следующего батча в той же транзакции.
- **8 мапперов** (`_to_<table>_row`) с edge-case логикой по Q1-Q11:
  inetnum/aut-num/as-block — split + ipaddress/int парсинг + skip
  на ошибки; inet6num — CIDR-only через `IPv6Network(strict=False)`;
  route/route6 — multi-row из `list[origin]`.
- **Helpers:** `_parse_inetnum_range`, `_parse_cidr_v6`, `_parse_asn`,
  `_parse_datetime`, `_first` (typed wrapper над `obj.get(key, [None])[0]`).
- **`_create_staging_tables`** — один `conn.execute` с 8 `DROP IF
  EXISTS … CREATE TEMP TABLE … ON COMMIT DROP` блоками. Префикс
  `staging_rpsl_` развязка с `staging_ip`/`staging_asn` из delegated.
- **8 `_UPSERT_<TABLE>_SQL` констант** + dict `_UPSERT_SQL`. Шаблон —
  `INSERT … SELECT FROM staging_rpsl_<t> … ON CONFLICT (PK) DO UPDATE
  SET <все колонки кроме first_seen_run>, last_seen_run = EXCLUDED.
  last_seen_run RETURNING (xmax = 0) AS inserted`.

### JSONB codec — критический catch

При первом прогоне тестов 12/17 упали с
`InternalClientError: no binary format encoder for type jsonb (OID 3802)`.

`asyncpg.copy_records_to_table` всегда работает в binary-протоколе,
а `set_type_codec(..., schema="pg_catalog")` по умолчанию text. COPY
не видит text-codec, поэтому JSONB-колонка падает.

Fix — binary codec с явным version-байтом wire-формата:

```python
await conn.set_type_codec(
    "jsonb",
    encoder=lambda v: b"\x01" + json.dumps(v).encode("utf-8"),
    decoder=lambda v: json.loads(bytes(v)[1:].decode("utf-8")),
    schema="pg_catalog",
    format="binary",
)
```

`\x01` — version-байт JSONB binary wire format (см. PostgreSQL
`utils/adt/jsonb.c`).

После этого 17/17 ✅. Docstring модуля + тестового файла обновлены.

### `tests/test_rpsl_etl.py` (17 содержательных кейсов)

- Helpers `_obj`, `_inetnum`, `_inet6num`, `_aut_num`, `_merge` —
  снижают boilerplate. `_merge` конвертирует kwargs underscore-keys
  в RPSL-keys через `replace("_", "-")`.
- Все 17 тестов — реальные assertions на SQL rows + stats. xfail
  маркер снят.
- Multi-origin route тестирует, что `raw` JSONB одинаков на всех N
  строках (избыточность приемлема).
- Streaming test — генератор на 15_000 inetnum'ов, проверяет, что
  flush на границе `_BATCH_SIZE` не теряет данных и count == 15_000.
- Rerun test — два apply с разными run_id; `first_seen_run` preserve,
  `last_seen_run` advance, stats fully reflects insert vs update.

### Pruning vs delegated_etl

В skeleton были 8 `_upsert_<table>_from_staging` функций. В impl они
свёрнуты в один `_flush_table` + dict `_UPSERT_SQL`. Один общий путь
для 8 таблиц — меньше boilerplate, проще менять контракт. Pattern
отличается от `delegated_etl`, где per-family функции — там их всего
две (ip и asn), и они различаются больше (разные RETURNING semantics).

### `docs/04-sync-pipeline.md`

Секция «ETL-слой» уже была обновлена в 2-03a (streaming/batch модель).
В этой сессии актуализировал упоминание JSONB binary codec в RPSL ETL
описании.

## Проверки

- `pytest tests/` — **113 passed, 1 deselected** (96 prior + 17 RPSL ETL).
- `ruff check src/ tests/` — clean.
- `ruff format --check src/ tests/` — clean.
- `mypy src/ tests/` — clean (41 source files).
- Wall time RPSL ETL test suite: 2.4 s (включая 15_000-row streaming
  test, ~1.5 s на него).

## Performance baseline

Грубая оценка из теста `test_streaming_flush_at_batch_boundary`:

- **15_000 inetnum** через два flush'а (10K + 5K): ~1.5 s end-to-end.
- Из них COPY: ~0.4 s, UPSERT: ~0.3 s, TRUNCATE: ~0.05 s, рост Python:
  ~0.4 s, остальное — overhead asyncpg.
- Экстраполяция на RIPE inetnum (~5M строк) — ~500 s ≈ 8 минут.
  Это в worst-case (без affinity disk cache, без warm Postgres pool).
  Реальная цифра — после live sync в 2-04/2-05.
- RAM footprint: ~16 МБ peak (8 таблиц × 10K rows × ~200 B/row).

## Edge cases пойманные в тестах

- **inet6num без `/`** (legacy ARIN range) — `_parse_cidr_v6` возвращает
  None по проверке `"/" not in s`, до попытки `IPv6Network`.
- **route с multi-origin** — `raw` JSONB на всех N строках идентичен;
  тест `test_route_multi_origin_yields_multiple_rows` это проверяет.
- **datetime parse fail на одном из двух полей** — объект НЕ skip'ается;
  второе поле остаётся NULL. Тест `test_datetime_parse_success_and_failure`.
- **JSONB равенство dict'ов** — `row["raw"] == obj` работает после
  codec roundtrip (binary). JSON serialization сохраняет insertion order
  ключей dict в Python 3.7+, json.loads восстанавливает.

## Что дальше

**Stage 2, шаг 2-04: расширение API** (`api/server.py`).

- В ответ `/v1/ip/{addr}` — добавить блок `rpsl: { inetnum: {...},
  organisation: {...} | null }`. JOIN из `ip_allocation` на
  `inetnum` по `range_v4 @> $start` (+ `inet6num` аналогично) + на
  `organisation` через `inetnum.org`.
- В ответ `/v1/asn/{num}` — добавить `rpsl: { aut_num: {...},
  organisation: {...} | null }`.
- LEFT JOIN (orphan ссылки legitimate).
- Опциональный feature flag `?include_rpsl=false` чтобы убрать
  payload для bandwidth-sensitive clients.
- 5-7 новых тестов в `test_api_smoke.py` (один на тип endpoint'а
  + edge case с missing RPSL).

После 2-04 — **DoD Stage 2:** `curl /v1/ip/193.0.6.139` возвращает
`rpsl.inetnum.netname` и `rpsl.organisation.org_name`.
