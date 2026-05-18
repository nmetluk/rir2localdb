# ADR-0008: Soft-delete stale records via `is_stale` + N-run grace

**Status:** Accepted (Stage 3-03, 2026-05-18).
**Supersedes consideration in:** original ADR-0001 (которая ставила
вопрос «что делать со stale-записями» и откладывала решение).

## Context

После daily ETL'а часть rows в таблицах `ip_allocation` / `asn_allocation`
/ 11 RPSL-таблиц может не появиться в текущем sync'е. Причины:

- RIR пересмотрел делегацию (legitimate stale).
- Источник временно отдал неполный snapshot (network blip, partial
  upload, ARIN IRR-format quirk).
- Sync сам failed for some sources (другие отработали успешно).

Без cleanup'а такие rows накапливаются и засоряют API-ответы.

## Decision

**Soft delete** через колонку `is_stale BOOLEAN NOT NULL DEFAULT FALSE`
на всех 13 таблицах с `last_seen_run`. **Hard delete не делаем.**

**Grace period:** запись помечается `is_stale=TRUE` только если её
`last_seen_run` < id N-ого с конца successful sync_run'а. Default
N=7 (`Settings.gc_grace_runs`) ≈ неделя daily-sync'ов. Этого
достаточно чтобы пережить:

- один failed sync (network blip).
- temporary 5xx от RIR.
- slowly-rolling snapshot updates.
- выходной с downtime машины.

**Symmetric clear:** при том же GC-проходе если stale запись была
touched (UPSERT обновил `last_seen_run`), флаг снимается обратно.

**API hides stale by default.** Lookup'и (`/v1/ip/`, `/v1/asn/`) с
default параметрами не возвращают stale-записи. Opt-in через
`?include_stale=true`. Каждый объект ответа содержит поле `is_stale`
для прозрачности.

**Partial indexes** для fast active-only path: 4 GiST partial-index'а
(`ip_allocation` v4/v6, `inetnum` v4, `inet6num` v6) с `WHERE
is_stale = FALSE`. Полные индексы из 0001/0002 сохранены — для
`include_stale=true` запросов и аналитики.

## Rationale

### Почему soft, не hard

1. **История дешевле места на диске.** 9.6M rows ≈ 5 GB; rate stale
   ~1-2% в год = 50-100 MB прироста, не критично.
2. **Hard delete необратимо.** Если sync ошибочно не отдал блок
   (например, 0 records от RIPE inetnum.utf8.gz на одном run'е),
   ВЕСЬ блок улетел бы. Бывает редко, но дёшево защититься.
3. **Compliance / debugging.** Удобно иметь «как было» для retroactive
   анализа спорных делегаций.

### Почему 7 run'ов, не TTL по времени

- Счётчик run'ов **предсказуем**: не зависит от того, упал ли cron
  или работал ли таймер. «Запись пропускалась 7 sync'ов» — однозначная
  семантика.
- TTL «N дней» требует special-case обработки downtime'а и
  paused-timer'а. Зачем сложность.

### Почему default hide

Большинство API-юзкейсов хотят «текущее состояние» — не история. Stale
запись с `last_seen_run` 2 недели назад скорее всего не актуальна.
Opt-in `?include_stale=true` для редких сценариев аналитики.

## Consequences

### Positive

- Чистые API-ответы по умолчанию.
- Гибкость для admin-аналитики (`include_stale=true`).
- Симметричное `clear` восстанавливает запись если она вернулась —
  no manual intervention.
- Sane defaults: bootstrap-mode (< N success-runs) не помечает ничего;
  можно безопасно крутить sync с самого начала.

### Negative

- 13 ALTER TABLE при миграции 0005 + 4 CREATE INDEX. На больших
  таблицах (5.5M+ inetnum, 1.5M route) — несколько минут wall-clock
  one-time hit.
- Daily GC: 13 UPDATE × 2 (mark + clear) на каждый sync. На больших
  таблицах ~1-5 секунд в сумме (доля stale в delta мала). Sequential,
  в одной транзакции с UPDATE sync_run.
- `rir2localdb_stale_records{table}` метрика делает 13 SELECT count(*)
  на каждый `/v1/metrics` scrape. Acceptable пока counts < 100k.

### Future

- Если stale-доля вырастет > 5% — добавить partial index `WHERE
  is_stale = TRUE` для быстрого `stale_records` count.
- При реальной необходимости — отдельная команда `rir2localdb
  purge-stale --older-than 365d` для hard-delete древних stale.
  Не в Stage 3-03.

## Implementation

- Миграция `0005_add_is_stale_columns` — column + 4 partial GiST.
- Модуль `src/rir2localdb/sync/gc.py` — `run_gc(session, settings)`
  с `GcStats`.
- `sync/orchestrator.py` — `run_gc` вызывается после успешного
  sync'а в той же транзакции; результат пишется в `sync_run.stats`
  (`gc_threshold_run_id`, `gc_marked_stale`, `gc_cleared_stale`).
- `cli.py gc --dry-run` — manual diagnostic с JSON-выводом.
- `api/routers/{ip,asn}.py` — `?include_stale=false` (default) + поле
  `is_stale` в response models.
- `api/metrics.py` — gauge `rir2localdb_stale_records{table}`.
- `Settings.gc_grace_runs: int = 7` (1..365).

## Open questions

- **Hard purge** для очень древних stale (> 1 год) — не покрыто; если
  понадобится, отдельный шаг.
- **Per-RIR thresholds.** Сейчас all-or-nothing. Если LACNIC отдаёт
  редкие partial snapshot'ы, можно было бы дать ему более длинный
  grace. Не делаем — overcomplication.
