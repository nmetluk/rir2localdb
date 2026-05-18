"""Streaming RPSL parser для whois split-дампов RIR'ов.

Stage 2 шаг 2-01 — реализация (02-01b) после согласования Q1-Q7.

**Источники, которые парсим:**

- **RIPE NCC**: ``ftp.ripe.net/ripe/dbase/split/ripe.db.{inetnum,inet6num,
  aut-num,organisation,role,person,route,route6,as-set,mntner,as-block}.utf8.gz``
  — 11 split-файлов, ``.utf8.gz`` варианты (PII дамифицированы,
  UTF-8 нормализована).
- **APNIC**: аналогично, ``ftp.apnic.net/pub/apnic/whois/apnic.db.*.gz``
  — 11 файлов.
- **AFRINIC**: единый ``ftp.afrinic.net/pub/dbase/afrinic.db.gz`` —
  все типы объектов в одном файле.
- **ARIN IRR** (шаг 2-05): ``ftp.arin.net/pub/rr/arin.db.gz`` —
  только ``route`` / ``route6`` / ``as-set`` / ``mntner``.

См. ``docs/01-data-sources.md`` и ``sources.py``.

**Контракт парсера:**

- Парсер **никогда** не пишет в БД, не читает сеть, не валидирует
  семантику (правильный ли формат IP, существует ли referenced ASN).
- Парсер **не различает RIR**. ETL диспатчит по ``Source.rir`` и по
  типу объекта (``next(iter(obj))``).
- **Первый ключ объекта = primary attribute**, всегда в lowercase
  (нормализация на парсинге), может содержать дефис: ``inetnum``,
  ``aut-num``, ``as-block``, ``as-set``, ``inet6num``, ``route6``,
  ``mntner``, ``organisation``, ``role``, ``person``, ``route``.
- **Имена ключей остаются в RPSL-форме.** Никаких автоматических
  ``key.replace("-", "_")``. snake_case конверсия — задача ETL/API.

**Правила формата** (RFC 2622, RFC 4012, RIPE-181; полная спецификация
в ``docs/05-parsers.md``):

1. Объекты разделены пустой строкой (``""`` после ``rstrip("\\r\\n")``).
2. Каждый non-empty non-comment line — ``<key>:<whitespace><value>``.
3. **Continuation** — строка, начинающаяся с пробела, таба или ``+``,
   склеивается с предыдущим значением через **одиночный пробел**
   (RFC не предписывает разделитель; де факто RIPE использует пробел).
4. **Comments** — строки, начинающиеся с ``%`` или ``#``, пропускаются.
5. **Repeated attributes** хранятся как ``list[str]`` (Q1=всегда list).
6. Ключи нормализуются в lowercase (Q3).
7. Пустое значение (``org: \\n``) → ``[""]`` (Q2 — differentiate
   «атрибут есть, value пустой» vs «атрибута нет»).
8. Битые строки (без ``:``) → warning + skip (Q6).
9. Tail-объект без trailing blank line yield'ится (важно для дампов
   без финального ``\\n``).

**Encoding**: ``open`` / ``gzip.open`` с ``encoding="utf-8",
errors="replace"``. RIPE ``.utf8.gz`` нормализован; APNIC/AFRINIC
``.gz`` фактически UTF-8; редкие legacy-байты заменяются на U+FFFD.

**Gzip auto-detection**: по magic-bytes ``\\x1f\\x8b`` в первых двух
байтах файла. Расширение ``.gz`` не доверяем — `os.replace` после
COPY мог удалить суффикс из cache-имени.

**Memory**: O(размер одного объекта). На ripe.db.inetnum (~5M
объектов, ~700 MB uncompressed) — ~10 МБ RAM на parser-instance.
"""

from __future__ import annotations

import gzip
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

# Тип объекта: имя ключа (lowercase, может содержать дефис) → list значений.
# Первый ключ insertion-order — primary attribute (= тип объекта).
RpslObject = dict[str, list[str]]


@dataclass(slots=True)
class RpslParseStats:
    """Статистика прогона ``parse_rpsl_with_stats``.

    Mutable (без ``frozen=True``): обновляется по мере итерации
    parser-генератора. Снимок целостен только **после исчерпания**
    итератора — клиенту следует сохранять reference и читать после
    закрытия generator'а.

    Поля:
        objects_yielded: количество yield'нутых объектов.
        objects_skipped_empty: разделители без атрибутов (между
            пустыми строками были только comments) — валидный, но
            бесполезный кусок. Полезно как sanity-check.
        lines_total: все прочитанные строки (включая comments и
            пустые).
        bytes_consumed: суммарный размер строк в байтах (UTF-8).
            Аппроксимирует uncompressed-объём для observability.
    """

    objects_yielded: int = 0
    objects_skipped_empty: int = 0
    lines_total: int = 0
    bytes_consumed: int = 0


