"""ETL: ``DelegatedRecord`` stream → ``ip_allocation`` / ``asn_allocation``.

Hot path по ADR-0005: работает напрямую с ``asyncpg.Connection``, не через
SQLAlchemy ORM. Алгоритм — variant A из ``docs/03-database-schema.md``
§ «Стратегия обновления»:

    1. CREATE TEMP TABLE staging_ip, staging_asn (ON COMMIT DROP).
    2. ``conn.copy_records_to_table(staging_ip, records=...)``  -- pure asyncpg.
    3. INSERT ... ON CONFLICT (rir, family, start_text, value) DO UPDATE …
       FROM staging_ip into ip_allocation.
    4. Аналогично для staging_asn / asn_allocation
       (ключ ``(rir, start_asn, count)``).

**Транзакционность.** Функция предполагает, что ``conn`` уже в активной
транзакции (открытой оркестратором). TEMP TABLE с ``ON COMMIT DROP``
будет удалена при commit/rollback этой внешней транзакции. ETL свою
транзакцию не открывает.

**Idempotency staging-таблиц.** Если ETL вызывается дважды в одной
внешней транзакции, TEMP-таблица уже существует — поэтому используем
``DROP TABLE IF EXISTS … CREATE TEMP TABLE …``. Стоимость нулевая.

**`first_seen_run` / `last_seen_run` логика в UPSERT'е.** На INSERT
оба = ``run_id`` (из VALUES). На UPDATE в SET включаем
``last_seen_run = EXCLUDED.last_seen_run``, а ``first_seen_run`` НЕ
упоминаем — в Postgres ON CONFLICT DO UPDATE колонки, не перечисленные
в SET, не меняются. Таким образом дата первого появления записи
консервируется.

**Stale-records.** Записи, не вошедшие в текущий run, остаются с
устаревшим ``last_seen_run``. GC — Stage 3 ops; в этом ETL ничего не
делаем (см. open question в CONTEXT.md).

**Counting inserted vs updated.** Используем PostgreSQL-трюк
``RETURNING (xmax = 0) AS inserted``: на INSERT'е xmax свежей строки
ещё 0; на UPDATE — это xid текущей транзакции. Стабильно работает
поверх лет, не задокументировано формально.

Публичный API:

    apply_delegated_etl(conn, records, run_id) -> EtlStats

См. также ``docs/05-parsers.md`` (формат входа) и ``docs/03`` (схема таблиц).
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

import asyncpg

from rir2localdb.parsers.delegated import DelegatedRecord

logger = logging.getLogger(__name__)


# Порядок колонок staging-таблиц. Жёстко закреплён: используется в
# CREATE TEMP TABLE, в copy_records_to_table и в INSERT-from-staging.
# Маппинги в _record_to_*_row следуют ему строго.

_IP_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "cc",
    "family",
    "range_v4",
    "range_v6",
    "prefix_length",
    "start_text",
    "value",
    "status",
    "allocated_on",
    "opaque_id",
    "extensions",
)

_ASN_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "cc",
    "asn_range",
    "start_asn",
    "count",
    "status",
    "allocated_on",
    "opaque_id",
    "extensions",
)


@dataclass(frozen=True, slots=True)
class EtlStats:
    """Счётчики одного вызова ``apply_delegated_etl``.

    Все поля 0 если input пуст. ``records_seen`` — общее количество
    итераций по input'у (включая ``skipped_unsupported_type``).
    ``ip_records`` + ``asn_records`` + ``skipped_unsupported_type``
    == ``records_seen``.
    """

    records_seen: int = 0
    ip_records: int = 0
    asn_records: int = 0
    skipped_unsupported_type: int = 0
    ip_inserted: int = 0
    ip_updated: int = 0
    asn_inserted: int = 0
    asn_updated: int = 0


# ---------------------------------------------------------------------------
# Публичный entry point.
# ---------------------------------------------------------------------------


async def apply_delegated_etl(
    conn: asyncpg.Connection,
    records: Iterable[DelegatedRecord],
    run_id: int,
) -> EtlStats:
    """Загрузить delegated-записи в ``ip_allocation``/``asn_allocation``.

    Шаги:
        1. Партиционировать входной поток на ip/asn-tuples (Python).
        2. CREATE TEMP TABLE staging_ip / staging_asn.
        3. copy_records_to_table → staging_ip / staging_asn.
        4. INSERT … ON CONFLICT DO UPDATE из staging в основные.
        5. Собрать ``EtlStats``.

    Args:
        conn: открытое asyncpg-соединение в активной транзакции
            (вызывающий код владеет transaction lifecycle).
        records: поток ``DelegatedRecord`` (например, из
            ``parsers.parse_delegated``). Iterable; функция итерирует
            его один раз и буферизует в памяти (для типичного RIR
            delegated-файла это ~50k записей, ~10 МБ).
        run_id: id текущего ``sync_run`` (FK target для
            ``first_seen_run`` / ``last_seen_run``).

    Returns:
        ``EtlStats`` с полным набором счётчиков.

    Raises:
        ValueError: если ``rec.start`` для ipv4/ipv6 не парсится в
            ip-адрес, либо для asn не парсится в int.
        asyncpg.PostgresError: на нарушении constraints (CHECK по
            family, FK на sync_run и т.д.) — внешняя транзакция
            откатится вызывающим кодом.
    """
    raise NotImplementedError("apply_delegated_etl — stage 1 step 6")


# ---------------------------------------------------------------------------
# Маппинги DelegatedRecord → staging-row.
#
# Чистые функции, без БД. Тестируются unit'ом. Порядок полей в кортеже
# строго соответствует _IP_COLUMNS / _ASN_COLUMNS.
# ---------------------------------------------------------------------------


def _record_to_ip_row(rec: DelegatedRecord) -> tuple[Any, ...]:
    """Маппинг ipv4/ipv6 записи в кортеж staging_ip.

    Вызывающий гарантирует ``rec.type in {"ipv4", "ipv6"}``.

    Семантика полей (соответствует ``docs/03`` § ``ip_allocation``):

    - **ipv4**: ``family=4``, ``range_v4 = asyncpg.Range(start_int,
      start_int + value)`` (полуоткрытый ``[start, end)``, discrete),
      ``range_v6 = None``, ``prefix_length = None``,
      ``start_text = rec.start`` (dotted-строка как есть; INET-колонка
      сама нормализует).
    - **ipv6**: ``family=6``, ``range_v4 = None``,
      ``range_v6 = asyncpg.Range(start_int, start_int + 2 ** (128 - value))``,
      ``prefix_length = rec.value``, ``start_text`` — canonical compressed
      form через ``str(ipaddress.IPv6Address(rec.start))``.

    Прочие поля: ``rir``, ``cc``, ``value``, ``status``, ``allocated_on``,
    ``opaque_id``, ``extensions`` — копируются из ``rec``.
    """
    raise NotImplementedError


def _record_to_asn_row(rec: DelegatedRecord) -> tuple[Any, ...]:
    """Маппинг asn-записи в кортеж staging_asn.

    Вызывающий гарантирует ``rec.type == "asn"``.

    - ``asn_range = asyncpg.Range(start_asn, start_asn + count)``,
      где ``start_asn = int(rec.start)``, ``count = rec.value``.
    - ``start_asn``, ``count`` хранятся как отдельные колонки —
      удобно для индекса по ``start_asn`` и для отображения.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# DDL/DML helpers.
