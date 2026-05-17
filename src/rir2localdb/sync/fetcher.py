"""HTTPS-загрузчик одного ``Source``.

Скелет (Stage 1, шаг 3) — типы и контракты, без реализации.

Поведение целиком описано в ``docs/04-sync-pipeline.md`` (три уровня
детекции изменений) и ``docs/02-architecture.md`` (контракт sync-слоя).

Высокоуровневый сценарий ``fetch()``:

1. **Tier 1 — md5-сосед.** Если у ``Source`` задан ``md5_url``, скачиваем
   крохотный ``.md5``-файл и сравниваем хэш с ``PreviousFetchState.last_md5``.
   - Совпало → ``FetchStatus.UNCHANGED``, основной файл не качаем.
   - 404 на ``.md5`` → молча пропускаем tier 1 (некоторые RIR его не
     публикуют), переходим к tier 2.
   - Другой HTTP-ошибочный статус (4xx) → ``FetchStatus.ERROR``.
2. **Tier 2 — conditional GET.** Шлём ``GET`` с ``If-Modified-Since`` и
   ``If-None-Match`` из ``PreviousFetchState``. ``304 Not Modified`` →
   ``FetchStatus.UNCHANGED`` (обновляем лишь ``last_fetched_at`` снаружи).
3. **Tier 3 — sha256.** Если пришло ``200 OK``, стримим тело в
   ``<data_dir>/cache/<rir>/<filename>.tmp``, считаем sha256, делаем
   atomic-rename. Если sha совпал с ``PreviousFetchState.last_sha256`` —
   ``FetchStatus.UNCHANGED`` (но файл на диске обновлён, что нормально).
   Иначе — ``FetchStatus.UPDATED`` (или ``NEW``, если ``previous is None``).

``fetch()`` **не бросает** на сетевых ошибках и HTTP-кодах — они
мэппятся в ``FetchStatus.ERROR`` с ``error`` полем. Программные ошибки
(кривой ``Source``, баг внутри) идут наверх как обычно.

Retry — только внутри одного HTTP-вызова (см. ``_retry_request``), не
повторяем всю tier-ную последовательность.
"""

from __future__ import annotations

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

    Передаётся в ``fetch()``. ``None`` означает «источник раньше не
    скачивался успешно» — все поля недоступны, ``fetch()`` идёт по
    короткому пути «качаем как NEW».
    """

    last_md5: str | None = None
    last_etag: str | None = None
    last_modified: str | None = None
    """Значение HTTP-заголовка ``Last-Modified`` как строка
    (RFC 7231 IMF-fixdate). Парсить в ``datetime`` тут не нужно —
    конвертация в TZ-aware ``datetime`` для ``sync_file.last_modified``
    делается в ``sync/state.py``.
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
    Для ``UNCHANGED`` через tier 1 или tier 2 — ``None`` (на диске
    остаётся прошлая копия, читать её state.py может по
    ``cache_path_for()``).
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
    """Человекочитаемая причина для ``FetchStatus.ERROR``; ``None``
    для всех остальных исходов."""


class FetchError(Exception):
    """Неустранимая ошибка одного HTTP-вызова: 4xx, исчерпанные retries,
    битые байты при потоковой загрузке.

    Внутри ``fetch()`` ловится и превращается в ``FetchResult`` с
    ``status=FetchStatus.ERROR``. Наружу ``fetch()`` не пробрасывает —
    оркестратор работает с ``FetchResult``, не с исключениями.
    """


def make_user_agent(version: str = __version__, homepage: str = PROJECT_HOMEPAGE) -> str:
    """Собрать значение HTTP-заголовка ``User-Agent``.

    Формат: ``rir2localdb/<version> (+<homepage>)``. RIR'ам полезно видеть
    осмысленный UA на трафике с зеркал — этикет и упрощает разбор
    инцидентов с их стороны.
    """
    raise NotImplementedError("make_user_agent — stage 1 step 3")


def make_http_client(settings: Settings) -> httpx.AsyncClient:
    """Собрать ``httpx.AsyncClient`` для одного sync-run'а.

    Один клиент на весь run, не один на каждый ``fetch()``: соединения
    переиспользуются, keep-alive работает, лимиты считаются корректно.
    Орхестратор отвечает за ``async with`` контекст-менеджер.

    Заполняет: ``timeout``, ``limits``, ``headers={"User-Agent": ...}``,
    ``follow_redirects=True``, HTTP/2 (через ``httpx[http2]``).
    """
    raise NotImplementedError("make_http_client — stage 1 step 3")


