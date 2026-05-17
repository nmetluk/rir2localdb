"""Парсер NRO delegated-extended pipe-формата.

Полная спецификация формата и edge cases — в ``docs/05-parsers.md``.

Парсер **никогда не пишет в БД** и не читает сеть — чистый stream:
получает путь к локальному файлу, отдаёт итератор записей. ETL-слой
сам решает, что с ними делать.

Публичный API:

    DelegatedRecord  -- неизменяемый dataclass одной записи
    parse_delegated(path: Path) -> Iterator[DelegatedRecord]

Что парсер **пропускает** (не yield'ит):

    - пустые строки;
    - комментарии (`#...`);
    - version-header (длина 7 полей, ``parts[0].isdigit()``);
    - summary lines (``parts[3] == "*"``);
    - IANA-записи (``parts[0] == "iana"``) — решение из ``docs/05``,
      их в ``ip_allocation`` не пишем;
    - записи с неизвестным ``type`` (не из ``{asn, ipv4, ipv6}``) —
      `logger.warning`, дальше работаем; forward-compatible если NRO
      когда-нибудь расширит словарь.

Что парсер делает строго:

    - Битая дата (``99999999`` или похожее) → `ValueError` наверх.
      Битый файл должен фейлить шаг — это не данные, которые можно
      «починить» молчанием.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_KNOWN_TYPES = frozenset({"asn", "ipv4", "ipv6"})


@dataclass(frozen=True, slots=True)
class DelegatedRecord:
    """Одна запись delegated-extended формата."""

    registry: str
    """``ripencc`` / ``apnic`` / ``arin`` / ``lacnic`` / ``afrinic``.
    IANA-записи фильтруются на уровне парсера и сюда не доходят.
    """
    cc: str | None
    """ISO-3166 alpha-2 (``DE``, ``US``, ...) или ``ZZ`` (unknown по NRO).
    Пустая строка в файле → ``None``. ``ZZ`` остаётся ``"ZZ"``.
    """
    type: Literal["asn", "ipv4", "ipv6"]
    start: str
    """Первый ресурс: ASN (число строкой), dotted IPv4, compressed IPv6."""
    value: int
    """Размер блока. Для ipv4 — количество адресов; для ipv6 — длина
    префикса (NRO выдаёт IPv6 только выравненными CIDR); для asn —
    количество ASN подряд. Парсер семантику не интерпретирует.
    """
    date: date | None
    """Дата выделения; пусто или ``00000000`` → ``None``."""
    status: str
    """``allocated`` / ``assigned`` / ``available`` / ``reserved`` /
    ``unallocated`` / ``ianapool`` / ... — пропускаем как есть."""
    opaque_id: str | None
    """Поле extended-формата; в reduced-формате отсутствует → ``None``."""
    extensions: str | None
    """Поле extended-формата; в reduced-формате отсутствует → ``None``."""


def parse_delegated(path: Path) -> Iterator[DelegatedRecord]:
    """Стримить записи из delegated-extended файла.

    Args:
        path: путь к локальному файлу (как правило, из
            ``<data_dir>/cache/<rir>/delegated-<rir>-extended-latest``).

    Yields:
        ``DelegatedRecord`` по одной записи.

    Raises:
        ValueError: если дата в записи не парсится в `date`.
        OSError: если файл нечитаем.
    """
    with path.open("rt", encoding="ascii", errors="replace") as fp:
        for raw in fp:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue

            parts = line.split("|")
            if len(parts) < 6:
                continue
            if parts[0] == "iana":
                continue
            if len(parts) == 7 and parts[0].isdigit():
                continue  # version header
            if parts[3] == "*":
                continue  # summary line

            type_ = parts[2]
            if type_ not in _KNOWN_TYPES:
                logger.warning(
                    "delegated: unknown resource type %r at registry=%s, "
                    "skipping; line=%r",
                    type_,
                    parts[0],
                    line[:200],
                )
                continue

            yield DelegatedRecord(
                registry=parts[0],
                cc=parts[1] or None,
                type=type_,  # type: ignore[arg-type]
                start=parts[3],
                value=int(parts[4]),
                date=_parse_date(parts[5]),
                status=parts[6],
                opaque_id=parts[7] if len(parts) > 7 and parts[7] else None,
                extensions=parts[8] if len(parts) > 8 and parts[8] else None,
            )


def _parse_date(s: str) -> date | None:
    """``"YYYYMMDD"`` → `date`; пустая строка или ``"00000000"`` → ``None``.

    Невалидную дату пропускает наверх через ``ValueError`` —
    битый файл должен фейлить шаг.
    """
    if not s or s == "00000000":
        return None
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
