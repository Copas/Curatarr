"""Badge wide cards and backdrops too: which image each snapshot is.

Revision ID: f3b7d5e9a246
Revises: e2a6c4d8f135
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b7d5e9a246"
down_revision = "e2a6c4d8f135"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("poster_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "image_type",
                sa.String(20),
                nullable=False,
                server_default="Primary",
            )
        )


def downgrade():
    with op.batch_alter_table("poster_snapshots") as batch:
        batch.drop_column("image_type")
