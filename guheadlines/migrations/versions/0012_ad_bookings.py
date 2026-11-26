"""Ads booked on the Advertise page (see ads.py): ad_bookings and ad_slots.

Revision ID: 0012_ad_bookings
Revises: 0011_bids
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_ad_bookings"
down_revision = "0011_bids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_bookings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("business", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("link", sa.Text, nullable=False),
        sa.Column("words", sa.Text, nullable=False),
        sa.Column("picture", sa.LargeBinary),
        sa.Column("picture_width", sa.Integer),
        sa.Column("picture_height", sa.Integer),
        sa.Column("days", postgresql.ARRAY(sa.Date), nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("paypal_order_id", sa.Text),
        sa.Column("paypal_capture_id", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ad_bookings_status", "ad_bookings", ["status"])
    op.create_table(
        "ad_slots",
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column(
            "booking_id",
            sa.Integer,
            sa.ForeignKey("ad_bookings.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_ad_slots_booking_id", "ad_slots", ["booking_id"])


def downgrade() -> None:
    op.drop_table("ad_slots")
    op.drop_table("ad_bookings")
