"""Add durable operational metric counters.

Revision ID: e41c9b7a2d05
Revises: c7a6d4e2f910
"""

import sqlalchemy as sa
from alembic import op

revision = "e41c9b7a2d05"
down_revision = "c7a6d4e2f910"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "metric_counters",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
    )


def downgrade():
    op.drop_table("metric_counters")
