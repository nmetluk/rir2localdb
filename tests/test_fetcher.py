"""9 сценариев для ``sync.fetcher.fetch`` через ``httpx.MockTransport``.

Реальный HTTPS сюда не ходит. Smoke против ``ftp.ripe.net`` подключается
вместе с orchestrator'ом (шаг 7) в ``tests/integration/``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from rir2localdb.config import Settings
from rir2localdb.sources import Format, Rir, Source, Tier
from rir2localdb.sync.fetcher import (
    FetchStatus,
    PreviousFetchState,
    cache_path_for,
    fetch,
    make_user_agent,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Фикстуры и helpers.
# ---------------------------------------------------------------------------

SAMPLE_URL = "https://example.test/delegated"
SAMPLE_MD5_URL = SAMPLE_URL + ".md5"
BODY = b"2|test|20260517|1|19920901|20260516|+0200\nlinexyz\n"
EXPECTED_SHA = hashlib.sha256(BODY).hexdigest()
EXPECTED_MD5 = hashlib.md5(BODY, usedforsecurity=False).hexdigest()


def _sample_source(*, with_md5: bool = True) -> Source:
    return Source(
        rir=Rir.RIPE,
        tier=Tier.CORE,
        format=Format.DELEGATED,
        url=SAMPLE_URL,
        md5_url=SAMPLE_MD5_URL if with_md5 else None,
        description="test source",
    )


def _settings(tmp_path: Path, *, retries: int = 3) -> Settings:
    # _env_file=None — отключаем чтение .env (там реальный database_url),
    # чтобы тесты не зависели от окружения вызова.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://test:test@localhost/test",
        data_dir=tmp_path,
        http_timeout=5.0,
        http_max_connections=2,
        http_retries=retries,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


class _RequestLog:
    """Записываем все Request'ы, чтобы потом считать вызовы и проверять headers."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def record(self, req: httpx.Request) -> None:
        self.requests.append(req)

    def hits(self, url: str) -> int:
        return sum(1 for r in self.requests if str(r.url) == url)

    def last_for(self, url: str) -> httpx.Request:
        matches = [r for r in self.requests if str(r.url) == url]
        assert matches, f"no requests to {url}"
        return matches[-1]


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": make_user_agent()},
    )


# ---------------------------------------------------------------------------
# Сценарии.
# ---------------------------------------------------------------------------


async def test_first_fetch_yields_new(tmp_path: Path) -> None:
    """Никакого previous, .md5 и body отдаются 200 — статус NEW, все поля заполнены."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(200, text=f"{EXPECTED_MD5}  delegated\n")
        if str(req.url) == SAMPLE_URL:
            return httpx.Response(
                200,
                content=BODY,
                headers={
                    "ETag": '"abc"',
                    "Last-Modified": "Wed, 17 May 2026 03:00:00 GMT",
                },
            )
        return httpx.Response(404)

    settings = _settings(tmp_path)
    source = _sample_source()
    async with _make_client(handler) as client:
        result = await fetch(client, source, None, settings, sleep=_no_sleep)

    expected_path = cache_path_for(settings, source)
    assert result.status == FetchStatus.NEW
    assert result.tier_used == 3
    assert result.url == SAMPLE_URL
    assert result.local_path == expected_path
    assert expected_path.exists()
    assert expected_path.read_bytes() == BODY
    assert result.size_bytes == len(BODY)
    assert result.content_sha256 == EXPECTED_SHA
    assert result.md5_sidecar == EXPECTED_MD5
    assert result.etag == '"abc"'
    assert result.last_modified == "Wed, 17 May 2026 03:00:00 GMT"
    assert result.error is None
    assert result.fetch_ms >= 0
    assert log.hits(SAMPLE_MD5_URL) == 1
    assert log.hits(SAMPLE_URL) == 1


async def test_md5_match_yields_unchanged_via_tier1(tmp_path: Path) -> None:
    """previous.last_md5 совпадает с свежим .md5 — основной файл не запрашивается."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(200, text=f"{EXPECTED_MD5}\n")
        return httpx.Response(500, text="should not be called")

    previous = PreviousFetchState(last_md5=EXPECTED_MD5)
    async with _make_client(handler) as client:
        result = await fetch(
            client, _sample_source(), previous, _settings(tmp_path), sleep=_no_sleep
        )

    assert result.status == FetchStatus.UNCHANGED
    assert result.tier_used == 1
    assert result.md5_sidecar == EXPECTED_MD5
    assert result.local_path is None
    assert result.content_sha256 is None
    assert result.size_bytes is None
    assert log.hits(SAMPLE_URL) == 0


async def test_conditional_get_304_yields_unchanged_via_tier2(tmp_path: Path) -> None:
    """md5 разошёлся (но мы доверяем серверу), conditional GET → 304."""
    log = _RequestLog()
    new_md5 = "f" * 32  # отличается от previous.last_md5

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(200, text=f"{new_md5}  delegated\n")
        if str(req.url) == SAMPLE_URL:
            # 304 с обновлёнными cache validators в ответе.
            return httpx.Response(
                304,
                headers={
                    "ETag": '"new-etag"',
                    "Last-Modified": "Thu, 18 May 2026 03:00:00 GMT",
                },
            )
        return httpx.Response(404)

    previous = PreviousFetchState(
        last_md5="0" * 32,
        last_etag='"old-etag"',
        last_modified="Wed, 17 May 2026 03:00:00 GMT",
        last_sha256="0" * 64,
    )
    async with _make_client(handler) as client:
        result = await fetch(
            client, _sample_source(), previous, _settings(tmp_path), sleep=_no_sleep
        )

    assert result.status == FetchStatus.UNCHANGED
    assert result.tier_used == 2
    assert result.local_path is None
    assert result.content_sha256 is None
    assert result.md5_sidecar == new_md5
    # ETag/Last-Modified обновлены из 304-ответа, не унаследованы.
    assert result.etag == '"new-etag"'
    assert result.last_modified == "Thu, 18 May 2026 03:00:00 GMT"
    # Conditional headers были посланы.
    main_req = log.last_for(SAMPLE_URL)
    assert main_req.headers.get("If-None-Match") == '"old-etag"'
    assert main_req.headers.get("If-Modified-Since") == "Wed, 17 May 2026 03:00:00 GMT"


