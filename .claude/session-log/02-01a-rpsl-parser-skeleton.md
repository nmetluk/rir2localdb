# Stage 2, шаг 2-01a: RPSL parser skeleton

**Дата:** 2026-05-18
**Статус:** ⚠️ частично (skeleton, ждёт Q1-Q7)
**Коммиты:** `08fb213` (skeleton), `43bf0c3` (docs/05 RPSL spec)

## Что сделано

- **`src/rir2localdb/parsers/rpsl.py`** — публичная поверхность:
  - `RpslObject = dict[str, str | list[str]]` (union на skeleton-этапе;
    Q1 сужает до `list[str]` в 02-01b).
  - `RpslParseStats` (frozen dataclass) с 4 полями:
    `objects_yielded`, `objects_skipped_empty`, `lines_total`,
    `bytes_consumed`.
  - `parse_rpsl(path) -> Iterator[RpslObject]` — `raise NotImplementedError`.
  - `parse_rpsl_with_stats(path) -> tuple[Iterator, RpslParseStats]` —
    `raise NotImplementedError`.
  - Docstring модуля — полный контекст: какие источники (RIPE 11
    split-файлов, APNIC 11, AFRINIC 1, ARIN IRR 1), правила формата,
    RFC ссылки, что НЕ делает парсер (БД, валидация, ссылки).
- **`src/rir2localdb/parsers/__init__.py`** — re-export
  `RpslObject`, `RpslParseStats`, `parse_rpsl`, `parse_rpsl_with_stats`.
- **`tests/test_rpsl_parser.py`** — single `pytest.skip` placeholder.
- **`docs/05-parsers.md`** — секция RPSL расширена:
  RFC ссылки (2622, 4012, RIPE-181), таблица 11 типов объектов,
  уточнённые правила формата, расширенная таблица edge-cases
  (tail-object, duplicates, mid-file EOF, encoding fallback).

## Проверки

- `ruff check` + `mypy` — clean (4 source files, 35 total).
- `pytest tests/test_rpsl_parser.py` — 1 skipped (placeholder).

## Вопросы к согласованию

### Q1. Тип значения — `str | list[str]` динамически или всегда `list[str]`?

(a) Динамический: одиночное значение — `str`, повторяющееся — `list`.
   Удобно при чтении, клиент должен `isinstance`.

(b) Всегда `list[str]`: даже одно значение → `["value"]`. Клиентский
   код упрощается (`obj["admin-c"][0]`), +5% памяти на одиночные.

**Мой default — (b)**, всегда list. Downstream ETL итерирует по
`obj.get("admin-c", [])` без `isinstance`. Объект почти всегда
содержит хотя бы одно повторяющееся поле (`descr`, `mnt-by`), так
что union типы только вредят.

### Q2. Пустые значения / continuation-only

`org: ` (двоеточие, пробел, ничего) — yield пустую строку или пропустить?
Continuation lines к пустому значению — конкатенация с `""`?

**Мой default**: пустое значение → yield `""` (атрибут присутствует,
value пустое); continuation lines склеиваются нормально через `\n`.
Это даёт differentiate между "атрибут есть, но пустой" и "атрибута нет".

### Q3. Чувствительность к регистру ключей

RIPE / APNIC / AFRINIC — lowercase. ARIN IRR — может быть Mixed Case.

**Мой default — (a)** всегда `key.lower()` при парсинге. RFC 2622
описывает RPSL как case-insensitive. Нормализуем на входе.

### Q4. Тип возврата — `dict` или per-object-type dataclass?

(a) `dict[str, list[str]]` — универсально, парсер не знает про схему.

(b) `RpslInetnum` / `RpslAutNum` / ... — 11 разных dataclass'ов,
   парсер инстанцирует правильный по первому ключу.

**Мой default — (a)**. Причины:
- 11 типов объектов = 11 dataclass'ов, plus support per-RIR расширения
  схемы (admin-c у одних, role-c у других) — много кода.
