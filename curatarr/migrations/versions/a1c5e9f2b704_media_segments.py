"""Skip segments kept by Curatarr (segments.py), for its Jellyfin plugin to
serve instead of Intro Skipper.

Revision ID: a1c5e9f2b704
Revises: f0a3b6c8d214
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c5e9f2b704"
down_revision = "f0a3b6c8d214"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_segments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("start", sa.Float(), nullable=False),
        sa.Column("end", sa.Float(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_media_segments_item_id", "media_segments", ["item_id"])
    op.create_index("ix_media_segments_version", "media_segments", ["version"])


def downgrade():
    op.drop_index("ix_media_segments_version", table_name="media_segments")
    op.drop_index("ix_media_segments_item_id", table_name="media_segments")
    op.drop_table("media_segments")
