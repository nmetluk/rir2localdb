"""``GET /v1/ip/{addr}`` — поиск allocation по IP-адресу + RPSL-обогащение.

Принимает IPv4 (dotted) и IPv6 (canonical / compressed / uppercase).
Возвращает самую специфичную (узкую) запись, охватывающую адрес —
RIR-данные иногда содержат overlapping allocations, и узкая запись
точнее.

Stage 2: подмешивается блок ``rpsl`` с самым узким ``inetnum`` /
``inet6num`` + LEFT JOIN на ``organisation`` по ``(rir, org_handle)``.
Cross-RIR ссылки на org допустимы — если org-handle не найден,
``rpsl.organisation`` будет ``null``. См. ADR-0007.

Query-параметр ``?include_rpsl=false`` отключает обогащение целиком
(``rpsl: null``) для bandwidth-sensitive клиентов.
"""

from __future__ import annotations

from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address, ip_address

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.api.schemas import (
    IpLookupResponse,
    IpRpslBlock,
    RpslInet6num,
    RpslInetnum,
    RpslOrganisation,
)

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


# Колонки organisation алиасятся префиксом ``o_``, чтобы не сталкиваться
# с одноимёнными ``rir`` / ``created`` / ``last_modified`` / ``source`` /
# ``mnt_by`` в inetnum-блоке. mappings().first() возвращает плоский dict.

_INETNUM_RPSL_SQL = text(
    """
    SELECT
        i.rir, host(i.start_text) AS start, i.value, i.netname, i.country,
        i.descr, i.org, i.admin_c, i.tech_c, i.status, i.mnt_by,
        i.created, i.last_modified, i.source,
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
    FROM inetnum i
    LEFT JOIN organisation o
        ON o.rir = i.rir AND o.org_handle = i.org
    WHERE i.range_v4 @> CAST(:ip AS int8)
    ORDER BY upper(i.range_v4) - lower(i.range_v4) ASC
    LIMIT 1
    """
)

_INET6NUM_RPSL_SQL = text(
    """
    SELECT
        i.rir, host(i.start_text) AS start, i.value, i.netname, i.country,
        i.descr, i.org, i.admin_c, i.tech_c, i.status, i.mnt_by,
        i.created, i.last_modified, i.source,
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
    FROM inet6num i
    LEFT JOIN organisation o
        ON o.rir = i.rir AND o.org_handle = i.org
    WHERE i.range_v6 @> CAST(:ip AS numeric)
    ORDER BY upper(i.range_v6) - lower(i.range_v6) ASC
    LIMIT 1
    """
)


@router.get("/ip/{addr}", response_model=IpLookupResponse)
async def lookup_ip(
    addr: str,
    request: Request,
    include_rpsl: bool = Query(default=True),
) -> IpLookupResponse:
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

        rpsl_block: IpRpslBlock | None = None
        if include_rpsl:
            rpsl_block = await _fetch_ip_rpsl(session, ip)

    return IpLookupResponse(address=str(ip), **dict(row), rpsl=rpsl_block)


async def _fetch_ip_rpsl(session: AsyncSession, ip: IPv4Address | IPv6Address) -> IpRpslBlock:
    """Самый узкий inetnum/inet6num, охватывающий ``ip``, + LEFT JOIN organisation.

    Возвращает ``IpRpslBlock`` всегда (даже когда оба поля ``None``).
    Cross-RIR org-handle: соответствие ищется по ``(rir, org_handle)``;
    если в дампах висит «осиротевшая» ссылка — ``organisation`` ``None``.
    """
    if isinstance(ip, IPv4Address):
        result = await session.execute(_INETNUM_RPSL_SQL, {"ip": int(ip)})
    else:
        result = await session.execute(_INET6NUM_RPSL_SQL, {"ip": Decimal(int(ip))})
    row = result.mappings().first()
    if row is None:
        return IpRpslBlock()

    inetnum_cls: type[RpslInetnum] | type[RpslInet6num]
    inetnum_cls = RpslInetnum if isinstance(ip, IPv4Address) else RpslInet6num
    inetnum_obj = inetnum_cls(
        rir=row["rir"],
        start=row["start"],
        value=row["value"],
        netname=row["netname"],
        country=row["country"],
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

    return IpRpslBlock(inetnum=inetnum_obj, organisation=org_obj)
