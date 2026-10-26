"""The nudity index: what each film shows, and when (nudity.py).

Revision ID: e5c2a8f14b37
Revises: d81f3b6a2c94
"""

import sqlalchemy as sa
from alembic import op

revision = "e5c2a8f14b37"
down_revision = "d81f3b6a2c94"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("nudity", sa.JSON()))
    op.add_column("chapter_states", sa.Column("nudity_size", sa.BigInteger()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("nudity_size")
        batch.drop_column("nudity")
