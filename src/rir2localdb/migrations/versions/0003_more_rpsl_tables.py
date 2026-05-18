"""more_rpsl_tables — Stage 2.50 § A. mntner, person, as_set.

Расширение Stage 2 RPSL coverage с 8 до 11 типов объектов. Закрывает
самую большую категорию ``objects_skipped_unknown_type`` из live DoD
(278k объектов на full sync). Таблицы по тем же паттернам что 0002:
PK ``(rir, primary_handle)``, raw JSONB, first/last_seen_run BIGINT FK
на ``sync_run.id``, partial indexes WHERE col IS NOT NULL.

``mntner``  — maintainer объекты; ссылки приходят из ``mnt-by`` всех
других объектов.
``person``  — контактные физлица; ссылки из ``admin-c`` / ``tech-c``.
``as_set``  — наборы AS-номеров для routing policies (RFC 2622 § 5).

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------
    # mntner — maintainer (referenced by mnt-by everywhere).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE mntner (
            rir            TEXT   NOT NULL,
            mntner         TEXT   NOT NULL,
            descr          TEXT,
            admin_c        TEXT[],
            tech_c         TEXT[],
            upd_to         TEXT[],
            mnt_nfy        TEXT[],
            auth           TEXT[],
            remarks        TEXT[],
            notify         TEXT[],
            abuse_mailbox  TEXT,
            mnt_by         TEXT[],
            referral_by    TEXT,
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, mntner)
        )
        """
    )
    op.execute(
        "CREATE INDEX mntner_admin_c_idx ON mntner USING gin (admin_c) WHERE admin_c IS NOT NULL"
    )

    # ---------------------------------------------------------------
    # person — contact handle (referenced by admin-c/tech-c/abuse-c).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE person (
            rir            TEXT   NOT NULL,
            nic_hdl        TEXT   NOT NULL,
            person         TEXT,
            address        TEXT[],
            phone          TEXT[],
            fax_no         TEXT[],
            email          TEXT[],
            remarks        TEXT[],
            notify         TEXT[],
            mnt_by         TEXT[],
            abuse_mailbox  TEXT,
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, nic_hdl)
        )
        """
    )
    op.execute("CREATE INDEX person_email_idx ON person USING gin (email) WHERE email IS NOT NULL")

    # ---------------------------------------------------------------
    # as_set — RPSL set objects for routing policies.
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE as_set (
            rir            TEXT   NOT NULL,
            as_set         TEXT   NOT NULL,
            descr          TEXT,
            members        TEXT[],
            mbrs_by_ref    TEXT[],
            remarks        TEXT[],
            tech_c         TEXT[],
            admin_c        TEXT[],
            notify         TEXT[],
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, as_set)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS as_set")
    op.execute("DROP TABLE IF EXISTS person")
    op.execute("DROP TABLE IF EXISTS mntner")
