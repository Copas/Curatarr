"""Remember the words on each Leaving Soon banner (days left).

Revision ID: e2a6c4d8f135
Revises: d9f5a3b7c124
"""

import sqlalchemy as sa
from alembic import op

revision = "e2a6c4d8f135"
down_revision = "d9f5a3b7c124"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("poster_snapshots", sa.Column("badge_label", sa.String(40)))


def downgrade():
    op.drop_column("poster_snapshots", "badge_label")
