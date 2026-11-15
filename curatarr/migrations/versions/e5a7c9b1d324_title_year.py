"""A title's release year, shown beside its name.

Two films can share a name (Carrie, 1976 and 2013); lists and pickers show
"Carrie (1976)". Filled in by the next sync, from Jellyfin, Radarr and Sonarr.

Revision ID: e5a7c9b1d324
Revises: d3f5b7c9e126
"""

import sqlalchemy as sa
from alembic import op

revision = "e5a7c9b1d324"
down_revision = "d3f5b7c9e126"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("media_identities") as batch:
        batch.add_column(sa.Column("year", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("media_identities") as batch:
        batch.drop_column("year")
