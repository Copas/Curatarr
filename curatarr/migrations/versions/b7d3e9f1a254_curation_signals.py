"""Genres, keywords and play counts, for seasonal shelves and rewatched titles.

Revision ID: b7d3e9f1a254
Revises: e2f7a4b9c613
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d3e9f1a254"
down_revision = "e2f7a4b9c613"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_identities", sa.Column("genres", sa.JSON()))
    op.add_column("media_identities", sa.Column("keywords", sa.JSON()))
    op.add_column(
        "user_media_state",
        sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("user_media_state", "play_count")
    op.drop_column("media_identities", "keywords")
    op.drop_column("media_identities", "genres")
