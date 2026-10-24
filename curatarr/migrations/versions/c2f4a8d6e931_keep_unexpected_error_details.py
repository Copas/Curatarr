"""Keep the details of an unexpected job error with the job.

Revision ID: c2f4a8d6e931
Revises: b9e3d7c1f420
"""

import sqlalchemy as sa
from alembic import op

revision = "c2f4a8d6e931"
down_revision = "b9e3d7c1f420"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("chapter_error_detail", sa.Text()))


def downgrade():
    op.drop_column("chapter_states", "chapter_error_detail")
