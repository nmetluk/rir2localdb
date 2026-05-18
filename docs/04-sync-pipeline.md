# 04 · Sync pipeline

Как именно файл попадает с сервера RIR в БД.

**Где живут детали:**
- HTTP-логика и трёхуровневая детекция — `src/rir2localdb/sync/fetcher.py`.
- Маппинг `FetchResult` → колонки `sync_file` (правила UPSERT по
  статусу и `tier_used`) — `src/rir2localdb/sync/state.py`, docstring модуля.
- Каталог источников — `src/rir2localdb/sources.py`.

## Транспорт: HTTPS, не FTP

Все пять RIR'ов обслуживают одни и те же файлы по HTTPS на тех же
хостах (`https://ftp.<rir>.net/...`), и это:

- проще (нет FTP-режимов passive/active, IPv6-капризов, лимитов
  на параллель);
- безопаснее (TLS);
- позволяет условные запросы (`If-Modified-Since`, `If-None-Match`).

FTP оставляем как fallback на Stage 3, если выяснится, что какой-то
RIR режет HTTPS быстрее, чем FTP. Спойлер: не режет.

## Детекция изменений — три уровня

Применяем по очереди, останавливаемся на первом сработавшем:

1. **`.md5`-сосед.** Если у источника есть рядом файл с тем же
   именем и расширением `.md5` — качаем его (он мизерный), парсим
   хэш и сравниваем с `sync_file.last_md5`. Совпало — `not_modified`.
   У всех пяти RIR `delegated-*-latest` имеет `.md5`. У RPSL-дампов —
   обычно нет (или `.gz` уже сам по себе детерминированный).
2. **`HEAD` с условным GET.** Если `.md5` нет — отправляем `HEAD`,
   смотрим `Last-Modified` и `ETag`. Если они совпадают с тем, что
   записано, — `not_modified`. Иначе — качаем с
   `If-Modified-Since`/`If-None-Match`, и если приходит 304 — всё
   равно `not_modified`.
3. **`sha256` контента.** Если ни md5, ни условный GET не дали
   ответ — качаем полностью и считаем sha256. Если совпало с
   `sync_file.last_sha256` — `not_modified` (только обновляем
   `last_fetched_at`).

`FetchResult.tier_used` (1/2/3 для успеха, `None` для `ERROR`) явно
фиксирует, какой именно tier закрыл запрос. Без этого поля
`sync/state.py` при UPSERT'е в `sync_file` не смог бы надёжно
различить UNCHANGED-via-tier-1 (обновляем md5) и UNCHANGED-via-tier-2
(сохраняем старый md5 для «recheck next run») в pathological-кейсе,
когда server вернул 304 без cache validators, а у нас не было
сохранённого ETag.

## Загрузка

- `httpx.AsyncClient` с `timeout=httpx.Timeout(60.0, connect=10.0)`
  и `limits=httpx.Limits(max_connections=10)`.
- Потоковая запись на диск через `client.stream()` —
  RPSL inet6num у RIPE ~36 МБ gzip'нутый и развёрнутый намного больше,
  держать в памяти не надо.
- Куда писать: `${DATA_DIR}/raw/<rir>/<date>/<filename>`. На каждый
  sync run — поддиректория. Старые поддиректории чистятся через
  `rir2localdb gc --keep 7`.
- Прогресс — в `tqdm` для CLI, в `structlog` каждые 10 секунд для
  cron-режима.

## Retry и backoff

- 3 попытки, экспоненциальный backoff (`2^attempt` секунд, capped
  60 с), полностью независимый таймаут на каждую попытку.
- Сетевые ошибки и 5xx — ретраим. 4xx (кроме 408, 429) — фейлим сразу
  и логируем; обычно это «файл переместили».
- 429 (rate limit) — ретраим с большим backoff и `Retry-After`,
  если он есть в заголовке.
- Если md5 не совпал после скачивания — ретраим. После 3-х неудач —
  поднимаем алерт и помечаем источник `last_status='failed'`,
  пайплайн идёт дальше (один битый файл не должен останавливать
  весь run).

## Идемпотентность sync run'а

- Один процесс одновременно — берётся `pg_advisory_lock` по
  фиксированному ID. Если другой процесс уже держит лок —
  CLI отказывает с понятным сообщением.
- Если процесс упал — `sync_run` остаётся в статусе `running`.
  Следующий запуск видит «висячий» run, помечает его `failed`
  с причиной `interrupted`, создаёт новый.

## Файловый GC

- Локальные сырые файлы — не источник истины (БД источник истины),
  но полезно держать недавнюю историю для отладки.
- Политика по умолчанию: хранить файлы последних 7 успешных
  прогонов, остальное удалять.

## Параллельность внутри run'а

- Делать ли загрузку RIR'ов параллельно? Не нужно. Сетевые скорости
  и так упираются в наш канал, а параллельный парсинг — это
  параллельный `COPY`, и они дерутся за WAL и `shared_buffers`.
- Внутри одного RIR: делаем `gather` для md5-чекеров (это HEAD'ы,
  параллельность безвредна), но фактическую загрузку и ETL —
  последовательно.
- Если на Stage 3 нужно ускорить — параллелим разные RIR-ы,
  потому что они независимы и пишут в разные таблицы.

## GC and stale records

После успешного sync'а orchestrator вызывает `run_gc(session, settings)`
**в той же транзакции**. Policy (ADR-0008):

- Запись помечается `is_stale=TRUE` если её `last_seen_run` строго
  меньше id N-ого с конца successful `sync_run`'а. `N = gc_grace_runs`
  (`Settings`, default 7).
- Симметрично: stale запись, touched текущим (или recent в last N)
  sync'ом — `is_stale` возвращается в `FALSE`.
