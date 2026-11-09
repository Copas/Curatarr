"""Which rules last widened an episode's intro (0.2.0rc105).

Revision ID: f4b8d2e6a913
Revises: e7a2c9f4b316
"""

import sqlalchemy as sa
from alembic import op

revision = "f4b8d2e6a913"
down_revision = "e7a2c9f4b316"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("intro_rule", sa.Integer()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("intro_rule")
