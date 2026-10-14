"""Checks that an episode's transcript fits its own summary.

Revision ID: d7a3b9e1f620
Revises: c5e1f8a2d904
"""

import sqlalchemy as sa
from alembic import op

revision = "d7a3b9e1f620"
down_revision = "c5e1f8a2d904"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chapter_states", sa.Column("identity_checked_at", sa.DateTime(timezone=True))
    )
    op.add_column("chapter_states", sa.Column("identity_issue", sa.JSON()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("identity_issue")
        batch.drop_column("identity_checked_at")
