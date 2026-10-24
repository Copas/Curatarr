"""Custom posters chosen for titles.

Revision ID: d4e7a1c9b352
Revises: c2f4a8d6e931
"""

import sqlalchemy as sa
from alembic import op

revision = "d4e7a1c9b352"
down_revision = "c2f4a8d6e931"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "custom_posters",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("media_identity_id", sa.String(length=36), nullable=False),
        sa.Column("jellyfin_user_id", sa.String(length=100)),
        sa.Column("image_path", sa.String(length=512), nullable=False),
        sa.Column("original_path", sa.String(length=512)),
        sa.Column("source", sa.String(length=255)),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("restored_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["media_identity_id"], ["media_identities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_posters_media_identity_id", "custom_posters", ["media_identity_id"]
    )


def downgrade():
    op.drop_index("ix_custom_posters_media_identity_id", table_name="custom_posters")
    op.drop_table("custom_posters")
