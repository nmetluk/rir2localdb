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

Добавляется поле `rpsl` в ответе `/ip` и `/asn`:

```json
{
  "query": "193.0.6.139",
  "allocations": [ ... ],         // как раньше, из delegated
  "rpsl": {
    "inetnum": {
      "range": "193.0.6.128 - 193.0.6.255",
      "netname": "RIPE-NCC-MNT",
      "country": "NL",
      "descr": "RIPE Network Coordination Centre",
      "status": "ASSIGNED PA",
      "org_handle": "ORG-RIEN1-RIPE",
      "mnt_by": ["RIPE-NCC-MNT"],
      "created": "2003-03-17T12:15:57Z",
      "last_modified": "2017-12-04T14:46:39Z",
      "source": "RIPE"
    },
    "organisation": {
      "handle": "ORG-RIEN1-RIPE",
      "org_name": "Reseaux IP Europeens Network Coordination Centre",
      "org_type": "RIR",
      "address": [...],
      "abuse_c": "AR17615-RIPE"
    }
  },
  "sources": ["delegated", "rpsl"]
}
```

Для ARIN — `rpsl` отсутствует (если нет Bulk Whois key), либо
содержит данные из ARIN Bulk Whois.

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
