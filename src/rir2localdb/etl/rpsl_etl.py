"""ETL: ``RpslObject`` stream → 8 RPSL-таблиц.

Stage 2 шаг 2-03 — RPSL rich-tier ETL.

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
    rows      = _MAPPERS[table](obj, rir, run_id)

Объекты неизвестных типов (``mntner``, ``person``, ``as-set``, ...)
учитываются в ``objects_skipped_unknown_type`` и пропускаются.

**Streaming + batched COPY.** Файл ripe.db.inetnum.gz содержит ~5M
объектов, ~700 МБ распакованных. Буферизовать всё в Python — RAM
1-2 GB. Поэтому ETL итерирует поток парсера и держит **8 батч-буферов
по таблицам**; когда любой буфер достигает ``_BATCH_SIZE`` (10000
строк), он COPY'ится в свою staging-таблицу + UPSERT'ится в основную
+ staging TRUNCATE'ится для следующего батча. Финальный flush после
исчерпания итератора.

**Порядок финального flush'а.** Без FK между RPSL-таблицами порядок
формально не важен, но мы фиксируем canonical-порядок для
предсказуемости логов:

    organisation → role → inetnum → inet6num → aut_num →
    route → route6 → as_block

**Multi-origin route.** RPSL-объект ``route`` с несколькими ``origin:``
атрибутами легален; PK ``(rir, prefix, origin)`` требует **по одной
строке на origin**. Маппер ``_to_route_rows`` возвращает ``list[tuple]``
длины N. Для других типов ``_to_<table>_row`` возвращает ``tuple | None``.

**JSONB.** Регистрируем jsonb-codec на ``conn`` в начале ``apply_rpsl_etl``
в **binary**-формате (encoder: ``b"\\x01" + json.dumps().encode()``;
decoder: ``json.loads(v[1:])``). ``\\x01`` — version-байт wire-format'а
JSONB. Binary нужен потому, что ``copy_records_to_table`` всегда идёт
по binary-протоколу — text-codec даёт ``InternalClientError: no binary
format encoder``. После регистрации Python ``dict`` передаётся в COPY
напрямую и обратно из SELECT.

**Транзакционность.** Функция предполагает, что ``conn`` уже в активной
транзакции (открытой оркестратором). Все 8 staging-таблиц с ``ON COMMIT
DROP`` будут удалены при commit/rollback внешней транзакции. ETL свою
транзакцию не открывает. Падение любого UPSERT откатывает весь run.

**Стандартные правила:**

- ``first_seen_run`` на INSERT = ``run_id``, на UPDATE preserve старого.
- ``last_seen_run`` = ``run_id`` в обоих случаях.
- INSERT vs UPDATE — через ``RETURNING (xmax = 0) AS inserted``.
- Stale records — не трогаются, GC отложено в Stage 3 ops (см. ADR-0001).

**Имена staging-таблиц.** ``staging_rpsl_<table>`` — префикс предотвращает
коллизию с ``staging_ip`` / ``staging_asn`` из ``delegated_etl`` (на
случай если оркестратор в одной транзакции прогонит оба ETL).

Публичный API:

    apply_rpsl_etl(conn, objects, rir, run_id) -> RpslEtlStats

См. также ``docs/04-sync-pipeline.md`` § «RPSL ETL» и
``docs/05-parsers.md`` (формат входа).
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import asyncpg

from rir2localdb.parsers.rpsl import RpslObject

logger = logging.getLogger(__name__)


# Размер батча COPY → staging. Подобран как компромисс между
# overhead'ом одного COPY-вызова и RAM-footprint'ом батча (10k
# средних RPSL-объектов ≈ 2 МБ Python-tuples).
_BATCH_SIZE: Final[int] = 10_000

# 4-byte ASN max (RFC 6793) — выше этого Postgres BIGINT вмещает, но
# семантически невалидно. Также фильтрует мусор типа AS999999999999.
_ASN_MAX: Final[int] = 2**32 - 1


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


# Canonical порядок финального flush'а. Списки fixed, в logging это
# даёт стабильный output.
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
      type, например ``"inetnum"`` или ``"mntner"``). Включает все
      встреченные типы, **в т.ч. skipped** (полезно для observability:
      «сколько mntner-объектов было в дампе»).
    - ``objects_skipped_unknown_type`` — объекты типов вне 8 целевых
      таблиц (mntner, person, as-set, ...). Subset ``objects_by_type``.
    - ``objects_skipped_malformed`` — объекты, которые маппер отверг
      (битый IP, нечисловой ASN, end<start и т.п.); см. Q1-Q4 в
      session-log 02-03a. Также subset ``objects_by_type``.
    - ``upsert_inserted`` / ``upsert_updated`` — по таблицам. Для
      route/route6 multi-origin даёт >1 row на объект.

    Инвариант: ``objects_seen == sum(objects_by_type.values()) +
    (число объектов без ключей)``. Объекты-без-ключей засчитываются
    в ``objects_skipped_malformed`` и не попадают в ``objects_by_type``
    (нечего туда положить).
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
        1. Register jsonb codec (encoder=json.dumps, decoder=json.loads).
        2. ``_create_staging_tables`` — 8 TEMP TABLE.
        3. Итерация ``objects``: каждый объект → диспатч по первому
           ключу → маппер возвращает ``tuple | None`` (или
           ``list[tuple]`` для route/route6).
        4. Append'им в per-table буфер; при ``len(buffer) >= _BATCH_SIZE``
           — flush (COPY + UPSERT + TRUNCATE staging).
        5. После исчерпания итератора — flush всех остаточных буферов
           в ``_UPSERT_ORDER``.

    Args:
        conn: открытое asyncpg-соединение в активной транзакции.
        objects: stream ``RpslObject`` (``parse_rpsl`` итератор).
            Итерируется один раз.
        rir: discriminator колонки ``rir`` (``"ripe"``, ``"apnic"``,
            ``"afrinic"``, ``"arin"``, ``"lacnic"``). Не валидируется.
        run_id: id текущего ``sync_run`` (FK target).

    Returns:
        ``RpslEtlStats`` после завершения всех UPSERT'ов.

    Raises:
        asyncpg.PostgresError: на нарушении constraints. Внешняя
            транзакция должна откатиться.
    """
    # JSONB codec в binary-формате: COPY-протокол asyncpg всегда binary,
    # text-codec там не работает (InternalClientError "no binary format
    # encoder"). Префикс ``\x01`` — version-байт JSONB binary wire format.
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: b"\x01" + json.dumps(v).encode("utf-8"),
        decoder=lambda v: json.loads(bytes(v)[1:].decode("utf-8")),
        schema="pg_catalog",
        format="binary",
    )
    stats = RpslEtlStats()
    await _create_staging_tables(conn)

    buffers: dict[str, list[tuple[Any, ...]]] = {t: [] for t in _UPSERT_ORDER}

    for obj in objects:
        stats.objects_seen += 1
        try:
            obj_type = next(iter(obj))
        except StopIteration:
            stats.objects_skipped_malformed += 1
            continue

        stats.objects_by_type[obj_type] = stats.objects_by_type.get(obj_type, 0) + 1

        table = _OBJECT_TYPE_TO_TABLE.get(obj_type)
        if table is None:
            stats.objects_skipped_unknown_type += 1
            continue

        mapper = _MAPPERS[table]
        result = mapper(obj, rir, run_id)

        if result is None or (isinstance(result, list) and not result):
            stats.objects_skipped_malformed += 1
            continue

        rows = result if isinstance(result, list) else [result]
        buffers[table].extend(rows)

        if len(buffers[table]) >= _BATCH_SIZE:
            await _flush_table(conn, table, buffers[table], stats, run_id)
            buffers[table].clear()

    # Финальный flush в canonical-порядке.
    for table in _UPSERT_ORDER:
        if buffers[table]:
            await _flush_table(conn, table, buffers[table], stats, run_id)
            buffers[table].clear()

    return stats


