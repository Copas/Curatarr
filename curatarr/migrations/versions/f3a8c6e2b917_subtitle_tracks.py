"""Subtitle tracks in other languages removed from a file (0.2.0rc76).

Revision ID: f3a8c6e2b917
Revises: e7c3a5d1f824
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a8c6e2b917"
down_revision = "e7c3a5d1f824"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("subtitle_tracks_size", sa.BigInteger()))
    op.add_column("chapter_states", sa.Column("subtitle_tracks", sa.JSON()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("subtitle_tracks")
        batch.drop_column("subtitle_tracks_size")
