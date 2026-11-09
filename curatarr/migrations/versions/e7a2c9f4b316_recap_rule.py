"""Which rules last found an episode's recap (0.2.0rc103).

Revision ID: e7a2c9f4b316
Revises: d5f1b3a8c2e4
"""

import sqlalchemy as sa
from alembic import op

revision = "e7a2c9f4b316"
down_revision = "d5f1b3a8c2e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("recap_rule", sa.Integer()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("recap_rule")
