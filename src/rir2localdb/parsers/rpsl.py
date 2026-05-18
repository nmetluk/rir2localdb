"""Streaming RPSL parser для whois split-дампов RIR'ов.

Stage 2 шаг 2-01 (skeleton). Реализация — в 02-01b после согласования Q1-Q6.

**Источники, которые мы будем парсить:**

- **RIPE NCC**: `ftp.ripe.net/ripe/dbase/split/ripe.db.{inetnum,inet6num,aut-num,
  organisation,role,person,route,route6,as-set,mntner,as-block}.utf8.gz` —
  11 файлов, по одному типу объектов в каждом. ``.utf8.gz``-варианты выбраны
  в ``sources.py`` (PII-данные дамифицированы, кодировка нормализована).
- **APNIC**: аналогично, `ftp.apnic.net/pub/apnic/whois/apnic.db.*.gz` —
  11 файлов.
- **AFRINIC**: единый дамп `ftp.afrinic.net/pub/dbase/afrinic.db.gz` —
  все типы объектов в одном файле. Парсер не различает источник, ETL
  диспатчит по первому ключу объекта.
- **ARIN IRR** (шаг 2-05): `ftp.arin.net/pub/rr/arin.db.gz` — только
  ``route``/``route6``/``as-set``/``mntner`` (не whois ARIN).

См. ``docs/01-data-sources.md`` § per-RIR и ``sources.py`` для полного каталога.

**Формат файла (упрощённо, полная спецификация — ``docs/05-parsers.md``):**

    inetnum:        193.0.0.0 - 193.0.0.255
    netname:        RIPE-NCC
    descr:          RIPE Network Coordination Centre
    descr:          Amsterdam, Netherlands
    +               continuation
                    continuation (space/tab)
    admin-c:        BRD-RIPE
    admin-c:        OPS4-RIPE
    status:         ASSIGNED PA
    source:         RIPE

    inetnum:        193.0.1.0 - 193.0.1.255
    ...

Правила:

1. Объекты разделены **пустой строкой**.
2. Каждый non-empty non-comment line — ``<key>:<whitespace><value>``.
3. **Continuation** — строка начинается с пробела/таба/``+``, склеивается
   с предыдущим значением через ``\\n``.
4. **Comments**: строки, начинающиеся с ``%`` или ``#``, пропускаются.
5. **Repeated attributes**: один ключ может встречаться несколько раз
   (``admin-c``, ``mnt-by``, ``descr``, ``remarks``, ...). Хранится как
   ``list[str]``.
6. **Primary key** объекта — первый non-comment атрибут (``inetnum:`` для
   inetnum-объекта, ``aut-num:`` для aut-num, etc.).
7. **Encoding**: RIPE отдаёт ``.utf8.gz`` (нормализовано). Прочие RIR'ы
   нативно UTF-8 (legacy ASCII совместимо).

**RFC references** (для уточнений edge-cases):

- RFC 2622 — RPSL.
- RFC 4012 — RPSLng (IPv6, multicast extensions).
- RIPE-181 — original Routing Policy specification.

**Что НЕ делает парсер** (out of scope, по контракту):

- Не пишет в БД (это ETL слой, шаг 2-03).
- Не валидирует семантику (правильный ли формат IP в ``inetnum:``).
- Не разрешает ссылки между объектами (``admin-c`` → ``role``-объект).
- Не различает RIR — input file — это просто bytes, диспетчеризация
  по ``source.rir`` и по типу объекта (``next(iter(obj))``) — задача ETL.

**Открытые вопросы (см. ``.claude/session-log/02-01a-rpsl-parser-skeleton.md``):**

- Q1: тип значения — всегда ``list[str]`` vs динамический ``str | list[str]``.
- Q2: пустые значения и continuation к ним.
- Q3: lowercase ключей (нормализация).
- Q4: dict vs per-object-type dataclass.
- Q5: gzip-streaming vs in-memory decompress.
- Q6: что делать с битыми объектами.
- Q7: как клиент узнаёт тип объекта (key-as-first vs synthetic field)?

Defaults в session-log.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Тип объекта: ключ → значения. Если Q1 → (b) "always list", тип будет
# `dict[str, list[str]]`. На skeleton-этапе оставляем union как маркер
# того, что решение не зафиксировано; в 02-01b сузим.
RpslObject = dict[str, str | list[str]]


@dataclass(frozen=True, slots=True)
class RpslParseStats:
    """Статистика прогона ``parse_rpsl_with_stats``. Для observability в ETL.

    ``objects_skipped_empty`` — объекты, между разделителями которых
    оказались только comments (не yield'ятся). Полезно как sanity-check
    что дамп не побит.

    ``bytes_consumed`` — uncompressed-байт после gzip-декомпрессии.
    ``lines_total`` — все строки файла, включая comments и пустые.
    """

    objects_yielded: int = 0
    objects_skipped_empty: int = 0
    lines_total: int = 0
    bytes_consumed: int = 0


def parse_rpsl(path: Path) -> Iterator[RpslObject]:
    """Стримит RPSL-объекты из файла ``.gz`` или plain text.

    Args:
        path: путь к локальному файлу. ``.gz``-расширение или magic-bytes
            ``\\x1f\\x8b`` → транспарентно через ``gzip.open``. Иначе —
            ``open`` в текстовом режиме.

    Yields:
        ``RpslObject`` для каждого валидного объекта в файле. Tail-объект
        (без trailing blank line) тоже yield'ится.

    Поведение по правилам формата:

    - Continuation lines (начинаются с space/tab/``+``) склеиваются с
      предыдущим значением через ``\\n``.
    - Repeated attributes собираются в ``list[str]`` (если Q1=(b) —
      всегда list, даже одно значение).
    - Comments (``%`` или ``#`` в начале строки) пропускаются.
    - Битые объекты (без primary key, без ``source:``, неконсистентные
      по формату) → пропуск с ``logger.warning`` (если Q6=(a)).
    - Encoding: для ``.utf8.gz`` и для plain ``.gz`` используется
      ``utf-8`` с ``errors='replace'`` (legacy ASCII совместимо).
    - Ключи нормализуются в lowercase (если Q3=(a) — default).

    Memory: O(размер одного объекта). Чтение потоковое — для ripe.db.inetnum
    (~700 MB uncompressed) хватает ~10 МБ RAM.
    """
    raise NotImplementedError("parse_rpsl — stage 2 step 2-01b impl")


def parse_rpsl_with_stats(
    path: Path,
) -> tuple[Iterator[RpslObject], RpslParseStats]:
    """То же, что :func:`parse_rpsl`, но с явной статистикой прогона.

    Returns:
        Кортеж ``(iterator, stats)``. ``stats`` мутабельно обновляется
        по мере итерации; финальные значения корректны только после
        исчерпания итератора. (Альтернатива — два прохода — дороже на
        больших файлах; ETL и так итерирует stream единожды.)

    Используется в ETL (шаг 2-03), чтобы зафиксировать
    ``parser_records_total`` / ``objects_skipped`` в ``sync_run.stats``
    JSONB, аналогично тому, как ``EtlStats`` фигурирует в
    ``SyncRunSummary``.
    """
    raise NotImplementedError("parse_rpsl_with_stats — stage 2 step 2-01b impl")


# ---------------------------------------------------------------------------
# Internal helpers — публичный API только два function'а выше.
# Конкретный design (например ``_iter_lines``, ``_emit_object``) выберу в 02-01b
# после согласования Q1-Q7. Здесь не объявляю, чтобы skeleton оставался узким.
# ---------------------------------------------------------------------------