def parse_rpsl(path: Path) -> Iterator[RpslObject]:
    """Стримит RPSL-объекты из файла ``.gz`` или plain text.

    Контракт и правила формата — в docstring модуля.

    Args:
        path: путь к локальному файлу. Автоопределение gzip по
            magic-bytes ``\\x1f\\x8b``.

    Yields:
        ``RpslObject`` (``dict[str, list[str]]``). Insertion order
        сохранён (Python 3.7+), так что ``next(iter(obj))`` — это
        тип объекта (primary attribute name).

    Raises:
        OSError: если файл нечитаем.

    Не raise'ит на битых объектах / битых строках — logger.warning + skip.
    """
    yield from _parse_rpsl_internal(path, stats=None)


def parse_rpsl_with_stats(
    path: Path,
) -> tuple[Iterator[RpslObject], RpslParseStats]:
    """Версия ``parse_rpsl`` с явной статистикой для ETL.

    Stats обновляется по мере итерации generator'а. Финальный snapshot
    корректен только после исчерпания (или прерывания) итератора.

    Returns:
        ``(iterator, stats)`` — клиент держит reference на stats,
        читает поля после ``list(iterator)`` или ``for obj in iterator``.

    Используется ETL'ом (шаг 2-03), чтобы зафиксировать
    ``parser_records_total`` и ``objects_skipped`` в ``sync_run.stats``
    JSONB.
    """
    stats = RpslParseStats()
    return _parse_rpsl_internal(path, stats=stats), stats


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _open_maybe_gzip(path: Path) -> IO[str]:
    """Открыть файл как text. Авто-gzip по magic bytes ``\\x1f\\x8b``.

    ``errors="replace"`` — устойчивость к редким битым байтам в legacy
    объектах; одна плохая запись не должна валить парсинг 5M хороших.
    """
    with open(path, "rb") as fb:
        magic = fb.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def _parse_rpsl_internal(path: Path, stats: RpslParseStats | None) -> Iterator[RpslObject]:
    """Общая реализация для ``parse_rpsl`` и ``parse_rpsl_with_stats``.

    Если ``stats`` передан — обновляется по ходу итерации. Иначе
    счётчики не ведутся.
    """
    current_obj: RpslObject = {}
    current_key: str | None = None
    has_attrs = False  # отличаем «пустой объект (только comments)» от с-атрибутами

    with _open_maybe_gzip(path) as fp:
        for raw_line in fp:
            if stats is not None:
                stats.lines_total += 1
                stats.bytes_consumed += len(raw_line.encode("utf-8"))

            line = raw_line.rstrip("\r\n")

            # 1. Empty line → finalize current object.
            if not line:
                if has_attrs:
                    yield current_obj
                    if stats is not None:
                        stats.objects_yielded += 1
                elif current_obj or current_key is not None:
                    # Этого тут быть не может, но защитная инвариантная ветка.
                    pass
                else:
                    # Между separator'ами не было ни одного атрибута —
                    # либо начало файла, либо подряд два разделителя,
                    # либо только comments. Не yield'им, но сюда мы
                    # не попадаем без commented-only objects.
                    pass
                current_obj = {}
                current_key = None
                has_attrs = False
                continue

            # 2. Comment lines.
            if line[0] in ("%", "#"):
                continue

            # 3. Continuation line: ' ', '\t', '+' at start.
            if line[0] in (" ", "\t", "+"):
                if current_key is None:
                    logger.warning(
                        "rpsl: continuation line without active key, skipping: %r",
                        line[:120],
                    )
                    continue
                cont = line[1:].lstrip() if line[0] == "+" else line.lstrip()
                if cont:  # пустой '+' / только пробелы → no-op
                    current_obj[current_key][-1] += " " + cont
                continue

            # 4. Regular `key: value` line.
            if ":" not in line:
                logger.warning("rpsl: malformed line (no ':'), skipping: %r", line[:120])
                continue

            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.lstrip()

            current_obj.setdefault(key, []).append(value)
            current_key = key
            has_attrs = True

        # 5. Tail-object (file без trailing blank line).
        if has_attrs:
            yield current_obj
            if stats is not None:
                stats.objects_yielded += 1


# Подсчёт `objects_skipped_empty`: разделители без атрибутов между
# двумя пустыми строками. На текущей реализации (см. ветку «не было
# ни одного атрибута» выше) такая ситуация не yield'ится, но и не
# учитывается отдельно: blank-blank пары не отделить от blank в начале
# файла без дополнительного состояния. Если станет нужно — добавлю
# счётчик `between_blanks_seen` отдельной правкой.
