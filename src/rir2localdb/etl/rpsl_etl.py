"""ETL: ``RpslObject`` stream → 8 RPSL-таблиц.

Stage 2 шаг 2-03 — RPSL rich-tier ETL. Контракт и принципы заложены
в шаге 2-03a (skeleton); тела реализуются в 2-03b.

Hot path по ADR-0005: работает напрямую с ``asyncpg.Connection``, не
через SQLAlchemy. Схема описана в ADR-0007 (один объект-тип = одна
таблица, ``rir`` discriminator). 8 целевых таблиц:

    inetnum, inet6num, aut_num, organisation, route, route6,
    as_block, role

**Архитектура: dispatcher по первому ключу.** Парсер гарантирует
insertion order, и первый ключ — это primary attribute (= тип
объекта). Диспатч:

    obj_type = next(iter(obj))            # "inetnum", "aut-num", ...
    table     = _OBJECT_TYPE_TO_TABLE[obj_type]
    rows      = _ROW_MAPPERS[table](obj, rir, run_id)

Объекты неизвестных типов (``mntner``, ``person``, ``as-set``, ...)
учитываются в ``objects_skipped_unknown_type`` и пропускаются.

**Streaming + batched COPY.** Файл ripe.db.inetnum.gz содержит ~5M
объектов, ~700 МБ распакованных. Буферизовать всё в Python — RAM
1-2 GB. Поэтому ETL итерирует поток парсера и держит **8 батч-буферов
по таблицам**; когда любой буфер достигает ``_BATCH_SIZE`` (10000
строк), он COPY'ится в свою staging-таблицу и очищается. Финальный
flush после исчерпания итератора. Затем — 8 UPSERT'ов по фиксированному
порядку.

**Порядок UPSERT'ов.** Без FK между RPSL-таблицами порядок формально
не важен, но мы фиксируем canonical-порядок для предсказуемости логов:

    organisation → role → inetnum → inet6num → aut_num →
    route → route6 → as_block

**Multi-origin route.** RPSL-объект ``route`` с несколькими ``origin:``
атрибутами легален; PK ``(rir, prefix, origin)`` требует **по одной
строке на origin**. Маппер ``_to_route_rows`` возвращает ``list[tuple]``
длины N. Для других типов ``_to_<table>_row`` возвращает ``tuple | None``.

**Транзакционность.** Функция предполагает, что ``conn`` уже в активной
транзакции (открытой оркестратором). Все 8 staging-таблиц с ``ON COMMIT
DROP`` будут удалены при commit/rollback внешней транзакции. ETL свою
транзакцию не открывает. Падение любого UPSERT откатывает весь run.

**Стандартные правила:**

- ``first_seen_run`` на INSERT = ``run_id``, на UPDATE preserve старого.
- ``last_seen_run`` = ``run_id`` в обоих случаях.
- INSERT vs UPDATE — через ``RETURNING (xmax = 0) AS inserted``.
- Stale records — не трогаются, GC отложено в Stage 3 ops (см. ADR-0001).

Публичный API:

    apply_rpsl_etl(conn, objects, rir, run_id) -> RpslEtlStats

См. также ``docs/04-sync-pipeline.md`` § «RPSL ETL» и
``docs/05-parsers.md`` (формат входа).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

import asyncpg

from rir2localdb.parsers.rpsl import RpslObject

logger = logging.getLogger(__name__)


# Размер батча COPY → staging. Подобран как компромисс между
# overhead'ом одного COPY-вызова и RAM-footprint'ом батча (10k
# средних RPSL-объектов ≈ 2 МБ Python-tuples).
_BATCH_SIZE: Final[int] = 10_000


# Маппинг первого ключа RPSL-объекта на имя SQL-таблицы. Ключи —
# в RPSL-форме (с дефисами, lowercase), таблицы — в snake_case.
_OBJECT_TYPE_TO_TABLE: Final[dict[str, str]] = {
    "inetnum": "inetnum",
    "inet6num": "inet6num",
    "aut-num": "aut_num",
    "organisation": "organisation",
    "route": "route",
    "route6": "route6",
    "as-block": "as_block",
    "role": "role",
}


# Canonical порядок UPSERT'ов. Списки fixed, в logging это даёт
# стабильный output.
_UPSERT_ORDER: Final[tuple[str, ...]] = (
    "organisation",
    "role",
    "inetnum",
    "inet6num",
    "aut_num",
    "route",
    "route6",
    "as_block",
)


# Порядок колонок staging-таблиц. Жёстко закреплён: используется в
# CREATE TEMP TABLE, в copy_records_to_table и в INSERT-from-staging.
# Маппинги в _to_<table>_row следуют им строго.

_INETNUM_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "start_text",
    "value",
    "range_v4",
    "netname",
    "country",
    "descr",
    "org",
    "admin_c",
    "tech_c",
    "status",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)

_INET6NUM_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "start_text",
    "value",
    "range_v6",
    "netname",
    "country",
    "descr",
    "org",
    "admin_c",
    "tech_c",
    "status",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)

_AUT_NUM_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "asn",
    "as_name",
    "descr",
    "org",
    "admin_c",
    "tech_c",
    "status",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)

_ORGANISATION_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "org_handle",
    "org_name",
    "org_type",
    "address",
    "phone",
    "fax_no",
    "email",
    "abuse_c",
    "admin_c",
    "tech_c",
    "mnt_by",
    "mnt_ref",
    "created",
    "last_modified",
    "source",
    "raw",
)

_ROUTE_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "prefix",
    "origin",
    "descr",
    "org",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)

_ROUTE6_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "prefix",
    "origin",
    "descr",
    "org",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)

_AS_BLOCK_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "as_block_start",
    "as_block_end",
    "asn_range",
    "descr",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)

_ROLE_COLUMNS: Final[tuple[str, ...]] = (
    "rir",
    "nic_hdl",
    "role",
    "address",
    "phone",
    "fax_no",
    "email",
    "abuse_mailbox",
    "admin_c",
    "tech_c",
    "mnt_by",
    "created",
    "last_modified",
    "source",
    "raw",
)


_TABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "inetnum": _INETNUM_COLUMNS,
    "inet6num": _INET6NUM_COLUMNS,
    "aut_num": _AUT_NUM_COLUMNS,
    "organisation": _ORGANISATION_COLUMNS,
    "route": _ROUTE_COLUMNS,
    "route6": _ROUTE6_COLUMNS,
    "as_block": _AS_BLOCK_COLUMNS,
    "role": _ROLE_COLUMNS,
}


@dataclass(slots=True)
class RpslEtlStats:
    """Счётчики одного вызова ``apply_rpsl_etl``.

    Mutable (без ``frozen=True``): обновляется по мере итерации
    parser-генератора. Это отличается от ``EtlStats`` из delegated_etl
    (immutable, построен в конце), потому что RPSL ETL **стримит**
    и не имеет момента «всё собрано» до конца.

    Семантика:

    - ``objects_seen`` — итераций по входному потоку, включая skipped.
    - ``objects_by_type`` — counts по первому ключу объекта (raw RPSL
      type, например ``"inetnum"`` или ``"mntner"``). Покрывает в т.ч.
      типы, для которых таблицы нет.
    - ``objects_skipped_unknown_type`` — объекты типов вне 8 целевых
      таблиц (mntner, person, as-set, ...).
    - ``objects_skipped_malformed`` — объекты, которые маппер
      отверг (битый IP, нечисловой ASN, end<start и т.п.); см. Q1-Q3
      в session-log 02-03a.
    - ``upsert_inserted`` / ``upsert_updated`` — по таблицам.
      Сумма по всем таблицам может превышать ``objects_seen``
      (route/route6 multi-origin даёт N rows на 1 объект); sanity-check
      в тестах это учитывает.
    """

    objects_seen: int = 0
    objects_by_type: dict[str, int] = field(default_factory=dict)
    objects_skipped_unknown_type: int = 0
    objects_skipped_malformed: int = 0
    upsert_inserted: dict[str, int] = field(default_factory=dict)
    upsert_updated: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Публичный entry point.
# ---------------------------------------------------------------------------


async def apply_rpsl_etl(
    conn: asyncpg.Connection,
    objects: Iterable[RpslObject],
    rir: str,
    run_id: int,
) -> RpslEtlStats:
    """Загрузить RPSL-объекты в 8 таблиц через staging + UPSERT.

    Шаги:
        1. ``_create_staging_tables`` — 8 TEMP TABLE.
        2. Итерация ``objects``: каждый объект → диспатч по первому
           ключу → маппер возвращает ``tuple | None`` (или
           ``list[tuple]`` для route/route6).
        3. Append'им в per-table буфер; при ``len(buffer) >= _BATCH_SIZE``
           — flush через ``conn.copy_records_to_table``.
        4. После исчерпания итератора — flush всех остаточных буферов.
        5. По ``_UPSERT_ORDER`` — INSERT … ON CONFLICT DO UPDATE из
           каждой staging-таблицы; считаем inserted/updated через
           ``RETURNING (xmax = 0)``.

    Args:
        conn: открытое asyncpg-соединение в активной транзакции.
        objects: stream ``RpslObject`` (``parse_rpsl`` итератор).
            Итерируется один раз. Должен быть streaming-friendly
            (большие RPSL-дампы не помещаются в RAM целиком).
        rir: discriminator колонки ``rir`` (``"ripe"``, ``"apnic"``,
            ``"afrinic"``, ``"arin"``, ``"lacnic"``). Не валидируется —
            оркестратор отвечает.
        run_id: id текущего ``sync_run`` (FK target для
            ``first_seen_run`` / ``last_seen_run``).

    Returns:
        ``RpslEtlStats`` после завершения всех UPSERT'ов.

    Raises:
        asyncpg.PostgresError: на нарушении constraints (FK на
            sync_run, NOT NULL, и т.п.). Внешняя транзакция должна
            откатиться.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Маппинги RpslObject → staging-row(s).
