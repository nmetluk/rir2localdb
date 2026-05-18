"""``GET /v1/ip/{addr}`` — поиск allocation по IP-адресу.

Принимает IPv4 (dotted) и IPv6 (canonical / compressed / uppercase).
Возвращает самую специфичную (узкую) запись, охватывающую адрес —
RIR-данные иногда содержат overlapping allocations, и узкая запись
точнее.
"""

from __future__ import annotations

from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address, ip_address

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from rir2localdb.api.schemas import IpLookupResponse

router = APIRouter()


_IPV4_SQL = text(
    """
    SELECT rir, family, cc, host(start_text) AS start, value, prefix_length,
           status, allocated_on, opaque_id, first_seen_run, last_seen_run
    FROM ip_allocation
    WHERE family = 4 AND range_v4 @> CAST(:ip AS int8)
    ORDER BY upper(range_v4) - lower(range_v4) ASC
    LIMIT 1
    """
)

_IPV6_SQL = text(
    """
    SELECT rir, family, cc, host(start_text) AS start, value, prefix_length,
           status, allocated_on, opaque_id, first_seen_run, last_seen_run
    FROM ip_allocation
    WHERE family = 6 AND range_v6 @> CAST(:ip AS numeric)
    ORDER BY upper(range_v6) - lower(range_v6) ASC
    LIMIT 1
    """
)


@router.get("/ip/{addr}", response_model=IpLookupResponse)
async def lookup_ip(addr: str, request: Request) -> IpLookupResponse:
    try:
        ip = ip_address(addr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid IP address: {exc}") from exc

    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        if isinstance(ip, IPv4Address):
            result = await session.execute(_IPV4_SQL, {"ip": int(ip)})
        else:
            assert isinstance(ip, IPv6Address)
            # 128-битное значение в int не лезет в PostgreSQL bigint —
            # отправляем как numeric через Decimal.
            result = await session.execute(_IPV6_SQL, {"ip": Decimal(int(ip))})
        row = result.mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"no allocation found for {addr}")

    return IpLookupResponse(address=str(ip), **dict(row))
