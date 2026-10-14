"""Previews found from transcripts, and checks on Intro Skipper's markers.

Revision ID: c5e1f8a2d904
Revises: a4c2e9d7b315
"""

import sqlalchemy as sa
from alembic import op

revision = "c5e1f8a2d904"
down_revision = "a4c2e9d7b315"
branch_labels = None
depends_on = None

COLUMNS = (
    ("preview_checked_at", sa.DateTime(timezone=True)),
    ("preview_start", sa.Float()),
    ("preview_end", sa.Float()),
    ("preview_source", sa.String(30)),
    ("preview_segment_id", sa.String(40)),
    ("markers_checked_at", sa.DateTime(timezone=True)),
    ("markers_hidden", sa.JSON()),
)


def upgrade():
    for name, kind in COLUMNS:
        op.add_column("chapter_states", sa.Column(name, kind))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        for name, _kind in reversed(COLUMNS):
            batch.drop_column(name)
