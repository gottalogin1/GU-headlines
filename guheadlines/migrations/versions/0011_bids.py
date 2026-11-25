"""Government bids and requests for proposals for the Bids page: bid_sources and bids.

Revision ID: 0011_bids
Revises: 0010_keyword_alerts
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_bids"
down_revision = "0010_keyword_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bid_sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("homepage", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("last_count", sa.Integer),
    )
    op.create_table(
        "bids",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("bid_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uid", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("number", sa.Text),
        sa.Column("kind", sa.Text),
        sa.Column("agency", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("document_url", sa.Text),
        sa.Column("posted_on", sa.Date),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("due_time", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_id", "uid", name="uq_bids_source_uid"),
    )
    op.create_index("ix_bids_due_at", "bids", ["due_at"])
    op.create_index("ix_bids_source", "bids", ["source_id", "removed_at"])


def downgrade() -> None:
    op.drop_table("bids")
    op.drop_table("bid_sources")
