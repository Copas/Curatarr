"""Track TV-friendly audio tracks Curatarr added.

Revision ID: c8e4f2a6b913
Revises: b7d3e9f1a254
"""

import sqlalchemy as sa
from alembic import op

revision = "c8e4f2a6b913"
down_revision = "b7d3e9f1a254"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("compat_audio_size", sa.BigInteger()))
    op.add_column("chapter_states", sa.Column("compat_audio", sa.JSON()))


def downgrade():
    op.drop_column("chapter_states", "compat_audio")
    op.drop_column("chapter_states", "compat_audio_size")
