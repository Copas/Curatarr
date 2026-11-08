"""How each generated subtitle file was made (0.2.0rc95).

Revision ID: b6d2e8f4a193
Revises: f3a8c6e2b917
"""

import sqlalchemy as sa
from alembic import op

revision = "b6d2e8f4a193"
down_revision = "f3a8c6e2b917"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("subtitle_made_with", sa.JSON()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        batch.drop_column("subtitle_made_with")
