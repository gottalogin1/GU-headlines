"""The websites that visits come from, for the private stats page: referrers.

Revision ID: 0008_referrers
Revises: 0007_stats
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_referrers"
down_revision = "0007_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "referrers",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("site", sa.Text, primary_key=True),
        sa.Column("visits", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("referrers")
