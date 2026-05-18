"""Сценарии ``etl.delegated_etl.apply_delegated_etl`` — 13 кейсов.

БД-операции идут через ``pg_conn`` (raw asyncpg) и ``pg_sync_run_id``
из ``conftest.py``; обе фикстуры — на одной транзакции с rollback'ом
в teardown, поэтому записи каждого теста живут только в его scope.
"""

from __future__ import annotations

import ipaddress
from datetime import date as date_
from typing import Any

import asyncpg
import pytest

from rir2localdb.etl.delegated_etl import EtlStats, apply_delegated_etl
from rir2localdb.parsers.delegated import DelegatedRecord


def _make_record(
    type_: str,
    start: str,
    value: int,
    *,
    registry: str = "ripencc",
    cc: str | None = "DE",
    rec_date: date_ | None = None,
    status: str = "allocated",
    opaque_id: str | None = "OP-1",
    extensions: str | None = "e-stats",
) -> DelegatedRecord:
    """Минимальный билдер DelegatedRecord, чтобы не плодить boilerplate в тестах."""
    return DelegatedRecord(
        registry=registry,
        cc=cc,
        type=type_,  # type: ignore[arg-type]
        start=start,
        value=value,
        date=rec_date,
        status=status,
        opaque_id=opaque_id,
        extensions=extensions,
    )


async def _new_sync_run_id(conn: asyncpg.Connection) -> int:
    """Создать ещё один sync_run (для rerun-тестов с двумя run_id)."""
    rid = await conn.fetchval(
        "INSERT INTO sync_run (tier, status) VALUES ('core', 'running') RETURNING id"
    )
    return int(rid)


# ---------------------------------------------------------------------------
# Сценарии.
# ---------------------------------------------------------------------------


