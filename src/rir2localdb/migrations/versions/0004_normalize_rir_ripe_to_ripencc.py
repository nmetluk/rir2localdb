"""normalize_rir_ripe_to_ripencc — Stage 2.50 § B.

До Stage 2.50:
- ``ip_allocation.rir = 'ripencc'`` (NRO delegated registry).
- RPSL таблицы (inetnum/...) сохраняли ``'ripe'`` (Rir.RIPE.value).

Расхождение мешало cross-tier запросам и было техдолгом. Stage 2.50
нормализует Rir.RIPE.value → 'ripencc' и UPDATE'ит existing rows
во всех 11 RPSL-таблицах.

Downgrade — обратные UPDATE'ы для отката, на случай rollback'а
с production. Семантически migration data-only (без DDL).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RPSL_TABLES: tuple[str, ...] = (
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
    for table in _RPSL_TABLES:
        op.execute(f"UPDATE {table} SET rir = 'ripencc' WHERE rir = 'ripe'")


def downgrade() -> None:
    for table in _RPSL_TABLES:
        op.execute(f"UPDATE {table} SET rir = 'ripe' WHERE rir = 'ripencc'")
