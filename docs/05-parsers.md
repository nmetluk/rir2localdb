# 05 · Парсеры

Два формата — два парсера.

## Delegated-extended (NRO format)

Полная спецификация — `https://www.nro.net/wp-content/uploads/nro-extended-stats-readme5.txt`.
Локальная копия не нужна, парсер тривиальный.

### Структура файла

```
2|ripencc|20260517|180123|19920901|20260516|+0200    ← version header (1 строка)
ripencc|*|asn|*|18054|summary                         ← summary lines (по одному на тип)
ripencc|*|ipv4|*|65521|summary
ripencc|*|ipv6|*|95702|summary
ripencc|DE|asn|196608|1|20070801|allocated|A91...|e-stats  ← records, дальше до EOF
...
```

Комментарии — строки, начинающиеся с `#`. Пустые строки —
пропускаются. CRLF и LF — оба нормально.

### Поля записи

```
0: registry    'ripencc' | 'apnic' | 'arin' | 'lacnic' | 'afrinic' | 'iana'
1: cc          ISO-3166 alpha-2, может быть 'ZZ' (unknown) или пусто
2: type        'asn' | 'ipv4' | 'ipv6'
3: start       первый ресурс (см. ниже)
4: value       размер блока (см. ниже)
5: date        'YYYYMMDD' или пусто
6: status      см. перечисление в delegated.py
7: opaque-id   (только в extended)
8: extensions  (только в extended; для нашего использования игнорим)
```

### Тонкости

- **`ipv4` value — это количество адресов, не префикс.**
  `65536` = `/16`. Но может быть и `20480`, что НЕ выравненный
  /20 — это объединение нескольких префиксов под одной записью.
  Поэтому в БД храним как range, а не как `cidr`.
- **`ipv6` value — это длина префикса, не количество.** Тут наоборот,
  RIR-ы выдают только выравненные блоки в IPv6.
- **Записи могут перекрываться по диапазону**, если один блок
  «вырезан» из другого (например, выделен суб-блок). Поэтому
  при lookup'е берём «самую специфичную» (по размеру) запись.
- **`status = 'reserved'` и `'available'`** включаются в файл.
  Не путать с `'allocated'`/`'assigned'`. В API можно отдавать всё,
  но фильтр по статусу полезен.
- **Записи `iana`** — встречаются в extended-формате когда RIR
  показывает, что какой-то блок ему ещё не передан. Их в наш
  `ip_allocation` не пишем (или пишем с `rir='iana'`, если это
  будет интересно).

### Скелет парсера

```python
@dataclass
class DelegatedRecord:
    registry: str
    cc: str | None
    type: Literal['asn', 'ipv4', 'ipv6']
    start: str
    value: int
    date: date | None
    status: str
    opaque_id: str | None
    extensions: str | None

def parse_delegated(path: Path) -> Iterator[DelegatedRecord]:
    with path.open('rt', encoding='ascii') as f:
        for line in f:
            line = line.rstrip('\r\n')
            if not line or line.startswith('#'):
                continue
            parts = line.split('|')
            if len(parts) < 6 or parts[3] == '*':   # version/summary
                continue
            yield DelegatedRecord(...)
```

## RPSL

RPSL — «whois-формат» (Routing Policy Specification Language), на котором
написаны inetnum, aut-num, organisation, person, role, mntner и пр.
Используется RIPE NCC, APNIC, AFRINIC, ARIN (только route/route6/as-set/mntner
в публичном IRR).

**RFC-ссылки:**

