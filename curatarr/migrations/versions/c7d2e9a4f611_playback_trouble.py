"""Playback tries and troubles (playback_trouble.py): files a device keeps
giving up on early.

Revision ID: c7d2e9a4f611
Revises: b3e8d1f6a729
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d2e9a4f611"
down_revision = "b3e8d1f6a729"
branch_labels = None
depends_on = None


def _timed():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "playback_tries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("media_part_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("device", sa.String(length=200), nullable=True),
        sa.Column("client", sa.String(length=200), nullable=True),
        sa.Column("play_method", sa.String(length=40), nullable=True),
        sa.Column("video_codec", sa.String(length=40), nullable=True),
        sa.Column("audio_codec", sa.String(length=40), nullable=True),
        sa.Column("transcode_reasons", sa.JSON(), nullable=True),
        sa.Column("runtime", sa.Float(), nullable=True),
        sa.Column("furthest", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended", sa.Boolean(), nullable=False),
        *_timed(),
        sa.ForeignKeyConstraint(["media_part_id"], ["media_parts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_playback_tries_item_id", "playback_tries", ["item_id"])
    op.create_index("ix_playback_tries_started_at", "playback_tries", ["started_at"])
    op.create_table(
        "playback_troubles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("device", sa.String(length=200), nullable=False),
        sa.Column("media_part_id", sa.String(length=36), nullable=True),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_try_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        *_timed(),
        sa.ForeignKeyConstraint(["media_part_id"], ["media_parts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_playback_troubles_state", "playback_troubles", ["state"])


def downgrade():
    op.drop_index("ix_playback_troubles_state", table_name="playback_troubles")
    op.drop_table("playback_troubles")
    op.drop_index("ix_playback_tries_started_at", table_name="playback_tries")
    op.drop_index("ix_playback_tries_item_id", table_name="playback_tries")
    op.drop_table("playback_tries")
