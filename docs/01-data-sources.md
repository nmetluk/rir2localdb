# 01 · Источники данных

Подробная карта того, что и где лежит у каждого RIR.
Машинно-читаемая версия этого документа — `src/rir2localdb/sources.py`.

## Слои источников

Чтобы не путать «всё со всем», файлы делятся на **tier**'ы:

- **`core`** — `delegated-<rir>-extended-latest` плюс `.md5`. Есть у
  всех пяти RIR. Минимально необходимый и достаточный набор для
  ответа «к какому реестру и стране относится этот IP/ASN и каков
  его статус». Объём небольшой (~20–40 МБ суммарно), парсится
  быстро, обновляется ежедневно.
- **`rich`** — RPSL split-dumps. Только у RIPE / APNIC / AFRINIC.
  Дают netname, описание, организацию, контакты, маршруты, AS-блоки.
  Объём существенный (RIPE inet6num ~36 МБ gzip'ом, aut-num ~8 МБ,
  и т.д.). Парсятся как поток RPSL-объектов.
- **`arin-rr`** — публичный IRR-дамп ARIN (`pub/rr/arin.db.gz`).
  Это **не** whois ARIN, а только маршрутные / mntner-объекты RPSL:
  `route`, `route6`, `as-set`, `mntner`. Объектов `inetnum`,
  `organisation`, `role`, `person`, `aut-num` с описаниями **в этом
  файле нет**. Полезно для дополнения ASN-ответов маршрутными
  политиками; для ownership-данных нужны другие источники (см. ниже).
- **`arin-bulk`** — официальный ARIN Bulk Whois. Требует API-ключ и
  согласие с ToU. Подключается опционально (через
  `RIR2LOCALDB_ARIN_BULK_KEY` env var). Stage 2+.

В Stage 1 включён только `core`. `rich` и `arin-rr` — в Stage 2.

## По каждому RIR

### AFRINIC

- Корень: `https://ftp.afrinic.net/pub/`
- Delegated extended: `pub/stats/afrinic/delegated-afrinic-extended-latest`
  + `.md5`
- RPSL dump (один файл): `pub/dbase/afrinic.db.gz`
- README по delegated: `pub/stats/afrinic/README-EXTENDED.txt`
- Зона: Африка.
- Особенности: единый файл вместо split'а, поэтому парсер должен
  уметь разделять объекты по типу (`inetnum:`, `inet6num:`, `aut-num:`,
  `organisation:` ...) налету.

### APNIC

- Корень: `https://ftp.apnic.net/`
- Delegated extended: `stats/apnic/delegated-apnic-extended-latest`
  + `.md5`
- RPSL split: `pub/apnic/whois/`
  - `apnic.db.inetnum.gz`
  - `apnic.db.inet6num.gz`
  - `apnic.db.aut-num.gz`
  - `apnic.db.organisation.gz`
  - `apnic.db.route.gz`, `apnic.db.route6.gz`
  - `apnic.db.as-block.gz`, `apnic.db.as-set.gz`
  - `apnic.db.mntner.gz`, `apnic.db.role.gz`, `apnic.db.irt.gz`
- Зона: Азия + Океания.

### ARIN

- Корень: `https://ftp.arin.net/pub/`
- Зона: Северная Америка + часть Карибов.

ARIN — единственный из пяти RIR, у которого **нет публичного полного
whois-дампа**. Поэтому источники у него делятся жёстче, чем у
остальных, и важно не путать слои.

**Открыто без ключа, используем:**

- **Delegated extended:** `pub/stats/arin/delegated-arin-extended-latest`
  + `.md5`. Полный, ежедневный, без ограничений по содержимому.
  Tier `core`. Из чего состоит: см. раздел «Формат delegated-extended»
  ниже. Это то, что позволяет ответить «к какому реестру и стране
  относится IP/ASN», но без названия организации и контактов.

- **Публичный IRR:** `pub/rr/arin.db.gz`. RPSL-формат, **но только
  маршрутные и mntner-объекты**: `route`, `route6`, `as-set`, `mntner`.
  В этом файле **нет** `inetnum`, `organisation`, `role`, `person`,
  `aut-num` с описаниями. Tier `arin-rr` (Stage 2). Полезно для
  маршрутных политик к ASN, не заменяет whois.

**Открыто, но не используем (для справки):**

- `pub/rpki/` — RPKI-репозиторий. Отдельный домен валидации, для
  нашей задачи вне scope.
- `pub/rwhois/` — список делегированных rwhois-серверов клиентов
  ARIN. Не централизованные данные, бесполезно для зеркала.

**Закрыто, только по API-ключу:**

- **Bulk Whois:**
  `https://accountws.arin.net/public/rest/downloads/bulkwhois?apikey=...`
  Требует заявки в ARIN, согласия с ToU и API-ключа. Отдаёт полный
  whois (inetnum, organisation, POC, role, customer) ZIP-архивом с
  XML и TXT. Tier `arin-bulk` (Stage 2, опционально).
  Включается через env `RIR2LOCALDB_ARIN_BULK_KEY` — без ключа этот
  tier недоступен.

