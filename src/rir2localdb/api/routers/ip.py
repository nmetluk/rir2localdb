"""``GET /v1/ip/{addr}`` — поиск allocation по IP-адресу + RPSL-обогащение.

Принимает IPv4 (dotted) и IPv6 (canonical / compressed / uppercase).
Возвращает самую специфичную (узкую) запись, охватывающую адрес —
RIR-данные иногда содержат overlapping allocations, и узкая запись
точнее.

Stage 2: подмешивается блок ``rpsl`` с самым узким ``inetnum`` /
``inet6num`` + LEFT JOIN на ``organisation`` по ``(rir, org_handle)``.
Cross-RIR ссылки на org допустимы — если org-handle не найден,
``rpsl.organisation`` будет ``null``. См. ADR-0007.

Stage 3-03: stale-records скрываются по умолчанию. ``?include_stale=
true`` показывает в т.ч. помеченные ``is_stale=TRUE`` GC'ом. Каждый
объект ответа содержит ``is_stale`` поле; клиент видит флаг даже
когда запрашивал без include_stale (в этом случае все возвращённые
записи — active, ``is_stale=false``).

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


# ``(is_stale = FALSE OR :include_stale)`` — при ``include_stale=true``
# фильтр пропадает (любая строка проходит), planner использует full
# GiST индекс. При ``include_stale=false`` — partial GiST индекс
# ``..._active_gist WHERE is_stale = FALSE`` (см. migration 0005).

_IPV4_SQL = text(
    """
    SELECT rir, family, cc, host(start_text) AS start, value, prefix_length,
           status, allocated_on, opaque_id, first_seen_run, last_seen_run, is_stale
    FROM ip_allocation
    WHERE family = 4 AND range_v4 @> CAST(:ip AS int8)
      AND (is_stale = FALSE OR :include_stale)
    ORDER BY upper(range_v4) - lower(range_v4) ASC
    LIMIT 1
    """
)

_IPV6_SQL = text(
    """
    SELECT rir, family, cc, host(start_text) AS start, value, prefix_length,
           status, allocated_on, opaque_id, first_seen_run, last_seen_run, is_stale
    FROM ip_allocation
    WHERE family = 6 AND range_v6 @> CAST(:ip AS numeric)
      AND (is_stale = FALSE OR :include_stale)
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
        i.created, i.last_modified, i.source, i.is_stale,
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
        o.source         AS o_source,
        o.is_stale       AS o_is_stale
    FROM inetnum i
    LEFT JOIN organisation o
        ON o.rir = i.rir AND o.org_handle = i.org
        AND (o.is_stale = FALSE OR :include_stale)
    WHERE i.range_v4 @> CAST(:ip AS int8)
      AND (i.is_stale = FALSE OR :include_stale)
    ORDER BY upper(i.range_v4) - lower(i.range_v4) ASC
    LIMIT 1
    """
)

_INET6NUM_RPSL_SQL = text(
    """
    SELECT
        i.rir, host(i.start_text) AS start, i.value, i.netname, i.country,
        i.descr, i.org, i.admin_c, i.tech_c, i.status, i.mnt_by,
        i.created, i.last_modified, i.source, i.is_stale,
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
        o.source         AS o_source,
        o.is_stale       AS o_is_stale
    FROM inet6num i
    LEFT JOIN organisation o
        ON o.rir = i.rir AND o.org_handle = i.org
        AND (o.is_stale = FALSE OR :include_stale)
    WHERE i.range_v6 @> CAST(:ip AS numeric)
      AND (i.is_stale = FALSE OR :include_stale)
    ORDER BY upper(i.range_v6) - lower(i.range_v6) ASC
    LIMIT 1
    """
)


@router.get("/ip/{addr}", response_model=IpLookupResponse)
async def lookup_ip(
    addr: str,
    request: Request,
    include_rpsl: bool = Query(default=True),
    include_stale: bool = Query(
        default=False,
        description="Include records marked as stale by GC (default: hide).",
    ),
) -> IpLookupResponse:
    try:
        ip = ip_address(addr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid IP address: {exc}") from exc

    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        if isinstance(ip, IPv4Address):
            result = await session.execute(
                _IPV4_SQL, {"ip": int(ip), "include_stale": include_stale}
            )
        else:
            assert isinstance(ip, IPv6Address)
            # 128-битное значение в int не лезет в PostgreSQL bigint —
            # отправляем как numeric через Decimal.
            result = await session.execute(
                _IPV6_SQL, {"ip": Decimal(int(ip)), "include_stale": include_stale}
            )
        row = result.mappings().first()

        if row is None:
            raise HTTPException(status_code=404, detail=f"no allocation found for {addr}")

        rpsl_block: IpRpslBlock | None = None
        if include_rpsl:
            rpsl_block = await _fetch_ip_rpsl(session, ip, include_stale=include_stale)

    return IpLookupResponse(address=str(ip), **dict(row), rpsl=rpsl_block)


async def _fetch_ip_rpsl(
    session: AsyncSession, ip: IPv4Address | IPv6Address, *, include_stale: bool
) -> IpRpslBlock:
    """Самый узкий inetnum/inet6num, охватывающий ``ip``, + LEFT JOIN organisation.

    Возвращает ``IpRpslBlock`` всегда (даже когда оба поля ``None``).
    Cross-RIR org-handle: соответствие ищется по ``(rir, org_handle)``;
    если в дампах висит «осиротевшая» ссылка — ``organisation`` ``None``.

    Stale-фильтр применяется и к inetnum/inet6num, и к организации.
    """
    params: dict[str, object] = {"include_stale": include_stale}
    if isinstance(ip, IPv4Address):
        params["ip"] = int(ip)
        result = await session.execute(_INETNUM_RPSL_SQL, params)
    else:
        params["ip"] = Decimal(int(ip))
        result = await session.execute(_INET6NUM_RPSL_SQL, params)
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
        is_stale=row["is_stale"],
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
            is_stale=row["o_is_stale"],
        )

    return IpRpslBlock(inetnum=inetnum_obj, organisation=org_obj)
