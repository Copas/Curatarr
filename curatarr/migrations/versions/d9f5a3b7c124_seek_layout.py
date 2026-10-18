"""Remember which files were checked for a seek index Roku can't reach.

Revision ID: d9f5a3b7c124
Revises: c8e4f2a6b913
"""

import sqlalchemy as sa
from alembic import op

revision = "d9f5a3b7c124"
down_revision = "c8e4f2a6b913"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("seek_layout_size", sa.BigInteger()))
    op.add_column("chapter_states", sa.Column("seek_layout", sa.JSON()))


def downgrade():
    op.drop_column("chapter_states", "seek_layout")
    op.drop_column("chapter_states", "seek_layout_size")
