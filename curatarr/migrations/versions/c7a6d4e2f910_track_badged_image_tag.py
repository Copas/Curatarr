"""Track Jellyfin's image tag for generated posters.

Revision ID: c7a6d4e2f910
Revises: bdf37dc1d2e6
"""

import sqlalchemy as sa
from alembic import op

revision = "c7a6d4e2f910"
down_revision = "bdf37dc1d2e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("poster_snapshots", sa.Column("badged_image_tag", sa.String(100)))


def downgrade():
    op.drop_column("poster_snapshots", "badged_image_tag")
