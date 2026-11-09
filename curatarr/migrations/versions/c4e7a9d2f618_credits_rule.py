"""Which rules last placed an episode's end credits (0.2.0rc98).

Revision ID: c4e7a9d2f618
Revises: b6d2e8f4a193
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e7a9d2f618"
down_revision = "b6d2e8f4a193"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("credits_rule", sa.Integer()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("credits_rule")
