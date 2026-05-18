"""Сценарии ``etl.rpsl_etl.apply_rpsl_etl`` — 18 кейсов (skeleton stubs).

Тела появятся в 2-03b. Сейчас все тесты ожидают ``NotImplementedError``
от skeleton и помечены ``xfail(strict=True)`` чтобы CI был зелёный,
но при первой реальной реализации сразу зажёг ``XPASS``-провал и
заставил заменить assertion на содержательный.

БД-операции идут через ``pg_conn`` + ``pg_sync_run_id`` из ``conftest``
(raw asyncpg, rollback в teardown).
"""

from __future__ import annotations

import asyncpg
import pytest

from rir2localdb.etl.rpsl_etl import RpslEtlStats, apply_rpsl_etl
from rir2localdb.parsers.rpsl import RpslObject

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.xfail(
        raises=NotImplementedError,
        strict=True,
        reason="2-03a skeleton; реализация в 2-03b",
    ),
]


def _obj(*pairs: tuple[str, str]) -> RpslObject:
    """Минимальный билдер RpslObject из (key, value)-пар.

    Сохраняет insertion order; одинаковые ключи аккумулируются в list.
    """
    out: RpslObject = {}
    for k, v in pairs:
        out.setdefault(k, []).append(v)
    return out


# ---------------------------------------------------------------------------
# 1-5. Базовые happy-path для каждого из 8 типов (компактно: один кейс на
# тип + объединённый кейс с микс-объектами).
# ---------------------------------------------------------------------------