**Стратегия по rich-данным ARIN в Stage 2** (финализируется в начале
Stage 2):

1. **База:** ежедневный mirror `pub/rr/arin.db.gz` (`arin-rr`)
   — даёт маршрутные/mntner-объекты для ASN-ответов.
2. **On-demand обогащение:** RDAP-запросы к `https://rdap.arin.net/`
   на lookup-time для конкретных IP/ASN — добывает ownership и
   контакты, кэшируется в локальную таблицу с TTL. Уважает
   ARIN rate-limit (см. их публичную политику).
3. **Опциональный fallback:** Bulk Whois API для инсталляций, готовых
   пройти ToU и держать полный whois локально без RDAP-latency.

То есть RDAP-обогащение по умолчанию вытесняет необходимость заявки
на Bulk Whois для типичного публичного сервиса. Заявка по-прежнему
имеет смысл, если нужно (а) full-table-scan по ownership, (б)
работа в офлайне, (в) тысячи lookup'ов в секунду.

### LACNIC

- Корень: `https://ftp.lacnic.net/pub/`
- Delegated extended: `pub/stats/lacnic/delegated-lacnic-extended-latest`
  + `.md5`
- Спецификация формата: `pub/stats/lacnic/RIR-Statistics-Exchange-Format.txt`
- Публичного bulk-whois нет. Для богатых данных у LACNIC — RDAP по
  адресам, с rate-limit; опциональное обогащение в Stage 5.
- Зона: Латинская Америка + Карибы.

### RIPE NCC

- Корень: `https://ftp.ripe.net/`
- Delegated extended: `pub/stats/ripencc/delegated-ripencc-extended-latest`
  + `.md5` (зеркало пути `ripe/stats/ripencc/...`).
- RPSL split: `ripe/dbase/split/`
  - `ripe.db.inetnum.gz` / `.utf8.gz`
  - `ripe.db.inet6num.gz` / `.utf8.gz`
  - `ripe.db.aut-num.gz` / `.utf8.gz`
  - `ripe.db.organisation.gz` / `.utf8.gz`
  - `ripe.db.route.gz` / `.route6.gz`
  - `ripe.db.as-block.gz`, `ripe.db.as-set.gz`
  - `ripe.db.mntner.gz`, `ripe.db.role.gz`, `ripe.db.irt.gz`
  - `ripe.db.domain.gz`, `ripe.db.filter-set.gz`, ...
  - Плюс `ripe-nonauth.db.*` — RIPE-NONAUTH (зеркалирование объектов
    из других регионов для IRR).
- Берём `.utf8.gz` версии (там личные данные дамифицированы и
  кодировка нормализована).
- Зона: Европа, Ближний Восток, Центральная Азия.

## Формат delegated-extended (одной таблицей)

Стандартизирован NRO. Пайп-разделённый, ASCII. Линии:

```
2|ripencc|20260517|180123|19920901|20260516|+0200
ripencc|*|asn|*|18054|summary
ripencc|*|ipv4|*|65521|summary
ripencc|*|ipv6|*|95702|summary
ripencc|DE|asn|196608|1|20070801|allocated|A91...|e-stats
ripencc|FR|ipv4|2.0.0.0|65536|20100712|allocated|A91...|e-stats
ripencc|GB|ipv6|2a00:1450::|32|20080801|allocated|A91...|e-stats
```

Колонки записи: `registry|cc|type|start|value|date|status|opaque-id|extensions`.

- `type`: `asn` | `ipv4` | `ipv6`
- `start`:
  - для `ipv4` — первый адрес (dotted),
  - для `ipv6` — первый адрес (compressed),
  - для `asn` — номер.
- `value`:
  - для `ipv4` — количество адресов (не префикс!),
  - для `ipv6` — длина префикса,
  - для `asn` — количество подряд.
- `status`: `allocated` | `assigned` | `available` | `reserved` |
  `unallocated` | `ianapool` | ...
- `date`: `YYYYMMDD` или пусто.
- `opaque-id`, `extensions` — присутствуют только в extended-варианте.

## Стратегия валидации и расписания

- Проверка целостности: где есть `.md5` — сверять обязательно.
- Если md5 не совпал — retry (до N раз), потом fail и алерт.
- Условный GET: `If-Modified-Since` + `ETag` (для HTTPS). Если сервер
  отвечает 304 — файл не качаем, помечаем как «проверен сегодня,
  не изменился».
- Cron расписание по умолчанию — раз в сутки в 03:00 UTC (RIR'ы
  обычно публикуют новые файлы между 00:00 и 02:00 UTC).
- Если sync run упал на середине — поднимаем с того же файла,
  состояние каждого файла учитывается в таблице `sync_file`.

## Этика и легальность

Все перечисленные источники, кроме `arin-bulk`, **публичные** и
явно предназначены для зеркалирования. Никаких login/scrape/обхода
rate-limit'ов. Для `arin-bulk` действует ToU ARIN, ключ получает
владелец инсталляции и сам отвечает за соблюдение условий.
