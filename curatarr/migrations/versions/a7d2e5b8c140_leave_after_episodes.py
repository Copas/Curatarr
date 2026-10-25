"""Leave after watched for seasons and episodes: which episodes a Leaving
Soon entry is for, and each flag's mode.

Revision ID: a7d2e5b8c140
Revises: f3a9c1d27b64
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d2e5b8c140"
down_revision = "f3a9c1d27b64"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("purge_candidates", sa.Column("target_part_ids", sa.JSON()))
    op.add_column("media_flags", sa.Column("mode", sa.String(length=20)))


def downgrade():
    with op.batch_alter_table("media_flags") as batch:
        batch.drop_column("mode")
    with op.batch_alter_table("purge_candidates") as batch:
        batch.drop_column("target_part_ids")
