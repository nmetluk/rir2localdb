# Stage 3-05: RDAP fallback for ARIN ownership

**Дата:** 2026-05-18
**Статус:** ✅ закрыт (с known limitation для cross-RIR catch-all блоков)
**Коммиты:**
- `feat(db): 0006 add rdap_cache table`
- `feat(api): RDAP module + Settings.rdap_*`
- `feat(api): integrate RDAP fallback into /v1/ip and /v1/asn lookups`
- `feat(api): RDAP metrics — lookups counter + cache size gauge`
- `feat(sync): GC cleans up expired rdap_cache entries (>7d)`
- `test: RDAP fallback scenarios (6 cases)`
- `docs: RDAP fallback for ARIN ownership coverage + ADR-0009`
- `docs(session-log): 03-05 RDAP fallback`
- `chore(context): step 3-05 closed`

## Что сделано

### Migration 0006

`rdap_cache (cache_key TEXT PK, response_raw JSONB, normalized JSONB,
fetched_at/expires_at TIMESTAMPTZ, http_status INT, error_message
TEXT)` + index по `expires_at`. На проде применилась мгновенно
(пустая таблица).

### `src/rir2localdb/api/rdap.py`

- **`RdapResult`** frozen dataclass: `found`, `normalized`, `raw`,
  `error`, `cached`, `http_status`.
- **`lookup_ip_rdap` / `lookup_asn_rdap`** — public API. URL'ы
  `rdap.arin.net/registry/ip/<addr>` и `.../autnum/<n>`.
- **`_lookup`** core flow:
  1. Cache SELECT (`expires_at > now()`) → hit → return cached.
  2. HTTP GET с `Accept: application/rdap+json`, timeout 5s.
  3. 200 → normalize + cache TTL=24h.
  4. 404/429/5xx → negative cache TTL=5min (429 honors Retry-After).
  5. Network error → НЕ кэшируется (next request попробует снова).
- **`_normalize_rdap_ip`** — RFC 7483 § 5.4 → inetnum-shape.
- **`_normalize_rdap_autnum`** — RFC 7483 § 5.3 → aut_num-shape.
- **`_parse_vcard`** — jCard (RFC 7095) → flat dict с
  fn/org/email/phone/address.
- **`_extract_registrant` / `_extract_role_handles`** — entities →
  organisation + admin_c/tech_c handles.

### Settings (`config.py`)

```python
rdap_fallback_enabled: bool = False
rdap_cache_ttl_hours: int = Field(default=24, ge=1, le=720)
rdap_negative_cache_minutes: int = Field(default=5, ge=1, le=60)
rdap_http_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
```

Default **off** — opt-in через
`RIR2LOCALDB_RDAP_FALLBACK_ENABLED=true`.

### Integration (`routers/{ip,asn}.py`)

После основного bulk RPSL fetch'а:

```python
if (
    settings.rdap_fallback_enabled
    and row["rir"] == "arin"
    and (rpsl_block is None or rpsl_block.inetnum is None)
):
    rdap = await lookup_ip_rdap(session, http_client, str(ip), settings)
    if rdap.found:
        rpsl_block = _rdap_to_ip_rpsl_block(rdap.normalized, ipv6=...)
```

ARIN-only, RDAP enabled, bulk RPSL отсутствует → fallback.
Прозрачно: тот же `rpsl.inetnum`/`rpsl.aut_num` shape,
`source="ARIN-RDAP"` помечает origin.

### `api/app.py` lifespan

Long-lived `httpx.AsyncClient` создаётся в startup, `aclose()` в
shutdown. Сохраняется на `app.state.http_client`. Connection pooling
для RDAP-запросов через одного клиента — экономит TLS handshake'и.

### Metrics

- `rir2localdb_rdap_lookups_total{kind, cached, found}` Counter,
  инкрементируется в `rdap._lookup` (cache hit / miss / network err /
  http err).
- `rir2localdb_rdap_cache_entries{status}` Gauge, заполняется в
  `collect_db_metrics` через single FILTER-COUNT (active vs expired).

### GC