async def _flush_table(
    conn: asyncpg.Connection,
    table: str,
    rows: list[tuple[Any, ...]],
    stats: RpslEtlStats,
    run_id: int,
) -> None:
    """COPY → staging + UPSERT + TRUNCATE staging.

    Очистка staging внутри транзакции через ``TRUNCATE`` — оставляет
    TEMP-таблицу живой (без её ``DROP``), сбрасывает только содержимое.
    Альтернатива ``DELETE WHERE TRUE`` идентична семантически, но
    медленнее.
    """
    columns = list(_TABLE_COLUMNS[table])
    staging = f"staging_rpsl_{table}"
    await conn.copy_records_to_table(staging, records=rows, columns=columns)
    result = await conn.fetch(_UPSERT_SQL[table], run_id)
    inserted = sum(1 for r in result if r["inserted"])
    updated = len(result) - inserted
    stats.upsert_inserted[table] = stats.upsert_inserted.get(table, 0) + inserted
    stats.upsert_updated[table] = stats.upsert_updated.get(table, 0) + updated
    await conn.execute(f"TRUNCATE {staging}")


# ---------------------------------------------------------------------------
# Чистые helpers: парсинг RPSL value-форматов.
#
# Возвращают ``None`` на любую ошибку формата. Caller сам логирует
# warning с контекстом (нужен ``rir`` и сырое значение для диагностики).
# ---------------------------------------------------------------------------


