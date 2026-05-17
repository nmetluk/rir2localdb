"""SQLAlchemy 2.x declarative модели для таблиц из миграции 0001.

Тут только то, что нужно ORM-потребителям (sync/state.py, queries для API).
ETL-горячие пути (COPY в staging) идут мимо ORM через сырой asyncpg,
поэтому ``IpAllocation`` / ``AsnAllocation`` появятся, когда понадобятся
API-запросам, не раньше.

См. ``docs/03-database-schema.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Корневой declarative-base для всех ORM-моделей проекта."""


class SyncRun(Base):
    """Журнал прогонов sync — одна строка на запуск ``rir2localdb sync``."""

    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tier: Mapped[str] = mapped_column(Text)
    """Какой tier запускали: ``core`` / ``rich`` / ``arin-rr`` / ``arin-bulk``."""
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text)
    """``running`` / ``success`` / ``failed`` (см. ``docs/03``)."""
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncFile(Base):
    """Состояние одного файла-источника. ``url`` — естественный PK."""

    __tablename__ = "sync_file"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    rir: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    """``source.format.value`` из ``sources.py``: ``delegated`` / ``rpsl`` /
    ``rpsl-gz`` / ``rpsl-split-gz`` / ``md5``."""
    last_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_run.id"), nullable=True
    )
    last_status: Mapped[str] = mapped_column(Text)
    """``new`` / ``updated`` / ``unchanged`` / ``error`` — соответствует
    ``FetchStatus.value`` в ``sync/fetcher.py``."""
    last_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_md5: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_parsed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
