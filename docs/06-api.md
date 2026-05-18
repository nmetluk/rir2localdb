# 06 · REST API

Тонкий слой поверх БД. FastAPI, OpenAPI на `/docs`.

## Принципы

- Возвращаем **факты из БД**. Не делаем live-whois fallback.
- Ответ — JSON. Опционально `?format=text` отдаёт классическое
  whois-подобное оформление (для удобства человека).
- Все ошибки — `{detail: ...}` в стиле FastAPI, HTTP-коды стандартные.
- Версионируется через префикс `/v1`. Без него — алиас на текущую.

## Эндпоинты Stage 1

### `GET /v1/ip/{addr}`

Принимает IPv4 или IPv6 (любой адрес внутри подсети тоже работает —
ищем охватывающий блок).

**200 OK:**
```json
{
  "query": "8.8.8.8",
  "family": 4,
  "allocations": [
    {
      "rir": "arin",
      "cc": "US",
      "range": "8.0.0.0/9",
      "range_start": "8.0.0.0",
      "range_end": "8.127.255.255",
      "value": 8388608,
      "status": "allocated",
      "allocated_on": "1992-12-01",
      "opaque_id": "5b30..."
    }
  ],
  "sources": ["delegated"]
}
```

Если несколько записей охватывают адрес (один блок вырезан из
другого) — отдаём все, отсортированные от самой специфичной к самой
общей.

**404 Not Found:** диапазон не найден ни в одном RIR.

**400 Bad Request:** не валидный IP.

### Stale records (Stage 3-03)

Все lookup-endpoint'ы (`/v1/ip/`, `/v1/asn/`) **скрывают stale-записи
по умолчанию**. Запись считается stale если GC пометил её
`is_stale=TRUE` (не появлялась в последних N успешных sync'ах,
default N=7). См. ADR-0008.

- `?include_stale=true` — включить stale-записи в результат поиска.
  Полезно для аналитики или recovery после поломавшегося sync'а.
- Поле `is_stale: bool` присутствует во всех объектах ответа.
  Active записи имеют `is_stale: false`. Stale (когда запросили с
  `include_stale=true`) — `is_stale: true`.

### `GET /v1/asn/{num}`

```json
{
  "query": "AS15169",
  "asn": 15169,
  "allocations": [
    {
      "rir": "arin",
      "cc": "US",
      "range": "15169-15169",
      "start": 15169,
      "count": 1,
      "status": "allocated",
      "allocated_on": "2000-03-08",
      "opaque_id": "5b30..."
    }
  ],
  "sources": ["delegated"]
}
```

Принимает форматы: `15169`, `AS15169`, `as15169`.

### `GET /v1/status`

```json
{
  "last_run": {
    "id": 142,
    "tier": "core",
    "started_at": "2026-05-17T03:00:01Z",
    "finished_at": "2026-05-17T03:01:42Z",
    "status": "success",
    "stats": {
      "files_total": 10,
      "files_changed": 3,
      "files_not_modified": 7,
      "records_upserted": 18432
    }
  },
  "files": {
    "https://ftp.ripe.net/.../delegated-ripencc-extended-latest": {
      "last_status": "fresh",
      "last_fetched_at": "2026-05-17T03:00:18Z",
      "last_md5": "a1b2..."
    },
    ...
  }
}
```

### `GET /v1/healthz`

Тривиальный healthcheck для оркестраторов. 200 если БД отвечает.

## Эндпоинты Stage 2 (RPSL)

В Stage 2-04 ответы `/v1/ip/{addr}` и `/v1/asn/{num}` обогащаются
полем `rpsl`. Контракт обоих:

- `rpsl: null` — клиент попросил `?include_rpsl=false`.
- `rpsl: {...}` — блок присутствует. Поля внутри (`inetnum` /
  `inet6num` / `aut_num` / `organisation`) могут быть `null`, если
  данных нет (RPSL-дамп ещё не загружен, либо адрес/ASN не покрыт).

Различие между «не запросили» (`rpsl: null`) и «запросили, не нашли»
(`rpsl: { inetnum: null, organisation: null }`) важно: первое не
требует ничего пересчитывать, второе — сигнал «попробуйте сделать
sync rich-tier».

