"""RDAP fallback для ARIN ownership/contacts.

Stage 3-05 § C. См. ADR-0009.

ARIN не публикует open bulk RPSL дамп (только IRR routes). Для
ownership/contacts полные whois-данные доступны через RDAP API
(RFC 7480-7484), open и без credentials. Используем on-demand
fallback: когда основной inetnum/aut_num lookup возвращает NULL И
allocation принадлежит ARIN'у — синхронно бежим к RDAP, кэшируем
результат в ``rdap_cache`` (default TTL 24h), отвечаем клиенту в
прозрачном для него формате (``rpsl.inetnum`` / ``rpsl.aut_num`` shape).

**Rate-limit strategy:** ARIN допускает burst ~50 req/min. Мы
полагаемся на 429-ответы + negative cache: при попадании на 429
кэшируем negative с коротким TTL (default 5 мин), что естественно
снижает нагрузку при множестве запросов на uncached blocks.

**Только для ARIN:** для RIPE / APNIC / AFRINIC у нас есть полный
bulk RPSL. Для LACNIC fallback не делаем в Stage 3-05 (можно
добавить позже).

Public API:

    lookup_ip_rdap(session, http_client, addr, settings) -> RdapResult
    lookup_asn_rdap(session, http_client, asn, settings) -> RdapResult
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rir2localdb.config import Settings

logger = logging.getLogger(__name__)


ARIN_RDAP_BASE: Final[str] = "https://rdap.arin.net/registry"


@dataclass(frozen=True, slots=True)
class RdapResult:
    """Результат одного RDAP lookup'а.

    ``found=True`` — RDAP вернул данные (200), ``normalized`` готов
    для подстановки в API response.
    ``found=False`` — 404 / network error / RDAP вернул объект, который
    не маппится. ``error`` non-empty в этом случае.
    ``cached=True`` — результат из ``rdap_cache``, ``False`` — свежий
    HTTP-запрос.
    """

    found: bool
    normalized: dict[str, Any] | None
    raw: dict[str, Any] | None
    error: str | None
    cached: bool
    http_status: int


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


async def lookup_ip_rdap(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    addr: str,
    settings: Settings,
) -> RdapResult:
    """RDAP IP lookup with DB cache.

    ``addr`` — canonical IP string (IPv4 dotted или IPv6 compressed),
    как из ``ipaddress.ip_address(...)``. cache_key = ``"ip:<addr>"``.
    """
    return await _lookup(
        session,
        http_client,
        cache_key=f"ip:{addr}",
        url=f"{ARIN_RDAP_BASE}/ip/{addr}",
        normalizer=_normalize_rdap_ip,
        settings=settings,
    )


async def lookup_asn_rdap(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    asn: int,
    settings: Settings,
) -> RdapResult:
    """RDAP ASN lookup with DB cache. cache_key = ``"autnum:<n>"``."""
    return await _lookup(
        session,
        http_client,
        cache_key=f"autnum:{asn}",
        url=f"{ARIN_RDAP_BASE}/autnum/{asn}",
        normalizer=_normalize_rdap_autnum,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Core lookup logic — cache + HTTP + normalize.
# ---------------------------------------------------------------------------


async def _lookup(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    *,
    cache_key: str,
    url: str,
    normalizer: Any,
    settings: Settings,
) -> RdapResult:
    kind = cache_key.split(":", 1)[0]  # "ip" | "autnum"
    # Импорт здесь, а не на top-level — избежать цикла rdap → metrics → app.
    from rir2localdb.api.metrics import rdap_lookups_total

    # 1. Cache lookup.
    cached_row = (
        await session.execute(
            text(
                "SELECT normalized, http_status, error_message FROM rdap_cache "
                "WHERE cache_key = :k AND expires_at > now()"
            ),
            {"k": cache_key},
        )
    ).first()
    if cached_row is not None:
        normalized, http_status, error_message = cached_row
        # JSONB → dict | str — asyncpg/SQLAlchemy default decoding.
        norm_dict: dict[str, Any] | None
        if isinstance(normalized, str):
            try:
                norm_dict = json.loads(normalized) if normalized else None
            except (ValueError, TypeError):
                norm_dict = None
        else:
            norm_dict = normalized if normalized else None
        found = norm_dict is not None and int(http_status) == 200
        rdap_lookups_total.labels(
            kind=kind, cached="true", found="true" if found else "false"
        ).inc()
        return RdapResult(
            found=found,
            normalized=norm_dict if found else None,
            raw=None,
            error=error_message,
            cached=True,
            http_status=int(http_status),
        )

    # 2. HTTP fetch.
    try:
        response = await http_client.get(
            url,
            headers={"Accept": "application/rdap+json"},
            timeout=settings.rdap_http_timeout_seconds,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("rdap network error url=%s err=%s", url, exc)
        rdap_lookups_total.labels(kind=kind, cached="false", found="false").inc()
        # НЕ кэшируем network errors — клиент следующий раз попробует снова.
        return RdapResult(
            found=False, normalized=None, raw=None, error=str(exc), cached=False, http_status=0
        )

    # 3. Branch on status.
    if response.status_code == 200:
        raw = response.json()
        try:
            normalized = normalizer(raw)
        except Exception as exc:
            logger.warning("rdap normalization failed url=%s err=%s", url, exc)
            return RdapResult(
                found=False,
                normalized=None,
                raw=raw,
                error=f"normalize: {exc}",
                cached=False,
                http_status=200,
            )
        ttl = timedelta(hours=settings.rdap_cache_ttl_hours)
        await _store_cache(session, cache_key, raw, normalized, ttl, 200, None)
        rdap_lookups_total.labels(kind=kind, cached="false", found="true").inc()
        return RdapResult(
            found=True, normalized=normalized, raw=raw, error=None, cached=False, http_status=200
        )

    # 4. Negative cache (404 / 429 / 5xx).
    ttl = timedelta(minutes=settings.rdap_negative_cache_minutes)
    error_msg = f"HTTP {response.status_code}"
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            # Если Retry-After в секундах — используем как TTL. Если HTTP-date
            # или что-то нечитаемое — игнорируем, остаёмся на default-TTL.
            with contextlib.suppress(ValueError):
                ttl = max(ttl, timedelta(seconds=int(retry_after)))
    await _store_cache(session, cache_key, {}, {}, ttl, response.status_code, error_msg)
    rdap_lookups_total.labels(kind=kind, cached="false", found="false").inc()
    return RdapResult(
        found=False,
        normalized=None,
        raw=None,
        error=error_msg,
        cached=False,
        http_status=response.status_code,
    )


async def _store_cache(
    session: AsyncSession,
    cache_key: str,
    raw: dict[str, Any],
    normalized: dict[str, Any],
    ttl: timedelta,
    http_status: int,
    error_message: str | None,
) -> None:
    """UPSERT в rdap_cache. expires_at = now() + ttl."""
    expires_at = datetime.now(tz=UTC) + ttl
    await session.execute(
        text(
            """
            INSERT INTO rdap_cache
                (cache_key, response_raw, normalized,
                 fetched_at, expires_at, http_status, error_message)
            VALUES (:k, CAST(:raw AS jsonb), CAST(:norm AS jsonb),
                    now(), :exp, :status, :err)
            ON CONFLICT (cache_key) DO UPDATE SET
                response_raw  = EXCLUDED.response_raw,
                normalized    = EXCLUDED.normalized,
                fetched_at    = EXCLUDED.fetched_at,
                expires_at    = EXCLUDED.expires_at,
                http_status   = EXCLUDED.http_status,
                error_message = EXCLUDED.error_message
            """
        ),
        {
            "k": cache_key,
            "raw": json.dumps(raw),
            "norm": json.dumps(normalized),
            "exp": expires_at,
            "status": http_status,
            "err": error_message,
        },
    )
    await session.commit()


# ---------------------------------------------------------------------------
# RDAP → наш inetnum/aut_num shape.
# ---------------------------------------------------------------------------


def _normalize_rdap_ip(raw: dict[str, Any]) -> dict[str, Any]:
    """RDAP IP-network response → inetnum/inet6num-shape dict.

    Минимальный mapping; для деталей RDAP-формата — RFC 7483 § 5.4.
    Schema совпадает с ``RpslInetnum`` / ``RpslInet6num`` pydantic-моделями.
    """
    start = raw.get("startAddress", "")
    end = raw.get("endAddress", "")
    if not start or not end:
        raise ValueError("RDAP response missing startAddress / endAddress")

    start_ip = ipaddress.ip_address(start)
    end_ip = ipaddress.ip_address(end)
    if isinstance(start_ip, ipaddress.IPv4Address):
        value = int(end_ip) - int(start_ip) + 1
    else:
        # IPv6: value = prefix length, но RDAP даёт start/end. Подсчитаем
        # длину префикса через bit_length разности; для не-CIDR-выровненных
        # блоков (редко) даём 0 (clients увидят, что value не sane).
        diff = int(end_ip) - int(start_ip) + 1
        bitlen = (diff - 1).bit_length() if diff > 1 else 0
        value = 128 - bitlen

    name = raw.get("name") or None
    country = raw.get("country") or None
    status = raw.get("type") or None  # e.g. "DIRECT ALLOCATION"

    registrant_handle, org_record = _extract_registrant(raw.get("entities") or [])
    admin_c = _extract_role_handles(raw.get("entities") or [], "administrative")
    tech_c = _extract_role_handles(raw.get("entities") or [], "technical")

    created, last_modified = _extract_events(raw.get("events") or [])

    inetnum: dict[str, Any] = {
        "rir": "arin",
        "start": str(start_ip),
        "value": value,
        "netname": name,
        "country": country,
        "descr": None,
        "org": registrant_handle,
        "admin_c": admin_c or None,
        "tech_c": tech_c or None,
        "status": status,
        "mnt_by": None,
        "created": created,
        "last_modified": last_modified,
        "source": "ARIN-RDAP",
        "is_stale": False,
    }
    return {"inetnum": inetnum, "organisation": org_record}


def _normalize_rdap_autnum(raw: dict[str, Any]) -> dict[str, Any]:
    """RDAP autnum response → aut_num-shape dict. RFC 7483 § 5.3."""
    asn_start = raw.get("startAutnum")
    if asn_start is None:
        raise ValueError("RDAP response missing startAutnum")

    name = raw.get("name") or None
    status = (raw.get("status") or [None])[0] if isinstance(raw.get("status"), list) else None

    registrant_handle, org_record = _extract_registrant(raw.get("entities") or [])
    admin_c = _extract_role_handles(raw.get("entities") or [], "administrative")
    tech_c = _extract_role_handles(raw.get("entities") or [], "technical")

    created, last_modified = _extract_events(raw.get("events") or [])

    aut_num: dict[str, Any] = {
        "rir": "arin",
        "asn": int(asn_start),
        "as_name": name,
        "descr": None,
        "org": registrant_handle,
        "admin_c": admin_c or None,
        "tech_c": tech_c or None,
        "status": status,
        "mnt_by": None,
        "created": created,
        "last_modified": last_modified,
        "source": "ARIN-RDAP",
        "is_stale": False,
    }
    return {"aut_num": aut_num, "organisation": org_record}


def _extract_registrant(
    entities: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    """Найти entity с роль ``registrant`` → (handle, organisation-shape dict).

    organisation-shape: если у entity есть vcardArray с fn / org —
    кладём в org_name; адрес/телефон/email — из vcard'а; иначе пусто.
    """
    for ent in entities:
        roles = ent.get("roles") or []
        if "registrant" not in roles:
            continue
        handle = ent.get("handle") or None
        vcard_fields = _parse_vcard(ent.get("vcardArray"))
        org = {
            "rir": "arin",
            "org_handle": handle or "",
            "org_name": vcard_fields.get("fn") or vcard_fields.get("org"),
            "org_type": None,
            "abuse_c": None,
            "address": vcard_fields.get("address"),
            "phone": vcard_fields.get("phone"),
            "email": vcard_fields.get("email"),
            "fax_no": None,
            "mnt_ref": None,
            "mnt_by": None,
            "created": None,
            "last_modified": None,
            "source": "ARIN-RDAP",
            "is_stale": False,
        }
        return handle, org
    return None, None


def _extract_role_handles(entities: list[dict[str, Any]], role: str) -> list[str]:
    return [
        ent["handle"] for ent in entities if role in (ent.get("roles") or []) and ent.get("handle")
    ]


def _extract_events(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """events → (created_iso, last_modified_iso). RFC 7483 § 4.5."""
    created = None
    last_modified = None
    for event in events:
        action = event.get("eventAction")
        date_str = event.get("eventDate")
        if action == "registration":
            created = date_str
        elif action == "last changed":
            last_modified = date_str
    return created, last_modified


def _parse_vcard(vcard_array: Any) -> dict[str, Any]:
    """RDAP vCard (jCard, RFC 7095) → плоский dict с полями fn/org/email/phone/address.

    jCard format: ``["vcard", [["fn", {}, "text", "GOGL"], ["adr", {}, "text",
    ["", "", "1600 Amphitheatre Pkwy", "Mountain View", "CA", "94043", "US"]], ...]]``
    """
    if not isinstance(vcard_array, list) or len(vcard_array) < 2:
        return {}
    properties = vcard_array[1]
    if not isinstance(properties, list):
        return {}

    out: dict[str, Any] = {}
    addresses: list[str] = []
    phones: list[str] = []
    emails: list[str] = []

    for prop in properties:
        if not isinstance(prop, list) or len(prop) < 4:
            continue
        name = prop[0]
        value = prop[3]

        if name == "fn" and isinstance(value, str):
            out["fn"] = value
        elif name == "org" and isinstance(value, str):
            out["org"] = value
        elif name == "adr" and isinstance(value, list):
            # value — структурированный array; склеиваем непустые части.
            joined = ", ".join(p for p in value if isinstance(p, str) and p)
            if joined:
                addresses.append(joined)
        elif name == "tel" and isinstance(value, str):
            phones.append(value)
        elif name == "email" and isinstance(value, str):
            emails.append(value)

    if addresses:
        out["address"] = addresses
    if phones:
        out["phone"] = phones
    if emails:
        out["email"] = emails
    return out
