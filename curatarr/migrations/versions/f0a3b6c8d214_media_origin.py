"""Where each title came from, recorded when it arrived (origin.record).

Revision ID: f0a3b6c8d214
Revises: e7b2c4f9a618
"""

import sqlalchemy as sa
from alembic import op

revision = "f0a3b6c8d214"
down_revision = "e7b2c4f9a618"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_identities") as batch:
        batch.add_column(sa.Column("origin", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("media_identities") as batch:
        batch.drop_column("origin")