#
# Чистые функции, без БД. Каждая получает (obj, rir, run_id) и
# возвращает кортеж в порядке соответствующего ``_<TABLE>_COLUMNS``.
# ``None`` означает «объект malformed, skip» — counter
# ``objects_skipped_malformed`` инкрементируется в dispatcher'е.
# ---------------------------------------------------------------------------


def _to_inetnum_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    """Маппинг ``inetnum:`` объекта в кортеж по ``_INETNUM_COLUMNS``.

    Q1: парсит ``inetnum: 193.0.0.0 - 193.0.0.255`` → ``start_text =
    "193.0.0.0"``, ``value = 256``, ``range_v4 = [start_int,
    start_int + value)``. Возвращает ``None`` если: битый IP,
    ``end < start``, формат не «IP - IP».

    descr: ``descr[0] if "descr" in obj else None`` (остальное в raw).
    admin-c/tech-c/mnt-by: ``obj.get(key)`` напрямую — список или None.
    created/last-modified: try-parse ISO-8601, None+warning при провале.
    raw: dict передаётся в asyncpg как есть (JSONB сериализатор сам
    обработает).
    """
    raise NotImplementedError


def _to_inet6num_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    """Маппинг ``inet6num:`` объекта.

    Q2: парсит ``inet6num: 2001:db8::/32`` → CIDR-only форма;
    ``start_text = "2001:db8::"``, ``value = 32`` (prefix_length
    SMALLINT), ``range_v6 = [start_int, start_int + 2**(128-32))``.
    Не-CIDR форма (``2001:db8:: - 2001:db8:ffff::``) встречается у
    legacy ARIN'а — возвращаем None+warning.
    """
    raise NotImplementedError


