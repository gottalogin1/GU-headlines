"""Visitor counts for the private stats page: page_views and story_clicks.

Revision ID: 0007_stats
Revises: 0006_weather
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_stats"
down_revision = "0006_weather"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_views",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("views", sa.Integer, nullable=False),
    )
    op.create_table(
        "story_clicks",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column(
            "article_id",
            sa.BigInteger,
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("clicks", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("story_clicks")
    op.drop_table("page_views")
