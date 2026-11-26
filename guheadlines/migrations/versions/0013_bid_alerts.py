"""Paid email alerts for new bids: bid_alert_plans and bid_alert_subscribers.

Revision ID: 0013_bid_alerts
Revises: 0012_ad_bookings
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_bid_alerts"
down_revision = "0012_ad_bookings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bid_alert_plans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Text, nullable=False),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("plan_id", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("client_id", "price_cents", name="uq_bid_alert_plans_price"),
    )
    op.create_table(
        "bid_alert_subscribers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("trades", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("keywords", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("token", sa.Text, nullable=False, unique=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("paypal_id", sa.Text, unique=True),
        sa.Column("approve_url", sa.Text),
        sa.Column("paypal_created_at", sa.DateTime(timezone=True)),
        sa.Column("paid_until", sa.DateTime(timezone=True)),
        sa.Column("last_bid_id", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_bid_alert_subscribers_email", "bid_alert_subscribers", ["email"])


def downgrade() -> None:
    op.drop_table("bid_alert_subscribers")
    op.drop_table("bid_alert_plans")