def _to_aut_num_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    """Маппинг ``aut-num:`` объекта.

    Q3: ``aut-num: AS3333`` → ``asn = 3333`` (BIGINT). Алгоритм:
    ``s = obj["aut-num"][0]``, ``s.removeprefix("AS")``, затем
    ``int(s)``. None+warning при ValueError или при asn > 2**32-1.

    ``as_name`` ← ``as-name:`` (если есть, первый).
    """
    raise NotImplementedError


def _to_organisation_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    """Маппинг ``organisation:`` объекта.

    ``org_handle = obj["organisation"][0]`` (raw value, обычно
    ``ORG-RIEN1-RIPE``). ``org_name`` ← ``org-name`` первый.
    Array-поля (address, phone, fax-no, email, admin-c, tech-c, mnt-by,
    mnt-ref) — Q7: если в объекте отсутствует, ``None`` (NULL), иначе
    ``obj[key]`` (list[str]). Пустой список не отдаём — он будет
    отличаться от NULL в partial-индексе по abuse_c.
    """
    raise NotImplementedError


def _to_route_rows(obj: RpslObject, rir: str, run_id: int) -> list[tuple[Any, ...]]:
    """Маппинг ``route:`` объекта в **N строк** (Q5: multi-origin).

    RPSL легально допускает несколько ``origin:`` атрибутов на один
    ``route``. PK ``(rir, prefix, origin)`` требует отдельной строки
    на каждый origin. Все N строк делят одинаковый ``raw`` JSONB
    (избыточность ~200 байт × N — приемлемо).

    Если ``origin:`` отсутствует или ``route:`` нечитаем как CIDR —
    возвращаем ``[]``; dispatcher инкрементирует
    ``objects_skipped_malformed``.
    """
    raise NotImplementedError


