"""``GET /v1/asn/{num}`` — поиск ASN-allocation + RPSL-обогащение.

Stage 2: подмешивается блок ``rpsl`` со строкой из ``aut_num`` + LEFT
JOIN на ``organisation`` по ``(rir, org_handle)``. Cross-RIR ссылки
на org допустимы; если org-handle висит orphan'ом —
``rpsl.organisation`` будет ``null``.

Query-параметр ``?include_rpsl=false`` отключает обогащение.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.api.schemas import (
    AsnLookupResponse,
    AsnRpslBlock,
    RpslAutNum,
    RpslOrganisation,
)

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


_AUT_NUM_RPSL_SQL = text(
    """
    SELECT
        a.rir, a.asn, a.as_name, a.descr, a.org, a.admin_c, a.tech_c,
        a.status, a.mnt_by, a.created, a.last_modified, a.source,
        o.rir            AS o_rir,
        o.org_handle     AS o_org_handle,
        o.org_name       AS o_org_name,
        o.org_type       AS o_org_type,
        o.abuse_c        AS o_abuse_c,
        o.address        AS o_address,
        o.phone          AS o_phone,
        o.email          AS o_email,
        o.fax_no         AS o_fax_no,
        o.mnt_ref        AS o_mnt_ref,
        o.mnt_by         AS o_mnt_by,
        o.created        AS o_created,
        o.last_modified  AS o_last_modified,
        o.source         AS o_source
    FROM aut_num a
    LEFT JOIN organisation o
        ON o.rir = a.rir AND o.org_handle = a.org
    WHERE a.asn = :asn
    LIMIT 1
    """
)


@router.get("/asn/{num}", response_model=AsnLookupResponse)
async def lookup_asn(
    num: int,
    request: Request,
    include_rpsl: bool = Query(default=True),
) -> AsnLookupResponse:
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
            raise HTTPException(status_code=404, detail=f"no allocation found for AS{num}")

        rpsl_block: AsnRpslBlock | None = None
        if include_rpsl:
            rpsl_block = await _fetch_asn_rpsl(session, num)

    return AsnLookupResponse(asn=num, **dict(row), rpsl=rpsl_block)


async def _fetch_asn_rpsl(session: AsyncSession, asn: int) -> AsnRpslBlock:
    """``aut_num`` для ``asn`` + LEFT JOIN на ``organisation``.

    Возвращает ``AsnRpslBlock`` всегда. Cross-RIR org-handle ищется по
    ``(rir, org_handle)``; orphan → ``organisation`` ``None``.
    """
    result = await session.execute(_AUT_NUM_RPSL_SQL, {"asn": asn})
    row = result.mappings().first()
    if row is None:
        return AsnRpslBlock()

    aut_num_obj = RpslAutNum(
        rir=row["rir"],
        asn=row["asn"],
        as_name=row["as_name"],
        descr=row["descr"],
        org=row["org"],
        admin_c=row["admin_c"],
        tech_c=row["tech_c"],
        status=row["status"],
        mnt_by=row["mnt_by"],
        created=row["created"],
        last_modified=row["last_modified"],
        source=row["source"],
    )

    org_obj: RpslOrganisation | None = None
    if row["o_org_handle"] is not None:
        org_obj = RpslOrganisation(
            rir=row["o_rir"],
            org_handle=row["o_org_handle"],
            org_name=row["o_org_name"],
            org_type=row["o_org_type"],
            abuse_c=row["o_abuse_c"],
            address=row["o_address"],
            phone=row["o_phone"],
            email=row["o_email"],
            fax_no=row["o_fax_no"],
            mnt_ref=row["o_mnt_ref"],
            mnt_by=row["o_mnt_by"],
            created=row["o_created"],
            last_modified=row["o_last_modified"],
            source=row["o_source"],
        )

    return AsnRpslBlock(aut_num=aut_num_obj, organisation=org_obj)
