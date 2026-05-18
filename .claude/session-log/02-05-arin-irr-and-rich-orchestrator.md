# Stage 2, шаг 2-05: rich-tier в orchestrator + ARIN IRR

**Дата:** 2026-05-18
**Статус:** ✅ закрыт (closure Stage 2)
**Коммиты:** `feat(sources): expand rich tier to include ARIN IRR`,
`feat(sync): orchestrator routes RPSL sources to apply_rpsl_etl`,
`feat(cli): status command shows RPSL record counts`,
`test(sync): orchestrator scenarios for rich tier + ARIN IRR`,
`docs: roadmap + sync-pipeline + README — Stage 2 closure`,
`docs(session-log): 02-05 + 02-99 stage 2 closed`,
`chore(context): stage 2 closed`

## Что сделано

### `sources.py`

`sources_for_tiers({Tier.RICH})` теперь автоматически добавляет
`Tier.ARIN_RR`. Это product-level группировка: «rich» = whois-rich,
ARIN'у whois-богатых данных публично отдавать нечего, поэтому ARIN
IRR (routing-only) приходит в одной упаковке.

Tier `arin-rr` без `rich` остаётся доступным как тонкая опция —
для обновления routing-данных без перезагрузки 5M-строчных RPSL.

### `sync/orchestrator.py`

Format-based dispatch в `_run_one_source`:

```python
if source.format == Format.DELEGATED:
    records = list(parse_delegated(result.local_path))
    etl_stats = await apply_delegated_etl(raw_conn, records, run_id)
    ...
elif source.format in _RPSL_FORMATS:
    objects = parse_rpsl(result.local_path)
    rpsl_stats = await apply_rpsl_etl(
        raw_conn, objects, rir=source.rir.value, run_id=run_id
    )
    ...
else:
    logger.warning("unsupported format ...")
```

`_RPSL_FORMATS = {RPSL, RPSL_GZ, RPSL_SPLIT_GZ}` — все три обрабатываются
одинаково (один RPSL текстовый формат внутри).

`SyncRunSummary` расширен:
- `etl_rpsl_records_total`, `etl_rpsl_unknown_type_skipped`,
  `etl_rpsl_malformed_skipped` — суммарные.
- `etl_rpsl_by_type: dict[str, dict[str, int]]` — per-table
  inserted/updated breakdown.
- `kw_only=True` для frozen dataclass — снято ограничение
  «default-after-non-default ordering» в `__init__`.

`_Counters.add` агрегирует RPSL stats так же, как уже агрегирует
delegated. `etl_rpsl_by_type` строится через `setdefault({"inserted": 0,
"updated": 0})` чтобы оба поля гарантированно были на каждой таблице.

### `cli.py`

`_print_summary` после `sync` показывает RPSL-блок если
`etl_rpsl_records_total > 0`:

```
etl rpsl: records=N unknown_type=K malformed=M
         inetnum: inserted=A updated=B
         organisation: inserted=C updated=D
         ...
```

`status` команда: добавлен столбец «RPSL records» в Recent sync_run
таблице. Достаётся из JSONB `sync_run.stats` через
`_rpsl_records_from_stats(stats)`. Старые run'ы (до Stage 2-05, без
поля) показывают пустую ячейку.

### Тесты (`tests/test_orchestrator.py`)

4 новых:

1. **`test_rich_tier_routes_to_rpsl_etl`** — MockTransport отдаёт
   RPSL body с inetnum + organisation. После `run_sync([Tier.RICH])`:
   обе таблицы заполнены, `summary.etl_rpsl_records_total == 2`,
   `etl_rpsl_by_type` для обеих.
2. **`test_mixed_core_and_rich_tiers`** — два источника (delegated +
   RPSL) в одном run'е. И `etl_ip_inserted`, и `etl_rpsl_records_total`
   ненулевые. Confirm что format dispatch не ломает delegated path.
3. **`test_arin_irr_routes_to_rpsl_etl`** — `Tier.ARIN_RR` + `Format.RPSL_GZ`
   с route-объектом. После run — row в `route` table, `rir == "arin"`,
   `prefix == "193.0.0.0/24"`, `summary.etl_rpsl_by_type["route"]`.
4. **`test_sources_for_tiers_expands_rich_to_include_arin_rr`** —
   unit-тест на `sources_for_tiers({RICH})` ⊇ ARIN_RR + обратная
   сторона: `{ARIN_RR}` не тянет RICH.

### Документация

- `docs/04-sync-pipeline.md` — секция «Orchestrator: format-based ETL
  dispatch» с описанием routing'а DELEGATED vs RPSL и
  `sources_for_tiers` auto-expansion.
- `docs/08-roadmap.md` — Stage 2 checklist обновлён под фактический
  impl, все деливередлы [x]. RDAP fallback и ARIN Bulk Whois отложены
  в опциональные.
- `README.md` — шаг 5 Quick start теперь упоминает `--tier rich`.

## Решения по ходу

