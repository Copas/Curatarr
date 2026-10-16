"""Checks files for burned-in subtitles and signs of a broken download.

Revision ID: b7e3d9a2c614
Revises: a4d81c6e93f2
"""

import sqlalchemy as sa
from alembic import op

revision = "b7e3d9a2c614"
down_revision = "a4d81c6e93f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chapter_states", sa.Column("file_checked_at", sa.DateTime(timezone=True))
    )
    op.add_column("chapter_states", sa.Column("file_check_size", sa.BigInteger()))
    op.add_column("chapter_states", sa.Column("file_check", sa.JSON()))
    op.add_column("chapter_states", sa.Column("file_problem", sa.Boolean()))
    op.create_index(
        "ix_chapter_states_file_problem", "chapter_states", ["file_problem"]
    )


def downgrade():
    op.drop_index("ix_chapter_states_file_problem", table_name="chapter_states")
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("file_problem")
        batch.drop_column("file_check")
        batch.drop_column("file_check_size")
        batch.drop_column("file_checked_at")