def cache_path_for(settings: Settings, source: Source) -> Path:
    """Путь к локальной копии файла источника.

    Layout: ``<settings.data_dir>/cache/<rir>/<filename>``. Это
    overwrite-in-place кэш «последняя успешная версия»; история run'ов
    хранится в БД (``sync_run``), не на диске. Stage 3 ops-hardening
    может добавить per-run snapshot — пока не нужно.
    """
    raise NotImplementedError("cache_path_for — stage 1 step 3")


async def fetch(
    client: httpx.AsyncClient,
    source: Source,
    previous: PreviousFetchState | None,
    settings: Settings,
) -> FetchResult:
    """Скачать ``source``, применив трёхуровневую детекцию изменений.

    Никогда не бросает исключений из-за сети/HTTP — все такие ошибки
    маппятся в ``FetchResult`` с ``status=FetchStatus.ERROR`` и
    непустым ``error``. Поднимается наружу только программная ошибка
    (баг, неконсистентный ``Source``).

    Args:
        client: общий ``httpx.AsyncClient`` для run'а
            (см. :func:`make_http_client`).
        source: что качать.
        previous: состояние из ``sync_file``; ``None`` — первый
            успешный fetch источника.
        settings: для ``data_dir`` (куда писать) и ``http_retries``
            (сколько раз ретраить).

    Returns:
        ``FetchResult`` с одним из четырёх статусов: ``NEW``,
        ``UPDATED``, ``UNCHANGED``, ``ERROR``. См. docstring модуля.
    """
    raise NotImplementedError("fetch — stage 1 step 3")


# ---------------------------------------------------------------------------
# Внутренние помощники — публичны только в пределах sync-пакета, но
# выставлены тут с явными контрактами, чтобы тесты и orchestrator могли
# на них опереться по необходимости.
# ---------------------------------------------------------------------------


async def _fetch_md5_sidecar(
    client: httpx.AsyncClient,
    md5_url: str,
    settings: Settings,
) -> str | None:
    """Скачать ``.md5``-сосед и вернуть распарсенный хэш.

    Returns:
        Hex-хэш (32 hex-символа в нижнем регистре) или ``None``, если
        sidecar отдал ``404`` (некоторые RIR'ы его не публикуют).

    Raises:
        FetchError: на любой другой ошибке (5xx после retries,
            4xx≠404, неконсистентный формат файла).
    """
    raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _NotModified:
    """Сервер ответил 304 на conditional GET — тело не скачано."""


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


async def _conditional_get(
    client: httpx.AsyncClient,
    source: Source,
    previous: PreviousFetchState | None,
    target_path: Path,
    settings: Settings,
) -> _ConditionalGetOutcome:
    """Сделать ``GET`` с ``If-Modified-Since`` + ``If-None-Match``.

    Если ответ ``304`` — возвращает ``_NotModified()``. Если ``200`` —
    стримит тело в ``target_path.tmp``, делает atomic rename, считает
    размер и sha256 на лету, возвращает ``_Downloaded(...)``.

    Все 5xx ретраятся через :func:`_retry_request`. 4xx — мгновенный
    ``FetchError``.
    """
    raise NotImplementedError


async def _retry_request(
    label: str,
    settings: Settings,
    do_request: Callable[[], Awaitable[httpx.Response]],
) -> httpx.Response:
    """Выполнить HTTP-запрос с retries по политике из ``docs/04``.

    Политика:
        - Всего ``settings.http_retries`` попыток (по умолчанию ``3``).
        - Ретраятся: ``httpx.RequestError`` (сеть, DNS, таймаут),
          ``httpx.HTTPStatusError`` со статусом ``>= 500``,
          ``429 Too Many Requests`` (с уважением к ``Retry-After``,
          если он есть).
        - НЕ ретраятся: ``4xx`` (кроме ``429``). Их обработка — на
          вызывающем коде (``404`` на .md5 — это «нет sidecar'а», не
          ошибка fetch'а в целом).
        - Backoff между попытками: ``min(2 ** attempt, max_delay)`` секунд,
          где ``attempt`` нумеруется с ``0`` (задержка перед первым
          retry). Для дефолтных ``http_retries=3`` это последовательность
          ``1s, 2s`` (два retry после первой попытки). ``max_delay``
          захардкожен в ``60s``.

    Args:
        label: короткий идентификатор для логов и сообщений в
            ``FetchError`` (например, ``"md5 sidecar"`` или
            ``"main file"``).
        settings: источник ``http_retries``.
        do_request: zero-arg async callable, которая выполняет один
            физический запрос. Должна сама вызывать ``response.raise_for_status()``
            если хочет, чтобы 5xx превратился в исключение для retry-логики.

    Returns:
        Успешный ``httpx.Response`` (статус-код любой ``2xx`` / ``3xx``;
        ``304`` отдельно — обработка снаружи).

    Raises:
        FetchError: при исчерпании retries или при 4xx-ответе.
    """
    raise NotImplementedError
