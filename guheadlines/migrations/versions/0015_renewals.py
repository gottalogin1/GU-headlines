"""Renewals of paid listings and booked ads: listing_renewals, the email of a
listing's buyer, and when each was told that it ends soon (renewals.py).

Revision ID: 0015_renewals
Revises: 0014_bid_alerts
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_renewals"
down_revision = "0014_bid_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("email", sa.Text))
    op.add_column("listings", sa.Column("reminded_at", sa.DateTime(timezone=True)))
    op.add_column("ad_bookings", sa.Column("reminded_at", sa.DateTime(timezone=True)))
    op.create_table(
        "listing_renewals",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "listing_id",
            sa.BigInteger,
            sa.ForeignKey("listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("order_id", sa.Text, nullable=False, unique=True),
        sa.Column("capture_id", sa.Text, unique=True),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("days", sa.Integer, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_listing_renewals_listing_id", "listing_renewals", ["listing_id"])


def downgrade() -> None:
    op.drop_table("listing_renewals")
    op.drop_column("ad_bookings", "reminded_at")
    op.drop_column("listings", "reminded_at")
    op.drop_column("listings", "email")
