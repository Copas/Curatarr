"""Curatarr's own intro, recorded beside Intro Skipper's for comparison.

Revision ID: a8c4e2f7b519
Revises: e5a7c9b1d324
"""

import sqlalchemy as sa
from alembic import op

revision = "a8c4e2f7b519"
down_revision = "e5a7c9b1d324"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("intro_found", sa.JSON()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("intro_found")
