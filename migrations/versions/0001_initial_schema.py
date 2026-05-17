"""initial schema — sync_run, sync_file, ip_allocation, asn_allocation.

Соответствует ``docs/03-database-schema.md``. Написана руками (не autogenerate)
из-за ``int8range`` / ``numrange`` и GiST-индексов: SQLAlchemy умеет range-типы
через ``dialects.postgresql``, но партиальные GiST и CHECK по family
ровнее выражаются сырым SQL'ем, а docs/03 — точный источник истины.

Revision ID: 0001
Revises:
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sync_run (
            id          BIGSERIAL PRIMARY KEY,
            tier        TEXT        NOT NULL,
            started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            status      TEXT        NOT NULL,
            stats       JSONB       NOT NULL DEFAULT '{}'::jsonb,
            error       TEXT
        )
        """
    )
    op.execute("CREATE INDEX sync_run_started_at_idx ON sync_run (started_at DESC)")
    op.execute(
        """
        CREATE INDEX sync_run_status_partial_idx
            ON sync_run (status)
         WHERE status IN ('running', 'failed')
        """
    )

    op.execute(
        """
        CREATE TABLE sync_file (
            url             TEXT        PRIMARY KEY,
            rir             TEXT        NOT NULL,
            tier            TEXT        NOT NULL,
            kind            TEXT        NOT NULL,
            last_run_id     BIGINT      REFERENCES sync_run(id),
            last_status     TEXT        NOT NULL,
            last_etag       TEXT,
            last_modified   TIMESTAMPTZ,
            last_md5        TEXT,
            last_sha256     TEXT,
            last_size       BIGINT,
            last_fetched_at TIMESTAMPTZ,
            last_parsed_at  TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX sync_file_rir_idx  ON sync_file (rir)")
    op.execute("CREATE INDEX sync_file_tier_idx ON sync_file (tier)")

    op.execute(
        """
        CREATE TABLE ip_allocation (
            id             BIGSERIAL    PRIMARY KEY,
            rir            TEXT         NOT NULL,
            cc             TEXT,
            family         SMALLINT     NOT NULL,
            range_v4       INT8RANGE,
            range_v6       NUMRANGE,
            prefix_length  SMALLINT,
            start_text     INET         NOT NULL,
            value          BIGINT       NOT NULL,
            status         TEXT         NOT NULL,
            allocated_on   DATE,
            opaque_id      TEXT,
            extensions     TEXT,
            first_seen_run BIGINT       NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT       NOT NULL REFERENCES sync_run(id),
            CONSTRAINT ip_allocation_family_range_chk CHECK (
                (family = 4 AND range_v4 IS NOT NULL AND range_v6 IS NULL) OR
                (family = 6 AND range_v6 IS NOT NULL AND range_v4 IS NULL)
            )
        )
        """
    )
    # Natural key: (rir, family, start_text, value) — смена value (размер блока)
    # трактуется как новая аллокация, старая помечается stale через last_seen_run.
    op.execute(
        """
        CREATE UNIQUE INDEX ip_allocation_natural_key_uq
            ON ip_allocation (rir, family, start_text, value)
        """
    )
    op.execute(
        """
        CREATE INDEX ip_allocation_v4_gist
            ON ip_allocation USING gist (range_v4)
         WHERE family = 4
        """
    )
    op.execute(
        """
        CREATE INDEX ip_allocation_v6_gist
            ON ip_allocation USING gist (range_v6)
         WHERE family = 6
        """
    )
    op.execute("CREATE INDEX ip_allocation_cc_idx  ON ip_allocation (cc)")
    op.execute("CREATE INDEX ip_allocation_rir_idx ON ip_allocation (rir)")

    op.execute(
        """
        CREATE TABLE asn_allocation (
            id             BIGSERIAL  PRIMARY KEY,
            rir            TEXT       NOT NULL,
            cc             TEXT,
            asn_range      INT8RANGE  NOT NULL,
            start_asn      BIGINT     NOT NULL,
            count          INTEGER    NOT NULL,
            status         TEXT       NOT NULL,
            allocated_on   DATE,
            opaque_id      TEXT,
            extensions     TEXT,
            first_seen_run BIGINT     NOT NULL REFERENCES sync_run(id),
            last_seen_run  BIGINT     NOT NULL REFERENCES sync_run(id)
        )
        """
    )
    # Natural key: (rir, start_asn, count).
    op.execute(
        """
        CREATE UNIQUE INDEX asn_allocation_natural_key_uq
            ON asn_allocation (rir, start_asn, count)
        """
    )
    op.execute(
        """
        CREATE INDEX asn_allocation_range_gist
            ON asn_allocation USING gist (asn_range)
        """
    )
    op.execute("CREATE INDEX asn_allocation_start_idx ON asn_allocation (start_asn)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asn_allocation")
    op.execute("DROP TABLE IF EXISTS ip_allocation")
    op.execute("DROP TABLE IF EXISTS sync_file")
    op.execute("DROP TABLE IF EXISTS sync_run")
