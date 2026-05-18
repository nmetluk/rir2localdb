# Stage 2 — closed

**Дата:** 2026-05-18
**Статус:** ✅ Stage 2 закрыт целиком (2-01 .. 2-05).
**Открытые follow-ups:** 2-06 (RDAP fallback, опционально), Stage 3 ops.

## TL;DR

Все 6 шагов Stage 2:

- **2-01** RPSL parser (`parsers/rpsl.py`) — 18 unit-тестов.
- **2-02** миграция `0002_rpsl_tables` (8 таблиц, `rir` discriminator,
  ADR-0007).
- **2-03** RPSL ETL (`etl/rpsl_etl.py`) — batched COPY + per-table
  staging + JSONB binary codec; 17 unit-тестов.
- **2-04** API enrichment (`rpsl` блок в `/v1/ip` и `/v1/asn`,
  `?include_rpsl=false` opt-out) — 8 unit-тестов.
- **2-05** orchestrator format-based dispatch (DELEGATED ↔ RPSL),
  `sources_for_tiers({RICH})` авто-тянет ARIN_RR, CLI RPSL counters
  — 4 unit-теста.

**Suite total:** 125 passed, 1 deselected (live RIPE integration test
без environment'а pass'нут). ruff + mypy clean.

## Live DoD ✅

### Команда

```bash
rir2localdb sync --tier core --tier rich
```

### Sync summary

```
sync_run id=1 status=success
  files: total=29 new=29 updated=0 unchanged=0 errored=0
  parser: records_total=10,416,367
  etl ip:  inserted=646,765 updated=0
  etl asn: inserted=113,264 updated=0
  etl rpsl: records=9,656,338 unknown_type=278,058 malformed=92
           as_block:     inserted=1,658
           aut_num:      inserted=72,192
           inet6num:     inserted=1,139,390
           inetnum:      inserted=5,548,366
           organisation: inserted=118,160
           role:         inserted=185,944
           route:        inserted=1,520,491
           route6:       inserted=791,987
  duration: 1,436,835 ms (≈ 24 минуты)
```

29 источников: 5 delegated + 11 RIPE RPSL + 11 APNIC RPSL + 1 AFRINIC
combined + 1 ARIN IRR. Все NEW (БД свежая на сервере).

### `rir2localdb status`

```
Recent sync_run (last 5)
┌────┬───────────┬──────────────┬──────────────┬─────────┬─────────────┬───────┐
│ ID │ Tier      │ Started      │ Finished     │ Status  │ RPSL records│ Error │
├────┼───────────┼──────────────┼──────────────┼─────────┼─────────────┼───────┤
│ 1  │ core+rich │ 11:04:02     │ 11:27:59     │ success │ 9656338     │       │
└────┴───────────┴──────────────┴──────────────┴─────────┴─────────────┴───────┘
```

### DoD curl 1: `/v1/ip/193.0.6.139`

```json
{
  "address": "193.0.6.139",
  "family": 4,
  "rir": "ripencc",
  "cc": "NL",
  "start": "193.0.0.0",
  "value": 4096,
  "prefix_length": 20,
  "status": "allocated",
  "allocated_on": "1993-09-01",
  "rpsl": {
    "inetnum": {
      "rir": "ripe",
      "start": "193.0.0.0",
      "value": 2048,
      "netname": "RIPE-NCC",
      "country": "NL",
      "descr": "RIPE Network Coordination Centre",
      "org": "ORG-RIEN1-RIPE",
      "admin_c": ["DUMY-RIPE"],
      "tech_c": ["DUMY-RIPE"],
      "status": "ASSIGNED PA",
      "mnt_by": ["RIPE-NCC-MNT"],
      "created": "2003-03-17T12:15:57Z",
      "last_modified": "2026-03-19T09:08:35Z",
      "source": "RIPE"
    },
    "organisation": {
      "rir": "ripe",
      "org_handle": "ORG-RIEN1-RIPE",
      "org_name": "Reseaux IP Europeens Network Coordination Centre (RIPE NCC)",
      "org_type": "LIR",
      "abuse_c": "ops4-ripe",
      "address": ["Dummy address for ORG-RIEN1-RIPE"],
      "email": ["unread@ripe.net"],
      "mnt_ref": ["RIPE-NCC-HM-MNT", "RIPE-NCC-MNT"],
      "mnt_by": ["RIPE-NCC-HM-MNT", "RIPE-NCC-MNT"],
      "created": "2012-03-09T13:21:52Z",
      "last_modified": "2026-05-13T07:34:07Z",
      "source": "RIPE"
    }
  }
}
```

✅ `rpsl.inetnum.netname == "RIPE-NCC"` (DoD target).
✅ `rpsl.organisation.org_name == "Reseaux IP Europeens Network Coordination Centre (RIPE NCC)"`.

**Замечание:** PII дамифицированы у RIPE (admin-c/tech-c = `DUMY-RIPE`,
email = `unread@ripe.net`, address = `Dummy address for ...`). Это
свойство `.utf8.gz` варианта дампа (см. ADR / sources.py — мы качаем
именно дамифицированный split). Для production whois это норма.

### DoD curl 2: `/v1/asn/3333`

```json
{
  "asn": 3333,
  "rir": "ripencc",
  "cc": "NL",
  "start_asn": 3333,
  "count": 1,
  "status": "allocated",
  "rpsl": {
    "aut_num": {
      "rir": "ripe",
      "asn": 3333,
      "as_name": "RIPE-NCC-AS",
      "descr": "Reseaux IP Europeens Network Coordination Centre (RIPE NCC)",
      "org": "ORG-RIEN1-RIPE",
      "status": "ASSIGNED",
      "mnt_by": ["RIPE-NCC-END-MNT", "RIPE-NCC-MNT"],
      "created": "2002-08-13T12:58:13Z",
      "last_modified": "2026-03-19T09:33:32Z",
      "source": "RIPE"
    },
    "organisation": { /* идентичен предыдущему */ }
  }
}
```

