"""HTTPS-загрузчик одного ``Source``.

Поведение целиком описано в ``docs/04-sync-pipeline.md`` (три уровня
детекции изменений) и ``docs/02-architecture.md`` (контракт sync-слоя).

Высокоуровневый сценарий ``fetch()``:

1. **Tier 1 — md5-сосед.** Если у ``Source`` задан ``md5_url``, скачиваем
   крохотный ``.md5``-файл и сравниваем хэш с ``PreviousFetchState.last_md5``.
   - Совпало → ``FetchStatus.UNCHANGED``, основной файл не качаем.
   - 404 на ``.md5`` → молча пропускаем tier 1 (некоторые RIR его не
     публикуют), переходим к tier 2.
   - Другой 4xx → ``FetchStatus.ERROR``.
2. **Tier 2 — conditional GET.** Шлём ``GET`` с ``If-Modified-Since`` и
   ``If-None-Match`` из ``PreviousFetchState``. ``304 Not Modified`` →
   ``FetchStatus.UNCHANGED``.
3. **Tier 3 — sha256.** Если пришло ``200 OK``, стримим тело в
   ``<data_dir>/cache/<rir>/<filename>.tmp``, считаем sha256, делаем
   atomic-rename. Если sha совпал с ``PreviousFetchState.last_sha256`` —
   ``FetchStatus.UNCHANGED`` (но файл на диске обновлён, что нормально).
   Иначе — ``FetchStatus.UPDATED`` (или ``NEW``, если ``previous is None``).

**Edge case.** Tier 1 может показать расхождение md5, а tier 2 — вернуть
``304 Not Modified``. Это противоречивое поведение зеркала; доверяем
серверу (возвращаем ``UNCHANGED``), но логируем warning. Следующий run
снова обнаружит расхождение и повторит — ничего не сломается.

``fetch()`` **не бросает** на сетевых ошибках и HTTP-кодах — они
мэппятся в ``FetchStatus.ERROR`` с ``error`` полем. Программные ошибки
(кривой ``Source``, баг внутри) идут наверх как обычно.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import httpx

from rir2localdb import __version__
from rir2localdb.config import Settings
from rir2localdb.sources import Source

PROJECT_HOMEPAGE: Final[str] = "https://github.com/nmetluk/rir2localdb"
_BACKOFF_BASE: Final[float] = 2.0
_BACKOFF_MAX_DELAY: Final[float] = 60.0

# Любая 32-hex последовательность, не граничащая с другим hex-символом.
# Покрывает GNU `<hash>  <file>`, single-space, bare `<hash>`, и BSD
# `MD5 (<file>) = <hash>`. ``\b`` корректен потому что hex-чары —
# word-chars в regex, и 64-hex sha256 не даст false positive.
_MD5_HEX_RE: Final[re.Pattern[str]] = re.compile(r"\b[0-9a-fA-F]{32}\b")

logger = logging.getLogger(__name__)


class FetchStatus(StrEnum):
    """Исход одного вызова ``fetch()``.

    ``NEW`` и ``UPDATED`` различаются только наличием предыдущей записи
    в ``sync_file``: семантически оба значат «на диске есть свежий файл,
    нужно его распарсить и применить ETL». Разделение — чтобы метрики
    и логи могли отличить первое появление источника от изменения.
    """

    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PreviousFetchState:
    """Состояние, прочитанное из ``sync_file`` для данного URL.

    ``None`` (в ``fetch()``) означает «источник раньше не скачивался
    успешно» — все поля недоступны, fetch идёт по короткому пути
    «качаем как NEW».
    """

    last_md5: str | None = None
    last_etag: str | None = None
    last_modified: str | None = None
    """Значение HTTP-заголовка ``Last-Modified`` как строка
    (RFC 7231 IMF-fixdate). Конвертация в TZ-aware ``datetime``
    для ``sync_file.last_modified`` — задача ``sync/state.py``.
    """
    last_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Результат одного вызова ``fetch()``.

    Минимальный набор полей, достаточный для записи в ``sync_file``
    и для дальнейшей передачи в parser/ETL (через ``local_path``).
    """

    status: FetchStatus
    url: str
    local_path: Path | None = None
    """Путь к свежескачанному файлу. Заполнен при ``NEW``/``UPDATED``
    и при ``UNCHANGED`` через tier 3 (когда тело всё-таки скачали).
    Для ``UNCHANGED`` через tier 1 или tier 2 — ``None``.
    """
    size_bytes: int | None = None
    content_sha256: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    md5_sidecar: str | None = None
    """Содержимое ``.md5``-сайдкара, если он был получен (включая случаи,
    когда tier 1 не сработал из-за рассогласования — мы всё равно
    запоминаем актуальный хэш, чтобы записать в ``sync_file.last_md5``).
    """
    fetch_ms: int = 0
    """Полная длительность ``fetch()``, миллисекунды (включая retries
    и все три tier'а)."""
    error: str | None = None
    """Человекочитаемая причина для ``FetchStatus.ERROR``."""
    tier_used: int | None = None
    """Какой tier закрыл запрос: ``1`` (md5 match), ``2`` (304),
    ``3`` (тело скачано). ``None`` для ``ERROR`` — не дошли до
    определения tier'а или упали посередине. Для ``NEW``/``UPDATED``
    всегда ``3``. ``state.py`` опирается на это поле для UPSERT-правил."""


