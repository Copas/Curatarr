"""Drop the Sonarr tag labels column again (the curatarr-pilot rule was withdrawn).

Revision ID: a1e5d9c3b7f2
Revises: f3b8c2d51a47
"""

import sqlalchemy as sa
from alembic import op

revision = "a1e5d9c3b7f2"
down_revision = "f3b8c2d51a47"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("media_identities", "arr_tags")


def downgrade():
    op.add_column("media_identities", sa.Column("arr_tags", sa.JSON()))
