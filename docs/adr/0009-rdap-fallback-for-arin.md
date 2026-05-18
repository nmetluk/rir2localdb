# ADR-0009: RDAP transparent fallback for ARIN ownership

**Status:** Accepted (Stage 3-05, 2026-05-18).

## Context

ARIN не публикует open bulk RPSL дамп с ownership/contacts. У других
четырёх RIR это есть (`ftp.ripe.net/.../ripe.db.*.gz`,
`ftp.apnic.net/.../apnic.db.*.gz`, `ftp.afrinic.net/.../afrinic.db.gz`,
LACNIC — без bulk, но это отдельный gap). У ARIN'а доступно только:

1. **IRR dump** (`ftp.arin.net/pub/rr/arin.db.gz`) — только routes /
   route6 / as-sets / mntner. Без inetnum / organisation / role /
   person. Stage 2-05 уже подключил.
2. **Bulk Whois API** — полный whois, но требует ToU + API-key +
   account approval. Бюрократия для большинства installations.
3. **RDAP** (RFC 7480-7484) — open JSON HTTP API, без credentials,
   rate-limited (ARIN: ~50 req/min). Идеально подходит для on-demand
   lookup'а одной записи.

API клиент сейчас получает `rpsl.inetnum = null` / `rpsl.organisation
= null` для ARIN-блоков. Coverage gap.

## Decision

**On-demand RDAP fallback с DB-cache, прозрачный для клиента.**

Flow в `/v1/ip/{addr}`:
1. SELECT из ip_allocation — основной allocation lookup.
2. SELECT из inetnum/organisation — bulk RPSL обогащение.
3. **Если bulk RPSL пуст И `allocation.rir == "arin"` И
   `settings.rdap_fallback_enabled == True`** → синхронный GET к
   `rdap.arin.net/registry/ip/<addr>`.
4. RDAP response нормализуется в нашу inetnum/organisation shape,
   кэшируется в `rdap_cache` таблице с TTL=24h (positive) / 5min
   (negative).
5. Клиент получает заполненный `rpsl` блок. ``source: "ARIN-RDAP"``
   отмечает происхождение для диагностики.

Аналогично для `/v1/asn/{num}` → `rdap.arin.net/registry/autnum/<n>`.

**Только для ARIN.** Для RIPE/APNIC/AFRINIC у нас есть bulk RPSL —
RDAP избыточен и был бы slow path. Для LACNIC fallback можно добавить
позже отдельным шагом.

**Opt-in через env.** `RIR2LOCALDB_RDAP_FALLBACK_ENABLED=false` по
умолчанию — пользователь явно включает.

## Rationale

### Почему RDAP, а не Bulk Whois

| | RDAP | Bulk Whois |
|---|---|---|
| Доступ | open | account + ToU + API-key |
| Формат | JSON, standard | XML, ARIN-specific |
| Объём | one record per request | full dump (~1 GB) |
| Use case | on-demand lookup | bulk-mirror |
| Setup сложность | zero | bureaucratic |

Для нашего use case (заполнить миссинг блок при lookup'е) — RDAP
лучше. Bulk Whois стал бы Stage 5+, если будет реальный спрос на
on-prem full ARIN whois без RDAP rate-limit'ов.

### Почему transparent fallback

Клиент знает что `rpsl.inetnum` либо есть, либо null. Откуда данные
(RPSL dump vs RDAP) — детали реализации, не семантика API. `source:
"ARIN-RDAP"` отмечен в response для debug, но клиенты не должны
зависеть.

### Почему DB-cache, не in-memory

- API процесс может рестартоваться (Docker, systemd) — in-memory
  cache потерян.
- Multiple API instances (LB scenario) — каждый растит свой кэш,
  RDAP видит N× запросов.
- DB-cache shared, persistent, видим через `/v1/metrics`.

### Почему synchronous, не async-populate

Async (вернуть `null` сразу + populate в background): клиент должен
поллить или запрашивать дважды. Сложнее, требует task queue.

Synchronous: +5s timeout latency в худшем случае на uncached blocks.
Для cached — sub-millisecond. ARIN — это ~20% IP-блоков, большинство
запросов будут cache hit при TTL=24h. Acceptable.

### Rate-limit handling

Полагаемся на 429-ответы + negative cache. Не реализуем in-process
throttling. Если поймали 429 — кэшируем negative с TTL = max(default,
Retry-After). При множественных uncached lookup'ах быстро упираемся
в негативный кэш и нагрузка падает.

## Consequences

### Positive

- ARIN-блоки получают rich ownership/contacts ответы.
- Opt-in: outage RDAP-сервиса не ломает default install.
- Negative cache защищает от штормов запросов на отсутствующие блоки.
- Transparent: клиент не различает источник.

### Negative

- +5s latency worst-case на первый запрос uncached ARIN-блока.
- Зависимость от внешнего сервиса (rdap.arin.net) для ARIN coverage.
- Не покрывает 4-byte ASN edge cases без специальной валидации
  (RDAP сам это обрабатывает).

### Open

- **LACNIC** — такой же gap но не покрыт Stage 3-05. Можно добавить
  отдельным шагом (`rdap.lacnic.net`, аналогичный module-level
  base-URL).
- **Bulk Whois API** ARIN — если кому-то нужен on-prem full whois
  без rate-limits, добавляется как отдельный source tier
  (`arin-bulk`) с env-gated API-key.
- **Cache invalidation на ANALYZE** — pg_class.reltuples для
  rdap_cache не критично, всё через явный TTL.

## Implementation

- Миграция `0006_add_rdap_cache` — single таблица с TTL.
- `src/rir2localdb/api/rdap.py` — `lookup_ip_rdap` / `lookup_asn_rdap`
  + normalizers + DB-cache helper.
- `api/routers/{ip,asn}.py` — conditional integration после bulk RPSL
  fetch.
- `api/app.py` lifespan — long-lived `httpx.AsyncClient` для RDAP
  pooling.
- `sync/gc.py` — cleanup `rdap_cache` старее 7 дней.
- `api/metrics.py` — `rir2localdb_rdap_lookups_total` (counter),
  `rir2localdb_rdap_cache_entries{status}` (gauge).
- `Settings.rdap_fallback_enabled` / `rdap_cache_ttl_hours` /
  `rdap_negative_cache_minutes` / `rdap_http_timeout_seconds`.
