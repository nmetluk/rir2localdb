"""Pydantic-модели ответов FastAPI.

Намеренно «плоские»: один объект = один HTTP-ответ, никаких
``{"data": ...}``-обёрток. FastAPI оборачивает в HTTP сам.

Полный контракт endpoint'ов — ``docs/06-api.md``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel


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


class StatusResponse(BaseModel):
    """``GET /v1/status``."""

    latest_sync_run: dict[str, Any] | None
    """Сериализованная строка ``sync_run`` (последний по ``started_at``)."""
    sources: list[dict[str, Any]]
    """Строки ``sync_file`` отсортированы по ``last_fetched_at DESC``."""
    db_alive: bool


class HealthzResponse(BaseModel):
    """``GET /v1/healthz`` — liveness."""

    status: Literal["ok"] = "ok"
