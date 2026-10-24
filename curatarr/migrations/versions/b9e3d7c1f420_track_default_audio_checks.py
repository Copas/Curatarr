"""Track checks of which audio track a file plays by default.

Revision ID: b9e3d7c1f420
Revises: a4c8e2f6b357
"""

import sqlalchemy as sa
from alembic import op

revision = "b9e3d7c1f420"
down_revision = "a4c8e2f6b357"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("default_audio_size", sa.BigInteger()))
    op.add_column("chapter_states", sa.Column("default_audio", sa.JSON()))


def downgrade():
    op.drop_column("chapter_states", "default_audio")
    op.drop_column("chapter_states", "default_audio_size")