def _to_route6_rows(obj: RpslObject, rir: str, run_id: int) -> list[tuple[Any, ...]]:
    """Маппинг ``route6:`` объекта. Аналогично ``_to_route_rows``, но
    для IPv6 prefix'а."""
    raise NotImplementedError


def _to_as_block_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    """Маппинг ``as-block:`` объекта.

    Q4: формат ``as-block: AS1 - AS1876``. Парсим оба ASN, валидируем
    ``start <= end``. ``asn_range = [start, end+1)`` (полуоткрытый
    INT8RANGE). None+warning на битый формат.
    """
    raise NotImplementedError


def _to_role_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    """Маппинг ``role:`` объекта.

    PK ``nic_hdl`` ← ``nic-hdl:`` (если нет — None+warning, skip).
    ``role`` колонка ← первый элемент ``obj["role"]`` (это display name).
    ``abuse_mailbox`` ← ``abuse-mailbox:`` первый, если есть.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# DDL/DML helpers (2-03b).
# ---------------------------------------------------------------------------


async def _create_staging_tables(conn: asyncpg.Connection) -> None:
    """``DROP IF EXISTS`` + ``CREATE TEMP TABLE`` для 8 staging-таблиц.

    Layout каждой staging совпадает с подмножеством соответствующей
    основной таблицы — все payload-колонки из ``_<TABLE>_COLUMNS``,
    БЕЗ ``first_seen_run`` / ``last_seen_run`` (добавляются на этапе
    INSERT-from-staging).

    ``ON COMMIT DROP`` — таблицы исчезают при commit/rollback внешней
    транзакции. ``DROP IF EXISTS`` перед ``CREATE`` — идемпотентность.
    """
    raise NotImplementedError


async def _upsert_inetnum_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """``INSERT … SELECT FROM staging_inetnum … ON CONFLICT (rir,
    start_text, value) DO UPDATE``. Returns ``(inserted, updated)``."""
    raise NotImplementedError


async def _upsert_inet6num_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для inet6num. PK ``(rir, start_text, value)``."""
    raise NotImplementedError


async def _upsert_aut_num_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для aut_num. PK ``(rir, asn)``."""
    raise NotImplementedError


async def _upsert_organisation_from_staging(
    conn: asyncpg.Connection, run_id: int
) -> tuple[int, int]:
    """То же для organisation. PK ``(rir, org_handle)``."""
    raise NotImplementedError


async def _upsert_route_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для route. PK ``(rir, prefix, origin)``."""
    raise NotImplementedError


async def _upsert_route6_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для route6. PK ``(rir, prefix, origin)``."""
    raise NotImplementedError


async def _upsert_as_block_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для as_block. PK ``(rir, as_block_start, as_block_end)``."""
    raise NotImplementedError


async def _upsert_role_from_staging(conn: asyncpg.Connection, run_id: int) -> tuple[int, int]:
    """То же для role. PK ``(rir, nic_hdl)``."""
    raise NotImplementedError