async def test_empty_input_no_changes(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    stats = await apply_delegated_etl(pg_conn, [], pg_sync_run_id)
    assert stats == EtlStats()
    ip_count = await pg_conn.fetchval("SELECT COUNT(*) FROM ip_allocation")
    asn_count = await pg_conn.fetchval("SELECT COUNT(*) FROM asn_allocation")
    assert ip_count == 0
    assert asn_count == 0


async def test_three_ipv4_records_inserted_with_correct_ranges(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    records = [
        _make_record("ipv4", "2.0.0.0", 65536, cc="FR"),
        _make_record("ipv4", "8.0.0.0", 16777216, cc="US"),
        _make_record("ipv4", "10.0.0.0", 20480, cc="JP"),
    ]
    stats = await apply_delegated_etl(pg_conn, records, pg_sync_run_id)
    assert stats.records_seen == 3
    assert stats.ip_records == 3
    assert stats.asn_records == 0
    assert stats.ip_inserted == 3
    assert stats.ip_updated == 0

    rows = await pg_conn.fetch(
        """
        SELECT family, lower(range_v4) AS lo, upper(range_v4) AS hi,
               host(start_text) AS ip, value, prefix_length, range_v6
        FROM ip_allocation ORDER BY value DESC
        """
    )
    assert len(rows) == 3
    # row 0 — 8.0.0.0, 16777216
    r0_start = int(ipaddress.IPv4Address("8.0.0.0"))
    assert rows[0]["family"] == 4
    assert rows[0]["lo"] == r0_start
    assert rows[0]["hi"] == r0_start + 16777216
    assert rows[0]["ip"] == "8.0.0.0"
    assert rows[0]["value"] == 16777216
    # value=16777216 — степень двойки (2^24), CIDR-aligned → /8.
    assert rows[0]["prefix_length"] == 8
    assert rows[0]["range_v6"] is None


async def test_three_ipv6_records_inserted_with_correct_ranges(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    records = [
        _make_record("ipv6", "2001:200::", 32, cc="JP"),
        _make_record("ipv6", "2a00:1450::", 32, cc="GB"),
        _make_record("ipv6", "2c00:f700::", 32, cc="ZA"),
    ]
    stats = await apply_delegated_etl(pg_conn, records, pg_sync_run_id)
    assert stats.ip_records == 3
    assert stats.ip_inserted == 3

    rows = await pg_conn.fetch(
        """
        SELECT family, lower(range_v6) AS lo, upper(range_v6) AS hi,
               host(start_text) AS ip, value, prefix_length, range_v4
        FROM ip_allocation ORDER BY host(start_text)
        """
    )
    assert len(rows) == 3
    r0_start = int(ipaddress.IPv6Address("2001:200::"))
    assert rows[0]["family"] == 6
    assert int(rows[0]["lo"]) == r0_start
    assert int(rows[0]["hi"]) == r0_start + (1 << (128 - 32))
    assert rows[0]["ip"] == "2001:200::"
    assert rows[0]["value"] == 32
    assert rows[0]["prefix_length"] == 32
    assert rows[0]["range_v4"] is None


async def test_three_asn_records_inserted_with_correct_ranges(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    records = [
        _make_record("asn", "2497", 1, cc="JP"),
        _make_record("asn", "15169", 1, cc="US"),
        _make_record("asn", "196608", 16, cc="DE"),
    ]
    stats = await apply_delegated_etl(pg_conn, records, pg_sync_run_id)
    assert stats.asn_records == 3
    assert stats.ip_records == 0
    assert stats.asn_inserted == 3
    assert stats.asn_updated == 0

    rows = await pg_conn.fetch(
        """
        SELECT start_asn, count, lower(asn_range) AS lo, upper(asn_range) AS hi
        FROM asn_allocation ORDER BY start_asn
        """
    )
    assert len(rows) == 3
    assert rows[0]["start_asn"] == 2497
    assert rows[0]["count"] == 1
    assert rows[0]["lo"] == 2497
    assert rows[0]["hi"] == 2498
    assert rows[2]["start_asn"] == 196608
    assert rows[2]["count"] == 16
    assert rows[2]["lo"] == 196608
    assert rows[2]["hi"] == 196608 + 16


async def test_mixed_input_split_correctly(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    records = [
        _make_record("ipv4", "8.0.0.0", 16777216),
        _make_record("asn", "15169", 1),
        _make_record("ipv6", "2001:200::", 32),
        _make_record("asn", "2497", 1),
        _make_record("ipv4", "10.0.0.0", 65536),
    ]
    stats = await apply_delegated_etl(pg_conn, records, pg_sync_run_id)
    assert stats.records_seen == 5
    assert stats.ip_records == 3
    assert stats.asn_records == 2
    assert stats.skipped_unsupported_type == 0
    assert stats.ip_inserted == 3
    assert stats.asn_inserted == 2

    ip_count = await pg_conn.fetchval("SELECT COUNT(*) FROM ip_allocation")
    asn_count = await pg_conn.fetchval("SELECT COUNT(*) FROM asn_allocation")
    assert ip_count == 3
    assert asn_count == 2


async def test_rerun_same_data_updates_last_seen_run(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    run1 = pg_sync_run_id
    records = [
        _make_record("ipv4", "2.0.0.0", 65536, cc="FR"),
        _make_record("asn", "15169", 1, cc="US"),
    ]
    stats1 = await apply_delegated_etl(pg_conn, records, run1)
    assert stats1.ip_inserted == 1
    assert stats1.asn_inserted == 1

    run2 = await _new_sync_run_id(pg_conn)
    stats2 = await apply_delegated_etl(pg_conn, records, run2)
    assert stats2.ip_inserted == 0
    assert stats2.ip_updated == 1
    assert stats2.asn_inserted == 0
    assert stats2.asn_updated == 1

    ip_row = await pg_conn.fetchrow("SELECT first_seen_run, last_seen_run FROM ip_allocation")
    asn_row = await pg_conn.fetchrow("SELECT first_seen_run, last_seen_run FROM asn_allocation")
    assert ip_row["first_seen_run"] == run1
    assert ip_row["last_seen_run"] == run2
    assert asn_row["first_seen_run"] == run1
    assert asn_row["last_seen_run"] == run2

    assert (await pg_conn.fetchval("SELECT COUNT(*) FROM ip_allocation")) == 1
    assert (await pg_conn.fetchval("SELECT COUNT(*) FROM asn_allocation")) == 1


async def test_rerun_changed_status_updates_status(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    run1 = pg_sync_run_id
    initial = [_make_record("ipv4", "2.0.0.0", 65536, cc="FR", status="allocated")]
    await apply_delegated_etl(pg_conn, initial, run1)

    run2 = await _new_sync_run_id(pg_conn)
    changed = [_make_record("ipv4", "2.0.0.0", 65536, cc="FR", status="reserved")]
    stats = await apply_delegated_etl(pg_conn, changed, run2)
    assert stats.ip_updated == 1
    assert stats.ip_inserted == 0

    row = await pg_conn.fetchrow("SELECT status, first_seen_run, last_seen_run FROM ip_allocation")
    assert row["status"] == "reserved"
    assert row["first_seen_run"] == run1
    assert row["last_seen_run"] == run2


async def test_rerun_changed_value_creates_new_row(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    run1 = pg_sync_run_id
    await apply_delegated_etl(
        pg_conn,
        [_make_record("ipv4", "2.0.0.0", 65536, cc="FR")],
        run1,
    )

    run2 = await _new_sync_run_id(pg_conn)
    # Тот же (rir, family, start_text), но другое value — это новая аллокация
    # по натуральному ключу.
    stats = await apply_delegated_etl(
        pg_conn,
        [_make_record("ipv4", "2.0.0.0", 131072, cc="FR")],
        run2,
    )
    assert stats.ip_inserted == 1
    assert stats.ip_updated == 0

    rows = await pg_conn.fetch(
        "SELECT value, first_seen_run, last_seen_run FROM ip_allocation ORDER BY value"
    )
    assert len(rows) == 2
    assert rows[0]["value"] == 65536
    assert rows[0]["first_seen_run"] == run1
    assert rows[0]["last_seen_run"] == run1  # stale — не вошла в run2
    assert rows[1]["value"] == 131072
    assert rows[1]["first_seen_run"] == run2
    assert rows[1]["last_seen_run"] == run2


async def test_unaligned_ipv4_value_preserved(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    # value=20480 — не степень двойки; ETL не «округляет» до /20.
    await apply_delegated_etl(
        pg_conn,
        [_make_record("ipv4", "10.0.0.0", 20480, cc="JP")],
        pg_sync_run_id,
    )
    row = await pg_conn.fetchrow(
        "SELECT upper(range_v4) - lower(range_v4) AS sz, value FROM ip_allocation"
    )
    assert row["sz"] == 20480
    assert row["value"] == 20480


async def test_ipv4_cidr_aligned_sets_prefix_length(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """value — степень двойки → ETL вычисляет prefix_length."""
    records = [
        _make_record("ipv4", "8.0.0.0", 16777216, cc="US"),  # /8
        _make_record("ipv4", "2.0.0.0", 65536, cc="FR"),  # /16
        _make_record("ipv4", "10.0.0.0", 256, cc="JP"),  # /24
    ]
    await apply_delegated_etl(pg_conn, records, pg_sync_run_id)
    rows = await pg_conn.fetch("SELECT value, prefix_length FROM ip_allocation ORDER BY value DESC")
    assert len(rows) == 3
    assert rows[0]["value"] == 16777216 and rows[0]["prefix_length"] == 8
    assert rows[1]["value"] == 65536 and rows[1]["prefix_length"] == 16
    assert rows[2]["value"] == 256 and rows[2]["prefix_length"] == 24


async def test_ipv4_unaligned_keeps_prefix_length_none(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    """value не степень двойки → prefix_length остаётся None."""
    await apply_delegated_etl(
        pg_conn,
        [_make_record("ipv4", "10.0.0.0", 20480, cc="JP")],  # объединение CIDR
        pg_sync_run_id,
    )
    row = await pg_conn.fetchrow("SELECT prefix_length FROM ip_allocation WHERE value = 20480")
    assert row["prefix_length"] is None


async def test_gist_lookup_finds_inserted_row(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    # 8.0.0.0 + 16777216 = 8.0.0.0/8 фактически.
    await apply_delegated_etl(
        pg_conn,
        [_make_record("ipv4", "8.0.0.0", 16777216, cc="US")],
        pg_sync_run_id,
    )
    target_ip = int(ipaddress.IPv4Address("8.0.0.1"))
    found = await pg_conn.fetchval(
        "SELECT COUNT(*) FROM ip_allocation WHERE family = 4 AND range_v4 @> $1::int8",
        target_ip,
    )
    assert found == 1


async def test_ipv6_canonical_form_in_start_text(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    # Вход не-canonical (uppercase + leading zeros), DB должна
    # хранить canonical compressed form.
    await apply_delegated_etl(
        pg_conn,
        [_make_record("ipv6", "2001:0DB8::1", 128, cc="DE")],
        pg_sync_run_id,
    )
    start_text = await pg_conn.fetchval(
        "SELECT host(start_text) FROM ip_allocation WHERE family = 6"
    )
    assert start_text == "2001:db8::1"


async def test_invalid_ipv4_start_raises_value_error(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    records: list[Any] = [_make_record("ipv4", "not-an-ip", 256)]
    with pytest.raises(ValueError):
        await apply_delegated_etl(pg_conn, records, pg_sync_run_id)


async def test_invalid_asn_start_raises_value_error(
    pg_conn: asyncpg.Connection,
    pg_sync_run_id: int,
) -> None:
    records: list[Any] = [_make_record("asn", "not-a-number", 1)]
    with pytest.raises(ValueError):
        await apply_delegated_etl(pg_conn, records, pg_sync_run_id)