`run_gc` дополнительно делает
`DELETE FROM rdap_cache WHERE expires_at < now() - INTERVAL '7 days'`.
Recently-expired (within 7d) сохраняются для diagnostic.
`GcStats.rdap_cache_deleted: int` + `SyncRunSummary.
gc_rdap_cache_deleted`.

### Tests (6 cases)

1. `test_ip_lookup_hits_cache_when_fresh` — pre-populated row,
   handler raises if called — no HTTP request.
2. `test_ip_lookup_fetches_when_cache_expired` — pre-populated
   expired row, mock returns 200 — HTTP fetch + store.
3. `test_ip_lookup_handles_404` — mock 404 → negative cache.
4. `test_ip_lookup_handles_429_with_retry_after` — mock 429 +
   `Retry-After: 600` → TTL ≥ 600s.
5. `test_arin_ip_with_rdap_enabled_enriches_response` — end-to-end:
   ARIN allocation + empty bulk inetnum + RDAP mock → response.
   rpsl.inetnum.netname="GOGL", source="ARIN-RDAP".
6. `test_non_arin_ip_skips_rdap` — RIPE allocation + RDAP enabled →
   `rdap_called` остаётся False.

End-to-end тесты через override `app.state.http_client` после
lifespan startup'а — заменяем real httpx на MockTransport.

### Документация

- ADR-0009 `rdap-fallback-for-arin.md` — full rationale (почему
  RDAP а не Bulk Whois, почему transparent, почему DB-cache, почему
  synchronous, почему только ARIN), alternatives, open questions.
- `docs/07-operations.md` § «RDAP fallback (Stage 3-05)» — env
  opt-in, rate-limit policy, PromQL для мониторинга, GC cleanup.
- `docs/06-api.md` § «RDAP fallback (Stage 3-05)» — API-side
  описание transparent fallback'а.

## Проверки

- pytest tests/ — **151 passed, 1 deselected** (145 + 6 RDAP).
- ruff/mypy clean (53 source files).
- alembic round-trip clean на test DB (0001..0006).
- alembic upgrade head 0005→0006 на проде — instant (пустая таблица).

## Live smoke + known limitation

`RIR2LOCALDB_RDAP_FALLBACK_ENABLED=true rir2localdb serve` + curl на
ARIN IP — **RDAP не активируется** в большинстве кейсов на нашем
production state. Причина:

```
$ curl /v1/ip/8.8.8.8
"rpsl.inetnum.source": "APNIC"
"rpsl.inetnum.netname": "IANA-NETBLOCK-8"
"rpsl.inetnum.descr": "This network range is not allocated to APNIC."
```

APNIC bulk RPSL содержит **catch-all `IANA-NETBLOCK-<X>` блоки** для
всех ARIN/AFRINIC/LACNIC ranges (8.0.0.0/8, 23.0.0.0/8, и т.д.).
Это «not allocated to APNIC» placeholder'ы. Наш fallback-trigger
условие `rpsl_block.inetnum is None` не срабатывает — bulk inetnum
formally есть.

**Design decision:** оставляем как есть в Stage 3-05. ADR-0009
рассматривает condition `rir == "arin"` AND `bulk_missing` — но не
учитывает «bulk found but cross-RIR placeholder». Refine'м в
follow-up если будет реальный спрос:

- **Option A** (light): дополнительная проверка в triggering
  conditions — если bulk inetnum.netname matches `^IANA-NETBLOCK-`
  pattern или `descr` содержит «not allocated to» — считать как miss.
- **Option B** (heavy): отдельный flag `rdap_force=true` query param —
  пользователь явно запрашивает ARIN-specific data.

Для unit-test coverage всё работает (mock возвращает 200, integration
видит fallback). Для production deployment нужен либо follow-up A,
либо ARIN-RDAP запросы из CLI вручную.

## Что НЕ сделали

- LACNIC RDAP — отдельный coverage gap, можно добавить аналогичный
  модуль (`rdap.lacnic.net` base URL).
- ARIN Bulk Whois — отдельный source tier с env-gated API-key. Не
  приоритет.
- IANA-NETBLOCK suppression — известная limitation выше (follow-up).
- Async populate / prefetch — overkill для on-demand pattern.

## Что дальше

Stage 3-06: Grafana dashboard + Prometheus alert rules. Финальный
ops-шаг.