- ETL и так маппит на колонки таблиц (`<rir>_inetnum`, `<rir>_aut_num`,
  ...) — типизация полезна там, не здесь.
- Парсер остаётся универсальным; новый тип объекта не требует правок
  парсера.

### Q5. Gzip-streaming или загрузка всего файла в память?

Файлы:
- `ripe.db.inetnum.utf8.gz` ≈ 50 MB gzip, ~700 MB uncompressed.
- `afrinic.db.gz` ≈ 30 MB gzip, ~400 MB uncompressed.

(a) `gzip.open(path, 'rt')` — стрим, O(1) память на парсер.

(b) `gzip.decompress` в bytes → split → parse. Быстрее CPU, но
   700 MB RAM на parser-instance, плюс мы можем иметь 5 файлов
   параллельно в Stage 3.

**Мой default — (a)**, стрим. Производительность достаточная
(оценка 30-60 сек/файл из docs/05, мы не in real-time pipeline).
Stage 3 ops может включить параллельность по RIR'ам, и стрим
позволяет 5 parser'ов без OOM-риска.

### Q6. Что делать с битыми объектами?

(a) Skip + warning лог.

(b) Yield as-is, пусть ETL фильтрует.

(c) Raise — fail-fast.

**Мой default — (a)**, skip + warning. RIR-дампы иногда содержат
legacy-объекты с отсутствующими полями (исторический мусор); один
битый объект не должен валить парсинг 5 миллионов хороших.
Warning видим в логах, аномалии диагностируются.

### Q7. Как клиент узнаёт тип объекта?

Парсер возвращает `dict[str, list[str]]`. Чтобы ETL диспатчил по
типу (`inetnum_etl` vs `aut_num_etl`), нужен способ узнать тип.

(a) **Convention**: тип — первый ключ. `next(iter(obj))` возвращает
   `"inetnum"` / `"aut-num"` / etc. Python dict сохраняет insertion
   order с 3.7. Контракт парсера: первый ключ — primary attribute.

(b) **Synthetic field**: парсер добавляет `obj["_object_type"] = "inetnum"`.
   Явно, но засоряет namespace ключей.

(c) **Wrapper-объект**: `RpslObject(type=str, primary_key=str, attrs=dict)`
   как namedtuple. Самый явный, но переписывает наш текущий
   `RpslObject` alias.

**Мой default — (a) convention**. Простой и идиоматичный. Insertion
order гарантирован Python ≥3.7. ETL делает `obj_type = next(iter(obj))`.
Edge case (пустой объект) уже отфильтрован Q6 → `parser_skipped_empty`.

## Открытые вопросы для следующих шагов (не блокеры)

- **Производительность** RIPE inetnum (~5M объектов) — оценю в 02-01b
  на live прогоне после impl. Если >2 минут — оптимизируем
  (manual byte-scanning без `str.partition`, как в docs/05).
- **`source:` валидация** — некоторые объекты в `arin.db.gz` могут
  идти без `source:`. Сейчас допускаем. ETL может потребовать
  обязательного поля в шаге 2-05.
- **Memory-mapping** для очень больших файлов — пока не нужно,
  поток разумен. Если 5 RIR'ов в параллель окажутся IO-bound —
  Stage 3 ops.

## Что дальше

02-01b — реализация (после ответов на Q1-Q7):
- Тело `parse_rpsl` и `parse_rpsl_with_stats`.
- Тесты `tests/test_rpsl_parser.py`: 5-7 сценариев
  (single object, multiple objects, continuation, repeated attrs,
  comments, empty values, malformed skip, gzip auto-detect).
- Возможно, smoke-тест на маленьком фрагменте реального
  `ripe.db.as-block.utf8.gz` (он маленький, ~16 KB).

После 02-01b — шаг 2-02 (миграция `rpsl_tables` под per-RIR схемы).
