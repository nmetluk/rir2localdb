"""Pydantic-модели ответов FastAPI.

Намеренно «плоские»: один объект = один HTTP-ответ, никаких
``{"data": ...}``-обёрток. FastAPI оборачивает в HTTP сам.

Полный контракт endpoint'ов — ``docs/06-api.md``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# RPSL-обогащение (Stage 2).
#
# Семантика блоков: если клиент запросил ``?include_rpsl=true`` (default),
# в ответе всегда есть объект ``rpsl`` — но его поля ``inetnum`` /
# ``organisation`` / ``aut_num`` могут быть ``null`` (RPSL для адреса не
# нашли, либо org_handle висит orphan'ом). Если клиент явно поставил
# ``?include_rpsl=false`` — поле ``rpsl`` целиком ``null``. Это даёт
# различимое «не запросили» vs «запросили и не нашли».
# ---------------------------------------------------------------------------


class RpslInetnum(BaseModel):
    """Запись из таблицы ``inetnum`` (IPv4)."""

    rir: str
    start: str
    value: int
    """Число адресов диапазона (IPv4)."""
    netname: str | None = None
    country: str | None = None
    descr: str | None = None
    org: str | None = None
    """``org-handle``; может оказаться orphan (нет строки в ``organisation``)."""
    admin_c: list[str] | None = None
    tech_c: list[str] | None = None
    status: str | None = None
    mnt_by: list[str] | None = None
    created: datetime | None = None
    last_modified: datetime | None = None
    source: str | None = None
    is_stale: bool = False
    """``True`` если GC пометил запись как не появлявшуюся в последних
    N успешных sync'ах (см. ADR-0008, Stage 3-03)."""


class RpslInet6num(BaseModel):
    """Запись из таблицы ``inet6num`` (IPv6)."""

    rir: str
    start: str
    value: int
    """Длина префикса (0..128)."""
    netname: str | None = None
    country: str | None = None
    descr: str | None = None
    org: str | None = None
    admin_c: list[str] | None = None
    tech_c: list[str] | None = None
    status: str | None = None
    mnt_by: list[str] | None = None
    created: datetime | None = None
    last_modified: datetime | None = None
    source: str | None = None
    is_stale: bool = False


class RpslAutNum(BaseModel):
    """Запись из таблицы ``aut_num``."""

    rir: str
    asn: int
    as_name: str | None = None
    descr: str | None = None
    org: str | None = None
    admin_c: list[str] | None = None
    tech_c: list[str] | None = None
    status: str | None = None
    mnt_by: list[str] | None = None
    created: datetime | None = None
    last_modified: datetime | None = None
    source: str | None = None
    is_stale: bool = False


class RpslOrganisation(BaseModel):
    """Запись из таблицы ``organisation``.

    Поле ``email`` соответствует SQL-колонке ``email`` (хранит значения
    RPSL-атрибута ``e-mail:``, нормализованного на ETL'е).
    """

    rir: str
    org_handle: str
    org_name: str | None = None
    org_type: str | None = None
    abuse_c: str | None = None
    address: list[str] | None = None
    phone: list[str] | None = None
    email: list[str] | None = None
    fax_no: list[str] | None = None
    mnt_ref: list[str] | None = None
    mnt_by: list[str] | None = None
    created: datetime | None = None
    last_modified: datetime | None = None
    source: str | None = None
    is_stale: bool = False


class IpRpslBlock(BaseModel):
    """RPSL-обогащение IP-ответа: ``inetnum`` (или ``inet6num``) + ``organisation``."""

    inetnum: RpslInetnum | RpslInet6num | None = None
    organisation: RpslOrganisation | None = None


class AsnRpslBlock(BaseModel):
    """RPSL-обогащение ASN-ответа: ``aut_num`` + ``organisation``."""

    aut_num: RpslAutNum | None = None
    organisation: RpslOrganisation | None = None


# ---------------------------------------------------------------------------
# Базовые lookup-ответы (Stage 1 + rpsl-расширение из Stage 2).
# ---------------------------------------------------------------------------


class IpLookupResponse(BaseModel):
    """``GET /v1/ip/{addr}``."""

    address: str
    """Канонизированный IP-адрес из path-параметра."""
    family: Literal[4, 6]
    rir: str
    cc: str | None
    start: str
    """Начало диапазона (canonical для IPv6, dotted для IPv4)."""
    value: int
    """NRO-семантика: для IPv4 — число адресов, для IPv6 — длина префикса."""
    prefix_length: int | None
    """Для IPv6 = ``value``; для IPv4 ``None`` в Stage 1."""
    status: str
    allocated_on: date | None
    opaque_id: str | None
    first_seen_run: int
    last_seen_run: int
    is_stale: bool = False
    """``True`` если GC пометил allocation как stale (см. ADR-0008).
    Записи с ``is_stale=True`` скрываются по умолчанию; opt-in через
    ``?include_stale=true``."""
    rpsl: IpRpslBlock | None = None
    """``None`` если ``?include_rpsl=false``. Иначе — блок (с возможно
    ``None``-полями, если RPSL-данных нет)."""


class AsnLookupResponse(BaseModel):
    """``GET /v1/asn/{num}``."""

    asn: int
    rir: str
    cc: str | None
    start_asn: int
    count: int
    status: str
    allocated_on: date | None
    opaque_id: str | None
    first_seen_run: int
    last_seen_run: int
    is_stale: bool = False
    rpsl: AsnRpslBlock | None = None
    """``None`` если ``?include_rpsl=false``. Иначе — блок (с возможно
    ``None``-полями, если RPSL-данных нет)."""


class RirSummary(BaseModel):
    """Агрегаты на один RIR. Используется в ``StatusResponse.summary_by_rir``."""

    rir: str
    ip_allocations: int
    asn_allocations: int
    last_fetched_at: datetime | None


class StatusResponse(BaseModel):
    """``GET /v1/status``."""

    latest_sync_run: dict[str, Any] | None
    """Сериализованная строка ``sync_run`` (последний по ``started_at``)."""
    sources: list[dict[str, Any]]
    """Строки ``sync_file`` отсортированы по ``last_fetched_at DESC``."""
    summary_by_rir: list[RirSummary]
    """Сводка по каждому RIR: число записей в ip/asn-таблицах и
    свежесть данных. Удобнее для дашбордов чем плоский ``sources``."""
    db_alive: bool


class HealthzResponse(BaseModel):
    """``GET /v1/healthz`` — liveness."""

    status: Literal["ok"] = "ok"