async def test_empty_input_no_changes(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    stats = await apply_rpsl_etl(pg_conn, [], rir="ripe", run_id=pg_sync_run_id)
    assert isinstance(stats, RpslEtlStats)
    assert stats.objects_seen == 0


async def test_inetnum_single_object(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _obj(
        ("inetnum", "193.0.0.0 - 193.0.0.255"),
        ("netname", "RIPE-NCC"),
        ("country", "NL"),
        ("source", "RIPE"),
    )
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


async def test_inet6num_cidr(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _obj(("inet6num", "2001:db8::/32"), ("source", "RIPE"))
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


async def test_aut_num_basic(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _obj(("aut-num", "AS3333"), ("as-name", "RIPE-NCC-AS"), ("source", "RIPE"))
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


async def test_mixed_object_types(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    """Микс из 8 типов в одном проходе → каждая таблица получает данные."""
    objects = [
        _obj(("organisation", "ORG-RIEN1-RIPE"), ("org-name", "RIPE NCC")),
        _obj(("role", "NOC@RIPE"), ("nic-hdl", "NCC1-RIPE")),
        _obj(("inetnum", "193.0.0.0 - 193.0.0.255")),
        _obj(("inet6num", "2001:db8::/32")),
        _obj(("aut-num", "AS3333")),
        _obj(("route", "193.0.0.0/24"), ("origin", "AS3333")),
        _obj(("route6", "2001:db8::/32"), ("origin", "AS3333")),
        _obj(("as-block", "AS1 - AS1876")),
    ]
    await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)


# ---------------------------------------------------------------------------
# 6-8. Multi-origin / multi-row семантика route.
# ---------------------------------------------------------------------------


async def test_route_multi_origin_yields_multiple_rows(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """Q5: route с N origin'ами → N строк (PK включает origin)."""
    obj = _obj(
        ("route", "193.0.0.0/24"),
        ("origin", "AS3333"),
        ("origin", "AS12345"),
    )
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


async def test_route6_multi_origin(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    obj = _obj(
        ("route6", "2001:db8::/32"),
        ("origin", "AS3333"),
        ("origin", "AS12345"),
    )
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


# ---------------------------------------------------------------------------
# 9-12. Skip-логика: unknown type, malformed.
# ---------------------------------------------------------------------------


async def test_unknown_type_counted_and_skipped(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """mntner/person/as-set типы — нет таблиц, считаются в
    ``objects_skipped_unknown_type``."""
    objects = [
        _obj(("mntner", "RIPE-NCC-MNT")),
        _obj(("person", "John Doe"), ("nic-hdl", "JD1-RIPE")),
        _obj(("as-set", "AS-RIPE")),
    ]
    await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)


async def test_malformed_inetnum_skipped(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    """Битый IP / end<start / one-IP формат → skip + warning."""
    objects = [
        _obj(("inetnum", "not-an-ip - also-not")),
        _obj(("inetnum", "193.0.0.100 - 193.0.0.50")),  # end<start
        _obj(("inetnum", "193.0.0.0")),  # one-IP, не range
    ]
    await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)


async def test_malformed_aut_num_too_large(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """ASN > 2^32-1 → skip + warning (Q3)."""
    obj = _obj(("aut-num", "AS9999999999"))
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


async def test_inet6num_non_cidr_skipped(pg_conn: asyncpg.Connection, pg_sync_run_id: int) -> None:
    """Не-CIDR форма inet6num (legacy ARIN) → skip + warning (Q2)."""
    obj = _obj(("inet6num", "2001:db8:: - 2001:db8:ffff::"))
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


# ---------------------------------------------------------------------------
# 13-14. Datetime parsing + array-vs-NULL.
# ---------------------------------------------------------------------------


async def test_datetime_parse_success_and_failure(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """Q6: валидный ISO-8601 → datetime; битый → None+warning, объект НЕ skip."""
    obj_ok = _obj(
        ("inetnum", "193.0.0.0 - 193.0.0.255"),
        ("created", "2003-03-17T12:15:57Z"),
        ("last-modified", "definitely-not-a-date"),
    )
    await apply_rpsl_etl(pg_conn, [obj_ok], rir="ripe", run_id=pg_sync_run_id)


async def test_empty_array_fields_stored_as_null(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """Q7: отсутствие admin-c/tech-c/mnt-by → NULL, не пустой массив."""
    obj = _obj(("inetnum", "193.0.0.0 - 193.0.0.255"), ("source", "RIPE"))
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


# ---------------------------------------------------------------------------
# 15. raw JSONB preservation — полный объект сохраняется.
# ---------------------------------------------------------------------------


async def test_raw_jsonb_preserves_full_object(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """Q8: raw содержит весь RpslObject как есть."""
    obj = _obj(
        ("inetnum", "193.0.0.0 - 193.0.0.255"),
        ("descr", "First line"),
        ("descr", "Second line"),
        ("descr", "Third line"),
        ("source", "RIPE"),
    )
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)


# ---------------------------------------------------------------------------
# 16. Rerun semantics: first_seen_run preserve, last_seen_run advance.
# ---------------------------------------------------------------------------


async def test_rerun_updates_last_seen_keeps_first_seen(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    obj = _obj(("inetnum", "193.0.0.0 - 193.0.0.255"), ("source", "RIPE"))
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=pg_sync_run_id)
    # second run with new id
    rid2 = await pg_conn.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('rich', 'running') RETURNING id"
    )
    await apply_rpsl_etl(pg_conn, [obj], rir="ripe", run_id=int(rid2))


# ---------------------------------------------------------------------------
# 17. Streaming sanity: батч-флаш не теряет данные.
# ---------------------------------------------------------------------------


async def test_streaming_flush_at_batch_boundary(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    """Скармливаем 1.5×_BATCH_SIZE inetnum'ов — flush должен сработать
    дважды, потерь не должно быть."""

    def _gen() -> list[RpslObject]:
        out: list[RpslObject] = []
        for i in range(15_000):
            out.append(
                _obj(
                    ("inetnum", f"10.{i // 256}.{i % 256}.0 - 10.{i // 256}.{i % 256}.255"),
                    ("source", "RIPE"),
                )
            )
        return out

    await apply_rpsl_etl(pg_conn, _gen(), rir="ripe", run_id=pg_sync_run_id)


# ---------------------------------------------------------------------------
# 18. Mixed unknown + valid → stats разделяет корректно.
# ---------------------------------------------------------------------------


async def test_stats_counts_seen_unknown_malformed_separately(
    pg_conn: asyncpg.Connection, pg_sync_run_id: int
) -> None:
    objects = [
        _obj(("inetnum", "193.0.0.0 - 193.0.0.255")),  # valid
        _obj(("mntner", "FOO-MNT")),  # unknown type
        _obj(("inetnum", "not-an-ip")),  # malformed
    ]
    await apply_rpsl_etl(pg_conn, objects, rir="ripe", run_id=pg_sync_run_id)