- [RFC 2622](https://www.rfc-editor.org/rfc/rfc2622) — RPSL (original, 1999).
  Базовая структура объектов, синтаксис атрибутов.
- [RFC 4012](https://www.rfc-editor.org/rfc/rfc4012) — RPSLng (2005).
  IPv6 (`inet6num`, `route6`), multicast extensions.
- [RIPE-181](https://www.ripe.net/publications/docs/ripe-181) — Routing
  Policy specification (предшественник RPSL).

**Типы объектов** (which we mirror via split-dumps):

| Тип | Что описывает | Primary key |
|---|---|---|
| `inetnum` | IPv4-блок и кому он назначен | `inetnum: 193.0.0.0 - 193.0.0.255` |
| `inet6num` | IPv6-префикс | `inet6num: 2001:7fd::/32` |
| `aut-num` | ASN, его routing policy | `aut-num: AS3333` |
| `route` | Маршрут IPv4 от ASN | `route: 193.0.0.0/22`, `origin: AS3333` |
| `route6` | Маршрут IPv6 | `route6: 2001:7fd::/32`, `origin: AS3333` |
| `as-block` | Диапазон ASN (для координации) | `as-block: AS1 - AS65535` |
| `as-set` | Набор AS (политики) | `as-set: AS-CUSTOMERS` |
| `organisation` | Юр.лицо | `organisation: ORG-RIEN1-RIPE` |
| `role` | Контактная группа (NOC, abuse) | `role: nomcom@example` |
| `person` | Контактное лицо (в RIPE дамифицировано в `.utf8.gz`) | `person: J. Doe` |
| `mntner` | Maintainer объекта (право редактирования) | `mntner: RIPE-NCC-MNT` |

### Структура

```
inetnum:        193.0.0.0 - 193.0.7.255
netname:        RIPE-NCC
descr:          RIPE Network Coordination Centre
country:        NL
admin-c:        BRD-RIPE
tech-c:         OPS4-RIPE
status:         ASSIGNED PA
mnt-by:         RIPE-NCC-MNT
created:        2003-03-17T12:15:57Z
last-modified:  2017-12-04T14:46:39Z
source:         RIPE

inetnum:        193.0.8.0 - 193.0.15.255
...
```

- **Объекты разделены пустой строкой** (`\n\n` или CRLF-эквивалент).
  Tail-объект (без trailing blank line) тоже валидный — парсер должен
  его yield'ить.
- **Атрибут — это `<name>: <value>`** (имя может содержать `-`).
  Двоеточие, потом whitespace (0+), потом значение. RFC 2622 §2 разрешает
  как минимум один пробел после `:`; на практике часто tab-выравнивание.
- **Продолжение значения** — следующая строка, начинающаяся либо
  с пробела/таба, либо с `+`. Семантика по RFC 2622 §2:
  ```
  descr:          Foo
                  Bar
  +               Baz
  ```
  → атрибут `descr` со значением `Foo\nBar\nBaz` (в нашем парсере —
  склейка через `\n`).
- **Атрибут может повторяться** (`mnt-by`, `admin-c`, `tech-c`,
  `remarks`, `descr`, `member-of`, `mp-import`, ...). Все значения
  хранятся как `list[str]`.
- **Primary key** — значение первого non-comment атрибута. Тип
  объекта определяется по имени этого атрибута (`inetnum:` →
  inetnum-объект, и т.д.).
- **Комментарии**: строки, начинающиеся с `%` или `#`, пропускаются.
  В bulk split-дампах редки, но возможны (header'ы, разделители).
  Inline `%` / `#` (не в начале строки) — литерал, частью значения.
- **Регистр ключей**: RPSL спецификация case-insensitive. RIPE / APNIC
  / AFRINIC используют lowercase консистентно. ARIN IRR может
  встречаться Mixed Case. Парсер нормализует в lowercase.
- **Кодировка**: для RIPE берём `.utf8.gz` (PII-данные дамифицированы,
  UTF-8 нормализована). APNIC и AFRINIC — `.gz` без `.utf8` суффикса,
  но фактически UTF-8 (legacy ASCII совместимо). Парсер открывает с
  `encoding="utf-8", errors="replace"` — устойчивость к редким
  битым байтам, без падения на одном плохом инструменте.
- **Размер**: RIPE `ripe.db.inetnum.utf8.gz` ~50 MB gzip, ~700 MB
  uncompressed; AFRINIC `afrinic.db.gz` ~30 MB / ~400 MB. Парсер
  потоковый, O(размер одного объекта) по памяти.

### Скелет парсера

См. `src/rir2localdb/parsers/rpsl.py` — публичный API + контракт.
Реализация в шаге 2-01b после согласования Q1-Q7. Образец-референс
(оригинальный из `docs/05` Stage 0):

```python
def parse_rpsl(reader: Iterable[str]) -> Iterator[dict[str, list[str]]]:
    """Yield RPSL objects as {attr_name: [value1, value2, ...]} dicts."""
    obj: dict[str, list[str]] = {}
    current_attr: str | None = None

    for raw in reader:
        line = raw.rstrip('\r\n')

        if not line:
            if obj:
                yield obj
                obj = {}
                current_attr = None
            continue

        if line[0] in (' ', '\t', '+'):
            if current_attr is None:
                continue                # malformed; skip
            obj[current_attr][-1] += '\n' + line[1:].lstrip()
            continue

        if line[0] in ('%', '#'):       # comment
            continue

        name, sep, value = line.partition(':')
        if not sep:
            continue                    # malformed; skip
        attr = name.strip().lower()
        obj.setdefault(attr, []).append(value.strip())
        current_attr = attr

    if obj:
        yield obj
```

Стрим через gz: `with gzip.open(path, 'rt', encoding='utf-8',
errors='replace') as f`.

Автоопределение gzip: сначала по расширению `.gz`, fallback на
magic-bytes `\x1f\x8b` в первых двух байтах файла.

### Распознавание типа объекта

Тип объекта определяется по **первому** атрибуту. Например, объект
с `inetnum:` как первым атрибутом — это inetnum-объект. Парсер
помечает каждый yield типом:

```python
def parse_rpsl_objects(reader): -> Iterator[RpslObject]:
    for raw in parse_rpsl(reader):
        first_attr = next(iter(raw))   # порядок ключей сохраняем
        yield RpslObject(type=first_attr, primary_key=raw[first_attr][0], attrs=raw)
```

Чтобы сохранить порядок ключей — используем обычный `dict`
(в Python 3.7+ упорядочен).

### Производительность

- 5 млн объектов в `ripe.db.inetnum.utf8.gz` парсятся за ~30–60 с
  в чистом Python. Если станет узким местом — переписываем
  парсер на ручной байтовый сканер (без `str.partition`),
  будет в 3–5 раз быстрее.
- ETL — узкое место. Лечится `COPY` в staging-таблицу пачками.

## Граничные случаи

| Кейс | Что делать |
|------|-----------|
| Битый объект (без `:` в строке) | Логировать, пропускать (Q6=a) |
| Объект без первого атрибута (только comments между разделителями) | Не yield'ить, инкрементировать `objects_skipped_empty` |
| Объект без `source:` атрибута | Допустим в RFC 2622, но в дампах RIR обычно есть. Yield, без warning'а. |
| Tail-объект без trailing blank line | Yield (валидный конец файла). |
| Файл оборвался посередине объекта (no trailing blank, no continuation closure) | Если есть primary key — yield as-is; иначе skip. |
| Дубль `primary_key` в одном файле | Парсер обоих yield'ит; ETL решает policy (last wins / merge). |
| Кодировка не UTF-8 | `errors='replace'` → `?` на месте битых байт. Не валим прогон. |
| Continuation line как самая первая строка объекта | Skip (malformed); current_attr — None. |
| `\\` в значениях | Литерал, не escape. |
| Trailing whitespace в значении | Strip'ается (`value.strip()`). |