def _parse_inetnum_range(s: str) -> tuple[int, int] | None:
    """``"193.0.0.0 - 193.0.0.255"`` → ``(start_int, count)``.

    Формат: ``"<IPv4> - <IPv4>"`` (пробелы вокруг ``-`` обязательны
    по RIPE convention). Возвращает ``None`` если: split не даёт
    ровно 2 части, любая часть не парсится как IPv4, или ``end < start``.
    """
    parts = s.split(" - ", 1)
    if len(parts) != 2:
        return None
    try:
        start = int(ipaddress.IPv4Address(parts[0].strip()))
        end = int(ipaddress.IPv4Address(parts[1].strip()))
    except (ipaddress.AddressValueError, ValueError):
        return None
    if end < start:
        return None
    return start, end - start + 1


def _parse_cidr_v6(s: str) -> tuple[int, int] | None:
    """``"2001:db8::/32"`` → ``(start_int, prefix_len)``.

    CIDR-only форма (Q2). Без ``"/"`` возвращает ``None`` — это
    отсекает legacy ARIN range-форму ``"2001:db8:: - 2001:db8:ff::"``.
    ``strict=False`` чтобы принять объявления с ненулевыми host-битами;
    ``network_address`` нормализует их.
    """
    if "/" not in s:
        return None
    try:
        net = ipaddress.IPv6Network(s.strip(), strict=False)
    except (ipaddress.AddressValueError, ValueError):
        return None
    return int(net.network_address), net.prefixlen


def _parse_asn(s: str) -> int | None:
    """``"AS3333"`` / ``"as3333"`` / ``"3333"`` → ``3333``.

    ``None`` если: не парсится в int, отрицательный, или > ``_ASN_MAX``
    (4-byte ASN limit, RFC 6793).
    """
    s = s.strip()
    if s[:2].upper() == "AS":
        s = s[2:]
    try:
        n = int(s)
    except ValueError:
        return None
    if not (0 <= n <= _ASN_MAX):
        return None
    return n


def _parse_datetime(s: str) -> datetime | None:
    """``"2003-03-17T12:15:57Z"`` → ``datetime``. Python 3.11+ ``Z``-suffix.

    None при ValueError. Caller решает, оставить ли None в БД (метаданные
    опциональны) или skip'нуть объект.
    """
    try:
        return datetime.fromisoformat(s.strip())
    except ValueError:
        return None


def _first(obj: RpslObject, key: str) -> str | None:
    """``obj.get(key, [None])[0]`` без unsafe-индексации."""
    vals = obj.get(key)
    return vals[0] if vals else None


def _canonicalize_v4_prefix(prefix: str) -> str:
    """Strip leading zeros from each octet of an IPv4 CIDR prefix.

    ARIN IRR publishes prefixes like ``069.031.132.000/23`` which
    ``ipaddress.IPv4Network`` rejects per RFC 3986 § 7.4. We normalize
    them to canonical form (``69.31.132.0/23``) before parsing.

    Any non-IPv4-shaped string is returned unchanged — downstream
    validation in IPv4Network will reject it normally.
    """
    addr, slash, suffix = prefix.partition("/")
    try:
        octets = [str(int(o)) for o in addr.split(".")]
    except ValueError:
        return prefix
    if len(octets) != 4:
        return prefix
    return ".".join(octets) + (slash + suffix if slash else "")


