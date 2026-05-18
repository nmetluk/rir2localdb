"""add_rdap_cache — Stage 3-05 § B. RDAP fallback cache.

Key-value cache для on-demand RDAP-запросов (ARIN fallback когда
bulk RPSL для блока отсутствует). См. ADR-0009.

``cache_key`` — composite ``<kind>:<value>`` (например ``ip:8.8.8.8``,
``autnum:15169``). ``response_raw`` — весь RDAP-ответ; ``normalized``
— наша inetnum/aut_num-shape для прямой подстановки в API responses.
``expires_at`` — пассивная expiration (запросы старее игнорируются
и обновляются на следующем lookup'е).

Negative-кешируем 404 / 429 / 5xx — с коротким TTL (``settings.
rdap_negative_cache_minutes``, default 5 мин) чтобы не долбить
RDAP при отсутствующих блоках.

GC чистит entries старее ``now() - 7 days`` (см. ``sync/gc.py``).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE rdap_cache (
            cache_key      TEXT        PRIMARY KEY,
            response_raw   JSONB       NOT NULL,
            normalized     JSONB       NOT NULL,
            fetched_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            expires_at     TIMESTAMPTZ NOT NULL,
            http_status    INTEGER     NOT NULL,
            error_message  TEXT
        )
        """
    )
    op.execute("CREATE INDEX rdap_cache_expires_at_idx ON rdap_cache (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rdap_cache")
