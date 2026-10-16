"""Counts what's said in each transcript for the Fun facts page.

Revision ID: c5a1f8e3d927
Revises: b7e3d9a2c614
"""

import sqlalchemy as sa
from alembic import op

revision = "c5a1f8e3d927"
down_revision = "b7e3d9a2c614"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("talk_stats", sa.JSON()))
    op.add_column("chapter_states", sa.Column("talk_stats_key", sa.String(32)))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("talk_stats_key")
        batch.drop_column("talk_stats")
