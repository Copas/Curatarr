"""Remove the nudity index: its detector found too little and mistook too
much to be worth keeping (added in e5c2a8f14b37).

Revision ID: f3a9d2c7b815
Revises: e5c2a8f14b37
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a9d2c7b815"
down_revision = "e5c2a8f14b37"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("nudity_size")
        batch.drop_column("nudity")


def downgrade():
    op.add_column("chapter_states", sa.Column("nudity", sa.JSON()))
    op.add_column("chapter_states", sa.Column("nudity_size", sa.BigInteger()))
