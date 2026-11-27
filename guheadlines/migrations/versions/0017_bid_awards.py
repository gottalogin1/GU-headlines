"""Awarded government bids for the award history page: bid_awards, and when each
bid source's award list was last read.

Revision ID: 0017_bid_awards
Revises: 0016_guides
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_bid_awards"
down_revision = "0016_guides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bid_sources", sa.Column("awards_run_at", sa.DateTime(timezone=True)))
    op.add_column("bid_sources", sa.Column("awards_error", sa.Text))
    op.add_column("bid_sources", sa.Column("awards_count", sa.Integer))
    op.create_table(
        "bid_awards",
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
        sa.Column("awarded_to", sa.Text),
        sa.Column("amount_cents", sa.BigInteger),
        sa.Column("amounts", sa.Text),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("document_url", sa.Text),
        sa.Column("posted_on", sa.Date),
        sa.Column("closed_on", sa.Date),
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
        sa.UniqueConstraint("source_id", "uid", name="uq_bid_awards_source_uid"),
    )
    op.create_index("ix_bid_awards_closed_on", "bid_awards", ["closed_on"])


def downgrade() -> None:
    op.drop_table("bid_awards")
    op.drop_column("bid_sources", "awards_count")
    op.drop_column("bid_sources", "awards_error")
    op.drop_column("bid_sources", "awards_run_at")
