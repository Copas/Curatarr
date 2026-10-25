"""Custom pictures of every kind: a poster, wide tile, background, logo or
banner (custom_posters.image_type, Jellyfin's image type).

Revision ID: c4e1a9d36f02
Revises: a7d2e5b8c140
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e1a9d36f02"
down_revision = "a7d2e5b8c140"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "custom_posters",
        sa.Column(
            "image_type", sa.String(length=20), nullable=False, server_default="Primary"
        ),
    )


def downgrade():
    op.execute("DELETE FROM custom_posters WHERE image_type != 'Primary'")
    with op.batch_alter_table("custom_posters") as batch:
        batch.drop_column("image_type")
