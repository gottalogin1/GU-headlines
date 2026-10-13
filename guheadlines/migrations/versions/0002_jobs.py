"""Job postings for the Jobs page: job_sources and jobs.

Revision ID: 0002_jobs
Revises: 0001_initial
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_jobs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("homepage", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("last_count", sa.Integer),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("job_sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("employer", sa.Text),
        sa.Column("pay", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("location", sa.Text),
        sa.Column("open_date", sa.Date),
        sa.Column("close_date", sa.Date),
        sa.Column("close_note", sa.Text),
        sa.Column("pdf_url", sa.Text),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("detail_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_source", "jobs", ["source_id", "removed_at"])
    op.create_index("ix_jobs_open_date", "jobs", [sa.text("open_date DESC NULLS LAST")])


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("job_sources")
