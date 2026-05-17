"""Каталог источников данных пяти RIR.

Этот файл — единственный источник истины для того, что пайплайн знает
о внешнем мире. Все остальные модули (sync/, parsers/, etl/) получают
URL'ы и метаданные отсюда.

Если у RIR изменился путь к файлу — правится только эта структура.
Никакие константы URL'ов в коде вне этого файла недопустимы.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class Rir(StrEnum):
    AFRINIC = "afrinic"
    APNIC = "apnic"
    ARIN = "arin"
    LACNIC = "lacnic"
    RIPE = "ripe"


class Tier(StrEnum):
    """Слой данных. См. docs/01-data-sources.md."""

    CORE = "core"          # delegated-extended, всегда включено
    RICH = "rich"          # RPSL split dumps; RIPE / APNIC / AFRINIC
    ARIN_RR = "arin-rr"    # public ARIN IRR dump
    ARIN_BULK = "arin-bulk"  # ARIN Bulk Whois, требует API-ключ


class Format(StrEnum):
    DELEGATED = "delegated"          # NRO pipe-format
    RPSL = "rpsl"                    # whois RPSL text
    RPSL_GZ = "rpsl-gz"              # gzipped RPSL
    RPSL_SPLIT_GZ = "rpsl-split-gz"  # gzipped RPSL, один тип объекта в файле
    MD5 = "md5"                      # checksum sidecar (не парсится, ис.для валидации)


@dataclass(frozen=True)
class Source:
    """Один конкретный URL, который мы периодически забираем.

    Attributes
    ----------
    rir
        К какому RIR относится.
    tier
        Слой данных. Включается флагом ``--tier``.
    format
        Какой парсер применять.
    url
        Полный HTTPS URL.
    object_type
        Для split-RPSL — какой тип объектов внутри (``inetnum``,
        ``aut-num`` и т.д.). Для остальных — ``None``.
    md5_url
        URL соседнего md5-файла, если RIR его публикует.
    description
        Человекочитаемое описание для логов и API ``/status``.
    """

    rir: Rir
    tier: Tier
    format: Format
    url: str
    object_type: str | None = None
    md5_url: str | None = None
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# CORE tier — delegated-extended для всех пяти RIR
# ---------------------------------------------------------------------------

_AFRINIC_DELEGATED = "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-extended-latest"
_APNIC_DELEGATED = "https://ftp.apnic.net/stats/apnic/delegated-apnic-extended-latest"
_ARIN_DELEGATED = "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest"
_LACNIC_DELEGATED = "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-extended-latest"
_RIPE_DELEGATED = "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest"


CORE_SOURCES: Final[tuple[Source, ...]] = (
    Source(
        rir=Rir.AFRINIC, tier=Tier.CORE, format=Format.DELEGATED,
        url=_AFRINIC_DELEGATED, md5_url=_AFRINIC_DELEGATED + ".md5",
        description="AFRINIC delegated-extended (daily)",
    ),
    Source(
        rir=Rir.APNIC, tier=Tier.CORE, format=Format.DELEGATED,
        url=_APNIC_DELEGATED, md5_url=_APNIC_DELEGATED + ".md5",
        description="APNIC delegated-extended (daily)",
    ),
    Source(
        rir=Rir.ARIN, tier=Tier.CORE, format=Format.DELEGATED,
        url=_ARIN_DELEGATED, md5_url=_ARIN_DELEGATED + ".md5",
        description="ARIN delegated-extended (daily)",
    ),
    Source(
        rir=Rir.LACNIC, tier=Tier.CORE, format=Format.DELEGATED,
        url=_LACNIC_DELEGATED, md5_url=_LACNIC_DELEGATED + ".md5",
        description="LACNIC delegated-extended (daily)",
    ),
    Source(
        rir=Rir.RIPE, tier=Tier.CORE, format=Format.DELEGATED,
        url=_RIPE_DELEGATED, md5_url=_RIPE_DELEGATED + ".md5",
        description="RIPE NCC delegated-extended (daily)",
    ),
)


# ---------------------------------------------------------------------------
# RICH tier — RPSL split dumps (RIPE, APNIC, AFRINIC)
# ---------------------------------------------------------------------------
# RIPE: используем .utf8.gz варианты (личные данные дамифицированы,
# кодировка UTF-8 нормализована).

_RIPE_DBASE = "https://ftp.ripe.net/ripe/dbase/split"

_RIPE_OBJECT_FILES = (
    # (object_type, filename)
    ("inetnum",       "ripe.db.inetnum.utf8.gz"),
    ("inet6num",      "ripe.db.inet6num.utf8.gz"),
    ("aut-num",       "ripe.db.aut-num.utf8.gz"),
    ("organisation",  "ripe.db.organisation.utf8.gz"),
    ("route",         "ripe.db.route.utf8.gz"),
    ("route6",        "ripe.db.route6.utf8.gz"),
    ("as-block",      "ripe.db.as-block.utf8.gz"),
    ("as-set",        "ripe.db.as-set.utf8.gz"),
    ("mntner",        "ripe.db.mntner.utf8.gz"),
    ("role",          "ripe.db.role.utf8.gz"),
    ("irt",           "ripe.db.irt.utf8.gz"),
)

_APNIC_DBASE = "https://ftp.apnic.net/pub/apnic/whois"
_APNIC_OBJECT_FILES = (
    ("inetnum",       "apnic.db.inetnum.gz"),
    ("inet6num",      "apnic.db.inet6num.gz"),
    ("aut-num",       "apnic.db.aut-num.gz"),
    ("organisation",  "apnic.db.organisation.gz"),
    ("route",         "apnic.db.route.gz"),
    ("route6",        "apnic.db.route6.gz"),
    ("as-block",      "apnic.db.as-block.gz"),
    ("as-set",        "apnic.db.as-set.gz"),
    ("mntner",        "apnic.db.mntner.gz"),
    ("role",          "apnic.db.role.gz"),
    ("irt",           "apnic.db.irt.gz"),
)

# AFRINIC отдаёт всё одним файлом, без split'а
_AFRINIC_DBASE = "https://ftp.afrinic.net/pub/dbase"


def _make_rich_sources() -> tuple[Source, ...]:
    items: list[Source] = []

    for obj_type, fname in _RIPE_OBJECT_FILES:
        items.append(
            Source(
                rir=Rir.RIPE, tier=Tier.RICH, format=Format.RPSL_SPLIT_GZ,
                url=f"{_RIPE_DBASE}/{fname}",
                object_type=obj_type,
                description=f"RIPE {obj_type} dump",
                tags=("dummified",),
            )
        )

    for obj_type, fname in _APNIC_OBJECT_FILES:
        items.append(
            Source(
                rir=Rir.APNIC, tier=Tier.RICH, format=Format.RPSL_SPLIT_GZ,
                url=f"{_APNIC_DBASE}/{fname}",
                object_type=obj_type,
                description=f"APNIC {obj_type} dump",
            )
        )

    items.append(
        Source(
            rir=Rir.AFRINIC, tier=Tier.RICH, format=Format.RPSL_GZ,
            url=f"{_AFRINIC_DBASE}/afrinic.db.gz",
            object_type=None,  # все типы в одном файле
            description="AFRINIC combined RPSL dump",
        )
    )

    return tuple(items)


RICH_SOURCES: Final[tuple[Source, ...]] = _make_rich_sources()


# ---------------------------------------------------------------------------
# ARIN-RR tier — public IRR dump (routing data, не whois)
# ---------------------------------------------------------------------------

ARIN_RR_SOURCES: Final[tuple[Source, ...]] = (
    Source(
        rir=Rir.ARIN, tier=Tier.ARIN_RR, format=Format.RPSL_GZ,
        url="https://ftp.arin.net/pub/rr/arin.db.gz",
        description="ARIN IRR RPSL dump (routing only, not whois)",
    ),
)


# ---------------------------------------------------------------------------
# ARIN-BULK tier — официальный Bulk Whois от ARIN
# ---------------------------------------------------------------------------
# Не плоский URL: требует API-ключ из env. Здесь — шаблон.
# fetcher.py подставит ключ из конфига и сформирует финальный URL.

ARIN_BULK_TEMPLATE: Final[str] = (
    "https://accountws.arin.net/public/rest/downloads/bulkwhois?apikey={apikey}"
)


# ---------------------------------------------------------------------------
# Публичный API модуля
# ---------------------------------------------------------------------------

ALL_SOURCES: Final[tuple[Source, ...]] = (
    *CORE_SOURCES,
    *RICH_SOURCES,
    *ARIN_RR_SOURCES,
)


def sources_for_tiers(tiers: set[Tier]) -> tuple[Source, ...]:
    """Вернуть источники для активных tier'ов."""
    return tuple(s for s in ALL_SOURCES if s.tier in tiers)


def sources_by_rir(rir: Rir, tier: Tier | None = None) -> tuple[Source, ...]:
    """Все источники одного RIR, опционально с фильтром по tier'у."""
    return tuple(
        s for s in ALL_SOURCES
        if s.rir == rir and (tier is None or s.tier == tier)
    )
