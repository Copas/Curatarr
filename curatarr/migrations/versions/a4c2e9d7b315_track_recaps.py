"""Recaps found from transcripts, and the transcript keys used to find them.

Revision ID: a4c2e9d7b315
Revises: f1b6d2e8c047
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c2e9d7b315"
down_revision = "f1b6d2e8c047"
branch_labels = None
depends_on = None

COLUMNS = (
    ("transcript_key", sa.String(32)),
    ("opening_key", sa.String(32)),
    ("recap_checked_at", sa.DateTime(timezone=True)),
    ("recap_start", sa.Float()),
    ("recap_end", sa.Float()),
    ("recap_source", sa.String(30)),
    ("recap_segment_id", sa.String(40)),
)


def upgrade():
    for name, kind in COLUMNS:
        op.add_column("chapter_states", sa.Column(name, kind))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        for name, _kind in reversed(COLUMNS):
            batch.drop_column(name)