# ---------------------------------------------------------------------------


async def _create_staging_tables(conn: asyncpg.Connection) -> None:
    """``DROP IF EXISTS`` + ``CREATE TEMP TABLE`` для staging_ip и staging_asn.

    Layout staging_ip совпадает с подмножеством ``ip_allocation`` —
    все payload-колонки из ``_IP_COLUMNS``, БЕЗ ``id`` / ``first_seen_run``
    / ``last_seen_run`` (они добавляются на этапе INSERT-from-staging).
    Аналогично для staging_asn ↔ ``asn_allocation``.

    ``ON COMMIT DROP`` — таблицы исчезают при commit/rollback внешней
    транзакции. ``DROP IF EXISTS`` перед ``CREATE`` — идемпотентность
    на случай повторного вызова в той же транзакции.
    """
    raise NotImplementedError


async def _upsert_ip_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """``INSERT … SELECT FROM staging_ip … ON CONFLICT DO UPDATE``.

    Натуральный ключ конфликта: ``(rir, family, start_text, value)``.

    Поведение колонок run-метаданных:
        - ``first_seen_run = $run_id`` в VALUES; на UPDATE-ветке НЕ
          упоминается в SET → preserve старого значения.
        - ``last_seen_run = $run_id`` и в VALUES, и в SET (=EXCLUDED).

    Подсчёт INSERT vs UPDATE через ``RETURNING (xmax = 0) AS inserted``:
    на свежевставленной строке ``xmax = 0``; на updated — ``xid`` нашей
    транзакции.

    Returns:
        ``(inserted_count, updated_count)``.
    """
    raise NotImplementedError


async def _upsert_asn_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для ``asn_allocation``. Натуральный ключ ``(rir, start_asn, count)``."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Маленькие чистые помощники (тестируются inline в test_delegated_etl).
# ---------------------------------------------------------------------------


def _ipv4_to_int(start: str) -> int:
    """``"8.0.0.0"`` → ``134217728``. ``ValueError`` если не парсится."""
    return int(ipaddress.IPv4Address(start))


def _ipv6_to_int_and_canonical(start: str) -> tuple[int, str]:
    """``"2001:0db8::1"`` → ``(int_form, "2001:db8::1")``.

    Canonical compressed form — для записи в ``start_text`` (колонка
    ``INET`` сама бы нормализовала, но мы храним predictable форму
    для unit-test'ов и для downstream-сравнений).
    """
    addr = ipaddress.IPv6Address(start)
    return int(addr), str(addr)
