"""Integration smoke: реальный fetch против ``ftp.ripe.net``.

Помечен ``@pytest.mark.integration`` — по умолчанию пропускается
(см. ``pyproject.toml`` ``addopts``). Запускать вручную:

    pytest -m integration

Не пишет в БД, не запускает full ``run_sync``. Только проверяет, что
fetcher умеет ходить по реальному HTTPS и что parser распознаёт
формат — никаких ассертов о содержимом отдельных записей.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rir2localdb.config import Settings
from rir2localdb.parsers.delegated import parse_delegated
from rir2localdb.sources import CORE_SOURCES, Rir
from rir2localdb.sync.fetcher import FetchStatus, fetch, make_http_client

pytestmark = pytest.mark.integration


async def test_live_ripe_delegated_fetch_and_parse(tmp_path: Path) -> None:
    """Fetch delegated-ripencc-extended-latest и парс первых ~100 записей."""
    ripe_core = next(s for s in CORE_SOURCES if s.rir == Rir.RIPE)

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://unused:unused@localhost/unused",
        data_dir=tmp_path,
        http_timeout=30.0,
        http_max_connections=2,
        http_retries=2,
    )

    async with make_http_client(settings) as client:
        result = await fetch(client, ripe_core, None, settings)

    assert result.status in (
        FetchStatus.NEW,
        FetchStatus.UPDATED,
        FetchStatus.UNCHANGED,
    ), f"fetch failed: {result.error!r}"
    assert result.local_path is not None
    assert result.local_path.exists(), result.local_path
    assert result.size_bytes is not None and result.size_bytes > 0

    records = list(parse_delegated(result.local_path))
    assert len(records) > 100, f"expected >100 records, got {len(records)}"

    sample = records[:200]
    assert any(r.type == "ipv4" and r.registry == "ripencc" for r in sample), (
        "no ipv4+ripencc record found in the first 200 records"
    )
