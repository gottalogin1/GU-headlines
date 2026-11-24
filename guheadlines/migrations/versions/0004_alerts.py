"""Remember which failing news sites and job boards were already reported.

Revision ID: 0004_alerts
Revises: 0003_remove_reddit_guam
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_alerts"
down_revision = "0003_remove_reddit_guam"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("alerts")