class FetchError(Exception):
    """Неустранимая ошибка одного HTTP-вызова: 4xx, исчерпанные retries,
    битые байты при потоковой загрузке.

    Внутри ``fetch()`` ловится и превращается в ``FetchResult`` с
    ``status=FetchStatus.ERROR``. Наружу ``fetch()`` не пробрасывает.

    ``status_code`` сохраняется отдельным атрибутом, чтобы вызывающий
    код (например, ``_fetch_md5_sidecar``) мог отличить 404 от 403
    без парсинга текста сообщения.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Discriminated union — исход одного запроса conditional GET.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _NotModified:
    """Сервер ответил 304 на conditional GET — тело не скачано.

    ``etag`` / ``last_modified`` — обновлённые значения из заголовков
    ответа 304 (HTTP-спецификация разрешает их присылать в 304),
    если сервер их прислал; иначе ``None`` и вызывающий код должен
    унаследовать значения из ``previous``.
    """

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class _Downloaded:
    """Тело успешно скачано в ``local_path``.

    ``etag`` / ``last_modified`` — из ответа, для следующего conditional GET.
    """

    local_path: Path
    size_bytes: int
    content_sha256: str
    etag: str | None
    last_modified: str | None


_ConditionalGetOutcome = _NotModified | _Downloaded


# ---------------------------------------------------------------------------
# Тривиальные фабрики.
# ---------------------------------------------------------------------------


def make_user_agent(version: str = __version__, homepage: str = PROJECT_HOMEPAGE) -> str:
    """Собрать значение HTTP-заголовка ``User-Agent``.

    Формат: ``rir2localdb/<version> (+<homepage>)``. Этикет для зеркал RIR.
    """
    return f"rir2localdb/{version} (+{homepage})"


def make_http_client(settings: Settings) -> httpx.AsyncClient:
    """Собрать ``httpx.AsyncClient`` для одного sync-run'а.

    Один клиент на весь run — соединения переиспользуются, keep-alive
    работает, лимиты считаются корректно. Орхестратор отвечает за
    ``async with`` контекст-менеджер вокруг него.
    """
    connect_timeout = min(10.0, settings.http_timeout)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout, connect=connect_timeout),
        limits=httpx.Limits(max_connections=settings.http_max_connections),
        headers={"User-Agent": make_user_agent()},
        follow_redirects=True,
    )


def cache_path_for(settings: Settings, source: Source) -> Path:
    """Путь к локальной копии файла источника.

    Layout: ``<settings.data_dir>/cache/<rir>/<filename>``. Overwrite-in-place;
    история run'ов хранится в БД (``sync_run``), не на диске.
    """
    return settings.data_dir / "cache" / source.rir.value / source.filename


# ---------------------------------------------------------------------------
# Главная функция.
# ---------------------------------------------------------------------------


async def fetch(
    client: httpx.AsyncClient,
    source: Source,
    previous: PreviousFetchState | None,
    settings: Settings,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> FetchResult:
    """Скачать ``source``, применив трёхуровневую детекцию изменений.

    Никогда не бросает исключений из-за сети/HTTP — все такие ошибки
    маппятся в ``FetchResult`` с ``status=FetchStatus.ERROR``.

    Args:
        client: общий ``httpx.AsyncClient`` для run'а.
        source: что качать.
        previous: состояние из ``sync_file``; ``None`` — первый
            успешный fetch источника.
        settings: для ``data_dir`` и ``http_retries``.
        sleep: куда уйти ждать между retries. По умолчанию
            ``asyncio.sleep``; в тестах подсовывается no-op, чтобы
            не ждать реальные секунды.
    """
    started = time.perf_counter()
    target_path = cache_path_for(settings, source)
    md5_sidecar_value: str | None = None

    try:
        # Tier 1 — md5 sidecar.
        if source.md5_url:
            md5_sidecar_value = await _fetch_md5_sidecar(
                client, source.md5_url, settings, sleep=sleep
            )
            if (
                md5_sidecar_value is not None
                and previous is not None
                and previous.last_md5 == md5_sidecar_value
            ):
                return FetchResult(
                    status=FetchStatus.UNCHANGED,
                    url=source.url,
                    md5_sidecar=md5_sidecar_value,
                    fetch_ms=_ms(started),
                    tier_used=1,
                )

        # Tier 2 + 3 — conditional GET с потенциальным стримом тела.
        outcome = await _conditional_get(
            client, source, previous, target_path, settings, sleep=sleep
        )

        if isinstance(outcome, _NotModified):
            # Tier 2 закрыл запрос. Edge-case: md5 рассогласовался,
            # но сервер всё равно говорит 304 — доверяем серверу,
            # но шумим в лог, чтобы инцидент не растворился.
            if (
                md5_sidecar_value is not None
                and previous is not None
                and previous.last_md5 != md5_sidecar_value
            ):
                logger.warning(
                    "md5 mismatch but server returned 304 for %s; "
                    "trusting server, will recheck next run",
                    source.url,
                )
            return FetchResult(
                status=FetchStatus.UNCHANGED,
                url=source.url,
                etag=outcome.etag or (previous.last_etag if previous else None),
                last_modified=outcome.last_modified
                or (previous.last_modified if previous else None),
                md5_sidecar=md5_sidecar_value,
                fetch_ms=_ms(started),
                tier_used=2,
            )

        # 200 — тело скачано. Tier 3: сравниваем sha256.
        if previous is not None and previous.last_sha256 == outcome.content_sha256:
            status = FetchStatus.UNCHANGED
        elif previous is None:
            status = FetchStatus.NEW
        else:
            status = FetchStatus.UPDATED

        return FetchResult(
            status=status,
            url=source.url,
            local_path=outcome.local_path,
            size_bytes=outcome.size_bytes,
            content_sha256=outcome.content_sha256,
            etag=outcome.etag,
            last_modified=outcome.last_modified,
            md5_sidecar=md5_sidecar_value,
            fetch_ms=_ms(started),
            tier_used=3,
        )

    except FetchError as exc:
        return FetchResult(
            status=FetchStatus.ERROR,
            url=source.url,
            md5_sidecar=md5_sidecar_value,
            fetch_ms=_ms(started),
            error=str(exc),
            tier_used=None,
        )


# ---------------------------------------------------------------------------
# Внутренние помощники.
# ---------------------------------------------------------------------------


async def _fetch_md5_sidecar(
    client: httpx.AsyncClient,
    md5_url: str,
    settings: Settings,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str | None:
    """Скачать ``.md5``-сосед и вернуть распарсенный хэш.

    Формат md5-файла у RIR'ов варьируется:
        - ``"<32hex>  <filename>\\n"`` (GNU ``md5sum -b``),
        - ``"<32hex> <filename>\\n"`` (одиночный пробел),
        - ``"<32hex>\\n"`` (bare хэш),
        - ``"MD5 (<filename>) = <32hex>\\n"`` (BSD-формат, RIPE).
    Извлекаем через regex: первая 32-hex последовательность с
    word-boundaries — единственный значимый токен во всех форматах.

    Returns:
        Hex-хэш в нижнем регистре или ``None``, если sidecar отдал ``404``.

    Raises:
        FetchError: на любой другой 4xx, на исчерпанные retries или
            на неконсистентный формат файла.
    """
    try:
        response = await _retry_request(
            f"md5 sidecar {md5_url}",
            settings,
            lambda: client.get(md5_url),
            sleep=sleep,
        )
    except FetchError as exc:
        if exc.status_code == 404:
            return None
        raise

    body = response.text.strip()
    match = _MD5_HEX_RE.search(body)
    if match:
        return match.group(0).lower()
    raise FetchError(f"md5 sidecar {md5_url}: cannot parse {body[:80]!r}")


async def _conditional_get(
    client: httpx.AsyncClient,
    source: Source,
    previous: PreviousFetchState | None,
    target_path: Path,
    settings: Settings,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _ConditionalGetOutcome:
    """Сделать ``GET`` с conditional headers и при 200 скачать тело.

    304 → ``_NotModified``. 200 → стримим тело через ``client.stream`` в
    ``<target_path>.tmp``, считаем sha256 на лету, ``os.replace`` →
    ``_Downloaded``. 5xx/429 ретраятся; 4xx-кроме-429 → ``FetchError``.
    """
    headers: dict[str, str] = {}
    if previous is not None:
        if previous.last_etag:
            headers["If-None-Match"] = previous.last_etag
        if previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_path.with_name(target_path.name + ".tmp")

    max_attempts = settings.http_retries
    last_error = "no attempts made"
    last_status: int | None = None

    for attempt in range(max_attempts):
        explicit_delay: float | None = None
        retryable = False

        try:
            async with client.stream("GET", source.url, headers=headers) as response:
                status = response.status_code
                if status == 304:
                    return _NotModified(
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                    )
                if status >= 400:
                    if status < 500 and status != 429:
                        # Терминальный 4xx — наружу как FetchError.
                        raise FetchError(
                            f"main file {source.url}: HTTP {status}",
                            status_code=status,
                        )
                    # 5xx или 429 — ретраим.
                    last_status = status
                    last_error = f"HTTP {status}"
                    if status == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            # HTTP-date формат игнорим, fallback на backoff.
                            with contextlib.suppress(ValueError):
                                explicit_delay = min(float(retry_after), _BACKOFF_MAX_DELAY)
                    retryable = True
                else:
                    # 2xx — стримим тело.
                    hasher = hashlib.sha256()
                    size = 0
                    with tmp.open("wb") as fp:
                        async for chunk in response.aiter_bytes():
                            hasher.update(chunk)
                            size += len(chunk)
                            fp.write(chunk)
                    os.replace(tmp, target_path)
                    return _Downloaded(
                        local_path=target_path,
                        size_bytes=size,
                        content_sha256=hasher.hexdigest(),
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                    )
        except httpx.RequestError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = None
            retryable = True
            tmp.unlink(missing_ok=True)

        if not retryable or attempt >= max_attempts - 1:
            break
        delay = (
            explicit_delay
            if explicit_delay is not None
            else min(_BACKOFF_BASE**attempt, _BACKOFF_MAX_DELAY)
        )
        await sleep(delay)

    tmp.unlink(missing_ok=True)
    raise FetchError(
        f"main file {source.url}: failed after {max_attempts} attempts ({last_error})",
        status_code=last_status,
    )


async def _retry_request(
    label: str,
    settings: Settings,
    do_request: Callable[[], Awaitable[httpx.Response]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> httpx.Response:
    """Выполнить HTTP-запрос с retries по политике из ``docs/04``.

    Политика:
        - Всего ``settings.http_retries`` попыток (по умолчанию ``3``).
        - Ретраятся: ``httpx.RequestError`` (сеть, DNS, таймаут),
          ``5xx``, ``429`` (с уважением к ``Retry-After`` — числовая
          форма; HTTP-date игнорируется, fallback на exponential backoff).
        - НЕ ретраятся: ``4xx`` кроме ``429`` — мгновенный ``FetchError``.
          ``404`` обработка специфическая (``_fetch_md5_sidecar`` ловит
          и возвращает ``None``).
        - Backoff между попытками: ``min(2 ** attempt, 60)`` секунд,
          где ``attempt`` нумеруется с ``0``. Для ``http_retries=3`` —
          последовательность задержек ``1s, 2s``.

    Args:
        label: короткий идентификатор для логов и сообщения ``FetchError``.
        settings: источник ``http_retries``.
        do_request: zero-arg async callable — один физический HTTP-запрос.
        sleep: куда ждать между попытками; в тестах подсовывается no-op.

    Returns:
        Успешный ``httpx.Response`` (статус ``< 400``).

    Raises:
        FetchError: при 4xx-кроме-429 или после исчерпания retries.
    """
    max_attempts = settings.http_retries
    last_error = "no attempts made"
    last_status: int | None = None

    for attempt in range(max_attempts):
        explicit_delay: float | None = None

        try:
            response = await do_request()
        except httpx.RequestError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = None
        else:
            status = response.status_code
            if status < 400:
                return response
            if status < 500 and status != 429:
                await response.aclose()
                raise FetchError(f"{label}: HTTP {status}", status_code=status)
            last_status = status
            last_error = f"HTTP {status}"
            if status == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    with contextlib.suppress(ValueError):
                        explicit_delay = min(float(retry_after), _BACKOFF_MAX_DELAY)
            await response.aclose()

        if attempt >= max_attempts - 1:
            break
        delay = (
            explicit_delay
            if explicit_delay is not None
            else min(_BACKOFF_BASE**attempt, _BACKOFF_MAX_DELAY)
        )
        await sleep(delay)

    raise FetchError(
        f"{label}: failed after {max_attempts} attempts ({last_error})",
        status_code=last_status,
    )


def _ms(started: float) -> int:
    """Длительность с момента ``started`` в миллисекундах (round-down)."""
    return int((time.perf_counter() - started) * 1000)