async def test_same_sha256_after_download_yields_unchanged_via_tier3(tmp_path: Path) -> None:
    """200 OK, тело скачано, sha256 == previous.last_sha256 (LACNIC-кейс)."""
    log = _RequestLog()
    new_md5 = "a" * 32

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(200, text=new_md5 + "\n")
        if str(req.url) == SAMPLE_URL:
            return httpx.Response(200, content=BODY)
        return httpx.Response(404)

    previous = PreviousFetchState(last_md5="b" * 32, last_sha256=EXPECTED_SHA)
    settings = _settings(tmp_path)
    source = _sample_source()
    async with _make_client(handler) as client:
        result = await fetch(client, source, previous, settings, sleep=_no_sleep)

    assert result.status == FetchStatus.UNCHANGED
    assert result.tier_used == 3
    assert result.local_path == cache_path_for(settings, source)
    assert result.local_path.exists()
    assert result.content_sha256 == EXPECTED_SHA
    assert result.size_bytes == len(BODY)


async def test_changed_content_yields_updated(tmp_path: Path) -> None:
    """200 OK, sha256 отличается от previous → UPDATED."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(200, text=f"{EXPECTED_MD5}  delegated\n")
        if str(req.url) == SAMPLE_URL:
            return httpx.Response(200, content=BODY, headers={"ETag": '"v2"'})
        return httpx.Response(404)

    previous = PreviousFetchState(last_md5="0" * 32, last_sha256="0" * 64)
    settings = _settings(tmp_path)
    source = _sample_source()
    async with _make_client(handler) as client:
        result = await fetch(client, source, previous, settings, sleep=_no_sleep)

    assert result.status == FetchStatus.UPDATED
    assert result.tier_used == 3
    assert result.local_path == cache_path_for(settings, source)
    assert result.local_path.read_bytes() == BODY
    assert result.content_sha256 == EXPECTED_SHA
    assert result.size_bytes == len(BODY)
    assert result.etag == '"v2"'


async def test_5xx_then_success_retries_and_returns_updated(tmp_path: Path) -> None:
    """500, 500, 200 на основной файл — UPDATED, ровно 3 attempt'а."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(404)  # graceful skip tier 1
        if str(req.url) == SAMPLE_URL:
            n = log.hits(SAMPLE_URL)
            if n < 3:
                return httpx.Response(500)
            return httpx.Response(200, content=BODY)
        return httpx.Response(404)

    previous = PreviousFetchState(last_sha256="0" * 64)
    async with _make_client(handler) as client:
        result = await fetch(
            client, _sample_source(), previous, _settings(tmp_path), sleep=_no_sleep
        )

    assert result.status == FetchStatus.UPDATED
    assert result.tier_used == 3
    assert result.error is None
    assert log.hits(SAMPLE_URL) == 3


async def test_persistent_5xx_returns_error_after_max_retries(tmp_path: Path) -> None:
    """500 на все попытки — ERROR, ровно ``http_retries`` attempt'ов."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(404)
        return httpx.Response(500, text="bad gateway")

    async with _make_client(handler) as client:
        result = await fetch(
            client, _sample_source(), None, _settings(tmp_path, retries=3), sleep=_no_sleep
        )

    assert result.status == FetchStatus.ERROR
    assert result.tier_used is None
    assert result.error is not None
    assert "500" in result.error
    assert log.hits(SAMPLE_URL) == 3


async def test_4xx_main_file_returns_error_no_retry(tmp_path: Path) -> None:
    """403 на основной файл — ERROR с первой попытки, без retries."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(404)
        return httpx.Response(403, text="forbidden")

    async with _make_client(handler) as client:
        result = await fetch(client, _sample_source(), None, _settings(tmp_path), sleep=_no_sleep)

    assert result.status == FetchStatus.ERROR
    assert result.tier_used is None
    assert result.error is not None
    assert "403" in result.error
    assert log.hits(SAMPLE_URL) == 1


async def test_404_md5_sidecar_falls_through_to_tier2(tmp_path: Path) -> None:
    """md5 sidecar отдал 404 — tier 1 пропускается, основной GET работает нормально."""
    log = _RequestLog()

    def handler(req: httpx.Request) -> httpx.Response:
        log.record(req)
        if str(req.url) == SAMPLE_MD5_URL:
            return httpx.Response(404)
        if str(req.url) == SAMPLE_URL:
            return httpx.Response(200, content=BODY)
        return httpx.Response(404)

    async with _make_client(handler) as client:
        result = await fetch(client, _sample_source(), None, _settings(tmp_path), sleep=_no_sleep)

    assert result.status == FetchStatus.NEW
    assert result.tier_used == 3
    assert result.md5_sidecar is None
    assert result.error is None
    assert log.hits(SAMPLE_MD5_URL) == 1
    assert log.hits(SAMPLE_URL) == 1
