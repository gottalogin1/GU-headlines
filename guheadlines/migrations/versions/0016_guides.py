"""Visitor guides that the site's owner writes (guides.py).

Revision ID: 0016_guides
Revises: 0015_renewals
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_guides"
down_revision = "0015_renewals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guides",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("section", sa.Text, nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("position", sa.Integer, nullable=False, server_default="100"),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("access", sa.Text, nullable=False, server_default="free"),
        sa.Column("photo", sa.Text),
        sa.Column("photo_alt", sa.Text, nullable=False, server_default=""),
        sa.Column("sponsor_name", sa.Text, nullable=False, server_default=""),
        sa.Column("sponsor_link", sa.Text, nullable=False, server_default=""),
        sa.Column("sponsor_text", sa.Text, nullable=False, server_default=""),
        sa.Column("sponsor_until", sa.Date),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('draft', 'published')", name="guides_status"),
        sa.CheckConstraint("access IN ('free', 'paid')", name="guides_access"),
    )
    op.create_index("guides_published", "guides", ["status", "position"])


def downgrade() -> None:
    op.drop_table("guides")