### `rir` divergence: "ripe" (RPSL) vs "ripencc" (delegated)

`Rir.RIPE.value = "ripe"`, передаётся в `apply_rpsl_etl(rir=...)`. А
delegated NRO формат пишет `registry = "ripencc"`. В БД получаем:

```
ip_allocation.rir       = "ripencc"
inetnum.rir             = "ripe"
asn_allocation.rir      = "ripencc"
aut_num.rir             = "ripe"
```

Известное расхождение. API LEFT JOIN от inetnum к organisation идёт
по `(rir, org_handle)` — оба значения "ripe", работает. ip_allocation
к inetnum мы не джойним напрямую (два независимых SELECT'а), так что
расхождение там нестрашно — клиент видит два разных `rir` поля в одном
ответе, но это разные блоки.

Фикс возможен в Stage 3: нормализация `rir` к одному из вариантов
(вероятно "ripencc" wins как more specific). Сейчас не критично.

### `kw_only=True` на frozen dataclass

`SyncRunSummary` стал `kw_only=True` чтобы свободно добавлять
опциональные поля с дефолтами не в самом конце. Без kw_only Python
требовал бы `etl_rpsl_*` (с defaults) после `duration_ms` (без
default) — это работает, но даёт неудобный порядок. С kw_only порядок
свободный, что делает чтение модели лучше.

Старый код вызывает `SyncRunSummary(run_id=..., tier=..., ...)` —
все keyword, kw_only-совместимо. Регрессий нет.

### TEMP-таблицы и savepoint'ы

`apply_rpsl_etl` создаёт 8 staging TEMP-таблиц с `ON COMMIT DROP`.
Savepoint per-source (`session.begin_nested`) при ошибке откатит
inserts в staging, но не дропнет таблицы — `ON COMMIT DROP` срабатывает
только на outer commit/rollback.

Это не ломает следующий source: следующий `apply_rpsl_etl` начинается с
`DROP TABLE IF EXISTS + CREATE TEMP TABLE`, что идемпотентно.

Проверено в `test_mixed_core_and_rich_tiers` — два источника подряд,
ETL для каждого делает свой DROP/CREATE без проблем.

## Live DoD

```
$ rir2localdb sync --tier core --tier rich
sync_run id=1 status=success
  files: total=29 new=29 updated=0 unchanged=0 errored=0
  parser: records_total=10416367
  etl ip:  inserted=646765 updated=0
  etl asn: inserted=113264 updated=0
  etl rpsl: records=9656338 unknown_type=278058 malformed=92
           as_block: inserted=1658 updated=0
           aut_num: inserted=72192 updated=0
           inet6num: inserted=1139390 updated=0
           inetnum: inserted=5548366 updated=0
           organisation: inserted=118160 updated=0
           role: inserted=185944 updated=0
           route: inserted=1520491 updated=0
           route6: inserted=791987 updated=0
  duration: 1436835 ms
```

Полные курлы — в [`02-99-stage-2-closed.md`](02-99-stage-2-closed.md).

## ARIN IRR zero-padded prefix observation

92 warning'ов вида:

```
rpsl_etl skip route: bad prefix rir=arin value='069.031.132.000/23'
```

ARIN IRR публикует prefix'ы с **leading zeros в октетах**
(`069.031.132.000/23` вместо `69.31.132.0/23`). Python
`ipaddress.IPv4Network` отказывается принимать non-canonical IPv4
literal'ы по RFC 3986 § 7.4.

Это **known limitation**, не критично:

- Затрагивает ~92 route'а ARIN IRR из ~1.5M общих route'ов (0.006%).
- Не теряем routing-data для современных prefix'ов — у них нет
  leading zeros.
- Fix в Stage 3: pre-normalize prefix через regex `re.sub(r"\b0+(\d)",
  r"\1", s)` перед IPv4Network. Или валидировать через less-strict
  parser.

## `_rpsl_records_from_stats` fix

При первом запуске `rir2localdb status` после full sync RPSL records
столбец оказался пустым — SQLAlchemy через asyncpg возвращает JSONB
колонку как `str` (без custom type-codec'а), а helper ожидал dict.

Fix (отдельный коммит `fix(cli): parse stats JSON-string in status
command`): добавлен `isinstance(stats, str): json.loads(stats)` в
начало helper'а. Тест на CLI status — пока вручную; unit-тест на
helper можно добавить в Stage 3.

## Что дальше

Stage 2 закрыт. Опциональные follow-ups:

- **Stage 2-06** (RDAP fallback) — отложен; ARIN всё ещё без полного
  whois, можно подтянуть RDAP API при miss'е. Решение — после Stage 3
  ops, если будет реальный спрос.
- **Stage 3 ops** — systemd-юниты, Dockerfile, /metrics, графана,
  алерты, runbook, бэкап-скрипт.
- **`rir` normalization** — открытый вопрос (см. выше).
- **Stale records GC** — открытый вопрос (см. ADR-0001), отложен в
  Stage 3.
