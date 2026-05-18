"""``GET /v1/asn/{num}`` — поиск ASN-allocation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from rir2localdb.api.schemas import AsnLookupResponse

router = APIRouter()

_ASN_MAX: int = 2**32 - 1  # 32-bit ASN, IANA-ограничение

_ASN_SQL = text(
    """
    SELECT rir, cc, start_asn, count, status, allocated_on,
           opaque_id, first_seen_run, last_seen_run
    FROM asn_allocation
    WHERE asn_range @> CAST(:asn AS int8)
    ORDER BY upper(asn_range) - lower(asn_range) ASC
    LIMIT 1
    """
)


@router.get("/asn/{num}", response_model=AsnLookupResponse)
async def lookup_asn(num: int, request: Request) -> AsnLookupResponse:
    if num < 0 or num > _ASN_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"ASN out of range (must be 0..{_ASN_MAX}): {num}",
        )

    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        result = await session.execute(_ASN_SQL, {"asn": num})
        row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404, detail=f"no allocation found for AS{num}"
        )

    return AsnLookupResponse(asn=num, **dict(row))
