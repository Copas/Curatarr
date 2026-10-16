"""Checks that films made in another language have real English audio.

Revision ID: f3c9d2a7b815
Revises: e2b8c4f61a93
"""

import sqlalchemy as sa
from alembic import op

revision = "f3c9d2a7b815"
down_revision = "e2b8c4f61a93"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chapter_states", sa.Column("dub_checked_at", sa.DateTime(timezone=True))
    )
    op.add_column("chapter_states", sa.Column("dub_file_size", sa.BigInteger()))
    op.add_column("chapter_states", sa.Column("dub_result", sa.JSON()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("dub_result")
        batch.drop_column("dub_file_size")
        batch.drop_column("dub_checked_at")
