"""rpsl_tables — Stage 2 § 2-02. Per-object-type таблицы для RPSL.

8 таблиц: inetnum, inet6num, aut_num, organisation, route, route6,
as_block, role. Один объект-тип = одна таблица; ``rir`` колонка
дискриминирует. См. ADR-0007.

Общий паттерн: per-RIR данные в одной таблице, ``raw JSONB`` хранит
полный RPSL-объект для случаев когда выделенных колонок не хватает.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------
    # inetnum — IPv4 RPSL-объекты.
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE inetnum (
            rir            TEXT      NOT NULL,
            start_text     INET      NOT NULL,
            value          BIGINT    NOT NULL,
            range_v4       INT8RANGE NOT NULL,
            netname        TEXT,
            country        TEXT,
            descr          TEXT,
            org            TEXT,
            admin_c        TEXT[],
            tech_c         TEXT[],
            status         TEXT,
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB     NOT NULL,
            first_seen_run BIGINT    NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT    NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, start_text, value)
        )
        """
    )
    op.execute("CREATE INDEX inetnum_range_v4_gist ON inetnum USING gist (range_v4)")
    op.execute("CREATE INDEX inetnum_org_idx ON inetnum (org) WHERE org IS NOT NULL")
    op.execute("CREATE INDEX inetnum_country_idx ON inetnum (country) WHERE country IS NOT NULL")

    # ---------------------------------------------------------------
    # inet6num — IPv6 RPSL-объекты.
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE inet6num (
            rir            TEXT     NOT NULL,
            start_text     INET     NOT NULL,
            value          SMALLINT NOT NULL,
            range_v6       NUMRANGE NOT NULL,
            netname        TEXT,
            country        TEXT,
            descr          TEXT,
            org            TEXT,
            admin_c        TEXT[],
            tech_c         TEXT[],
            status         TEXT,
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB    NOT NULL,
            first_seen_run BIGINT   NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT   NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, start_text, value)
        )
        """
    )
    op.execute("CREATE INDEX inet6num_range_v6_gist ON inet6num USING gist (range_v6)")
    op.execute("CREATE INDEX inet6num_org_idx ON inet6num (org) WHERE org IS NOT NULL")
    op.execute("CREATE INDEX inet6num_country_idx ON inet6num (country) WHERE country IS NOT NULL")

    # ---------------------------------------------------------------
    # aut_num — single ASN с rich data из RPSL. Не путать с
    # asn_allocation (delegated, может быть range). PK (rir, asn).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE aut_num (
            rir            TEXT   NOT NULL,
            asn            BIGINT NOT NULL,
            as_name        TEXT,
            descr          TEXT,
            org            TEXT,
            admin_c        TEXT[],
            tech_c         TEXT[],
            status         TEXT,
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, asn)
        )
        """
    )
    op.execute("CREATE INDEX aut_num_org_idx ON aut_num (org) WHERE org IS NOT NULL")
    op.execute("CREATE INDEX aut_num_asn_idx ON aut_num (asn)")

    # ---------------------------------------------------------------
    # organisation — юридическое лицо. PK (rir, org_handle).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE organisation (
            rir            TEXT   NOT NULL,
            org_handle     TEXT   NOT NULL,
            org_name       TEXT,
            org_type       TEXT,
            address        TEXT[],
            phone          TEXT[],
            fax_no         TEXT[],
            email          TEXT[],
            abuse_c        TEXT,
            admin_c        TEXT[],
            tech_c         TEXT[],
            mnt_by         TEXT[],
            mnt_ref        TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, org_handle)
        )
        """
    )
    op.execute(
        "CREATE INDEX organisation_abuse_c_idx ON organisation (abuse_c) WHERE abuse_c IS NOT NULL"
    )

    # ---------------------------------------------------------------
    # route — IPv4 маршруты IRR. PK (rir, prefix, origin).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE route (
            rir            TEXT   NOT NULL,
            prefix         CIDR   NOT NULL,
            origin         TEXT   NOT NULL,
            descr          TEXT,
            org            TEXT,
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, prefix, origin)
        )
        """
    )
    op.execute("CREATE INDEX route_origin_idx ON route (origin)")
    op.execute("CREATE INDEX route_prefix_gist ON route USING gist (prefix inet_ops)")

    # ---------------------------------------------------------------
    # route6 — IPv6 маршруты IRR. PK (rir, prefix, origin).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE route6 (
            rir            TEXT   NOT NULL,
            prefix         CIDR   NOT NULL,
            origin         TEXT   NOT NULL,
            descr          TEXT,
            org            TEXT,
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB  NOT NULL,
            first_seen_run BIGINT NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, prefix, origin)
        )
        """
    )
    op.execute("CREATE INDEX route6_origin_idx ON route6 (origin)")
    op.execute("CREATE INDEX route6_prefix_gist ON route6 USING gist (prefix inet_ops)")

    # ---------------------------------------------------------------
    # as_block — RPSL ASN-блок (range AS-номеров). PK (rir, start, end).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE as_block (
            rir            TEXT      NOT NULL,
            as_block_start BIGINT    NOT NULL,
            as_block_end   BIGINT    NOT NULL,
            asn_range      INT8RANGE NOT NULL,
            descr          TEXT,
            mnt_by         TEXT[],
            created        TIMESTAMPTZ,
            last_modified  TIMESTAMPTZ,
            source         TEXT,
            raw            JSONB     NOT NULL,
            first_seen_run BIGINT    NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT    NOT NULL REFERENCES sync_run(id),
            PRIMARY KEY (rir, as_block_start, as_block_end)
        )
        """
    )
    op.execute("CREATE INDEX as_block_range_gist ON as_block USING gist (asn_range)")

    # ---------------------------------------------------------------
    # role — RPSL контактная роль (NOC, abuse, etc.). PK (rir, nic_hdl).
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE role (
            rir            TEXT   NOT NULL,
            nic_hdl        TEXT   NOT NULL,
            role           TEXT,
            address        TEXT[],
            phone          TEXT[],
            fax_no         TEXT[],
            email          TEXT[],
            abuse_mailbox  TEXT,
            admin_c        TEXT[],
            tech_c         TEXT[],
            mnt_by         TEXT[],
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
    op.execute(
        "CREATE INDEX role_abuse_mailbox_idx ON role (abuse_mailbox) "
        "WHERE abuse_mailbox IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS role")
    op.execute("DROP TABLE IF EXISTS as_block")
    op.execute("DROP TABLE IF EXISTS route6")
    op.execute("DROP TABLE IF EXISTS route")
    op.execute("DROP TABLE IF EXISTS organisation")
    op.execute("DROP TABLE IF EXISTS aut_num")
    op.execute("DROP TABLE IF EXISTS inet6num")
    op.execute("DROP TABLE IF EXISTS inetnum")
