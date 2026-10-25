"""Flags on titles, seasons, episodes and files: Leave after watched and
Never compress.

Revision ID: f3a9c1d27b64
Revises: e8b2c4f7a913
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a9c1d27b64"
down_revision = "e8b2c4f7a913"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_flags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_identity_id", sa.String(length=36), nullable=False),
        sa.Column("season_number", sa.Integer()),
        sa.Column("media_part_id", sa.String(length=36)),
        sa.Column("flag", sa.String(length=30), nullable=False),
        sa.Column("jellyfin_user_id", sa.String(length=100)),
        sa.Column("set_by", sa.String(length=100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["media_identity_id"], ["media_identities.id"]),
        sa.ForeignKeyConstraint(["media_part_id"], ["media_parts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_flags_media_identity_id", "media_flags", ["media_identity_id"]
    )


def downgrade():
    op.drop_index("ix_media_flags_media_identity_id", table_name="media_flags")
    op.drop_table("media_flags")
