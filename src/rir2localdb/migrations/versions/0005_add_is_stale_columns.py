"""add_is_stale_columns — Stage 3-03 § C. Soft-delete для stale records.

Closes ADR-0001 / opens ADR-0008.

Добавляет ``is_stale BOOLEAN NOT NULL DEFAULT FALSE`` на 13 таблиц с
``last_seen_run``: 2 delegated (ip_allocation, asn_allocation) + 11
RPSL (inetnum, inet6num, aut_num, organisation, role, route, route6,
as_block, mntner, person, as_set).

Партиальные GiST-индексы ``..._active_gist WHERE is_stale = FALSE``
для 4 range-based lookup паттернов:

- ``ip_allocation`` v4/v6
- ``inetnum`` v4
- ``inet6num`` v6

Это даёт быстрый default-path для API (``include_stale=false``).
Оригинальные full-index'ы (``..._gist`` без partial-clause) остаются —
для ``?include_stale=true`` запросов и для аналитики.

Не-range-based таблицы (organisation, role, mntner, person, ...)
запрашиваются по PK ``(rir, handle)``, который и так fast; partial
индекс по ``is_stale`` там излишен.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Все таблицы с ``last_seen_run`` — субъекты GC.
_TABLES: tuple[str, ...] = (
    "ip_allocation",
    "asn_allocation",
    "inetnum",
    "inet6num",
    "aut_num",
    "organisation",
    "role",
    "route",
    "route6",
    "as_block",
    "mntner",
    "person",
    "as_set",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN is_stale BOOLEAN NOT NULL DEFAULT FALSE")

    # Partial GiST индексы для range-based active-only поиска.
    # ip_allocation: family-разделение уже было в оригинальных
    # ``ip_allocation_v4_gist`` / ``v6_gist`` (см. 0001).
    op.execute(
        "CREATE INDEX ip_allocation_v4_active_gist "
        "ON ip_allocation USING gist (range_v4) "
        "WHERE family = 4 AND is_stale = FALSE"
    )
    op.execute(
        "CREATE INDEX ip_allocation_v6_active_gist "
        "ON ip_allocation USING gist (range_v6) "
        "WHERE family = 6 AND is_stale = FALSE"
    )
    op.execute(
        "CREATE INDEX inetnum_range_v4_active_gist "
        "ON inetnum USING gist (range_v4) "
        "WHERE is_stale = FALSE"
    )
    op.execute(
        "CREATE INDEX inet6num_range_v6_active_gist "
        "ON inet6num USING gist (range_v6) "
        "WHERE is_stale = FALSE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS inet6num_range_v6_active_gist")
    op.execute("DROP INDEX IF EXISTS inetnum_range_v4_active_gist")
    op.execute("DROP INDEX IF EXISTS ip_allocation_v6_active_gist")
    op.execute("DROP INDEX IF EXISTS ip_allocation_v4_active_gist")
    for table in reversed(_TABLES):
        op.execute(f"ALTER TABLE {table} DROP COLUMN is_stale")
