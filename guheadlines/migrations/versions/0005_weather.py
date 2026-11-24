"""Weather for the warning banner and the weather box: the weather table.

Revision ID: 0005_weather
Revises: 0004_alerts
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_weather"
down_revision = "0004_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weather",
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("data", JSONB, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("weather")