**Пример полного ответа `/v1/ip/193.0.6.139` (RIPE-блок):**

```json
{
  "address": "193.0.6.139",
  "family": 4,
  "rir": "ripencc",
  "cc": "NL",
  "start": "193.0.0.0",
  "value": 65536,
  "prefix_length": null,
  "status": "allocated",
  "allocated_on": "1992-06-09",
  "opaque_id": "ripe",
  "first_seen_run": 1,
  "last_seen_run": 1,
  "rpsl": {
    "inetnum": {
      "rir": "ripe",
      "start": "193.0.6.128",
      "value": 128,
      "netname": "RIPE-NCC",
      "country": "NL",
      "descr": "RIPE Network Coordination Centre",
      "org": "ORG-RIEN1-RIPE",
      "admin_c": ["RD123-RIPE"],
      "tech_c": ["OPS4-RIPE"],
      "status": "ASSIGNED PA",
      "mnt_by": ["RIPE-NCC-MNT"],
      "created": "2003-03-17T12:15:57Z",
      "last_modified": "2017-12-04T14:46:39Z",
      "source": "RIPE"
    },
    "organisation": {
      "rir": "ripe",
      "org_handle": "ORG-RIEN1-RIPE",
      "org_name": "Reseaux IP Europeens Network Coordination Centre",
      "org_type": "RIR",
      "abuse_c": "AR17615-RIPE",
      "address": ["Stationsplein 11", "1012 AB Amsterdam", "Netherlands"],
      "phone": ["+31 20 535 4444"],
      "email": ["hostmaster@ripe.net"],
      "fax_no": null,
      "mnt_ref": ["RIPE-NCC-HM-MNT"],
      "mnt_by": ["RIPE-NCC-MNT"],
      "created": "2004-04-17T12:00:00Z",
      "last_modified": "2020-09-10T08:21:12Z",
      "source": "RIPE"
    }
  }
}
```

**Пример полного ответа `/v1/asn/3333`:**

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
      "descr": "Reseaux IP Europeens Network Coordination Centre",
      "org": "ORG-RIEN1-RIPE",
      "admin_c": ["BRD-RIPE"],
      "tech_c": ["OPS4-RIPE"],
      "status": "ASSIGNED",
      "mnt_by": ["RIPE-NCC-MNT"],
      "source": "RIPE"
    },
    "organisation": { ... }
  }
}
```

**Outer query параметр.** `?include_rpsl=false` (default `true`)
выключает обогащение полностью; в ответе `"rpsl": null`. Это полезно
для bandwidth-sensitive батчевых клиентов, которым нужны только
delegated-данные.

**Cross-RIR org_handle.** RPSL допускает orphan-ссылки на
`organisation` (legacy / cross-RIR). Маппинг идёт по `(rir,
org_handle)`; если соответствия нет — `rpsl.organisation` будет `null`,
а `rpsl.inetnum.org` содержит исходный handle для трассировки.

**Для ARIN-данных.** ARIN не публикует RPSL-дампы кроме ARIN IRR
(routes/route6/as-sets/mntner). Therefore `/v1/ip/<US-addr>` пока
будет иметь `rpsl.inetnum = null`, `rpsl.organisation = null`. Stage
2-05 добавит ARIN IRR данные для route'ов; Stage 2-06 — RDAP fallback
для ownership.

## Что не делаем в API

- **Кэширование.** PostgreSQL с GiST-индексом отдаёт ответ за
  единицы миллисекунд. Кэш — преждевременная оптимизация.
- **Аутентификация.** Внутренний сервис. Если нужно — кладётся
  за reverse proxy с basic auth / API key. Не в коде сервиса.
- **Rate limiting.** То же самое — задача reverse proxy.
- **Bulk lookup endpoint.** Можно добавить в Stage 4, но
  отрицательно влияет на простоту. Пока — один адрес на запрос.

## Совместимость с whois-форматом

`?format=text` возвращает text/plain с раскладкой как у RIPE whois:

```
% Information related to '8.0.0.0/9'

inetnum:        8.0.0.0 - 8.127.255.255
netname:        (from delegated stats)
country:        US
status:         allocated
source:         ARIN (delegated)
```

Это не настоящий whois, и заголовок прямо это показывает. Цель —
читабельность.
