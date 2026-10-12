"""Generated subtitles alongside chapter state.

Revision ID: f1b6d2e8c047
Revises: e7c3a9f15d42
"""

import sqlalchemy as sa
from alembic import op

revision = "f1b6d2e8c047"
down_revision = "e7c3a9f15d42"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chapter_states", sa.Column("subtitle_status", sa.String(30)))
    op.add_column("chapter_states", sa.Column("subtitle_path", sa.Text()))
    op.add_column("chapter_states", sa.Column("subtitle_fingerprint", sa.String(64)))
    op.add_column(
        "chapter_states",
        sa.Column("subtitle_processed_at", sa.DateTime(timezone=True)),
    )
    op.add_column("chapter_states", sa.Column("subtitle_error", sa.Text()))


def downgrade():
    with op.batch_alter_table("chapter_states") as batch:
        for column in (
            "subtitle_error",
            "subtitle_processed_at",
            "subtitle_fingerprint",
            "subtitle_path",
            "subtitle_status",
        ):
            batch.drop_column(column)
