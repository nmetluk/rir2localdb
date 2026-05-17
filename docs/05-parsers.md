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

RPSL — это «whois-формат», на котором написаны inetnum, aut-num,
organisation и пр.

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

- **Объекты разделены пустой строкой.**
- **Атрибут — это `<name>: <value>`** (имя может содержать `-`).
  Двоеточие, потом пробелы (1+), потом значение.
- **Продолжение значения** — следующая строка, начинающаяся либо
  с пробела/таба, либо с `+`. Например:
  ```
  descr:          Foo
                  Bar
  +               Baz
  ```
  → атрибут `descr` со значением `Foo\nBar\nBaz`.
- **Атрибут может повторяться** (`mnt-by`, `admin-c`, `remarks`, ...).
  Все значения — массив.
- **Комментарии**: `%` и `#` в начале строки в bulk-дампах
  обычно отсутствуют, но парсер должен их пропускать.
- **Кодировка**: для RIPE берём `.utf8.gz` (она UTF-8 нормализована).
  APNIC и AFRINIC — UTF-8 без `.utf8` суффикса.

### Скелет парсера

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

Стрим работает на потоке gz: `with gzip.open(path, 'rt', encoding='utf-8') as f`.

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
| Битый объект (без `:` в строке) | Логировать, пропускать |
| Объект без первого атрибута, известного нам | Сохранять в `raw`, поле `type='unknown'` |
| Файл оборвался посередине объекта | Не yield'ить недоделанный |
| Дубль `primary_key` в одном файле | Последний выигрывает + предупреждение в логе |
| Кодировка не UTF-8 (старые AFRINIC, например) | Fallback на `latin-1`, пометить файл в `sync_file.last_status='warn'` |
