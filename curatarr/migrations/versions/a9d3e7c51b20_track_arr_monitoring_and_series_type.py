"""Track the arr monitored flag and Sonarr series type.

Revision ID: a9d3e7c51b20
Revises: e41c9b7a2d05
"""

import sqlalchemy as sa
from alembic import op

revision = "a9d3e7c51b20"
down_revision = "e41c9b7a2d05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_identities", sa.Column("arr_monitored", sa.Boolean()))
    op.add_column("media_identities", sa.Column("series_type", sa.String(20)))


def downgrade():
    op.drop_column("media_identities", "series_type")
    op.drop_column("media_identities", "arr_monitored")