# ---------------------------------------------------------------------------
# Маппинги RpslObject → staging-row(s).
# Каждый возвращает кортеж в порядке соответствующего ``_<TABLE>_COLUMNS``.
# ``None`` (или ``[]``) означает «объект malformed, skip».
# ---------------------------------------------------------------------------


def _to_inetnum_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    raw_value = obj["inetnum"][0] if obj.get("inetnum") else ""
    parsed = _parse_inetnum_range(raw_value)
    if parsed is None:
        logger.warning("rpsl_etl skip inetnum: bad range rir=%s value=%r", rir, raw_value)
        return None
    start_int, count = parsed
    start_text = str(ipaddress.IPv4Address(start_int))
    range_v4 = asyncpg.Range(start_int, start_int + count)
    return (
        rir,
        start_text,
        count,
        range_v4,
        _first(obj, "netname"),
        _first(obj, "country"),
        _first(obj, "descr"),
        _first(obj, "org"),
        obj.get("admin-c"),
        obj.get("tech-c"),
        _first(obj, "status"),
        obj.get("mnt-by"),
        _parse_datetime_or_none(obj, "created", rir, "inetnum"),
        _parse_datetime_or_none(obj, "last-modified", rir, "inetnum"),
        _first(obj, "source"),
        obj,
    )


def _to_inet6num_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    raw_value = obj["inet6num"][0] if obj.get("inet6num") else ""
    parsed = _parse_cidr_v6(raw_value)
    if parsed is None:
        logger.warning("rpsl_etl skip inet6num: not CIDR-form rir=%s value=%r", rir, raw_value)
        return None
    start_int, prefix_len = parsed
    start_text = str(ipaddress.IPv6Address(start_int))
    # NUMRANGE: bounds могут быть >2^63; asyncpg сериализует Python int
    # через textual encoding для numrange.
    range_v6 = asyncpg.Range(start_int, start_int + (1 << (128 - prefix_len)))
    return (
        rir,
        start_text,
        prefix_len,
        range_v6,
        _first(obj, "netname"),
        _first(obj, "country"),
        _first(obj, "descr"),
        _first(obj, "org"),
        obj.get("admin-c"),
        obj.get("tech-c"),
        _first(obj, "status"),
        obj.get("mnt-by"),
        _parse_datetime_or_none(obj, "created", rir, "inet6num"),
        _parse_datetime_or_none(obj, "last-modified", rir, "inet6num"),
        _first(obj, "source"),
        obj,
    )


def _to_aut_num_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    raw_value = obj["aut-num"][0] if obj.get("aut-num") else ""
    asn = _parse_asn(raw_value)
    if asn is None:
        logger.warning("rpsl_etl skip aut-num: bad ASN rir=%s value=%r", rir, raw_value)
        return None
    return (
        rir,
        asn,
        _first(obj, "as-name"),
        _first(obj, "descr"),
        _first(obj, "org"),
        obj.get("admin-c"),
        obj.get("tech-c"),
        _first(obj, "status"),
        obj.get("mnt-by"),
        _parse_datetime_or_none(obj, "created", rir, "aut-num"),
        _parse_datetime_or_none(obj, "last-modified", rir, "aut-num"),
        _first(obj, "source"),
        obj,
    )


def _to_organisation_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    org_handle = obj["organisation"][0] if obj.get("organisation") else ""
    if not org_handle:
        logger.warning("rpsl_etl skip organisation: empty handle rir=%s", rir)
        return None
    return (
        rir,
        org_handle,
        _first(obj, "org-name"),
        _first(obj, "org-type"),
        obj.get("address"),
        obj.get("phone"),
        obj.get("fax-no"),
        obj.get("e-mail") or obj.get("email"),
        _first(obj, "abuse-c"),
        obj.get("admin-c"),
        obj.get("tech-c"),
        obj.get("mnt-by"),
        obj.get("mnt-ref"),
        _parse_datetime_or_none(obj, "created", rir, "organisation"),
        _parse_datetime_or_none(obj, "last-modified", rir, "organisation"),
        _first(obj, "source"),
        obj,
    )


