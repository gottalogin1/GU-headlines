"""The editions of the newsletter feed (/feed.xml): newsletter_editions.

Revision ID: 0009_newsletter
Revises: 0008_referrers
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_newsletter"
down_revision = "0008_referrers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "newsletter_editions",
        sa.Column("formed_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("article_ids", postgresql.ARRAY(sa.BigInteger), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("newsletter_editions")
