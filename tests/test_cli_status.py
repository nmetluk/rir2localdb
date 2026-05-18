"""``rir2localdb status --json`` — machine-readable output контракт.

Запускает CLI как **subprocess** (не CliRunner!) — у CliRunner конфликт
с уже активным event loop'ом pytest-asyncio: ``asyncio.run()`` внутри
``status`` команды бросает «cannot be called from a running event loop».
Subprocess изолирован, заодно проверяет реальный entry-point.

Seed выполняется через ``clean_db`` (autocommit asyncpg) — данные
закоммичены, subprocess их видит.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date

import asyncpg
import pytest


async def _seed_minimal_status_data(conn: asyncpg.Connection) -> int:
    """Один sync_run + один sync_file + по строке ip_allocation/asn_allocation."""
    rid = await conn.fetchval(
        "INSERT INTO sync_run (tier, status, stats) "
        "VALUES ('core+rich', 'success', "
        "       '{\"etl_rpsl_records_total\": 42}'::jsonb) "
        "RETURNING id"
    )
    rid = int(rid)
    await conn.execute(
        """
        INSERT INTO sync_file
            (url, rir, tier, kind, last_run_id, last_status,
             last_fetched_at, last_parsed_at, last_size)
        VALUES ('https://test.example/delegated', 'ripencc', 'core',
                'delegated', $1, 'updated', now(), now(), 12345)
        """,
        rid,
    )
    await conn.execute(
        """
        INSERT INTO ip_allocation
            (rir, cc, family, range_v4, start_text, value, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ('ripencc', 'NL', 4, '[33554432,33619968)'::int8range,
                '2.0.0.0', 65536, 'allocated', $1, $2, $2)
        """,
        date(2010, 7, 12),
        rid,
    )
    await conn.execute(
        """
        INSERT INTO asn_allocation
            (rir, cc, asn_range, start_asn, count, status,
             allocated_on, first_seen_run, last_seen_run)
        VALUES ('ripencc', 'NL', '[3333,3334)'::int8range, 3333, 1,
                'allocated', $1, $2, $2)
        """,
        date(1994, 5, 19),
        rid,
    )
    return rid


@pytest.mark.asyncio
async def test_status_json_output_schema(
    clean_db: asyncpg.Connection,
    test_database_url: str,
) -> None:
    """``status --json`` отдаёт валидный JSON со схемой
    ``{recent_runs, sources, summary_by_rir, db_alive}`` и непустыми
    списками после seed'а."""
    run_id = await _seed_minimal_status_data(clean_db)

    env = os.environ.copy()
    env["RIR2LOCALDB_DATABASE_URL"] = test_database_url

    result = subprocess.run(
        [sys.executable, "-m", "rir2localdb", "status", "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    payload = json.loads(result.stdout)

    assert set(payload.keys()) == {"recent_runs", "sources", "summary_by_rir", "db_alive"}
    assert payload["db_alive"] is True

    assert isinstance(payload["recent_runs"], list)
    assert len(payload["recent_runs"]) == 1
    run = payload["recent_runs"][0]
    assert run["id"] == run_id
    assert run["tier"] == "core+rich"
    assert run["status"] == "success"
    assert run["rpsl_records"] == 42

    assert isinstance(payload["sources"], list)
    assert len(payload["sources"]) == 1
    src = payload["sources"][0]
    assert src["url"] == "https://test.example/delegated"
    assert src["rir"] == "ripencc"
    assert src["last_size_bytes"] == 12345

    assert isinstance(payload["summary_by_rir"], list)
    rir_entry = next((s for s in payload["summary_by_rir"] if s["rir"] == "ripencc"), None)
    assert rir_entry is not None
    assert rir_entry["ip_allocations"] == 1
    assert rir_entry["asn_allocations"] == 1