def _to_route_rows(obj: RpslObject, rir: str, run_id: int) -> list[tuple[Any, ...]]:
    # ARIN IRR публикует ``069.031.132.000/23``-style prefix'ы, которые
    # IPv4Network отвергает по RFC 3986 §7.4. Канонизируем до парсинга
    # и сохраняем уже canonical форму в БД.
    prefix_str = _canonicalize_v4_prefix((obj["route"][0] if obj.get("route") else "").strip())
    try:
        ipaddress.IPv4Network(prefix_str, strict=False)
    except (ipaddress.AddressValueError, ValueError):
        logger.warning("rpsl_etl skip route: bad prefix rir=%s value=%r", rir, prefix_str)
        return []
    origins = obj.get("origin") or []
    if not origins:
        logger.warning("rpsl_etl skip route: no origin rir=%s prefix=%s", rir, prefix_str)
        return []
    descr = _first(obj, "descr")
    org = _first(obj, "org")
    mnt_by = obj.get("mnt-by")
    created = _parse_datetime_or_none(obj, "created", rir, "route")
    last_modified = _parse_datetime_or_none(obj, "last-modified", rir, "route")
    source = _first(obj, "source")
    rows: list[tuple[Any, ...]] = []
    for origin_raw in origins:
        origin = origin_raw.strip()
        if not origin:
            continue
        rows.append(
            (
                rir,
                prefix_str,
                origin,
                descr,
                org,
                mnt_by,
                created,
                last_modified,
                source,
                obj,
            )
        )
    return rows


def _to_route6_rows(obj: RpslObject, rir: str, run_id: int) -> list[tuple[Any, ...]]:
    prefix_str = (obj["route6"][0] if obj.get("route6") else "").strip()
    try:
        ipaddress.IPv6Network(prefix_str, strict=False)
    except (ipaddress.AddressValueError, ValueError):
        logger.warning("rpsl_etl skip route6: bad prefix rir=%s value=%r", rir, prefix_str)
        return []
    origins = obj.get("origin") or []
    if not origins:
        logger.warning("rpsl_etl skip route6: no origin rir=%s prefix=%s", rir, prefix_str)
        return []
    descr = _first(obj, "descr")
    org = _first(obj, "org")
    mnt_by = obj.get("mnt-by")
    created = _parse_datetime_or_none(obj, "created", rir, "route6")
    last_modified = _parse_datetime_or_none(obj, "last-modified", rir, "route6")
    source = _first(obj, "source")
    rows: list[tuple[Any, ...]] = []
    for origin_raw in origins:
        origin = origin_raw.strip()
        if not origin:
            continue
        rows.append(
            (
                rir,
                prefix_str,
                origin,
                descr,
                org,
                mnt_by,
                created,
                last_modified,
                source,
                obj,
            )
        )
    return rows


def _to_as_block_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    raw_value = obj["as-block"][0] if obj.get("as-block") else ""
    parts = raw_value.split(" - ", 1)
    if len(parts) != 2:
        logger.warning("rpsl_etl skip as-block: bad range rir=%s value=%r", rir, raw_value)
        return None
    start_asn = _parse_asn(parts[0])
    end_asn = _parse_asn(parts[1])
    if start_asn is None or end_asn is None or start_asn > end_asn:
        logger.warning("rpsl_etl skip as-block: bad bounds rir=%s value=%r", rir, raw_value)
        return None
    asn_range = asyncpg.Range(start_asn, end_asn + 1)
    return (
        rir,
        start_asn,
        end_asn,
        asn_range,
        _first(obj, "descr"),
        obj.get("mnt-by"),
        _parse_datetime_or_none(obj, "created", rir, "as-block"),
        _parse_datetime_or_none(obj, "last-modified", rir, "as-block"),
        _first(obj, "source"),
        obj,
    )


def _to_role_row(obj: RpslObject, rir: str, run_id: int) -> tuple[Any, ...] | None:
    nic_hdl = _first(obj, "nic-hdl")
    if not nic_hdl:
        logger.warning("rpsl_etl skip role: no nic-hdl rir=%s", rir)
        return None
    return (
        rir,
        nic_hdl,
        _first(obj, "role"),
        obj.get("address"),
        obj.get("phone"),
        obj.get("fax-no"),
        obj.get("e-mail") or obj.get("email"),
        _first(obj, "abuse-mailbox"),
        obj.get("admin-c"),
        obj.get("tech-c"),
        obj.get("mnt-by"),
        _parse_datetime_or_none(obj, "created", rir, "role"),
        _parse_datetime_or_none(obj, "last-modified", rir, "role"),
        _first(obj, "source"),
        obj,
    )


