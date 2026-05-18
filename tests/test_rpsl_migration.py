"""Smoke-тест миграции ``0002_rpsl_tables`` — таблицы и индексы существуют.

Schema-уровень test: после ``alembic upgrade head`` (через ``test_engine``
фикстуру) все 8 RPSL-таблиц на месте, GiST/btree индексы созданы.
Insert/select-проверки данных — в шаге 2-03 (RPSL ETL).
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


_RPSL_TABLES = {
    "inetnum",
    "inet6num",
    "aut_num",
    "organisation",
    "route",
    "route6",
    "as_block",
    "role",
}


_EXPECTED_INDEXES = {
    "inetnum_range_v4_gist",
    "inet6num_range_v6_gist",
    "route_prefix_gist",
    "route6_prefix_gist",
    "as_block_range_gist",
    "inetnum_org_idx",
    "inetnum_country_idx",
    "inet6num_org_idx",
    "inet6num_country_idx",
    "aut_num_org_idx",
    "aut_num_asn_idx",
    "organisation_abuse_c_idx",
    "route_origin_idx",
    "route6_origin_idx",
    "role_abuse_mailbox_idx",
}


async def test_rpsl_tables_exist(pg_conn: asyncpg.Connection) -> None:
    rows = await pg_conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables = {r["tablename"] for r in rows}
    missing = _RPSL_TABLES - tables
    assert not missing, f"missing RPSL tables: {missing}"


async def test_rpsl_indexes_exist(pg_conn: asyncpg.Connection) -> None:
    rows = await pg_conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    indexes = {r["indexname"] for r in rows}
    missing = _EXPECTED_INDEXES - indexes
    assert not missing, f"missing indexes: {missing}"


async def test_inetnum_pk_natural_shape(pg_conn: asyncpg.Connection) -> None:
    """PK ``inetnum`` = ``(rir, start_text, value)``."""
    rows = await pg_conn.fetch(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'inetnum'::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """
    )
    pk_cols = [r["attname"] for r in rows]
    assert pk_cols == ["rir", "start_text", "value"]


async def test_aut_num_pk_natural_shape(pg_conn: asyncpg.Connection) -> None:
    """PK ``aut_num`` = ``(rir, asn)`` — single ASN, не range."""
    rows = await pg_conn.fetch(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'aut_num'::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """
    )
    pk_cols = [r["attname"] for r in rows]
    assert pk_cols == ["rir", "asn"]


async def test_route_pk_natural_shape(pg_conn: asyncpg.Connection) -> None:
    """PK ``route`` = ``(rir, prefix, origin)`` — несколько origin'ов на prefix."""
    rows = await pg_conn.fetch(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'route'::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """
    )
    pk_cols = [r["attname"] for r in rows]
    assert pk_cols == ["rir", "prefix", "origin"]
