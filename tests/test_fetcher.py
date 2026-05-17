"""Сценарии для ``sync.fetcher.fetch`` — стабы (Stage 1, шаг 3).

Все 9 сценариев формализуют поведение трёхуровневой детекции изменений
(см. ``docs/04-sync-pipeline.md`` и docstring ``rir2localdb.sync.fetcher``).

Все запросы будут мокаться через ``httpx.MockTransport`` — реальный HTTPS
сюда не ходит. Smoke-тест против ``ftp.ripe.net`` подключается, когда
появится ``sync.orchestrator`` (шаг 7), и живёт отдельным
``tests/integration/`` маркером.

Каждый сценарий ниже:
    - описан одной фразой в docstring,
    - явно говорит, *какой именно* tier должен сработать,
    - и какие поля ``FetchResult`` он ожидает заполненными.

Реализация — в шаге 3, когда напишем body ``fetch()``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_first_fetch_yields_new() -> None:
    """No previous state, .md5 и body отдаются 200 OK.

    Ожидание:
        - status == NEW,
        - local_path указывает на ``<data_dir>/cache/<rir>/<filename>``,
        - size_bytes / content_sha256 / etag / last_modified заполнены,
        - md5_sidecar заполнен,
        - error is None.
    """
    raise NotImplementedError("tier 0 / first fetch — stage 1 step 3")


async def test_md5_match_yields_unchanged_via_tier1() -> None:
    """previous.last_md5 совпадает с свежим .md5 → tier 1 закрывает запрос.

    Ожидание:
        - status == UNCHANGED,
        - основной файл НЕ запрашивался (проверяется через mock-transport
          ассерт «GET <main_url> был вызван 0 раз»),
        - local_path is None (тело не скачивали),
        - md5_sidecar заполнен (свежий хэш),
        - content_sha256 / size_bytes / etag / last_modified is None.
    """
    raise NotImplementedError("tier 1 / md5 match — stage 1 step 3")


async def test_conditional_get_304_yields_unchanged_via_tier2() -> None:
    """Tier 1 не сработал (md5 разошёлся / sidecar 404), сервер на основной
    GET ответил ``304 Not Modified``.

    Ожидание:
        - status == UNCHANGED,
        - запрос на основной файл отправлен с заголовками
          ``If-Modified-Since`` и ``If-None-Match`` из previous,
        - local_path is None,
        - size_bytes / content_sha256 is None,
        - etag / last_modified — обновлённые из ответа 304 (если сервер
          их прислал), иначе унаследованы из previous.
    """
    raise NotImplementedError("tier 2 / 304 — stage 1 step 3")


async def test_same_sha256_after_download_yields_unchanged_via_tier3() -> None:
    """Сервер отдал 200, тело скачано, но sha256 совпал с previous.last_sha256
    (типичный кейс LACNIC, где Last-Modified шумит).

    Ожидание:
        - status == UNCHANGED,
        - local_path указывает на свежий файл (он был перезаписан),
        - content_sha256 заполнен (==previous.last_sha256),
        - size_bytes заполнен.
    """
    raise NotImplementedError("tier 3 / sha match — stage 1 step 3")


async def test_changed_content_yields_updated() -> None:
    """200 OK, sha256 отличается от previous.last_sha256.

    Ожидание:
        - status == UPDATED,
        - local_path заполнен, файл записан атомарно (``.tmp`` → rename),
        - content_sha256 / size_bytes / etag / last_modified — из ответа.
    """
    raise NotImplementedError("tier 3 / sha diff — stage 1 step 3")


async def test_5xx_then_success_retries_and_returns_updated() -> None:
    """Mock-transport отдаёт 500, 500, 200 на основной файл.

    Ожидание:
        - status == UPDATED (или NEW, если previous=None),
        - сделано ровно 3 попытки (или ``settings.http_retries``),
        - backoff не превратился в реальное ожидание в тесте
          (mock-time или monkeypatched ``asyncio.sleep``),
        - error is None.
    """
    raise NotImplementedError("retry success — stage 1 step 3")


async def test_persistent_5xx_returns_error_after_max_retries() -> None:
    """Mock-transport отдаёт 500 на все попытки.

    Ожидание:
        - status == ERROR,
        - error содержит код последнего ответа и URL,
        - сделано ровно ``settings.http_retries`` попыток (не больше).
    """
    raise NotImplementedError("retry exhausted — stage 1 step 3")


async def test_4xx_main_file_returns_error_no_retry() -> None:
    """Mock-transport отдаёт 403 на основной файл с первой же попытки.

    Ожидание:
        - status == ERROR,
        - ровно 1 попытка (4xx не ретраится),
        - error упоминает 403.
    """
    raise NotImplementedError("4xx no retry — stage 1 step 3")


async def test_404_md5_sidecar_falls_through_to_tier2() -> None:
    """У ``Source`` задан ``md5_url``, но он отвечает 404 (как у части
    RPSL-дампов). Tier 1 пропускается, дальше — обычный conditional GET.

    Ожидание:
        - status в {NEW, UPDATED, UNCHANGED} в зависимости от ответа
          на основной файл,
        - md5_sidecar is None,
        - 404 на .md5 НЕ превращает результат в ERROR.
    """
    raise NotImplementedError("md5 404 graceful — stage 1 step 3")
