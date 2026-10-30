"""Whether a subtitle file beside the video is in sync (0.2.0rc54).

Revision ID: e7c3a5d1f824
Revises: d4b7e2a9c516
"""

import sqlalchemy as sa
from alembic import op

revision = "e7c3a5d1f824"
down_revision = "d4b7e2a9c516"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("subtitle_sync", sa.JSON()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("subtitle_sync")