def _parse_datetime_or_none(obj: RpslObject, key: str, rir: str, obj_type: str) -> datetime | None:
    """Парсить ``obj[key][0]`` как datetime; на ValueError — warning + None.

    Объект на этом НЕ skip'ается — метаданные опциональны (Q6).
    """
    raw = _first(obj, key)
    if raw is None:
        return None
    parsed = _parse_datetime(raw)
    if parsed is None:
        logger.warning("rpsl_etl: bad %s in %s rir=%s value=%r", key, obj_type, rir, raw)
    return parsed


_MapperType = Callable[[RpslObject, str, int], tuple[Any, ...] | list[tuple[Any, ...]] | None]

_MAPPERS: Final[dict[str, _MapperType]] = {
    "inetnum": _to_inetnum_row,
    "inet6num": _to_inet6num_row,
    "aut_num": _to_aut_num_row,
    "organisation": _to_organisation_row,
    "route": _to_route_rows,
    "route6": _to_route6_rows,
    "as_block": _to_as_block_row,
    "role": _to_role_row,
}


# ---------------------------------------------------------------------------
# DDL: 8 staging-таблиц.
# ---------------------------------------------------------------------------


async def _create_staging_tables(conn: asyncpg.Connection) -> None:
    """``DROP IF EXISTS`` + ``CREATE TEMP TABLE`` для 8 staging-таблиц.

    Каждая копирует payload-колонки соответствующей основной таблицы,
    без ``first_seen_run`` / ``last_seen_run`` (добавляются на этапе
    INSERT-from-staging).
    """
    await conn.execute(_CREATE_STAGING_SQL)


_CREATE_STAGING_SQL: Final[str] = """
DROP TABLE IF EXISTS staging_rpsl_inetnum;
CREATE TEMP TABLE staging_rpsl_inetnum (
    rir           TEXT,
    start_text    INET,
    value         BIGINT,
    range_v4      INT8RANGE,
    netname       TEXT,
    country       TEXT,
    descr         TEXT,
    org           TEXT,
    admin_c       TEXT[],
    tech_c        TEXT[],
    status        TEXT,
    mnt_by        TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_inet6num;
CREATE TEMP TABLE staging_rpsl_inet6num (
    rir           TEXT,
    start_text    INET,
    value         SMALLINT,
    range_v6      NUMRANGE,
    netname       TEXT,
    country       TEXT,
    descr         TEXT,
    org           TEXT,
    admin_c       TEXT[],
    tech_c        TEXT[],
    status        TEXT,
    mnt_by        TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_aut_num;
CREATE TEMP TABLE staging_rpsl_aut_num (
    rir           TEXT,
    asn           BIGINT,
    as_name       TEXT,
    descr         TEXT,
    org           TEXT,
    admin_c       TEXT[],
    tech_c        TEXT[],
    status        TEXT,
    mnt_by        TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_organisation;
CREATE TEMP TABLE staging_rpsl_organisation (
    rir           TEXT,
    org_handle    TEXT,
    org_name      TEXT,
    org_type      TEXT,
    address       TEXT[],
    phone         TEXT[],
    fax_no        TEXT[],
    email         TEXT[],
    abuse_c       TEXT,
    admin_c       TEXT[],
    tech_c        TEXT[],
    mnt_by        TEXT[],
    mnt_ref       TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_route;
CREATE TEMP TABLE staging_rpsl_route (
    rir           TEXT,
    prefix        CIDR,
    origin        TEXT,
    descr         TEXT,
    org           TEXT,
    mnt_by        TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_route6;
CREATE TEMP TABLE staging_rpsl_route6 (
    rir           TEXT,
    prefix        CIDR,
    origin        TEXT,
    descr         TEXT,
    org           TEXT,
    mnt_by        TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_as_block;
CREATE TEMP TABLE staging_rpsl_as_block (
    rir            TEXT,
    as_block_start BIGINT,
    as_block_end   BIGINT,
    asn_range      INT8RANGE,
    descr          TEXT,
    mnt_by         TEXT[],
    created        TIMESTAMPTZ,
    last_modified  TIMESTAMPTZ,
    source         TEXT,
    raw            JSONB
) ON COMMIT DROP;

DROP TABLE IF EXISTS staging_rpsl_role;
CREATE TEMP TABLE staging_rpsl_role (
    rir           TEXT,
    nic_hdl       TEXT,
    role          TEXT,
    address       TEXT[],
    phone         TEXT[],
    fax_no        TEXT[],
    email         TEXT[],
    abuse_mailbox TEXT,
    admin_c       TEXT[],
    tech_c        TEXT[],
    mnt_by        TEXT[],
    created       TIMESTAMPTZ,
    last_modified TIMESTAMPTZ,
    source        TEXT,
    raw           JSONB
) ON COMMIT DROP;
"""


