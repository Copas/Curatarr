"""Track Sonarr tag labels for each show.

Revision ID: f3b8c2d51a47
Revises: c4e8a1f07d36
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b8c2d51a47"
down_revision = "c4e8a1f07d36"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_identities", sa.Column("arr_tags", sa.JSON()))


def downgrade():
    op.drop_column("media_identities", "arr_tags")
