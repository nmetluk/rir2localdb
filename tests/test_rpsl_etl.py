"""Сценарии ``etl.rpsl_etl.apply_rpsl_etl`` — 17 кейсов.

БД-операции идут через ``pg_conn`` (raw asyncpg в транзакции с
rollback'ом в teardown) + ``pg_sync_run_id`` фабрику.

``apply_rpsl_etl`` регистрирует jsonb-codec на ``conn`` в binary-формате
(version-байт ``\\x01`` + ``json.dumps``-encoded body), поэтому
``SELECT raw …`` после него возвращает Python ``dict`` напрямую.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import asyncpg
import pytest

from rir2localdb.etl.rpsl_etl import RpslEtlStats, apply_rpsl_etl
from rir2localdb.parsers.rpsl import RpslObject

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _obj(*pairs: tuple[str, Any]) -> RpslObject:
    """Билдер ``RpslObject`` из (key, value)-пар.

    ``value`` может быть ``str`` (одиночная строка → ``[value]``) или
    ``list[str]`` (multi-valued ключ типа multi-origin route).
    Дубли ключа аккумулируются в один список.
    """
    out: RpslObject = {}
    for k, v in pairs:
        vals = v if isinstance(v, list) else [v]
        out.setdefault(k, []).extend(vals)
    return out


def _inetnum(start: str, end: str, **fields: Any) -> RpslObject:
    """Билдер inetnum-объекта. ``fields`` — kwargs с underscore-ключами,
    автоматически конвертируются в RPSL-ключи (``admin_c`` → ``admin-c``)."""
    out: RpslObject = {"inetnum": [f"{start} - {end}"]}
    _merge(out, fields)
    return out


def _inet6num(prefix: str, **fields: Any) -> RpslObject:
    out: RpslObject = {"inet6num": [prefix]}
    _merge(out, fields)
    return out


def _aut_num(asn_value: str, **fields: Any) -> RpslObject:
    out: RpslObject = {"aut-num": [asn_value]}
    _merge(out, fields)
    return out


def _merge(obj: RpslObject, fields: dict[str, Any]) -> None:
    for k, v in fields.items():
        key = k.replace("_", "-")
        obj[key] = v if isinstance(v, list) else [v]


# ---------------------------------------------------------------------------
# 1. Empty input.
# ---------------------------------------------------------------------------


async def test_empty_input_no_changes(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    stats = await apply_rpsl_etl(pg_conn, [], rir="ripe", run_id=pg_sync_run_id)

    assert isinstance(stats, RpslEtlStats)
    assert stats.objects_seen == 0
    assert stats.objects_by_type == {}
    assert stats.objects_skipped_unknown_type == 0
    assert stats.objects_skipped_malformed == 0
    assert stats.upsert_inserted == {}
    assert stats.upsert_updated == {}


# ---------------------------------------------------------------------------
# 2. inetnum happy path — все поля.
# ---------------------------------------------------------------------------


async def test_inetnum_single_object(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _inetnum(
        "193.0.0.0",
        "193.0.0.255",
        netname="RIPE-NCC",
        country="NL",
        descr=["First line", "Second line"],
        admin_c=["ADMIN1-RIPE"],
        tech_c=["TECH1-RIPE", "TECH2-RIPE"],
        mnt_by=["RIPE-NCC-MNT"],
        status="ALLOCATED PA",
        org="ORG-RIEN1-RIPE",
        created="2003-03-17T12:15:57Z",
        last_modified="2020-01-01T00:00:00Z",
        source="RIPE",
    )

    stats = await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    rows = await pg_conn.fetch("SELECT * FROM inetnum")
    assert len(rows) == 1
    row = rows[0]
    assert row["rir"] == "ripe"
    assert str(row["start_text"]) == "193.0.0.0"
    assert row["value"] == 256
    start_int = int(ipaddress.IPv4Address("193.0.0.0"))
    assert row["range_v4"].lower == start_int
    assert row["range_v4"].upper == start_int + 256
    assert row["netname"] == "RIPE-NCC"
    assert row["country"] == "NL"
    assert row["descr"] == "First line"
    assert row["org"] == "ORG-RIEN1-RIPE"
    assert row["admin_c"] == ["ADMIN1-RIPE"]
    assert row["tech_c"] == ["TECH1-RIPE", "TECH2-RIPE"]
    assert row["mnt_by"] == ["RIPE-NCC-MNT"]
    assert row["status"] == "ALLOCATED PA"
    assert row["created"].year == 2003
    assert row["last_modified"].year == 2020
    assert row["source"] == "RIPE"
    assert row["raw"] == obj
    assert row["first_seen_run"] == pg_sync_run_id
    assert row["last_seen_run"] == pg_sync_run_id

    assert stats.objects_seen == 1
    assert stats.objects_by_type == {"inetnum": 1}
    assert stats.upsert_inserted == {"inetnum": 1}
    assert stats.upsert_updated.get("inetnum", 0) == 0


# ---------------------------------------------------------------------------
# 3. inet6num CIDR.
# ---------------------------------------------------------------------------


async def test_inet6num_cidr(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _inet6num("2001:db8::/32", source="RIPE")

    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    row = await pg_conn.fetchrow("SELECT * FROM inet6num")
    assert row is not None
    assert str(row["start_text"]) == "2001:db8::"
    assert row["value"] == 32
    start_int = int(ipaddress.IPv6Address("2001:db8::"))
    assert int(row["range_v6"].lower) == start_int
    assert int(row["range_v6"].upper) == start_int + (1 << (128 - 32))


# ---------------------------------------------------------------------------
# 4. aut_num.
# ---------------------------------------------------------------------------


async def test_aut_num_basic(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _aut_num("AS3333", as_name="RIPE-NCC-AS", source="RIPE")

    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    row = await pg_conn.fetchrow("SELECT * FROM aut_num")
    assert row is not None
    assert row["asn"] == 3333
    assert row["as_name"] == "RIPE-NCC-AS"


# ---------------------------------------------------------------------------
# 5. Все 8 типов в одном проходе.
# ---------------------------------------------------------------------------


async def test_mixed_object_types(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    objects = [
        _obj(("organisation", "ORG-RIEN1-RIPE"), ("org-name", "RIPE NCC")),
        _obj(("role", "NOC@RIPE"), ("nic-hdl", "NCC1-RIPE")),
        _inetnum("193.0.0.0", "193.0.0.255"),
        _inet6num("2001:db8::/32"),
        _aut_num("AS3333"),
        _obj(("route", "193.0.0.0/24"), ("origin", "AS3333")),
        _obj(("route6", "2001:db8::/32"), ("origin", "AS3333")),
        _obj(("as-block", "AS1 - AS1876")),
    ]

    stats = await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)

    assert stats.objects_seen == 8
    assert stats.objects_by_type == {
        "organisation": 1,
        "role": 1,
        "inetnum": 1,
        "inet6num": 1,
        "aut-num": 1,
        "route": 1,
        "route6": 1,
        "as-block": 1,
    }
    for table in (
        "inetnum",
        "inet6num",
        "aut_num",
        "organisation",
        "route",
        "route6",
        "as_block",
        "role",
    ):
        count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {table}")
        assert count == 1, f"expected 1 row in {table}, got {count}"
    assert stats.objects_skipped_unknown_type == 0
    assert stats.objects_skipped_malformed == 0


# ---------------------------------------------------------------------------
# 6-7. Multi-origin route(6) → N строк.
# ---------------------------------------------------------------------------


async def test_route_multi_origin_yields_multiple_rows(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _obj(
        ("route", "193.0.0.0/24"),
        ("origin", ["AS3333", "AS12345", "AS65000"]),
    )

    stats = await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    rows = await pg_conn.fetch("SELECT * FROM route ORDER BY origin")
    assert len(rows) == 3
    origins = [r["origin"] for r in rows]
    assert origins == ["AS12345", "AS3333", "AS65000"]
    for r in rows:
        assert str(r["prefix"]) == "193.0.0.0/24"
        assert r["raw"] == obj  # одинаковый raw на всех N строках
    assert stats.upsert_inserted["route"] == 3
    assert stats.objects_seen == 1  # один объект, три строки


async def test_route6_multi_origin(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _obj(
        ("route6", "2001:db8::/32"),
        ("origin", ["AS3333", "AS12345"]),
    )

    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    rows = await pg_conn.fetch("SELECT * FROM route6 ORDER BY origin")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 8. Unknown types.
# ---------------------------------------------------------------------------


async def test_unknown_type_counted_and_skipped(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    objects = [
        _obj(("mntner", "RIPE-NCC-MNT")),
        _obj(("person", "John Doe"), ("nic-hdl", "JD1-RIPE")),
        _obj(("as-set", "AS-RIPE")),
    ]

    stats = await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)

    assert stats.objects_seen == 3
    assert stats.objects_skipped_unknown_type == 3
    assert stats.objects_skipped_malformed == 0
    assert stats.upsert_inserted == {}
    for table in ("inetnum", "inet6num", "aut_num", "organisation", "role"):
        count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {table}")
        assert count == 0


# ---------------------------------------------------------------------------
# 9. Malformed inetnum.
# ---------------------------------------------------------------------------


async def test_malformed_inetnum_skipped(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    objects = [
        _obj(("inetnum", "not-an-ip - also-not")),  # битый IP
        _obj(("inetnum", "193.0.0.100 - 193.0.0.50")),  # end<start
        _obj(("inetnum", "193.0.0.0")),  # без " - "
    ]

    stats = await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)

    assert stats.objects_seen == 3
    assert stats.objects_skipped_malformed == 3
    count = await pg_conn.fetchval("SELECT COUNT(*) FROM inetnum")
    assert count == 0


# ---------------------------------------------------------------------------
# 10. aut-num overflow.
# ---------------------------------------------------------------------------


async def test_malformed_aut_num_too_large(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _aut_num("AS9999999999")

    stats = await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    assert stats.objects_skipped_malformed == 1
    count = await pg_conn.fetchval("SELECT COUNT(*) FROM aut_num")
    assert count == 0


# ---------------------------------------------------------------------------
# 11. inet6num non-CIDR (legacy ARIN range) skip.
# ---------------------------------------------------------------------------


async def test_inet6num_non_cidr_skipped(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _inet6num("2001:db8:: - 2001:db8:ffff::")

    stats = await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    assert stats.objects_skipped_malformed == 1
    count = await pg_conn.fetchval("SELECT COUNT(*) FROM inet6num")
    assert count == 0


# ---------------------------------------------------------------------------
# 12. Datetime parse — успех + провал в одном объекте.
# ---------------------------------------------------------------------------


async def test_datetime_parse_success_and_failure(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _inetnum(
        "193.0.0.0",
        "193.0.0.255",
        created="2003-03-17T12:15:57Z",
        last_modified="definitely-not-a-date",
    )

    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    row = await pg_conn.fetchrow("SELECT created, last_modified FROM inetnum")
    assert row is not None
    assert row["created"] is not None
    assert row["created"].year == 2003
    assert row["last_modified"] is None  # bad datetime → NULL, объект не skip


# ---------------------------------------------------------------------------
# 13. Empty array fields → NULL.
# ---------------------------------------------------------------------------


async def test_empty_array_fields_stored_as_null(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _inetnum("193.0.0.0", "193.0.0.255", source="RIPE")
    # никаких admin-c / tech-c / mnt-by

    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    row = await pg_conn.fetchrow("SELECT admin_c, tech_c, mnt_by FROM inetnum")
    assert row is not None
    assert row["admin_c"] is None
    assert row["tech_c"] is None
    assert row["mnt_by"] is None


# ---------------------------------------------------------------------------
# 14. raw JSONB round-trip.
# ---------------------------------------------------------------------------


async def test_raw_jsonb_preserves_full_object(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _obj(
        ("inetnum", "193.0.0.0 - 193.0.0.255"),
        ("descr", ["First", "Second", "Third"]),
        ("remarks", "custom note"),  # unmapped field — должен сохраниться в raw
        ("source", "RIPE"),
    )

    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)

    raw = await pg_conn.fetchval("SELECT raw FROM inetnum")
    assert raw == obj


# ---------------------------------------------------------------------------
# 15. Rerun: first_seen_run preserve, last_seen_run advance, UPDATE counter.
# ---------------------------------------------------------------------------


async def test_rerun_updates_last_seen_keeps_first_seen(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _inetnum("193.0.0.0", "193.0.0.255", source="RIPE")

    stats1 = await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)
    assert stats1.upsert_inserted["inetnum"] == 1
    assert stats1.upsert_updated.get("inetnum", 0) == 0

    rid2_raw = await pg_conn.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('rich', 'running') RETURNING id"
    )
    rid2 = int(rid2_raw)

    stats2 = await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=rid2)
    assert stats2.upsert_inserted.get("inetnum", 0) == 0
    assert stats2.upsert_updated["inetnum"] == 1

    row = await pg_conn.fetchrow("SELECT first_seen_run, last_seen_run FROM inetnum")
    assert row is not None
    assert row["first_seen_run"] == pg_sync_run_id
    assert row["last_seen_run"] == rid2


# ---------------------------------------------------------------------------
# 16. Streaming flush at batch boundary — 15K объектов.
# ---------------------------------------------------------------------------


async def test_streaming_flush_at_batch_boundary(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    def _gen() -> list[RpslObject]:
        out: list[RpslObject] = []
        for i in range(15_000):
            base = f"10.{i // 256}.{i % 256}.0"
            top = f"10.{i // 256}.{i % 256}.255"
            out.append(_inetnum(base, top))
        return out

    stats = await apply_rpsl_etl(pg_conn, _gen(), rir="test", run_id=pg_sync_run_id)

    assert stats.objects_seen == 15_000
    assert stats.upsert_inserted["inetnum"] == 15_000
    count = await pg_conn.fetchval("SELECT COUNT(*) FROM inetnum")
    assert count == 15_000


# ---------------------------------------------------------------------------
# 17. Stats: seen / unknown / malformed / by_type разделены корректно.
# ---------------------------------------------------------------------------


async def test_stats_counts_seen_unknown_malformed_separately(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    objects = [
        _inetnum("193.0.0.0", "193.0.0.255"),  # valid
        _obj(("mntner", "FOO-MNT")),  # unknown type
        _inetnum("not-an-ip", "also-not"),  # malformed
        _inetnum("10.0.0.0", "10.0.0.255"),  # valid #2
    ]

    stats = await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)

    assert stats.objects_seen == 4
    assert stats.objects_skipped_unknown_type == 1
    assert stats.objects_skipped_malformed == 1
    assert stats.objects_by_type == {"inetnum": 3, "mntner": 1}
    assert stats.upsert_inserted["inetnum"] == 2


# ---------------------------------------------------------------------------
# Followup: ARIN IRR zero-padded IPv4 prefix normalization.
# ---------------------------------------------------------------------------


async def test_route_arin_zero_padded_prefix_normalized(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """ARIN IRR publishes prefixes like 069.031.132.000/23 (zero-padded
    octets, non-RFC-3986). The ETL canonicalizes them via
    ``_canonicalize_v4_prefix`` before passing to ``ipaddress.IPv4Network``,
    and stores the canonical form in the ``route`` table.
    """
    obj = _obj(
        ("route", "069.031.132.000/23"),
        ("origin", "AS65000"),
        ("source", "ARIN"),
    )

    stats = await apply_rpsl_etl(pg_conn, iter([obj]), rir="arin", run_id=pg_sync_run_id)

    assert stats.objects_seen == 1
    assert stats.upsert_inserted.get("route", 0) == 1

    row = await pg_conn.fetchrow(
        "SELECT prefix::text AS prefix, origin FROM route WHERE rir = 'arin' LIMIT 1"
    )
    assert row is not None
    assert row["prefix"] == "69.31.132.0/23"
    assert row["origin"] == "AS65000"
