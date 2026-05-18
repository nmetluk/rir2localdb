# Stage 3-03: Stale-records GC

**Дата:** 2026-05-18
**Статус:** ✅ закрыт
**Коммиты:**
- `feat(db): 0005 add is_stale column + active GiST partial indexes`
- `feat(sync): GC module — mark/clear stale by last_seen_run threshold`
- `feat(sync): orchestrator runs GC after successful sync`
- `feat(api): hide stale by default, opt-in via include_stale + is_stale field`
- `feat(api): stale_records gauge in /v1/metrics`
- `feat(cli): gc command — manual GC run with --dry-run`
- `test: GC scenarios (6 cases)`
- `docs: GC policy + is_stale semantics, ADR-0008`
- `docs(session-log): 03-03 stale records GC`
- `chore(context): step 3-03 closed`

## Что сделано

### Миграция 0005

`is_stale BOOLEAN NOT NULL DEFAULT FALSE` на 13 таблицах с
`last_seen_run` (2 delegated + 11 RPSL). PostgreSQL ALTER TABLE с
DEFAULT не rewriteит существующие rows (только metadata), быстро
даже на 5.5M-row inetnum.

4 partial GiST indexes для active-only fast path:
- `ip_allocation_v{4,6}_active_gist WHERE family=N AND is_stale=FALSE`
- `inetnum_range_v4_active_gist WHERE is_stale=FALSE`
- `inet6num_range_v6_active_gist WHERE is_stale=FALSE`

Оригинальные full-indexes сохранены для `?include_stale=true` queries.

Round-trip 0001→0005 / downgrade→upgrade clean на test DB. На prod
полный round-trip занял ~9 минут (CREATE INDEX на больших таблицах ×
3 фазы) — однократный hit.

### `src/rir2localdb/sync/gc.py`

- `GcStats(grace_runs, threshold_run_id, marked_stale, cleared_stale)`
  — frozen kw_only dataclass.
- `run_gc(session, settings) -> GcStats`:
  1. `SELECT id FROM sync_run WHERE status='success' ORDER BY id DESC
     OFFSET (grace-1) LIMIT 1` → threshold_id или NULL (bootstrap).
  2. На каждой из 13 таблиц: `UPDATE ... SET is_stale=TRUE WHERE
     last_seen_run < threshold AND is_stale=FALSE` + симметричный
     clear. Counts через `CursorResult.rowcount`.
- Работает в текущей транзакции вызывающего (orchestrator commit'ит
  атомарно с UPDATE sync_run).

### Orchestrator integration

В `run_sync` после успешного ETL'а:

1. `UPDATE sync_run SET status='success'` (промежуточный — чтобы
   текущий run попал в success-окно threshold).
2. `await run_gc(session, settings)`.
3. `_finalize_sync_run` пишет финальный JSONB stats с
   `gc_threshold_run_id`, `gc_marked_stale`, `gc_cleared_stale`.

GC бежит ТОЛЬКО на success — failed sync не трогает БД. Атомарно с
UPDATE sync_run в одной транзакции.

### `Settings.gc_grace_runs`

```python
gc_grace_runs: int = Field(default=7, ge=1, le=365)
```

Default 7 ≈ неделя daily-sync'ов. Settable через
`RIR2LOCALDB_GC_GRACE_RUNS` env. Validation 1..365.

### API: hide stale by default

`/v1/ip/{addr}` и `/v1/asn/{num}`:
- `?include_stale: bool = False` (default).
- SQL `WHERE (is_stale = FALSE OR :include_stale)` — при false planner
  использует partial GiST индекс; при true — full GiST.
- LEFT JOIN на organisation тоже фильтрует stale (через `AND (o.is_stale
  = FALSE OR :include_stale)`).
- Все Pydantic-модели расширены полем `is_stale: bool = False`.

### CLI `gc`

`rir2localdb gc [--dry-run]`:
- Открывает session+transaction, вызывает `run_gc`, печатает JSON.
- `--dry-run` — final rollback вместо commit.
- Production GC бежит автоматически после daily sync.

### `rir2localdb_stale_records{table}` gauge

`SELECT count(*) FROM <table> WHERE is_stale = TRUE` для 13 таблиц на
каждый `/v1/metrics` scrape. Counts обычно <1% — фильтр узкий, дёшев.

### Тесты (6 сценариев)

- bootstrap — 3 runs, grace=7 → ничего.
- marked stale — 8 runs, запись только в run 1 → is_stale=TRUE.
- cleared — stale запись touched сейчас → is_stale=FALSE.
- API hides — 404 для stale-only.
- API shows with include_stale=true — 200 + `is_stale: true`.
- sync_run.stats JSONB roundtrip с gc_* полями.

### Документация

- `docs/04-sync-pipeline.md` § «GC and stale records» — policy +
  bootstrap + manual CLI + API hide + metrics.
- `docs/06-api.md` § «Stale records (Stage 3-03)» —
  `?include_stale=true` + `is_stale` поле в responses.
- ADR-0008 `soft-delete-via-is-stale` — closes ADR-0001 open
  question. Подробное rationale (почему soft, почему 7 runs,
  почему default hide).

## Live smoke ✅

### `rir2localdb gc --dry-run`

```json
{
  "grace_runs": 7,
  "threshold_run_id": null,
  "marked_stale": {},
  "cleared_stale": {}
}

[dry-run] no changes persisted.
```

Bootstrap-mode корректно (5 success-runs на проде, нужно 7+).

### `rir2localdb sync --tier core`

Incremental sync, 5/5 unchanged (HTTP 304 для всех md5/HEAD checks),
duration 4.65 секунд. sync_run id=5 status=success.

После sync проверка через `psql`:
```
 id | gc_thresh | marked
  5 |           | {}      ← Stage 3-03 sync: bootstrap, gc_* пустые
  3 |           |         ← старый pre-3-03 sync (нет gc-fields)
  2 |           |
```

GC integration работает: на success-run'е GC бежит, JSONB пишется.
Bootstrap-mode (< 7 success) → threshold=NULL, ничего не помечено.

### `curl /v1/metrics | grep stale_records`

```
# HELP rir2localdb_stale_records Number of records currently marked as stale by GC (per table)
# TYPE rir2localdb_stale_records gauge
rir2localdb_stale_records{table="ip_allocation"} 0.0
rir2localdb_stale_records{table="asn_allocation"} 0.0
rir2localdb_stale_records{table="inetnum"} 0.0
... (13 таблиц)
```

Все нули — ожидаемо (bootstrap-mode).

Также видно от commit'а 3eaf2d0:
- `rir2localdb_last_sync_run_duration_seconds 4.60203` ← clock_timestamp
  fix работает (раньше показывал 0).

## Проверки

- pytest tests/ — **145 passed, 1 deselected** (139 + 6 GC).
- ruff check / format / mypy — clean (50 source files).
- alembic round-trip 0001..0005 clean на test DB и на prod.

## Что НЕ сделали

- Hard delete / purge old stale — отдельная команда, Stage 4+ если
  понадобится.
- Per-RIR thresholds — overcomplication, не нужно.
- TTL по timestamp вместо счётчика runs — счётчик предсказуемее.
- Notifications когда что-то stale'ится — Stage 3-06 (Grafana alerts
  на growth of `rir2localdb_stale_records`).

## Что дальше

Stage 3-04: Docker image + compose для prod (api + sync worker).
