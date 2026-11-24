"""Events for the Events page: event_calendars and events.

Revision ID: 0004_events
Revises: 0003_remove_reddit_guam
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_events"
down_revision = "0003_remove_reddit_guam"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_calendars",
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
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "calendar_id",
            sa.Integer,
            sa.ForeignKey("event_calendars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uid", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("all_day", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("location", sa.Text),
        sa.Column("organizer", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("agenda_url", sa.Text),
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
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("calendar_id", "uid", name="uq_events_calendar_uid"),
    )
    op.create_index("ix_events_starts_at", "events", ["starts_at"])
    op.create_index("ix_events_calendar", "events", ["calendar_id", "removed_at"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("event_calendars")