# ---------------------------------------------------------------------------
# UPSERT SQL — одна константа на таблицу.
#
# Каждый INSERT … SELECT FROM staging_rpsl_<t> … ON CONFLICT (PK)
# DO UPDATE SET <all payload + last_seen_run> RETURNING (xmax = 0).
# first_seen_run в SET НЕ упоминается → preserve.
# Параметр $1 = run_id, используется дважды в SELECT (для
# first_seen_run И last_seen_run). asyncpg допускает один параметр
# в нескольких позициях.
# ---------------------------------------------------------------------------


_UPSERT_INETNUM_SQL: Final[str] = """
INSERT INTO inetnum (
    rir, start_text, value, range_v4, netname, country, descr, org,
    admin_c, tech_c, status, mnt_by, created, last_modified, source, raw,
    first_seen_run, last_seen_run
)
SELECT
    rir, start_text, value, range_v4, netname, country, descr, org,
    admin_c, tech_c, status, mnt_by, created, last_modified, source, raw,
    $1, $1
FROM staging_rpsl_inetnum
ON CONFLICT (rir, start_text, value) DO UPDATE SET
    range_v4      = EXCLUDED.range_v4,
    netname       = EXCLUDED.netname,
    country       = EXCLUDED.country,
    descr         = EXCLUDED.descr,
    org           = EXCLUDED.org,
    admin_c       = EXCLUDED.admin_c,
    tech_c        = EXCLUDED.tech_c,
    status        = EXCLUDED.status,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_INET6NUM_SQL: Final[str] = """
INSERT INTO inet6num (
    rir, start_text, value, range_v6, netname, country, descr, org,
    admin_c, tech_c, status, mnt_by, created, last_modified, source, raw,
    first_seen_run, last_seen_run
)
SELECT
    rir, start_text, value, range_v6, netname, country, descr, org,
    admin_c, tech_c, status, mnt_by, created, last_modified, source, raw,
    $1, $1
FROM staging_rpsl_inet6num
ON CONFLICT (rir, start_text, value) DO UPDATE SET
    range_v6      = EXCLUDED.range_v6,
    netname       = EXCLUDED.netname,
    country       = EXCLUDED.country,
    descr         = EXCLUDED.descr,
    org           = EXCLUDED.org,
    admin_c       = EXCLUDED.admin_c,
    tech_c        = EXCLUDED.tech_c,
    status        = EXCLUDED.status,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_AUT_NUM_SQL: Final[str] = """
INSERT INTO aut_num (
    rir, asn, as_name, descr, org, admin_c, tech_c, status, mnt_by,
    created, last_modified, source, raw, first_seen_run, last_seen_run
)
SELECT
    rir, asn, as_name, descr, org, admin_c, tech_c, status, mnt_by,
    created, last_modified, source, raw, $1, $1
FROM staging_rpsl_aut_num
ON CONFLICT (rir, asn) DO UPDATE SET
    as_name       = EXCLUDED.as_name,
    descr         = EXCLUDED.descr,
    org           = EXCLUDED.org,
    admin_c       = EXCLUDED.admin_c,
    tech_c        = EXCLUDED.tech_c,
    status        = EXCLUDED.status,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_ORGANISATION_SQL: Final[str] = """
INSERT INTO organisation (
    rir, org_handle, org_name, org_type, address, phone, fax_no, email,
    abuse_c, admin_c, tech_c, mnt_by, mnt_ref, created, last_modified,
    source, raw, first_seen_run, last_seen_run
)
SELECT
    rir, org_handle, org_name, org_type, address, phone, fax_no, email,
    abuse_c, admin_c, tech_c, mnt_by, mnt_ref, created, last_modified,
    source, raw, $1, $1