- **Hard delete не делаем.** Stale-навсегда; отдельный purge —
  Stage 4+ если понадобится.

13 таблиц субъекты GC: `ip_allocation`, `asn_allocation`, 11 RPSL
(`inetnum`, `inet6num`, `aut_num`, `organisation`, `role`, `route`,
`route6`, `as_block`, `mntner`, `person`, `as_set`).

**Bootstrap.** Пока успешных sync_run'ов меньше N, threshold = NULL,
GC ничего не делает. Безопасно стартовать с нуля.

**Manual diagnostic** — `rir2localdb gc --dry-run` показывает JSON
со счётчиками без записи в БД. Production GC бежит автоматически
после daily sync через systemd timer.

**API:** запросы `/v1/ip/{addr}` и `/v1/asn/{num}` по умолчанию
скрывают stale (404 если активной записи нет). `?include_stale=true`
включает их в ответ. Каждый блок объект ответа содержит поле
`is_stale: bool` для прозрачности.

**Metrics:** gauge `rir2localdb_stale_records{table}` — текущее
число stale-rows на каждую таблицу. Растёт при normal aging,
сбрасывается при clear-back.

## Orchestrator: format-based ETL dispatch

`sync/orchestrator.py` после `fetch` / `write_result` смотрит
`source.format` и роутит на нужный ETL:

- `Format.DELEGATED` → `parse_delegated` + `apply_delegated_etl(raw_conn,
  records, run_id)`.
- `Format.RPSL` / `Format.RPSL_GZ` / `Format.RPSL_SPLIT_GZ` →
  `parse_rpsl` (стрим) + `apply_rpsl_etl(raw_conn, objects, rir=
  source.rir.value, run_id=run_id)`.
- Любой другой формат → warning + skip (defense-in-depth; не
  встречается в каталоге).

Один `run_sync` может смешивать delegated- и RPSL-источники в одной
транзакции (общий `raw_conn` через `get_raw_connection`). Падение
любого ETL откатывает весь run (savepoint per-source оставляет outer
транзакцию живой при per-source ошибках, но critical ETL failure не
изолируется savepoint'ом из-за TEMP-таблиц с `ON COMMIT DROP`).

### `sources_for_tiers` auto-expansion

`Tier.RICH` в запросе автоматически тянет `Tier.ARIN_RR`, потому что
ARIN не публикует полный RPSL — только IRR-дамп. Это product-level
группировка: `--tier rich` означает «whois-rich + ARIN IRR».
Tier `arin-rr` без `rich` остаётся доступным для специфичных случаев
(обновить routing-данные без перезагрузки 5M-строчных RPSL).

## ETL-слой

Между парсером и БД сидит `etl/`. Два модуля:

### `etl/delegated_etl.py` (Stage 1)

`apply_delegated_etl(conn, records, run_id) -> EtlStats`. Стрим
`DelegatedRecord` → две таблицы (`ip_allocation` / `asn_allocation`)
через одну staging-таблицу на семейство. Буферизация всех записей в
памяти приемлема — типичный delegated-файл ~50k строк, ~10 МБ.

### `etl/rpsl_etl.py` (Stage 2)

`apply_rpsl_etl(conn, objects, rir, run_id) -> RpslEtlStats`. Стрим
`RpslObject` → восемь таблиц (`inetnum`, `inet6num`, `aut_num`,
`organisation`, `route`, `route6`, `as_block`, `role`).

- **Dispatcher по первому ключу.** `obj_type = next(iter(obj))` →
  выбор маппера. Типы вне 8 целевых (`mntner`, `person`, `as-set`, ...)
  считаются в `objects_skipped_unknown_type` и пропускаются.
- **Batched COPY.** Файл `ripe.db.inetnum.gz` — ~5M объектов;
  буферизовать всё в RAM нельзя. ETL держит 8 батч-буферов по
  таблицам (`_BATCH_SIZE = 10_000`); при заполнении любого — `COPY
  staging_<table>` + reset. После исчерпания итератора — финальный
  flush + 8 UPSERT'ов в фиксированном порядке (`organisation → role →
  inetnum → inet6num → aut_num → route → route6 → as_block`).
- **Multi-origin route.** PK включает `origin`, поэтому RPSL-объект
  `route` с N `origin:` строками даёт N строк в SQL (раздельный
  edge-case в API — JOIN по `(rir, prefix)` без `origin`).
- **first_seen_run / last_seen_run / stale records** — идентично
  `delegated_etl` (preserve `first_seen_run` на UPDATE; stale GC
  отложено в Stage 3).
- **JSONB binary codec.** `apply_rpsl_etl` регистрирует на conn'е
  jsonb-codec в `format="binary"` с version-байтом `\x01` префиксом
  (encoder: `b"\x01" + json.dumps(v).encode()`; decoder: `json.loads(v[1:])`).
  Это обязательно: `copy_records_to_table` всегда работает в binary
  протоколе, text-codec там даёт `InternalClientError`. После регистрации
  Python `dict` round-trip'ит через INSERT и SELECT.

См. ADR-0007 для обоснования «одна таблица на тип, не per-(rir,type)»
и docstring `src/rir2localdb/etl/rpsl_etl.py` для конкретики
edge-кейсов мапперов.

## Расширяемость

Добавить новый источник (например, `apnic.db.role.gz`) — это:

1. Добавить `Source(...)` в `sources.py`.
2. Если формат RPSL — парсер уже есть.
3. Если новый формат — реализовать парсер в `parsers/`.
4. Добавить ETL handler в `etl/` (если нужно).

То есть **источник декларативен**, код вокруг не трогаем.
