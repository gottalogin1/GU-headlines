"""Guide packs: visitor guides sold together through PayPal, and the passes
that buyers read them with (guide_packs.py).

Revision ID: 0017_guide_packs
Revises: 0016_guides
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_guide_packs"
down_revision = "0016_guides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guide_packs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("about", sa.Text, nullable=False, server_default=""),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("every_paid", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("position", sa.Integer, nullable=False, server_default="100"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('draft', 'on_sale')", name="guide_packs_status"),
        sa.CheckConstraint("price_cents > 0", name="guide_packs_price"),
    )
    op.create_table(
        "guide_pack_guides",
        sa.Column(
            "pack_id",
            sa.Integer,
            sa.ForeignKey("guide_packs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "guide_id", sa.Integer, sa.ForeignKey("guides.id", ondelete="CASCADE"), primary_key=True
        ),
    )
    op.create_table(
        "guide_passes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("token", sa.Text, nullable=False, unique=True),
        sa.Column(
            "pack_id",
            sa.Integer,
            sa.ForeignKey("guide_packs.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("order_id", sa.Text, nullable=False, unique=True),
        sa.Column("capture_id", sa.Text, unique=True),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("email", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="paid"),
        sa.Column("opened", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("emailed_at", sa.DateTime(timezone=True)),
        sa.Column("refunded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('paid', 'pending', 'off', 'refunded')", name="guide_passes_status"
        ),
    )
    op.create_index("guide_passes_email", "guide_passes", [sa.text("lower(email)")])


def downgrade() -> None:
    op.drop_table("guide_passes")
    op.drop_table("guide_pack_guides")
    op.drop_table("guide_packs")
