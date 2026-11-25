"""The stories that were in a keyword alert (ALERT_KEYWORDS): keyword_alerts.

Revision ID: 0010_keyword_alerts
Revises: 0009_newsletter
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_keyword_alerts"
down_revision = "0009_newsletter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "keyword_alerts",
        sa.Column(
            "article_id",
            sa.BigInteger,
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("keyword_alerts")