FROM staging_rpsl_organisation
ON CONFLICT (rir, org_handle) DO UPDATE SET
    org_name      = EXCLUDED.org_name,
    org_type      = EXCLUDED.org_type,
    address       = EXCLUDED.address,
    phone         = EXCLUDED.phone,
    fax_no        = EXCLUDED.fax_no,
    email         = EXCLUDED.email,
    abuse_c       = EXCLUDED.abuse_c,
    admin_c       = EXCLUDED.admin_c,
    tech_c        = EXCLUDED.tech_c,
    mnt_by        = EXCLUDED.mnt_by,
    mnt_ref       = EXCLUDED.mnt_ref,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_ROUTE_SQL: Final[str] = """
INSERT INTO route (
    rir, prefix, origin, descr, org, mnt_by, created, last_modified,
    source, raw, first_seen_run, last_seen_run
)
SELECT
    rir, prefix, origin, descr, org, mnt_by, created, last_modified,
    source, raw, $1, $1
FROM staging_rpsl_route
ON CONFLICT (rir, prefix, origin) DO UPDATE SET
    descr         = EXCLUDED.descr,
    org           = EXCLUDED.org,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_ROUTE6_SQL: Final[str] = """
INSERT INTO route6 (
    rir, prefix, origin, descr, org, mnt_by, created, last_modified,
    source, raw, first_seen_run, last_seen_run
)
SELECT
    rir, prefix, origin, descr, org, mnt_by, created, last_modified,
    source, raw, $1, $1
FROM staging_rpsl_route6
ON CONFLICT (rir, prefix, origin) DO UPDATE SET
    descr         = EXCLUDED.descr,
    org           = EXCLUDED.org,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_AS_BLOCK_SQL: Final[str] = """
INSERT INTO as_block (
    rir, as_block_start, as_block_end, asn_range, descr, mnt_by,
    created, last_modified, source, raw, first_seen_run, last_seen_run
)
SELECT
    rir, as_block_start, as_block_end, asn_range, descr, mnt_by,
    created, last_modified, source, raw, $1, $1
FROM staging_rpsl_as_block
ON CONFLICT (rir, as_block_start, as_block_end) DO UPDATE SET
    asn_range     = EXCLUDED.asn_range,
    descr         = EXCLUDED.descr,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""

_UPSERT_ROLE_SQL: Final[str] = """
INSERT INTO role (
    rir, nic_hdl, role, address, phone, fax_no, email, abuse_mailbox,
    admin_c, tech_c, mnt_by, created, last_modified, source, raw,
    first_seen_run, last_seen_run
)
SELECT
    rir, nic_hdl, role, address, phone, fax_no, email, abuse_mailbox,
    admin_c, tech_c, mnt_by, created, last_modified, source, raw,
    $1, $1
FROM staging_rpsl_role
ON CONFLICT (rir, nic_hdl) DO UPDATE SET
    role          = EXCLUDED.role,
    address       = EXCLUDED.address,
    phone         = EXCLUDED.phone,
    fax_no        = EXCLUDED.fax_no,
    email         = EXCLUDED.email,
    abuse_mailbox = EXCLUDED.abuse_mailbox,
    admin_c       = EXCLUDED.admin_c,
    tech_c        = EXCLUDED.tech_c,
    mnt_by        = EXCLUDED.mnt_by,
    created       = EXCLUDED.created,
    last_modified = EXCLUDED.last_modified,
    source        = EXCLUDED.source,
    raw           = EXCLUDED.raw,
    last_seen_run = EXCLUDED.last_seen_run
RETURNING (xmax = 0) AS inserted
"""


_UPSERT_SQL: Final[dict[str, str]] = {
    "inetnum": _UPSERT_INETNUM_SQL,
    "inet6num": _UPSERT_INET6NUM_SQL,
    "aut_num": _UPSERT_AUT_NUM_SQL,
    "organisation": _UPSERT_ORGANISATION_SQL,
    "route": _UPSERT_ROUTE_SQL,
    "route6": _UPSERT_ROUTE6_SQL,
    "as_block": _UPSERT_AS_BLOCK_SQL,
    "role": _UPSERT_ROLE_SQL,
}
