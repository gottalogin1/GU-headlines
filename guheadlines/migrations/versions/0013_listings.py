"""Paid listings: classified ads, featured job posts, featured events and the
business directory (listings.py).

Revision ID: 0013_listings
Revises: 0012_ad_bookings
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_listings"
down_revision = "0012_ad_bookings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("category", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("location", sa.Text),
        sa.Column("price", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("contact", sa.Text),
        sa.Column("link", sa.Text),
        sa.Column("photo", sa.Text),
        sa.Column("photo_width", sa.Integer),
        sa.Column("photo_height", sa.Integer),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("all_day", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("featured", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("order_id", sa.Text, unique=True),
        sa.Column("capture_id", sa.Text, unique=True),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("refunded_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("live_until", sa.DateTime(timezone=True)),
        sa.Column("event_id", sa.BigInteger, sa.ForeignKey("events.id", ondelete="SET NULL")),
        sa.Column("note", sa.Text),
    )
    op.create_index("ix_listings_kind_status", "listings", ["kind", "status"])


def downgrade() -> None:
    op.drop_table("listings")