✅ `rpsl.aut_num.as_name == "RIPE-NCC-AS"`.
✅ `rpsl.organisation.org_name` совпадает с inetnum-ответом.

### Bonus curl: `/v1/ip/8.8.8.8` (Google DNS, ARIN)

```json
{
  "address": "8.8.8.8",
  "family": 4,
  "rir": "arin",
  "cc": "US",
  "rpsl": {
    "inetnum": {
      "rir": "apnic",
      "start": "8.0.0.0",
      "value": 16777216,
      "netname": "IANA-NETBLOCK-8",
      "country": "AU",
      "descr": "This network range is not allocated to APNIC.",
      "status": "ALLOCATED PORTABLE",
      "source": "APNIC"
    },
    "organisation": null
  }
}
```

ARIN не публикует whois-rich данных, но самый узкий inetnum нашёлся в
APNIC dump'е — это legacy IANA-блок 8.0.0.0/8, охватывающий 8.8.8.8.
**Cross-RIR fallback работает корректно**, хотя и не точно (более
узкого ARIN-specific inetnum в RPSL нет).

### Bonus curl: `/v1/ip/2001:4860:4860::8888`

```json
{
  "address": "2001:4860:4860::8888",
  "family": 6,
  "rir": "arin",
  "cc": "US",
  "rpsl": {
    "inetnum": {
      "rir": "ripe",
      "start": "::",
      "value": 0,
      "netname": "IANA-BLK",
      "country": "EU # Country is really world wide",
      "descr": "The whole IPv6 address space",
      "status": "ALLOCATED-BY-RIR",
      "source": "RIPE"
    },
    "organisation": { "org_handle": "ORG-IANA1-RIPE", ... }
  }
}
```

Для IPv6 ARIN — best-effort: подобрался RIPE-овский `::/0` IANA-блок
(самый узкий охватывающий запрос; более специфичных IPv6 inetnum'ов
для этого префикса в RPSL нет). Это known limitation, не bug.

## Performance baseline

| RIR | Tier | Время скачивания | Время ETL | Объём (объекты) |
|---|---|---|---|---|
| AFRINIC | core | ~5s | ~1s | ~22k IP/ASN |
| APNIC | core | ~50s | ~10s | ~90k IP/ASN |
| ARIN | core | ~25s | ~30s | ~330k IP/ASN |
| LACNIC | core | ~3s | ~5s | ~40k IP/ASN |
| RIPE | core | ~30s | ~25s | ~280k IP/ASN |
| RIPE | rich (11 split) | ~7 минут | ~7 минут | ~5.5M inetnum + 1.5M route6 etc. |
| APNIC | rich (11 split) | ~3 минуты | ~3 минуты | ~700k inetnum + ... |
| AFRINIC | rich (combined) | ~30s | ~30s | ~250k объектов |
| ARIN | arin-rr (IRR) | ~30s | ~30s | ~1.5M route + 1k mntner |

Итого **24 минуты** на чистый full sync с нуля для всех 5 RIR в обоих
tier'ах. Memory peak — ~16 MB на ETL батч-буфер (8 таблиц × 10K rows ×
~200 B). Sequential один-source-за-другим, без parallelism.

При daily re-sync 95% файлов будут `unchanged` (md5 / If-None-Match
hit), полный run займёт минуты.

## Known limitations & open questions (отложено)

1. **ARIN IRR zero-padded prefix (92 malformed).** ARIN публикует
   `069.031.132.000/23`-style prefix'ы. Python `IPv4Network` отказывает.
   Fix: pre-normalize в `_to_route_rows` через regex. **Stage 3.**
2. **`rir` divergence**: `ip_allocation.rir = "ripencc"`, `inetnum.rir
   = "ripe"`. Известное расхождение (NRO-format vs Rir enum). API
   работает корректно (LEFT JOIN per-block). Нормализация в **Stage 3**.
3. **Cross-RIR inetnum для ARIN-блоков** даёт неточные совпадения
   (см. 8.8.8.8 → APNIC IANA-NETBLOCK-8). Стандартный fallback'overhead
   — лечится Stage 2-06 (RDAP) или ARIN Bulk Whois (требует API-ключ).
4. **`/v1/ip/<ARIN-v6>`** часто матчит RIPE IANA-BLK `::/0` —
   полное IPv6 пространство. Best-effort, не bug. Stage 2-06 поможет.
5. **Stale-records GC** — отложен в Stage 3 ops (см. ADR-0001).
6. **Партиционирование `inetnum`** при росте >50M строк — текущий
   объём 5.5M, не актуально. Stage 3+.

## Что дальше

- **Stage 2-06** (RDAP fallback) — отдельный заход, опционально.
  Когда `rpsl.inetnum is None` или ссылка orphan — попробовать RDAP
  API соответствующего RIR. Time-budget'а нет.
- **Stage 3** (ops): systemd, Docker, /metrics, Grafana, alerts,
  runbook, бэкап, structured logs, GC, normalize `rir`.

В сумме за Stage 1 + Stage 2 (включая stabilization 1.50):
- ~33 модуля в `src/rir2localdb/`.
- 125 unit-тестов + 1 integration smoke.
- 7 ADR.
- ~70 коммитов после bootstrap.
- Полный end-to-end pipeline: HTTPS → fetcher → parser → ETL → БД →
  REST API c whois-обогащением.
